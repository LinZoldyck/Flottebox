"""Lecture des avis de contravention et rapprochement avec les locations (directes et Getaround)."""
from __future__ import annotations

import io
import logging
import mimetypes
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Fine, FineStatus, Organization, Rental, RentalStatus, Vehicle
from app.services import ai_assistant
from app.services.storage import save_file
from app.utils import find_plate, normalize_plate

log = logging.getLogger(__name__)

MATCH_TOLERANCE = timedelta(minutes=30)
SUPPORTED = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

_DT_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})\D{1,12}?(\d{1,2})\s*[hH:]\s*(\d{2})")
_NOTICE_RE = re.compile(r"(?:avis|n°\s*d'avis|numero de l'avis|numéro de l'avis|N° avis)[^\d]{0,40}(\d[\d ]{8,18}\d)", re.I)
_LOCATION_RE = re.compile(r"(?:lieu(?: de l'infraction)?|localisation)\s*:?\s*(.+)", re.I)
_AMOUNT_RE = re.compile(r"(\d{1,4}[,.]\d{2})\s*(?:€|eur)", re.I)
_DEADLINE_RE = re.compile(r"(?:avant le|au plus tard le|date limite[^\d]{0,20})\s*(\d{2})/(\d{2})/(\d{4})", re.I)
_OFFENSE_RE = re.compile(r"(?:nature de l'infraction|infraction)\s*:?\s*(.+)", re.I)


def extract_pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:  # noqa: BLE001
        return ""


def parse_fine_text(text: str) -> dict:
    """Extraction par expressions régulières (avis PDF « texte »)."""
    out: dict = {}
    out["plate"] = find_plate(text)
    m = _DT_RE.search(text)
    if m:
        dd, mm, yyyy, hh, mi = (int(x) for x in m.groups())
        try:
            out["offense_at"] = datetime(yyyy, mm, dd, hh, mi)
        except ValueError:
            pass
    m = _NOTICE_RE.search(text)
    if m:
        out["notice_number"] = m.group(1).replace(" ", "")
    m = _LOCATION_RE.search(text)
    if m:
        out["location"] = m.group(1).strip()[:300]
    m = _OFFENSE_RE.search(text)
    if m:
        out["offense_type"] = m.group(1).strip()[:300]
    m = _AMOUNT_RE.search(text)
    if m:
        out["amount"] = float(m.group(1).replace(",", "."))
    m = _DEADLINE_RE.search(text)
    if m:
        dd, mm, yyyy = (int(x) for x in m.groups())
        try:
            out["designation_deadline"] = date(yyyy, mm, dd)
        except ValueError:
            pass
    return out


def analyse(fine: Fine, data: bytes, media_type: str) -> None:
    """Remplit les champs de la contravention à partir du fichier."""
    fields: dict = {}
    if media_type == "application/pdf":
        text = extract_pdf_text(data)
        fine.raw_text = text[:20000]
        fields = parse_fine_text(text)
        fine.extraction_method = "texte"

    if not fields.get("plate") or not fields.get("offense_at"):
        ai = ai_assistant.extract_fine_with_claude(data, media_type)
        if ai:
            fine.extraction_method = "claude"
            fields = {
                "plate": normalize_plate(ai.plate) or fields.get("plate", ""),
                "offense_at": ai_assistant.parse_date_time(ai.offense_date, ai.offense_time) or fields.get("offense_at"),
                "notice_number": re.sub(r"\s", "", ai.notice_number) or fields.get("notice_number", ""),
                "location": ai.location or fields.get("location", ""),
                "offense_type": ai.offense_type or fields.get("offense_type", ""),
                "amount": ai.amount if ai.amount is not None else fields.get("amount"),
                "designation_deadline": _parse_iso_date(ai.designation_deadline) or fields.get("designation_deadline"),
            }
        elif not fine.extraction_method:
            fine.extraction_method = "manuel"

    for key, value in fields.items():
        if value not in (None, ""):
            setattr(fine, key, value)


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        return None


def match(db: Session, fine: Fine) -> None:
    """Rapproche la contravention d'un véhicule puis de la location en cours au moment de l'infraction."""
    if fine.status in (FineStatus.DESIGNATED, FineStatus.IGNORED):
        return
    fine.rental_id = None
    if not fine.plate or not fine.offense_at:
        fine.status = FineStatus.UNMATCHED
        fine.match_note = "Immatriculation ou date/heure manquante : complétez les champs puis relancez."
        return

    vehicle = db.scalar(select(Vehicle).where(Vehicle.organization_id == fine.organization_id,
                                              Vehicle.plate == normalize_plate(fine.plate)))
    fine.vehicle_id = vehicle.id if vehicle else None
    if not vehicle:
        fine.status = FineStatus.UNMATCHED
        fine.match_note = f"Aucun véhicule de la flotte avec la plaque {fine.plate}."
        return

    t = fine.offense_at
    candidates = db.scalars(
        select(Rental).where(
            Rental.vehicle_id == vehicle.id,
            Rental.status != RentalStatus.CANCELLED,
            Rental.starts_at <= t + MATCH_TOLERANCE,
            Rental.ends_at >= t - MATCH_TOLERANCE,
        )
    ).all()

    def actual_window(r: Rental) -> tuple[datetime, datetime]:
        ci, co = r.inspection("checkin"), r.inspection("checkout")
        return (ci.done_at if ci else r.starts_at, co.done_at if co else r.ends_at)

    exact = [r for r in candidates if actual_window(r)[0] <= t <= actual_window(r)[1]]
    chosen = (exact or candidates)
    if len(chosen) == 1:
        r = chosen[0]
        fine.rental_id = r.id
        fine.status = FineStatus.MATCHED
        who = r.customer.display_name if r.customer else "conducteur inconnu"
        src = "Getaround" if r.source.value == "getaround" else "location directe"
        fine.match_note = f"Location {r.reference} ({src}) — {who}" + ("" if exact else " (dans la marge de tolérance)")
    elif len(chosen) > 1:
        fine.status = FineStatus.UNMATCHED
        fine.match_note = "Plusieurs locations possibles : " + ", ".join(r.reference for r in chosen)
    else:
        fine.status = FineStatus.UNMATCHED
        fine.match_note = f"Aucune location du véhicule {vehicle.plate} le {t:%d/%m/%Y à %H:%M} (usage interne ?)."


def ingest(db: Session, org: Organization, data: bytes, filename: str) -> Fine:
    suffix = Path(filename).suffix.lower()
    media_type = SUPPORTED.get(suffix) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if suffix not in SUPPORTED:
        raise ValueError("Format non pris en charge (PDF, JPG, PNG, WEBP).")
    rel = save_file(data, f"org{org.id}/fines", suffix)
    fine = Fine(organization_id=org.id, file_path=rel, original_name=filename)
    db.add(fine)
    analyse(fine, data, media_type)
    db.flush()
    match(db, fine)
    db.commit()
    return fine


def scan_inbox(db: Session) -> int:
    """Traite les fichiers déposés dans data/contraventions_a_traiter/<id_organisation>/ ."""
    root = get_settings().fines_inbox_dir
    count = 0
    for org in db.scalars(select(Organization)):
        folder = root / str(org.id)
        folder.mkdir(parents=True, exist_ok=True)
        done = folder / "traites"
        done.mkdir(exist_ok=True)
        for path in sorted(folder.iterdir()):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED:
                continue
            try:
                ingest(db, org, path.read_bytes(), path.name)
                path.rename(done / path.name)
                count += 1
            except Exception:  # noqa: BLE001
                db.rollback()
                log.exception("Échec du traitement de %s", path)
    return count
