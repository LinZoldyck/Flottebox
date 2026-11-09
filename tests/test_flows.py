from datetime import datetime, timedelta

import httpx
from pypdf import PdfReader
from sqlalchemy import select

from app.db import SessionLocal
from app.models import (Customer, Fine, FineStatus, Invoice, Organization, Rental, RentalSource, RentalStatus, Role,
                        User, Vehicle)
from app.services import ai_assistant, fines, getaround, jobs, pennylane
from app.services.sms import OUTBOX
from app.utils import normalize_plate, now_local
from tests.conftest import csrf_of, jpeg_bytes


def create_vehicle_and_customer(c):
    r = c.form("/vehicles/new", data={"plate": "er836an", "brand": "Opel", "model": "Movano", "fuel_type": "Diesel",
                                      "parking_address": "10 rue de Paris, 75010 Paris", "masterlock_code": "4821",
                                      "current_mileage": "101897", "daily_price": "77", "included_km_per_day": "200",
                                      "extra_km_price": "0.48", "deposit_amount": "1000", "active": "on"})
    assert r.status_code == 200
    r = c.form("/customers/new", data={"is_company": "on", "company_name": "Event Pro", "phone": "06 12 34 56 78",
                                       "email": "client@event.fr", "first_name": "Raphaël", "last_name": "Martin"})
    assert r.status_code == 200
    db = SessionLocal()
    v = db.scalar(select(Vehicle))
    cu = db.scalar(select(Customer))
    db.close()
    return v, cu


def test_roles_and_auth(owner):
    c = owner
    assert c.get("/users").status_code == 200
    r = c.form("/users", data={"email": "viewer@test.fr", "full_name": "V", "role": "viewer", "password": "motdepasse123"})
    assert "viewer@test.fr" in r.text
    c.post("/logout", data={"csrf_token": csrf_of(c.get("/").text)})
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    c.form("/login", data={"email": "viewer@test.fr", "password": "motdepasse123"})
    assert c.get("/vehicles").status_code == 200
    assert c.get("/settings").status_code == 403
    assert c.get("/vehicles/new").status_code == 403
    # mauvais jeton CSRF refusé
    assert c.post("/account/password", data={"csrf_token": "x", "current": "a", "new": "b"}).status_code == 400


