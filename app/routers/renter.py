"""Espace locataire (location hors plateforme) : connexion par code SMS, code de la boîte à clés,
acceptation du contrat, états des lieux de départ et de retour avec photos."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Inspection, Organization, Photo, Rental, RentalSource, RentalStatus
from app.routers.fleet import _apply_customer_form
from app.security import client_ip, flash, renter_code_limiter
from app.services import contracts
from app.services.storage import UploadError, absolute, save_image
from app.utils import now_local
from app.web import csrf_protect, render

router = APIRouter(prefix="/r", dependencies=[Depends(csrf_protect)])

REQUIRED_ANGLES = ["Avant", "Arrière", "Côté gauche", "Côté droit", "Intérieur / cabine", "Compteur & carburant"]
IDENTITY_FIELDS = ["civility", "first_name", "last_name", "address", "postal_code", "city", "birth_place",
                   "license_number", "license_issue_place", "birth_date", "license_issue_date"]


def code_window(rental: Rental) -> tuple:
    s = get_settings()
    return (rental.starts_at - timedelta(minutes=s.access_code_lead_minutes),
            rental.ends_at + timedelta(hours=s.access_code_grace_hours))


def current_rental(request: Request, db: Session) -> Rental | None:
    rental_id = request.session.get("renter_rental_id")
    rental = db.get(Rental, rental_id) if rental_id else None
    if not rental or rental.status == RentalStatus.CANCELLED:
        return None
    opens, closes = code_window(rental)
    if not (opens <= now_local() <= closes):
        return None
    return rental


@router.get("")
def code_page(request: Request):
    return render(request, "renter/login.html")


@router.post("")
def code_login(request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    ip = client_ip(request)
    if not renter_code_limiter.hit(ip):
        flash(request, "Trop de tentatives. Réessayez dans 15 minutes.", "error")
        return RedirectResponse("/r", status_code=303)
    code = code.strip().upper().replace(" ", "")
    rental = db.scalar(select(Rental).where(Rental.access_code == code, Rental.source == RentalSource.DIRECT,
                                            Rental.status != RentalStatus.CANCELLED)) if len(code) >= 6 else None
    if not rental:
        flash(request, "Code invalide.", "error")
        return RedirectResponse("/r", status_code=303)
    opens, closes = code_window(rental)
    now = now_local()
    if now < opens:
        flash(request, f"Votre code sera actif à partir du {opens:%d/%m à %H:%M}.", "error")
        return RedirectResponse("/r", status_code=303)
    if now > closes:
        flash(request, "Ce code a expiré.", "error")
        return RedirectResponse("/r", status_code=303)
    renter_code_limiter.reset(ip)
    request.session.clear()
    request.session["renter_rental_id"] = rental.id
    return RedirectResponse("/r/home", status_code=303)


@router.get("/home")
def home(request: Request, db: Session = Depends(get_db)):
    rental = current_rental(request, db)
    if not rental:
        flash(request, "Session expirée, saisissez à nouveau votre code.", "error")
        return RedirectResponse("/r", status_code=303)
    org = db.get(Organization, rental.organization_id)
    return render(request, "renter/home.html", rental=rental, org=org,
                  missing=rental.customer.missing_designation_fields if rental.customer else ["Identité"],
                  checkin=rental.inspection("checkin"), checkout=rental.inspection("checkout"),
                  terms=org.contract_terms or contracts.DEFAULT_TERMS)


@router.post("/identity")
async def identity(request: Request, db: Session = Depends(get_db)):
    rental = current_rental(request, db)
    if not rental or not rental.customer:
        return RedirectResponse("/r", status_code=303)
    form = await request.form()
    missing = [f for f in IDENTITY_FIELDS if not str(form.get(f, "")).strip()]
    if missing:
        flash(request, "Merci de remplir tous les champs (obligatoires pour le contrat).", "error")
        return RedirectResponse("/r/home", status_code=303)
    await _apply_customer_form(request, rental.customer)
    db.commit()
    return RedirectResponse("/r/home", status_code=303)


@router.post("/accept")
def accept(request: Request, full_name: str = Form(...), agree: bool = Form(False), db: Session = Depends(get_db)):
    rental = current_rental(request, db)
    if not rental:
        return RedirectResponse("/r", status_code=303)
    if not agree or len(full_name.strip()) < 3:
        flash(request, "Cochez la case et saisissez vos nom et prénom pour accepter le contrat.", "error")
    elif not rental.terms_accepted_at:
        rental.terms_accepted_at = now_local()
        rental.terms_accepted_ip = client_ip(request)
        rental.terms_accepted_name = full_name.strip()
        db.commit()
    return RedirectResponse("/r/home", status_code=303)


@router.get("/inspection/{kind}")
def inspection_form(request: Request, kind: str, db: Session = Depends(get_db)):
    rental = current_rental(request, db)
    if not rental or kind not in ("checkin", "checkout"):
        return RedirectResponse("/r", status_code=303)
    return render(request, "renter/inspection.html", rental=rental, kind=kind, angles=REQUIRED_ANGLES,
                  previous=rental.inspection("checkin"))


@router.post("/inspection/{kind}")
async def inspection_save(request: Request, kind: str, mileage: int = Form(...), fuel_level: int = Form(...),
                          cleanliness_score: int = Form(5), cleanliness_issues: str = Form(""),
                          damages: str = Form(""), notes: str = Form(""), db: Session = Depends(get_db)):
    rental = current_rental(request, db)
    if not rental or kind not in ("checkin", "checkout"):
        return RedirectResponse("/r", status_code=303)
    back = RedirectResponse(f"/r/inspection/{kind}", status_code=303)
    if rental.inspection(kind):
        flash(request, "Cet état des lieux a déjà été enregistré.", "error")
        return RedirectResponse("/r/home", status_code=303)
    if kind == "checkin" and not rental.terms_accepted_at:
        flash(request, "Acceptez d'abord le contrat.", "error")
        return RedirectResponse("/r/home", status_code=303)
    checkin = rental.inspection("checkin")
    if kind == "checkout" and not checkin:
        flash(request, "L'état des lieux de départ n'a pas été fait.", "error")
        return RedirectResponse("/r/home", status_code=303)
    if kind == "checkout" and mileage < checkin.mileage:
        flash(request, f"Le kilométrage doit être supérieur ou égal à celui du départ ({checkin.mileage} km).", "error")
        return back
    if not 0 <= fuel_level <= 100 or not 1 <= cleanliness_score <= 5:
        flash(request, "Valeurs de carburant ou de propreté invalides.", "error")
        return back

    form = await request.form()
    files: list[tuple[str, UploadFile]] = []
    for i, angle in enumerate(REQUIRED_ANGLES):
        f = form.get(f"photo_{i}")
        if not getattr(f, "filename", ""):
            flash(request, f"Photo obligatoire manquante : {angle}.", "error")
            return back
        files.append((angle, f))
    for f in form.getlist("extra_photos"):
        if getattr(f, "filename", ""):
            files.append(("Détail", f))

    insp = Inspection(rental_id=rental.id, kind=kind, mileage=mileage, fuel_level=fuel_level,
                      cleanliness_score=cleanliness_score, cleanliness_issues=cleanliness_issues.strip(),
                      damages=damages.strip(), notes=notes.strip(), done_at=now_local(), done_by_ip=client_ip(request))
    try:
        for angle, f in files:
            rel = save_image(await f.read(), f"org{rental.organization_id}/rentals/{rental.id}/{kind}")
            insp.photos.append(Photo(angle=angle, path=rel))
    except UploadError as exc:
        flash(request, str(exc), "error")
        return back

    rental.inspections.append(insp)
    if kind == "checkin":
        rental.status = RentalStatus.IN_PROGRESS
    else:
        rental.status = RentalStatus.RETURNED
        rental.vehicle.current_mileage = max(rental.vehicle.current_mileage, mileage)
    db.flush()
    contracts.generate_and_store(db.get(Organization, rental.organization_id), rental)
    db.commit()
    flash(request, "État des lieux enregistré. Merci !", "success")
    return RedirectResponse("/r/home", status_code=303)


@router.get("/contract.pdf")
def contract(request: Request, db: Session = Depends(get_db)):
    rental = current_rental(request, db)
    if not rental:
        return RedirectResponse("/r", status_code=303)
    if not rental.contract_path:
        contracts.generate_and_store(db.get(Organization, rental.organization_id), rental)
        db.commit()
    return FileResponse(absolute(rental.contract_path), media_type="application/pdf",
                        filename=f"contrat-{rental.reference}.pdf")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/r", status_code=303)
