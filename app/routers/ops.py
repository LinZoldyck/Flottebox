"""Contraventions, messagerie Getaround (assistant IA), paramètres et intégrations."""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Fine, FineStatus, Invoice, Message, Organization, Rental, RentalSource, Role, User
from app.routers.fleet import _get, manager
from app.security import flash, get_current_user, hash_token, new_api_token, require_role
from app.services import contracts, fines, getaround, jobs, pennylane
from app.services.storage import UploadError, save_image
from app.utils import normalize_plate, now_local
from app.web import csrf_protect, render

router = APIRouter(dependencies=[Depends(csrf_protect)])


# ---------------- Contraventions ----------------

@router.get("/fines")
def fines_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    items = db.scalars(select(Fine).where(Fine.organization_id == user.organization_id)
                       .order_by(Fine.created_at.desc())).all()
    inbox = get_settings().fines_inbox_dir / str(user.organization_id)
    return render(request, "fines.html", user=user, fines=items, inbox=inbox)


@router.post("/fines/upload")
async def fines_upload(request: Request, user: User = Depends(manager), db: Session = Depends(get_db)):
    form = await request.form()
    org = db.get(Organization, user.organization_id)
    count = 0
    for f in form.getlist("files"):
        if not getattr(f, "filename", ""):
            continue
        try:
            fines.ingest(db, org, await f.read(), f.filename)
            count += 1
        except (ValueError, UploadError) as exc:
            flash(request, f"{f.filename} : {exc}", "error")
    flash(request, f"{count} avis importé(s) et analysé(s).", "success" if count else "info")
    return RedirectResponse("/fines", status_code=303)


@router.post("/fines/scan")
def fines_scan(request: Request, user: User = Depends(manager), db: Session = Depends(get_db)):
    n = fines.scan_inbox(db)
    flash(request, f"{n} fichier(s) traité(s) depuis le dossier de dépôt.", "success")
    return RedirectResponse("/fines", status_code=303)