def test_direct_rental_full_flow(owner):
    c = owner
    vehicle, customer = create_vehicle_and_customer(c)
    assert vehicle.plate == "ER-836-AN"
    start = now_local() + timedelta(minutes=20)
    r = c.form("/rentals/new", data={"vehicle_id": vehicle.id, "customer_id": customer.id,
                                     "starts_at": start.strftime("%Y-%m-%dT%H:%M"),
                                     "ends_at": (start + timedelta(hours=11)).strftime("%Y-%m-%dT%H:%M"),
                                     "protection": "Essentielle"})
    assert r.status_code == 200 and "créée" in r.text

    db = SessionLocal()
    rental = db.scalar(select(Rental))
    assert rental.price_total == 77 and rental.included_km == 200 and len(rental.access_code) == 8
    # Envoi automatique du code 30 min avant
    OUTBOX.clear()
    assert jobs.send_due_access_codes(db) == 1
    assert rental.access_code in OUTBOX[0]["body"] and OUTBOX[0]["to"] == "+33612345678"
    assert jobs.send_due_access_codes(db) == 0
    code = rental.access_code
    db.close()

    # Côté locataire (nouvelle session)
    c.cookies.clear()
    r = c.form("/r", data={"code": "BADCODE1"})
    assert "Code invalide" in r.text
    r = c.form("/r", data={"code": code.lower()})
    assert "Mes informations conducteur" in r.text
    assert "4821" not in r.text  # code de la boîte masqué tant que le contrat n'est pas accepté
    r = c.form("/r/home", "/r/identity", data={
        "civility": "M", "first_name": "Raphaël", "last_name": "Martin", "address": "1 rue Test", "postal_code": "75010",
        "city": "Paris", "birth_date": "1997-02-01", "birth_place": "Lyon", "license_number": "15AK15217",
        "license_issue_date": "2015-03-16", "license_issue_place": "Préfecture du Rhône"})
    assert "Contrat de location" in r.text
    r = c.form("/r/home", "/r/accept", data={"agree": "on", "full_name": "Raphaël Martin"})
    assert "4821" in r.text

    photos = [(f"photo_{i}", (f"p{i}.jpg", jpeg_bytes(), "image/jpeg")) for i in range(6)]
    r = c.form("/r/inspection/checkin", data={"mileage": "101897", "fuel_level": "55", "cleanliness_score": "5"},
               files=photos)
    assert "Bonne route" in r.text
    r = c.form("/r/inspection/checkout", data={"mileage": "102111", "fuel_level": "55", "cleanliness_score": "4",
                                               "damages": "Rayure porte arrière"},
               files=photos + [("extra_photos", ("d.jpg", jpeg_bytes((0, 0, 200)), "image/jpeg"))])
    assert "Rendre les clés" in r.text and "214 km" in r.text and "14 km" in r.text

    pdf = c.get("/r/contract.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    text = "\n".join(p.extract_text() for p in PdfReader(__import__("io").BytesIO(pdf.content)).pages)
    for expected in ["Réservation", "Informations de début de location", "Informations de fin de location",
                     "ER836AN", "15AK15217", "Distance parcourue", "214 km", "Rayure porte arrière",
                     "Photos — état des lieux de départ", "Conditions générales"]:
        assert expected in text, expected

    db = SessionLocal()
    rental = db.scalar(select(Rental))
    assert rental.status == RentalStatus.RETURNED and rental.extra_km == 14
    assert len(rental.inspection("checkout").photos) == 7
    assert db.get(Vehicle, vehicle.id).current_mileage == 102111
    lines = pennylane.build_lines(rental)
    assert lines[1]["quantity"] == 14 and lines[1]["unit_price_ttc"] == 0.48
    db.close()


def test_double_booking_rejected(owner):
    c = owner
    v, cu = create_vehicle_and_customer(c)
    data = {"vehicle_id": v.id, "customer_id": cu.id, "starts_at": "2030-01-10T08:00", "ends_at": "2030-01-10T18:00"}
    c.form("/rentals/new", data=data)
    r = c.form("/rentals/new", data={**data, "starts_at": "2030-01-10T17:00", "ends_at": "2030-01-11T10:00"})
    assert "déjà loué" in r.text


def test_code_not_active_too_early(owner):
    c = owner
    v, cu = create_vehicle_and_customer(c)
    c.form("/rentals/new", data={"vehicle_id": v.id, "customer_id": cu.id,
                                 "starts_at": "2030-01-10T08:00", "ends_at": "2030-01-10T18:00"})
    db = SessionLocal()
    code = db.scalar(select(Rental)).access_code
    db.close()
    r = c.form("/r", data={"code": code})
    assert "sera actif à partir" in r.text


FINE_TEXT = """AVIS DE CONTRAVENTION
Numéro de l'avis : 1234 5678 9012
Le 04/10/2026 à 14h35
Lieu : AVENUE DE LA REPUBLIQUE 75011 PARIS
Nature de l'infraction : EXCES DE VITESSE INFERIEUR A 20 KM/H
Véhicule immatriculé ER-836-AN
Montant de l'amende forfaitaire : 135,00 €
Désignation à effectuer avant le 19/11/2026
"""


def test_fine_text_parsing():
    out = fines.parse_fine_text(FINE_TEXT)
    assert out["plate"] == "ER-836-AN"
    assert out["offense_at"] == datetime(2026, 10, 4, 14, 35)
    assert out["notice_number"] == "123456789012"
    assert out["amount"] == 135.0
    assert "REPUBLIQUE" in out["location"]
    assert str(out["designation_deadline"]) == "2026-11-19"


def test_fine_matching_direct_and_getaround(owner):
    c = owner
    v, cu = create_vehicle_and_customer(c)
    db = SessionLocal()
    org = db.scalar(select(Organization))
    direct = Rental(organization_id=org.id, vehicle_id=v.id, customer_id=cu.id, source=RentalSource.DIRECT,
                    starts_at=datetime(2026, 10, 4, 10, 30), ends_at=datetime(2026, 10, 4, 22, 0))
    ga_driver = Customer(organization_id=org.id, first_name="Lina", last_name="Ga", getaround_user_id="77")
    db.add_all([direct, ga_driver])
    db.flush()
    ga = Rental(organization_id=org.id, vehicle_id=v.id, customer_id=ga_driver.id, source=RentalSource.GETAROUND,
                external_id="999", starts_at=datetime(2026, 10, 6, 8, 0), ends_at=datetime(2026, 10, 6, 20, 0))
    db.add(ga)
    db.commit()

    f1 = Fine(organization_id=org.id, file_path="x", plate="ER836AN", offense_at=datetime(2026, 10, 4, 14, 35))
    f2 = Fine(organization_id=org.id, file_path="y", plate="ER-836-AN", offense_at=datetime(2026, 10, 6, 19, 0))
    f3 = Fine(organization_id=org.id, file_path="z", plate="ER-836-AN", offense_at=datetime(2026, 10, 8, 9, 0))
    f4 = Fine(organization_id=org.id, file_path="w", plate="ZZ-000-ZZ", offense_at=datetime(2026, 10, 4, 14, 0))
    for f in (f1, f2, f3, f4):
        db.add(f)
        fines.match(db, f)
    db.commit()
    assert f1.status == FineStatus.MATCHED and f1.rental_id == direct.id and "location directe" in f1.match_note
    assert f2.status == FineStatus.MATCHED and f2.rental_id == ga.id and "Getaround" in f2.match_note
    assert f3.status == FineStatus.UNMATCHED and f4.status == FineStatus.UNMATCHED
    db.close()


def test_fine_upload_and_robot_api(owner):
    c = owner
    v, cu = create_vehicle_and_customer(c)
    db = SessionLocal()
    org = db.scalar(select(Organization))
    db.add(Rental(organization_id=org.id, vehicle_id=v.id, customer_id=cu.id,
                  starts_at=datetime(2026, 10, 4, 10, 30), ends_at=datetime(2026, 10, 4, 22, 0)))
    db.commit()
    db.close()

    # PDF « texte » d'un avis fictif
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for line in FINE_TEXT.replace("€", "EUR").splitlines():
        pdf.cell(0, 6, line.encode("latin-1", "replace").decode("latin-1"), new_x="LMARGIN", new_y="NEXT")
    r = c.form("/fines", "/fines/upload", files=[("files", ("avis.pdf", bytes(pdf.output()), "application/pdf"))])
    assert "1 avis importé" in r.text and "Conducteur identifié" in r.text

    # Jeton du robot
    r = c.form("/settings", "/settings/api-token")
    token = r.text.split("<code>fb_")[1].split("</code>")[0]
    headers = {"Authorization": f"Bearer fb_{token}"}
    assert c.get("/api/fines", headers={"Authorization": "Bearer nope"}).status_code == 401
    listing = c.get("/api/fines", headers=headers).json()
    assert len(listing) == 1
    d = c.get(f"/api/fines/{listing[0]['id']}/designation", headers=headers).json()
    assert d["avis"]["immatriculation"] == "ER836AN" and d["conducteur"]["nom"] == "Martin"
    assert "Date de naissance" in d["manquants"]
    assert c.post(f"/api/fines/{listing[0]['id']}/designated", headers=headers).json() == {"ok": True}


def test_assistant_guardrails(monkeypatch):
    d = ai_assistant.decide_reply("Bonjour, je voudrais être remboursé de la caution", "", "")
    assert d.decision == "escalate" and "rembours" in d.reason
    d = ai_assistant.decide_reply("J'ai eu un petit accident, que faire ?", "", "")
    assert d.decision == "escalate"
    # sans clé API : toujours transmis à un humain
    d = ai_assistant.decide_reply("Où est garé le camion ?", "Le camion est au 10 rue de Paris", "")
    assert d.decision == "escalate" and "non configurée" in d.reason


def test_incoming_message_draft_and_auto(owner, monkeypatch):
    c = owner
    v, cu = create_vehicle_and_customer(c)
    db = SessionLocal()
    org = db.scalar(select(Organization))
    rental = Rental(organization_id=org.id, vehicle_id=v.id, source=RentalSource.GETAROUND, external_id="555",
                    starts_at=now_local(), ends_at=now_local() + timedelta(hours=5))
    db.add(rental)
    db.commit()
    monkeypatch.setattr(ai_assistant, "decide_reply", lambda *a, **k: ai_assistant.AssistantDecision(
        decision="reply", category="acces", reply="Le véhicule est au 10 rue de Paris.", reason="FAQ"))

    sent = []

    class FakeClient:
        def send_message(self, rid, content):
            sent.append((rid, content))

    from app.models import Message
    m1 = Message(organization_id=org.id, rental_id=rental.id, external_id="m1", direction="in", body="Où est le camion ?")
    db.add(m1)
    db.commit()
    jobs.handle_incoming(db, org, m1, FakeClient())
    assert m1.ai_status == "draft" and not sent  # mode brouillon par défaut

    org.assistant_mode = "auto"
    m2 = Message(organization_id=org.id, rental_id=rental.id, external_id="m2", direction="in", body="Adresse ?")
    db.add(m2)
    db.commit()
    jobs.handle_incoming(db, org, m2, FakeClient())
    assert m2.ai_status == "sent" and sent == [("555", "Le véhicule est au 10 rue de Paris.")]
    db.close()


def test_getaround_sync_with_mock_api(owner):
    c = owner
    v, _ = create_vehicle_and_customer(c)

    def handler(request: httpx.Request):
        p = request.url.path
        if p.endswith("/cars"):
            return httpx.Response(200, json=[{"id": 42, "plate_number": "ER836AN"}])
        if p.endswith("/rentals"):
            return httpx.Response(200, json=[{"id": 10526144, "car_id": 42, "starts_at": "2026-10-04T08:30:00Z",
                                              "ends_at": "2026-10-04T20:00:00Z", "status": "ended",
                                              "driver_id": 7}])
        if p.endswith("/users/7"):
            return httpx.Response(200, json={"id": 7, "first_name": "Raphaël", "last_name": "Cartagena",
                                             "birth_date": "1997-02-01"})
        if p.endswith("/messages"):
            return httpx.Response(200, json=[{"id": 1, "content": "Bonjour", "sender_type": "driver",
                                              "created_at": "2026-10-04T07:00:00Z"}])
        return httpx.Response(404)

    db = SessionLocal()
    org = db.scalar(select(Organization))
    client = getaround.GetaroundClient("tok", base_url="https://api.test/owner/v1", transport=httpx.MockTransport(handler))
    assert getaround.sync_rentals(db, org, client=client) == 1
    assert getaround.sync_rentals(db, org, client=client) == 1  # idempotent
    rentals = db.scalars(select(Rental)).all()
    assert len(rentals) == 1
    r = rentals[0]
    assert r.starts_at == datetime(2026, 10, 4, 10, 30)  # converti à l'heure de Paris
    assert r.status == RentalStatus.RETURNED and r.customer.last_name == "Cartagena"
    assert db.get(Vehicle, v.id).getaround_car_id == "42"
    db.close()


def test_getaround_webhook_signature(owner):
    import hashlib
    import hmac
    db = SessionLocal()
    org = db.scalar(select(Organization))
    org.getaround_webhook_secret = "s3cret"
    db.commit()
    body = b'{"type":"rental.booked"}'
    assert owner.post(f"/webhooks/getaround/{org.id}", content=body, headers={"X-Getaround-Signature": "bad"}).status_code == 401
    sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert owner.post(f"/webhooks/getaround/{org.id}", content=body, headers={"X-Getaround-Signature": sig}).status_code == 200
    db.close()


def test_pennylane_invoice_with_mock(owner):
    c = owner
    v, cu = create_vehicle_and_customer(c)
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path, request.content))
        if request.url.path.endswith("/customers"):
            return httpx.Response(200, json={"items": []})
        if request.url.path.endswith("/company_customers"):
            return httpx.Response(201, json={"id": 321})
        if request.url.path.endswith("/customer_invoices"):
            return httpx.Response(201, json={"id": 9876, "public_file_url": "https://pennylane/x.pdf"})
        return httpx.Response(404)

    db = SessionLocal()
    org = db.scalar(select(Organization))
    rental = Rental(organization_id=org.id, vehicle_id=v.id, customer_id=cu.id, price_total=77, included_km=200,
                    extra_km_price=0.48, starts_at=datetime(2026, 10, 4, 10, 30), ends_at=datetime(2026, 10, 4, 22, 0))
    db.add(rental)
    db.flush()
    invoice = pennylane.create_invoice_for_rental(org, rental)
    db.add(invoice)
    db.flush()
    pl = pennylane.PennylaneClient("tok", transport=httpx.MockTransport(handler))
    pennylane.push_invoice(org, invoice, pl)
    assert invoice.status == "sent" and invoice.pennylane_id == "9876"
    assert db.get(Customer, cu.id).pennylane_customer_id == "321"
    pennylane.push_invoice(org, invoice, pl)  # pas de doublon
    assert sum(1 for m, p, _ in calls if p.endswith("/customer_invoices")) == 1
    import json
    payload = json.loads(next(b for m, p, b in calls if p.endswith("/customer_invoices")))
    assert payload["customer_id"] == 321 and payload["draft"] is True
    assert payload["invoice_lines"][0]["raw_currency_unit_price"] == "77.00"
    db.close()


