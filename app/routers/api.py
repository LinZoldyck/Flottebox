"""API JSON (jeton Bearer) pour le robot de désignation, et réception des webhooks Getaround."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Fine, FineStatus, Organization
from app.routers.ops import designation_payload, payment_payload
from app.security import get_api_org
from app.services import getaround, jobs
from app.utils import now_local

router = APIRouter()


@router.get("/api/fines")
def api_fines(type: str = "contravention", org: Organization = Depends(get_api_org), db: Session = Depends(get_db)):
    """type=contravention : à désigner ; type=fps : FPS restant à payer."""
    q = select(Fine).where(Fine.organization_id == org.id)
    if type == "fps":
        q = q.where(Fine.kind == "fps", Fine.paid_at.is_(None), Fine.status != FineStatus.IGNORED)
    else:
        q = q.where(Fine.status == FineStatus.MATCHED, Fine.kind != "fps")
    items = db.scalars(q).all()
    return [{"id": f.id, "numero_avis": f.notice_number, "immatriculation": f.plate,
             "date": f.offense_at.isoformat() if f.offense_at else None, "note": f.match_note} for f in items]


@router.get("/api/fines/{fine_id}/designation")
def api_designation(fine_id: int, org: Organization = Depends(get_api_org), db: Session = Depends(get_db)):
    fine = db.get(Fine, fine_id)
    if not fine or fine.organization_id != org.id:
        raise HTTPException(status_code=404)
    if fine.is_fps:
        raise HTTPException(status_code=409, detail="Forfait post-stationnement : pas de désignation possible. "
                                                    "Payez-le puis refacturez le locataire depuis l'application.")
    return designation_payload(org, fine)


@router.get("/api/fines/{fine_id}/payment")
def api_payment(fine_id: int, org: Organization = Depends(get_api_org), db: Session = Depends(get_db)):
    fine = db.get(Fine, fine_id)
    if not fine or fine.organization_id != org.id:
        raise HTTPException(status_code=404)
    if not fine.is_fps:
        raise HTTPException(status_code=409, detail="Ce n'est pas un FPS : une contravention se désigne "
                                                    "(robot antai_designation.py).")
    return payment_payload(fine)


@router.post("/api/fines/{fine_id}/paid")
def api_paid(fine_id: int, org: Organization = Depends(get_api_org), db: Session = Depends(get_db)):
    fine = db.get(Fine, fine_id)
    if not fine or fine.organization_id != org.id or not fine.is_fps:
        raise HTTPException(status_code=404)
    fine.paid_at, fine.paid_amount = now_local(), fine.amount
    db.commit()
    return {"ok": True}


@router.post("/api/fines/{fine_id}/designated")
def api_designated(fine_id: int, org: Organization = Depends(get_api_org), db: Session = Depends(get_db)):
    fine = db.get(Fine, fine_id)
    if not fine or fine.organization_id != org.id:
        raise HTTPException(status_code=404)
    fine.status, fine.designated_at = FineStatus.DESIGNATED, now_local()
    db.commit()
    return {"ok": True}


@router.post("/webhooks/getaround/{org_id}")
async def getaround_webhook(org_id: int, request: Request, db: Session = Depends(get_db)):
    org = db.get(Organization, org_id)
    body = await request.body()
    signature = request.headers.get(get_settings().getaround_signature_header, "")
    if not org or not getaround.verify_webhook(org.getaround_webhook_secret, body, signature):
        raise HTTPException(status_code=401, detail="Signature invalide")
    # Les événements peuvent arriver en double ou dans le désordre : on resynchronise simplement
    # (opérations idempotentes) au lieu de traiter chaque événement individuellement.
    try:
        client = getaround.GetaroundClient(org.getaround_token)
        getaround.sync_rentals(db, org, days_back=7, days_ahead=60, client=client)
        for msg in getaround.poll_messages(db, org, client=client):
            jobs.handle_incoming(db, org, msg, client)
    except getaround.GetaroundError:
        pass  # on répond 2xx malgré tout ; le planificateur réessaiera
    return {"ok": True}


@router.get("/healthz")
def health():
    return {"status": "ok"}
