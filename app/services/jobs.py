"""Tâches automatiques (planificateur intégré) : codes d'accès SMS, synchro Getaround, messagerie IA, PV."""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import Message, Organization, Rental, RentalSource, RentalStatus
from app.services import ai_assistant, fines, getaround
from app.services.sms import SmsError, send_sms
from app.utils import now_local

log = logging.getLogger(__name__)


def access_sms_text(rental: Rental, org: Organization) -> str:
    s = get_settings()
    return (f"{org.name} : votre location {rental.vehicle.plate} commence le {rental.starts_at:%d/%m à %H:%M}. "
            f"Code d'accès : {rental.access_code} — connectez-vous sur {s.base_url}/r pour voir le code de la "
            f"boîte à clés et faire l'état des lieux.")


def send_due_access_codes(db: Session) -> int:
    s = get_settings()
    now = now_local()
    due = db.scalars(select(Rental).where(
        Rental.source == RentalSource.DIRECT,
        Rental.status == RentalStatus.CONFIRMED,
        Rental.access_code_sent_at.is_(None),
        Rental.starts_at <= now + timedelta(minutes=s.access_code_lead_minutes),
        Rental.ends_at >= now,
    )).all()
    sent = 0
    for rental in due:
        if not rental.customer or not rental.customer.phone:
            continue
        org = db.get(Organization, rental.organization_id)
        try:
            send_sms(rental.customer.phone, access_sms_text(rental, org))
            rental.access_code_sent_at = now
            sent += 1
        except SmsError:
            log.exception("SMS non envoyé pour %s", rental.reference)
    db.commit()
    return sent


def rental_context(rental: Rental | None) -> str:
    if not rental:
        return ""
    v = rental.vehicle
    return (f"Véhicule : {v.brand} {v.model} ({v.plate}), carburant {v.fuel_type}. Adresse : {v.parking_address}.\n"
            f"Début : {rental.starts_at:%d/%m/%Y %H:%M} — Fin : {rental.ends_at:%d/%m/%Y %H:%M}.\n"
            f"Kilomètres inclus : {rental.included_km or 'voir Getaround'}.")


def handle_incoming(db: Session, org: Organization, msg: Message, client: getaround.GetaroundClient | None = None) -> None:
    """Applique l'assistant IA à un message entrant selon le mode choisi (off / draft / auto)."""
    if org.assistant_mode == "off":
        return
    history_msgs = db.scalars(select(Message).where(Message.rental_id == msg.rental_id, Message.id != msg.id)
                              .order_by(Message.sent_at.desc()).limit(6)).all() if msg.rental_id else []
    history = "\n".join(f"[{'Nous' if m.direction == 'out' else 'Locataire'}] {m.body}" for m in reversed(history_msgs))
    result = ai_assistant.decide_reply(msg.body, org.assistant_knowledge, rental_context(msg.rental), history)
    msg.ai_category, msg.ai_reason = result.category, result.reason
    if result.decision == "escalate":
        msg.ai_status = "escalated"
    else:
        msg.ai_reply = result.reply
        msg.ai_status = "draft"
        if org.assistant_mode == "auto" and msg.rental and msg.rental.external_id:
            try:
                (client or getaround.GetaroundClient(org.getaround_token)).send_message(msg.rental.external_id, result.reply)
                msg.ai_status = "sent"
                msg.handled_at = now_local()
            except getaround.GetaroundError as exc:
                msg.ai_status = "error"
                msg.ai_reason += f" | Envoi impossible : {exc}"
    db.commit()


def run_getaround(db: Session) -> None:
    for org in db.scalars(select(Organization).where(Organization.getaround_token != "")):
        try:
            client = getaround.GetaroundClient(org.getaround_token)
            getaround.sync_rentals(db, org, client=client)
            for msg in getaround.poll_messages(db, org, client=client):
                handle_incoming(db, org, msg, client)
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("Synchronisation Getaround échouée pour l'organisation %s", org.id)


def _job(fn):
    def wrapper():
        db = SessionLocal()
        try:
            fn(db)
        except Exception:  # noqa: BLE001
            log.exception("Tâche %s en échec", fn.__name__)
        finally:
            db.close()
    wrapper.__name__ = fn.__name__
    return wrapper


def start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler(timezone=get_settings().timezone)
    scheduler.add_job(_job(send_due_access_codes), "interval", minutes=1, id="access_codes")
    scheduler.add_job(_job(run_getaround), "interval", minutes=5, id="getaround")
    scheduler.add_job(_job(fines.scan_inbox), "interval", minutes=5, id="fines_inbox")
    scheduler.start()
    return scheduler