def test_utils():
    assert normalize_plate("ab 123 cd") == "AB-123-CD"
    assert normalize_plate("ER836AN") == "ER-836-AN"


FPS_TEXT = """Avis de Paiement
Forfait de post-stationnement (FPS)
PANTIN
Numéro de l'avis de paiement : 21930055500014 26 1 289 003 975 Clé 73
Date d'envoi de l'avis de paiement : 21/10/2026
Le véhicule dont le certificat d'immatriculation est à votre nom a stationné le 16/10/2026 sur le territoire de PANTIN
Date et heure de constatation de l'absence ou de l'insuffisance de paiement immédiat de la redevance :
Le 16/10/2026 à 10h33
Lieu :
48 RUE DIDEROT
93500 PANTIN
N° d'immatriculation du véhicule :
DS-301-FV
Le montant du FPS dû est égal à : 35 euros.
Ce FPS a cessé de produire ses effets le 17/10/2026 à 11h33.
"""


def test_fps_parsing():
    out = fines.parse_fine_text(FPS_TEXT)
    assert out["kind"] == "fps"
    assert out["notice_number"] == "21930055500014261289003975"
    assert out["plate"] == "DS-301-FV"
    assert out["offense_at"] == datetime(2026, 10, 16, 10, 33)
    assert out["location"] == "48 RUE DIDEROT 93500 PANTIN"
    assert out["amount"] == 35.0
    assert str(out["designation_deadline"]) == "2026-11-21"  # RAPO : 1 mois après l'envoi
    assert fines.parse_fine_text(FINE_TEXT)["kind"] == "contravention"


