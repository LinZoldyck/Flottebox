"""API JSON (jeton Bearer) pour le robot de désignation, et réception des webhooks Getaround."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Fine, FineStatus, Organization
from app.routers.ops import designation_payload
from app.security import get_api_org
from app.services import getaround, jobs
from app.utils import now_local

router = APIRouter()


@router.get("/api/fines")
def api_fines(org: Organization = Depends(get_api_org), db: Session = Depends(get_db)):
    items = db.scalars(select(Fine).where(Fine.organization_id == org.id, Fine.status == FineStatus.MATCHED,
                                       Fine.kind != "fps")).all()
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