@router.get("/fines/{fine_id}")
def fine_detail(request: Request, fine_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    fine = _get(db, Fine, fine_id, user)
    candidates = []
    if fine.vehicle_id:
        candidates = db.scalars(select(Rental).where(Rental.vehicle_id == fine.vehicle_id)
                                .order_by(Rental.starts_at.desc()).limit(30)).all()
    return render(request, "fine_detail.html", user=user, fine=fine, candidates=candidates,
                  designation=designation_payload(db.get(Organization, user.organization_id), fine))


@router.post("/fines/{fine_id}")
def fine_update(request: Request, fine_id: int, notice_number: str = Form(""), plate: str = Form(""),
                offense_at: str = Form(""), location: str = Form(""), offense_type: str = Form(""),
                amount: str = Form(""), designation_deadline: str = Form(""), rental_id: str = Form(""),
                kind: str = Form("contravention"), user: User = Depends(manager), db: Session = Depends(get_db)):
    fine = _get(db, Fine, fine_id, user)
    fine.kind = "fps" if kind == "fps" else "contravention"
    fine.notice_number, fine.plate = notice_number.strip(), normalize_plate(plate)
    fine.offense_at = datetime.fromisoformat(offense_at) if offense_at else None
    fine.location, fine.offense_type = location.strip(), offense_type.strip()
    fine.amount = float(amount.replace(",", ".")) if amount.strip() else None
    fine.designation_deadline = date.fromisoformat(designation_deadline) if designation_deadline else None
    if rental_id == "auto":
        fines.match(db, fine)
    elif rental_id:
        rental = _get(db, Rental, int(rental_id), user)
        fine.rental_id, fine.vehicle_id, fine.status = rental.id, rental.vehicle_id, FineStatus.MATCHED
        fine.match_note = f"Rapprochement manuel : {rental.reference}"
    db.commit()
    flash(request, "Contravention mise à jour.", "success")
    return RedirectResponse(f"/fines/{fine.id}", status_code=303)


@router.post("/fines/{fine_id}/status")
def fine_status(request: Request, fine_id: int, status: FineStatus = Form(...), user: User = Depends(manager),
                db: Session = Depends(get_db)):
    fine = _get(db, Fine, fine_id, user)
    fine.status = status
    if status == FineStatus.DESIGNATED:
        fine.designated_at = now_local()
    db.commit()
    return RedirectResponse(f"/fines/{fine.id}", status_code=303)


@router.post("/fines/{fine_id}/rebill")
def fine_rebill(request: Request, fine_id: int, admin_fee: str = Form("0"), user: User = Depends(manager),
                db: Session = Depends(get_db)):
    """FPS : le titulaire paie, puis refacture le montant (et d'éventuels frais de gestion) au locataire."""
    fine = _get(db, Fine, fine_id, user)
    org = db.get(Organization, user.organization_id)
    back = RedirectResponse(f"/fines/{fine.id}", status_code=303)
    if not fine.rental or fine.rental.source != RentalSource.DIRECT:
        flash(request, "Refacturation possible seulement pour une location directe (Getaround : déclarez le FPS "
                       "dans votre espace propriétaire Getaround).", "error")
        return back
    if fine.invoice_id:
        invoice = db.get(Invoice, fine.invoice_id)
        if invoice.status == "sent":
            flash(request, "Ce FPS a déjà été refacturé.", "error")
            return back
        pennylane.push_invoice(org, invoice)  # nouvel essai d'envoi
        db.commit()
        flash(request, "Facture envoyée à Pennylane." if invoice.status == "sent"
              else f"Envoi Pennylane en échec : {invoice.error}", "success" if invoice.status == "sent" else "error")
        return back
    try:
        fee = float(admin_fee.replace(",", ".") or 0)
    except ValueError:
        fee = 0
    lines = [{"label": f"Refacturation forfait post-stationnement n° {fine.notice_number} du "
                       f"{fine.offense_at:%d/%m/%Y %H:%M} ({fine.location}) — véhicule {fine.plate}, "
                       f"contrat {fine.rental.reference}",
              "quantity": 1, "unit_price_ttc": round(fine.amount or 0, 2)}]
    if fee > 0:
        lines.append({"label": "Frais de gestion du FPS", "quantity": 1, "unit_price_ttc": round(fee, 2)})
    invoice = Invoice(organization_id=org.id, rental_id=fine.rental.id, rental=fine.rental, kind="fps", lines=lines,
                      total_ttc=round(sum(line["unit_price_ttc"] for line in lines), 2))
    db.add(invoice)
    db.flush()
    pennylane.push_invoice(org, invoice)
    fine.invoice_id = invoice.id
    fine.status = FineStatus.REBILLED
    db.commit()
    if invoice.status == "sent":
        flash(request, "Facture de refacturation créée en brouillon dans Pennylane.", "success")
    else:
        flash(request, f"Facture enregistrée, envoi Pennylane en échec : {invoice.error}", "error")
    return back


def designation_payload(org: Organization, fine: Fine) -> dict:
    """Données attendues par le formulaire de désignation ANTAI (utilisées par le robot)."""
    c = fine.rental.customer if fine.rental and fine.rental.customer else None
    return {
        "fine_id": fine.id,
        "type": fine.kind,
        "avis": {
            "numero_avis": fine.notice_number,
            "immatriculation": fine.plate.replace("-", ""),
            "date_infraction": f"{fine.offense_at:%d/%m/%Y}" if fine.offense_at else "",
            "heure_infraction": f"{fine.offense_at:%H:%M}" if fine.offense_at else "",
        },
        "titulaire": {
            "raison_sociale": org.name, "siret": org.siret, "representant": org.representative_name,
            "adresse": org.address, "code_postal": org.postal_code, "ville": org.city, "email": org.email,
        },
        "conducteur": None if not c else {
            "civilite": c.civility, "nom": c.last_name, "prenom": c.first_name,
            "date_naissance": f"{c.birth_date:%d/%m/%Y}" if c.birth_date else "",
            "lieu_naissance": c.birth_place, "adresse": c.address, "code_postal": c.postal_code,
            "ville": c.city, "pays": c.country,
            "numero_permis": c.license_number,
            "date_permis": f"{c.license_issue_date:%d/%m/%Y}" if c.license_issue_date else "",
            "lieu_permis": c.license_issue_place, "categorie_permis": c.license_category,
            "email": c.email,
        },
        "manquants": c.missing_designation_fields if c else ["Conducteur non identifié"],
    }


# ---------------- Messagerie Getaround ----------------

@router.get("/messages")
def messages_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    msgs = db.scalars(select(Message).where(Message.organization_id == user.organization_id)
                      .order_by(Message.sent_at.desc()).limit(200)).all()
    return render(request, "messages.html", user=user, messages=msgs)


@router.post("/messages/{message_id}/send")
def message_send(request: Request, message_id: int, reply: str = Form(...), user: User = Depends(manager),
                 db: Session = Depends(get_db)):
    msg = _get(db, Message, message_id, user)
    org = db.get(Organization, user.organization_id)
    if not msg.rental or not msg.rental.external_id:
        flash(request, "Message non rattaché à une location Getaround.", "error")
        return RedirectResponse("/messages", status_code=303)
    try:
        getaround.GetaroundClient(org.getaround_token).send_message(msg.rental.external_id, reply.strip())
        msg.ai_reply, msg.ai_status, msg.handled_at = reply.strip(), "sent", now_local()
        flash(request, "Réponse envoyée sur Getaround.", "success")
    except getaround.GetaroundError as exc:
        flash(request, str(exc), "error")
    db.commit()
    return RedirectResponse("/messages", status_code=303)


@router.post("/messages/{message_id}/dismiss")
def message_dismiss(request: Request, message_id: int, user: User = Depends(manager), db: Session = Depends(get_db)):
    msg = _get(db, Message, message_id, user)
    msg.ai_status, msg.handled_at = "ignored", now_local()
    db.commit()
    return RedirectResponse("/messages", status_code=303)


@router.post("/messages/test")
def message_test(request: Request, body: str = Form(...), user: User = Depends(manager), db: Session = Depends(get_db)):
    """Bac à sable : tester l'assistant sur un message fictif (rien n'est envoyé)."""
    org = db.get(Organization, user.organization_id)
    from app.services.ai_assistant import decide_reply
    result = decide_reply(body, org.assistant_knowledge, "", "")
    flash(request, f"Décision : {result.decision} ({result.category}) — {result.reply or result.reason}", "info")
    return RedirectResponse("/messages", status_code=303)


@router.post("/getaround/sync")
def getaround_sync(request: Request, user: User = Depends(manager), db: Session = Depends(get_db)):
    org = db.get(Organization, user.organization_id)
    try:
        client = getaround.GetaroundClient(org.getaround_token)
        n = getaround.sync_rentals(db, org, client=client)
        new = getaround.poll_messages(db, org, client=client)
        for msg in new:
            jobs.handle_incoming(db, org, msg, client)
        flash(request, f"{n} location(s) synchronisée(s), {len(new)} nouveau(x) message(s).", "success")
    except getaround.GetaroundError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(request.headers.get("referer", "/rentals"), status_code=303)


# ---------------- Paramètres ----------------

ORG_FIELDS = ["name", "legal_form", "siret", "vat_number", "address", "postal_code", "city", "phone", "email",
              "representative_name", "contract_terms", "assistant_knowledge", "invoice_vat_rate"]


@router.get("/settings")
def settings_page(request: Request, user: User = Depends(require_role(Role.OWNER)), db: Session = Depends(get_db)):
    org = db.get(Organization, user.organization_id)
    return render(request, "settings.html", user=user, org=org, default_terms=contracts.DEFAULT_TERMS,
                  webhook_url=f"{get_settings().base_url}/webhooks/getaround/{org.id}",
                  new_token=request.session.pop("new_api_token", None),
                  claude_ready=bool(get_settings().anthropic_api_key))


@router.post("/settings")
async def settings_save(request: Request, user: User = Depends(require_role(Role.OWNER)), db: Session = Depends(get_db)):
    org = db.get(Organization, user.organization_id)
    form = await request.form()
    for field in ORG_FIELDS:
        if field in form:
            setattr(org, field, str(form[field]).strip())
    if form.get("assistant_mode") in ("off", "draft", "auto"):
        org.assistant_mode = str(form["assistant_mode"])
    for secret in ("getaround_token", "getaround_webhook_secret", "pennylane_token"):
        value = str(form.get(secret, "")).strip()
        if value:  # champ vide = conserver la valeur actuelle
            setattr(org, secret, value)
    logo = form.get("logo")
    if getattr(logo, "filename", ""):
        try:
            org.logo_path = save_image(await logo.read(), f"org{org.id}/logo", max_side=600)
        except UploadError as exc:
            flash(request, str(exc), "error")
    db.commit()
    flash(request, "Paramètres enregistrés.", "success")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/api-token")
def settings_token(request: Request, user: User = Depends(require_role(Role.OWNER)), db: Session = Depends(get_db)):
    org = db.get(Organization, user.organization_id)
    token = new_api_token()
    org.api_token_hash = hash_token(token)
    db.commit()
    request.session["new_api_token"] = token
    return RedirectResponse("/settings#robot", status_code=303)


@router.post("/settings/clear-secret")
def settings_clear(request: Request, name: str = Form(...), user: User = Depends(require_role(Role.OWNER)),
                   db: Session = Depends(get_db)):
    if name not in ("getaround_token", "getaround_webhook_secret", "pennylane_token"):
        raise HTTPException(status_code=400)
    setattr(db.get(Organization, user.organization_id), name, "")
    db.commit()
    return RedirectResponse("/settings", status_code=303)