def test_fps_rebill_and_no_designation(owner, monkeypatch):
    c = owner
    c.form("/vehicles/new", data={"plate": "DS-301-FV", "brand": "Citroën", "model": "Jumper", "active": "on"})
    c.form("/customers/new", data={"is_company": "on", "company_name": "Meubles SARL", "phone": "0600000000",
                                   "first_name": "Alex", "last_name": "Test"})
    c.form("/rentals/new", data={"vehicle_id": 1, "customer_id": 1, "starts_at": "2026-10-16T08:00",
                                 "ends_at": "2026-10-16T18:00", "price_total": "77"})
    db = SessionLocal()
    org = db.scalar(select(Organization))
    fine = Fine(organization_id=org.id, file_path="x")
    for k, v in fines.parse_fine_text(FPS_TEXT).items():
        setattr(fine, k, v)
    db.add(fine)
    db.flush()
    fines.match(db, fine)
    db.commit()
    assert fine.status == FineStatus.MATCHED and fine.is_fps
    fine_id = fine.id
    db.close()

    page = c.get(f"/fines/{fine_id}").text
    assert "pas de désignation" in page and "robot/antai_designation.py" not in page

    sent = []
    monkeypatch.setattr(pennylane, "push_invoice", lambda org, inv, client=None: (sent.append(inv), setattr(inv, "status", "sent")))
    c.form(f"/fines/{fine_id}", f"/fines/{fine_id}/rebill", data={"admin_fee": "15"})
    db = SessionLocal()
    fine = db.get(Fine, fine_id)
    assert fine.status == FineStatus.REBILLED and fine.invoice_id
    inv = db.get(Invoice, fine.invoice_id)
    assert inv.total_ttc == 50.0 and inv.kind == "fps" and "21930055500014261289003975" in inv.lines[0]["label"]
    db.close()

    # la refacturation du FPS ne doit pas bloquer ni remplacer la facture de la location
    c.form("/rentals/1", "/rentals/1/invoice")
    db = SessionLocal()
    kinds = sorted(i.kind for i in db.scalars(select(Invoice)))
    assert kinds == ["fps", "rental"]
    db.close()

    r = c.form("/settings", "/settings/api-token")
    token = r.text.split("<code>fb_")[1].split("</code>")[0]
    h = {"Authorization": f"Bearer fb_{token}"}
    assert c.get(f"/api/fines/{fine_id}/designation", headers=h).status_code == 409
    assert c.get("/api/fines", headers=h).json() == []


def test_add_missing_columns_migration(tmp_path):
    from sqlalchemy import create_engine, inspect, text
    import app.db as appdb
    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as conn:  # base d'une ancienne version : table fines sans colonne "kind"
        conn.execute(text("CREATE TABLE fines (id INTEGER PRIMARY KEY, organization_id INTEGER, file_path VARCHAR)"))
        conn.execute(text("INSERT INTO fines (organization_id, file_path) VALUES (1, 'a.pdf')"))
    old = appdb.engine
    appdb.engine = eng
    try:
        appdb.add_missing_columns()
    finally:
        appdb.engine = old
    cols = {c["name"] for c in inspect(eng).get_columns("fines")}
    assert {"kind", "invoice_id", "plate"} <= cols
    with eng.connect() as conn:
        assert conn.execute(text("SELECT kind FROM fines")).scalar() == "contravention"
