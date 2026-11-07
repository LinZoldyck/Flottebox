"""Envoi de SMS. Fournisseurs : console (développement), Twilio, Brevo."""
from __future__ import annotations

import logging

import httpx

from app.config import get_settings
from app.utils import normalize_phone

log = logging.getLogger(__name__)

# Boîte d'envoi utilisée par le fournisseur "console" (et par les tests).
OUTBOX: list[dict] = []


class SmsError(Exception):
    pass


def send_sms(to: str, body: str) -> None:
    s = get_settings()
    phone = normalize_phone(to)
    if not phone:
        raise SmsError("Numéro de téléphone manquant")

    if s.sms_provider == "twilio":
        r = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json",
            data={"To": phone, "From": s.twilio_from, "Body": body},
            auth=(s.twilio_account_sid, s.twilio_auth_token),
            timeout=20,
        )
    elif s.sms_provider == "brevo":
        r = httpx.post(
            "https://api.brevo.com/v3/transactionalSMS/sms",
            json={"sender": s.brevo_sender[:11], "recipient": phone.lstrip("+"), "content": body, "type": "transactional"},
            headers={"api-key": s.brevo_api_key},
            timeout=20,
        )
    else:
        OUTBOX.append({"to": phone, "body": body})
        log.warning("SMS (console) -> %s : %s", phone, body)
        return

    if r.status_code >= 300:
        raise SmsError(f"{s.sms_provider} a répondu {r.status_code} : {r.text[:300]}")
