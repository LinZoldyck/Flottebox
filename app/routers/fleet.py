"""Tableau de bord, véhicules, clients, locations directes, contrats et factures."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (AuditLog, Customer, Fine, FineStatus, Invoice, Message, Organization, Rental, RentalSource,
                        RentalStatus, Role, User, Vehicle)
from app.security import flash, get_current_user, require_role
from app.services import contracts, pennylane
from app.services.jobs import access_sms_text
from app.services.sms import SmsError, send_sms
from app.services.storage import absolute
from app.utils import generate_access_code, normalize_plate, now_local
from app.web import csrf_protect, render

router = APIRouter(dependencies=[Depends(csrf_protect)])
manager = require_role(Role.MANAGER)


def _get(db: Session, model, obj_id: int, user: User):
    obj = db.get(model, obj_id)
    if not obj or obj.organization_id != user.organization_id:
        raise HTTPException(status_code=404, detail="Introuvable")
    return obj


def _date(value: str):
    return date.fromisoformat(value) if value else None


@router.get("/")
def dashboard(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    now = now_local()
    org_id = user.organization_id
    upcoming = db.scalars(select(Rental).where(Rental.organization_id == org_id, Rental.ends_at >= now,
                                               Rental.status != RentalStatus.CANCELLED)
                          .order_by(Rental.starts_at).limit(10)).all()
    fines_todo = db.scalars(select(Fine).where(Fine.organization_id == org_id,
                                               Fine.status.in_([FineStatus.NEW, FineStatus.MATCHED, FineStatus.UNMATCHED]))
                            .order_by(Fine.designation_deadline)).all()
    escalated = db.scalars(select(Message).where(Message.organization_id == org_id,
                                                 Message.ai_status.in_(["escalated", "draft", "error"]))
                           .order_by(Message.sent_at.desc())).all()
    to_invoice = db.scalars(select(Rental).where(Rental.organization_id == org_id, Rental.source == RentalSource.DIRECT,
                                                 Rental.status == RentalStatus.RETURNED, ~Rental.invoices.any())).all()
    vehicles = db.scalars(select(Vehicle).where(Vehicle.organization_id == org_id, Vehicle.active)).all()
    return render(request, "dashboard.html", user=user, upcoming=upcoming, fines_todo=fines_todo,
                  escalated=escalated, to_invoice=to_invoice, vehicles=vehicles, now=now)


# ---------------- Véhicules ----------------

@router.get("/vehicles")
def vehicles_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicles = db.scalars(select(Vehicle).where(Vehicle.organization_id == user.organization_id).order_by(Vehicle.plate)).all()
    return render(request, "vehicles.html", user=user, vehicles=vehicles)


@router.get("/vehicles/new")
@router.get("/vehicles/{vehicle_id:int}")
def vehicle_form(request: Request, vehicle_id: int | None = None, user: User = Depends(manager), db: Session = Depends(get_db)):
    vehicle = _get(db, Vehicle, vehicle_id, user) if vehicle_id else Vehicle(fuel_type="Diesel", included_km_per_day=100,
                                                                              extra_km_price=0.30, deposit_amount=1000,
                                                                              daily_price=0)
    return render(request, "vehicle_form.html", user=user, vehicle=vehicle)


@router.post("/vehicles/new")
@router.post("/vehicles/{vehicle_id:int}")
def vehicle_save(request: Request, vehicle_id: int | None = None, plate: str = Form(...), brand: str = Form(""),
                 model: str = Form(""), fuel_type: str = Form("Diesel"), vin: str = Form(""),
                 parking_address: str = Form(""), masterlock_code: str = Form(""), getaround_car_id: str = Form(""),
                 current_mileage: int = Form(0), daily_price: float = Form(0), included_km_per_day: int = Form(100),
                 extra_km_price: float = Form(0.3), deposit_amount: float = Form(1000), active: bool = Form(False),
                 user: User = Depends(manager), db: Session = Depends(get_db)):
    vehicle = _get(db, Vehicle, vehicle_id, user) if vehicle_id else Vehicle(organization_id=user.organization_id)
    plate = normalize_plate(plate)
    clash = db.scalar(select(Vehicle).where(Vehicle.organization_id == user.organization_id, Vehicle.plate == plate,
                                            Vehicle.id != (vehicle.id or 0)))
    if clash:
        flash(request, f"Un véhicule {plate} existe déjà.", "error")
        return RedirectResponse(request.url.path, status_code=303)
    vehicle.plate, vehicle.brand, vehicle.model, vehicle.fuel_type, vehicle.vin = plate, brand, model, fuel_type, vin
    vehicle.parking_address, vehicle.masterlock_code = parking_address, masterlock_code.strip()
    vehicle.getaround_car_id = getaround_car_id.strip()
    vehicle.current_mileage, vehicle.daily_price = current_mileage, daily_price
    vehicle.included_km_per_day, vehicle.extra_km_price = included_km_per_day, extra_km_price
    vehicle.deposit_amount, vehicle.active = deposit_amount, active or not vehicle_id
    db.add(vehicle)
    db.commit()
    flash(request, f"Véhicule {plate} enregistré.", "success")
    return RedirectResponse("/vehicles", status_code=303)


# ---------------- Clients ----------------

@router.get("/customers")
def customers_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    customers = db.scalars(select(Customer).where(Customer.organization_id == user.organization_id)
                           .order_by(Customer.company_name, Customer.last_name)).all()
    return render(request, "customers.html", user=user, customers=customers)


@router.get("/customers/new")
@router.get("/customers/{customer_id:int}")
def customer_form(request: Request, customer_id: int | None = None, user: User = Depends(manager),
                  db: Session = Depends(get_db)):
    customer = _get(db, Customer, customer_id, user) if customer_id else Customer(is_company=True, civility="M",
                                                                                   country="FR", license_category="B")
    return render(request, "customer_form.html", user=user, customer=customer)


CUSTOMER_FIELDS = ["company_name", "siret", "civility", "first_name", "last_name", "email", "phone", "address",
                   "postal_code", "city", "country", "birth_place", "license_number", "license_issue_place",
                   "license_category"]


async def _apply_customer_form(request: Request, customer: Customer) -> None:
    form = await request.form()
    for field in CUSTOMER_FIELDS:
        if field in form:
            setattr(customer, field, str(form[field]).strip())
    if "is_company" in form or "company_name" in form:
        customer.is_company = form.get("is_company") == "on"
    for field in ("birth_date", "license_issue_date"):
        if field in form:
            setattr(customer, field, _date(str(form[field])))


@router.post("/customers/new")
@router.post("/customers/{customer_id:int}")
async def customer_save(request: Request, customer_id: int | None = None, user: User = Depends(manager),
                        db: Session = Depends(get_db)):
    customer = _get(db, Customer, customer_id, user) if customer_id else Customer(organization_id=user.organization_id)
    await _apply_customer_form(request, customer)
    db.add(customer)
    db.commit()
    flash(request, "Client enregistré.", "success")
    nxt = request.query_params.get("next")
    return RedirectResponse(nxt if nxt and nxt.startswith("/") else "/customers", status_code=303)


# ---------------- Locations ----------------

@router.get("/rentals")
def rentals_page(request: Request, source: str = "", user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = select(Rental).where(Rental.organization_id == user.organization_id)
    if source in ("direct", "getaround"):
        q = q.where(Rental.source == RentalSource(source))
    rentals = db.scalars(q.order_by(Rental.starts_at.desc()).limit(300)).all()
    return render(request, "rentals.html", user=user, rentals=rentals, source=source)


@router.get("/rentals/new")
def rental_new(request: Request, user: User = Depends(manager), db: Session = Depends(get_db)):
    org_id = user.organization_id
    vehicles = db.scalars(select(Vehicle).where(Vehicle.organization_id == org_id, Vehicle.active)).all()
    customers = db.scalars(select(Customer).where(Customer.organization_id == org_id)
                           .order_by(Customer.company_name, Customer.last_name)).all()
    start = (now_local() + timedelta(days=1)).replace(hour=8, minute=0, second=0)
    return render(request, "rental_form.html", user=user, vehicles=vehicles, customers=customers,
                  start=start, end=start.replace(hour=19))


def _overlaps(db: Session, vehicle_id: int, start: datetime, end: datetime, exclude: int = 0) -> Rental | None:
    return db.scalar(select(Rental).where(Rental.vehicle_id == vehicle_id, Rental.id != exclude,
                                          Rental.status != RentalStatus.CANCELLED,
                                          Rental.starts_at < end, Rental.ends_at > start))


@router.post("/rentals/new")
def rental_create(request: Request, vehicle_id: int = Form(...), customer_id: int = Form(...),
                  starts_at: datetime = Form(...), ends_at: datetime = Form(...), price_total: float = Form(0),
                  included_km: int | None = Form(None), extra_km_price: float | None = Form(None),
                  deposit_amount: float | None = Form(None), protection: str = Form(""), notes: str = Form(""),
                  user: User = Depends(manager), db: Session = Depends(get_db)):
    vehicle = _get(db, Vehicle, vehicle_id, user)
    customer = _get(db, Customer, customer_id, user)
    if ends_at <= starts_at:
        flash(request, "La fin doit être postérieure au début.", "error")
        return RedirectResponse("/rentals/new", status_code=303)
    clash = _overlaps(db, vehicle.id, starts_at, ends_at)
    if clash:
        flash(request, f"Le véhicule est déjà loué sur ce créneau ({clash.reference}).", "error")
        return RedirectResponse("/rentals/new", status_code=303)
    rental = Rental(organization_id=user.organization_id, source=RentalSource.DIRECT, vehicle_id=vehicle.id,
                    customer_id=customer.id, starts_at=starts_at, ends_at=ends_at, status=RentalStatus.CONFIRMED,
                    protection=protection, notes=notes, access_code=generate_access_code())
    rental.vehicle = vehicle
    days = rental.days
    rental.price_total = price_total or round(vehicle.daily_price * days, 2)
    rental.included_km = included_km if included_km is not None else vehicle.included_km_per_day * days
    rental.extra_km_price = extra_km_price if extra_km_price is not None else vehicle.extra_km_price
    rental.deposit_amount = deposit_amount if deposit_amount is not None else vehicle.deposit_amount
    db.add(rental)
    db.add(AuditLog(organization_id=user.organization_id, user_id=user.id, action="rental.create", detail=vehicle.plate))
    db.commit()
    flash(request, f"Location {rental.reference} créée. Le code d'accès sera envoyé par SMS "
                   f"30 min avant le début.", "success")
    return RedirectResponse(f"/rentals/{rental.id}", status_code=303)


@router.get("/rentals/{rental_id}")
def rental_detail(request: Request, rental_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rental = _get(db, Rental, rental_id, user)
    messages = db.scalars(select(Message).where(Message.rental_id == rental.id).order_by(Message.sent_at)).all()
    fines_list = db.scalars(select(Fine).where(Fine.rental_id == rental.id)).all()
    return render(request, "rental_detail.html", user=user, rental=rental, messages=messages, fines=fines_list,
                  sms_preview=access_sms_text(rental, db.get(Organization, user.organization_id)))


@router.post("/rentals/{rental_id}/status")
def rental_status(request: Request, rental_id: int, status: RentalStatus = Form(...), user: User = Depends(manager),
                  db: Session = Depends(get_db)):
    rental = _get(db, Rental, rental_id, user)
    rental.status = status
    db.commit()
    flash(request, f"Statut : {status.label}.", "success")
    return RedirectResponse(f"/rentals/{rental.id}", status_code=303)


@router.post("/rentals/{rental_id}/send-code")
def rental_send_code(request: Request, rental_id: int, regenerate: bool = Form(False), user: User = Depends(manager),
                     db: Session = Depends(get_db)):
    rental = _get(db, Rental, rental_id, user)
    if regenerate or not rental.access_code:
        rental.access_code = generate_access_code()
    try:
        send_sms(rental.customer.phone if rental.customer else "",
                 access_sms_text(rental, db.get(Organization, user.organization_id)))
        rental.access_code_sent_at = now_local()
        flash(request, "SMS envoyé.", "success")
    except SmsError as exc:
        flash(request, f"SMS non envoyé : {exc}", "error")
    db.commit()
    return RedirectResponse(f"/rentals/{rental.id}", status_code=303)


@router.post("/rentals/{rental_id}/contract")
def rental_contract_generate(request: Request, rental_id: int, user: User = Depends(manager), db: Session = Depends(get_db)):
    rental = _get(db, Rental, rental_id, user)
    contracts.generate_and_store(db.get(Organization, user.organization_id), rental)
    db.commit()
    flash(request, "Contrat régénéré.", "success")
    return RedirectResponse(f"/rentals/{rental.id}", status_code=303)


@router.get("/rentals/{rental_id}/contract.pdf")
def rental_contract(rental_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rental = _get(db, Rental, rental_id, user)
    if not rental.contract_path:
        contracts.generate_and_store(db.get(Organization, user.organization_id), rental)
        db.commit()
    return FileResponse(absolute(rental.contract_path), media_type="application/pdf",
                        filename=f"contrat-{rental.reference}.pdf")


@router.post("/rentals/{rental_id}/invoice")
def rental_invoice(request: Request, rental_id: int, user: User = Depends(manager), db: Session = Depends(get_db)):
    rental = _get(db, Rental, rental_id, user)
    org = db.get(Organization, user.organization_id)
    invoice = next((i for i in rental.invoices if i.status != "sent"), None)
    if any(i.status == "sent" for i in rental.invoices):
        flash(request, "Une facture a déjà été envoyée à Pennylane pour cette location.", "error")
        return RedirectResponse(f"/rentals/{rental.id}", status_code=303)
    if not invoice:
        invoice = pennylane.create_invoice_for_rental(org, rental)
        db.add(invoice)
        db.flush()
    pennylane.push_invoice(org, invoice)
    db.commit()
    if invoice.status == "sent":
        flash(request, "Facture créée en brouillon dans Pennylane.", "success")
    else:
        flash(request, f"Échec Pennylane : {invoice.error}", "error")
    return RedirectResponse(f"/rentals/{rental.id}", status_code=303)


@router.get("/invoices")
def invoices_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    invoices = db.scalars(select(Invoice).where(Invoice.organization_id == user.organization_id)
                          .order_by(Invoice.created_at.desc())).all()
    return render(request, "invoices.html", user=user, invoices=invoices)


@router.get("/files/{path:path}")
def serve_file(path: str, user: User = Depends(get_current_user)):
    if not path.startswith(f"org{user.organization_id}/"):
        raise HTTPException(status_code=404)
    file = absolute(path)
    if not file.exists():
        raise HTTPException(status_code=404)
    return FileResponse(file)
