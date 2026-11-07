"""Client de l'API Getaround « Owner » (https://api-docs.getaround.com).

Points connus de l'API (à vérifier dans votre espace pro Getaround, l'accès se fait sur demande) :
- Base : https://api-eu.getaround.com/owner/v1 — authentification « Authorization: Bearer <jeton> »
- GET  /cars, /rentals?start_date=&end_date= (fenêtre de 30 jours max), /rentals/{id}, /users/{id}
- GET/POST /rentals/{id}/messages (lecture / envoi de messages au locataire)
- GET  /rentals/{id}/checkin, /rentals/{id}/checkout, /invoices
- Webhooks signés HMAC (activation à demander à Getaround), pagination via l'en-tête Link, 100 req/min.

L'ouverture du véhicule (Getaround Connect) n'est pas exposée par l'API publique : elle reste gérée
par l'application Getaround du locataire. Les champs des réponses sont lus de façon défensive car le
format exact peut évoluer ; la réponse brute est conservée dans Rental.raw.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Customer, Message, Organization, Rental, RentalSource, RentalStatus, Vehicle
from app.utils import normalize_plate, now_local, to_local_naive

log = logging.getLogger(__name__)


class GetaroundError(Exception):
    pass


class GetaroundClient:
    def __init__(self, token: str, base_url: str | None = None, transport: httpx.BaseTransport | None = None):
        if not token:
            raise GetaroundError("Jeton API Getaround non configuré (Paramètres > Intégrations).")
        self.http = httpx.Client(
            base_url=base_url or get_settings().getaround_base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
            transport=transport,
        )

    def _get_all(self, path: str, params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        url: str | None = path
        while url:
            r = self.http.get(url, params=params)
            if r.status_code >= 300:
                raise GetaroundError(f"GET {path} : {r.status_code} {r.text[:200]}")
            payload = r.json()
            if isinstance(payload, dict):
                payload = next((v for v in payload.values() if isinstance(v, list)), [payload])
            items.extend(payload)
            url = r.links.get("next", {}).get("url")
            params = None
        return items

    def cars(self) -> list[dict]:
        return self._get_all("/cars")

    def rentals(self, start: date, end: date) -> list[dict]:
        out: list[dict] = []
        cursor = start
        while cursor < end:  # fenêtres de 30 jours maximum
            chunk_end = min(cursor + timedelta(days=30), end)
            out += self._get_all("/rentals", {"start_date": cursor.isoformat(), "end_date": chunk_end.isoformat()})
            cursor = chunk_end
        return out

    def user(self, user_id: str) -> dict:
        r = self.http.get(f"/users/{user_id}")
        if r.status_code >= 300:
            raise GetaroundError(f"GET /users/{user_id} : {r.status_code}")
        return r.json()

    def messages(self, rental_id: str) -> list[dict]:
        return self._get_all(f"/rentals/{rental_id}/messages")

    def send_message(self, rental_id: str, content: str) -> dict:
        r = self.http.post(f"/rentals/{rental_id}/messages", json={"content": content})
        if r.status_code >= 300:
            raise GetaroundError(f"Envoi du message refusé : {r.status_code} {r.text[:200]}")
        return r.json() if r.content else {}


def verify_webhook(secret: str, body: bytes, signature: str) -> bool:
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    sig = signature.split("=", 1)[-1].strip()
    return hmac.compare_digest(expected, sig)


def _dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return to_local_naive(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _first(d: dict, *keys, default=None):
    for k in keys:
        cur = d
        for part in k.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur not in (None, ""):
            return cur
    return default


def _status(raw: dict) -> RentalStatus:
    st = str(_first(raw, "status", "state", default="")).lower()
    if "cancel" in st:
        return RentalStatus.CANCELLED
    if st in ("ended", "finished", "checked_out", "closed"):
        return RentalStatus.RETURNED
    if st in ("ongoing", "started", "checked_in", "in_progress"):
        return RentalStatus.IN_PROGRESS
    return RentalStatus.CONFIRMED


def upsert_driver(db: Session, org: Organization, raw: dict, client: GetaroundClient | None) -> Customer | None:
    driver = _first(raw, "driver", "renter", "user", default=None)
    user_id = str(_first(raw, "driver_id", "renter_id", "driver.id", "renter.id", default="") or "")
    if not isinstance(driver, dict):
        driver = {}
    if user_id and client and not driver.get("last_name"):
        try:
            driver = {**driver, **client.user(user_id)}
        except GetaroundError:
            pass
    if not user_id and not driver:
        return None
    customer = None
    if user_id:
        customer = db.scalar(select(Customer).where(Customer.organization_id == org.id,
                                                    Customer.getaround_user_id == user_id))
    if not customer:
        customer = Customer(organization_id=org.id, getaround_user_id=user_id, is_company=False)
        db.add(customer)
        db.flush()
    customer.first_name = _first(driver, "first_name", "firstname", default=customer.first_name)
    customer.last_name = _first(driver, "last_name", "lastname", default=customer.last_name)
    customer.email = _first(driver, "email", default=customer.email)
    customer.phone = _first(driver, "phone", "phone_number", default=customer.phone)
    customer.address = _first(driver, "address.street", "address_line1", default=customer.address)
    customer.postal_code = _first(driver, "address.postal_code", "postal_code", default=customer.postal_code)
    customer.city = _first(driver, "address.city", "city", default=customer.city)
    customer.license_number = _first(driver, "driving_license_number", "license_number", default=customer.license_number)
    bd = _first(driver, "birth_date", "birthdate", default=None)
    if bd:
        try:
            customer.birth_date = date.fromisoformat(str(bd)[:10])
        except ValueError:
            pass
    customer.birth_place = _first(driver, "birth_place", "birthplace", default=customer.birth_place)
    return customer


def sync_rentals(db: Session, org: Organization, days_back: int = 60, days_ahead: int = 60,
                 client: GetaroundClient | None = None) -> int:
    client = client or GetaroundClient(org.getaround_token)
    vehicles = {v.getaround_car_id: v for v in db.scalars(select(Vehicle).where(Vehicle.organization_id == org.id))
                if v.getaround_car_id}

    # Associe automatiquement les véhicules Getaround à la flotte via la plaque.
    try:
        for car in client.cars():
            car_id = str(_first(car, "id", default=""))
            plate = normalize_plate(_first(car, "plate_number", "license_plate", "plate", default=""))
            if car_id and car_id not in vehicles and plate:
                v = db.scalar(select(Vehicle).where(Vehicle.organization_id == org.id, Vehicle.plate == plate))
                if v:
                    v.getaround_car_id = car_id
                    vehicles[car_id] = v
    except GetaroundError as exc:
        log.warning("Liste des véhicules Getaround indisponible : %s", exc)

    today = now_local().date()
    unique: dict[str, dict] = {}
    for raw in client.rentals(today - timedelta(days=days_back), today + timedelta(days=days_ahead)):
        unique[str(_first(raw, "id", default=""))] = raw  # une location peut chevaucher deux fenêtres
    count = 0
    for ext_id, raw in unique.items():
        car_id = str(_first(raw, "car_id", "car.id", default=""))
        vehicle = vehicles.get(car_id)
        starts, ends = _dt(_first(raw, "starts_at", "start_date")), _dt(_first(raw, "ends_at", "end_date"))
        if not ext_id or not vehicle or not starts or not ends:
            continue
        rental = db.scalar(select(Rental).where(Rental.organization_id == org.id,
                                                Rental.source == RentalSource.GETAROUND,
                                                Rental.external_id == ext_id))
        if not rental:
            rental = Rental(organization_id=org.id, source=RentalSource.GETAROUND, external_id=ext_id,
                            vehicle_id=vehicle.id, starts_at=starts, ends_at=ends)
            db.add(rental)
            db.flush()
        rental.vehicle_id = vehicle.id
        rental.starts_at, rental.ends_at = starts, ends
        rental.status = _status(raw)
        price = _first(raw, "price", "total_price", "owner_earnings", default=None)
        if isinstance(price, (int, float, str)):
            try:
                rental.price_total = float(price)
            except ValueError:
                pass
        rental.included_km = int(_first(raw, "distance_included", "included_distance", default=0) or 0)
        rental.raw = raw
        customer = upsert_driver(db, org, raw, client)
        if customer:
            db.flush()
            rental.customer_id = customer.id
        count += 1
    db.commit()
    return count


def poll_messages(db: Session, org: Organization, client: GetaroundClient | None = None) -> list[Message]:
    """Récupère les nouveaux messages des locations Getaround récentes et renvoie les messages entrants nouveaux."""
    client = client or GetaroundClient(org.getaround_token)
    now = now_local()
    rentals = db.scalars(select(Rental).where(
        Rental.organization_id == org.id, Rental.source == RentalSource.GETAROUND,
        Rental.ends_at >= now - timedelta(days=3), Rental.starts_at <= now + timedelta(days=30),
    )).all()
    new_incoming: list[Message] = []
    for rental in rentals:
        try:
            raws = client.messages(rental.external_id)
        except GetaroundError as exc:
            log.warning("Messages de %s indisponibles : %s", rental.external_id, exc)
            continue
        for raw in raws:
            ext = str(_first(raw, "id", default=""))
            if not ext or db.scalar(select(Message.id).where(Message.organization_id == org.id, Message.external_id == ext)):
                continue
            sender = str(_first(raw, "sender_type", "author_type", "sender.role", "from", default="")).lower()
            is_owner = any(k in sender for k in ("owner", "proprietaire", "host"))
            msg = Message(organization_id=org.id, rental_id=rental.id, external_id=ext,
                          direction="out" if is_owner else "in",
                          body=str(_first(raw, "content", "body", "text", default="")),
                          sent_at=_dt(_first(raw, "created_at", "sent_at")) or now)
            db.add(msg)
            if not is_owner:
                new_incoming.append(msg)
    db.commit()
    return new_incoming
