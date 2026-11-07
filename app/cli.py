"""Commandes d'administration.

    python -m app.cli create-owner --company "Ma Société" --email moi@exemple.fr --name "Prénom Nom"
    python -m app.cli scan-fines
    python -m app.cli send-codes
"""
import argparse
import getpass

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Organization, Role, User
from app.security import hash_password


def seed_demo(db) -> None:
    """Données fictives pour découvrir l'application."""
    import io
    from datetime import date, datetime, timedelta

    from PIL import Image, ImageDraw

    from app.models import Customer, Inspection, Photo, Rental, RentalStatus, Vehicle
    from app.services.contracts import generate_and_store
    from app.services.storage import save_image
    from app.utils import generate_access_code, now_local

    if db.scalar(select(User).where(User.email == "demo@flottebox.local")):
        raise SystemExit("La démo existe déjà.")
    org = Organization(name="Utilitaires Démo", legal_form="SAS", siret="912 345 678 00011",
                       address="5 avenue des Lilas", postal_code="93100", city="Montreuil", phone="01 00 00 00 00",
                       email="contact@demo.fr", representative_name="Jean Démo",
                       assistant_knowledge="Les véhicules sont garés au 5 avenue des Lilas, Montreuil (parking extérieur).\n"
                                           "Ouverture via l'application Getaround (Getaround Connect).\n"
                                           "Restituer avec le même niveau de carburant. Diesel uniquement.")
    db.add(org)
    db.flush()

    def picture(text, color, size=(800, 600)):
        im = Image.new("RGB", size, color)
        ImageDraw.Draw(im).text((20, 20), text, fill="white")
        buf = io.BytesIO()
        im.save(buf, "JPEG")
        return buf.getvalue()

    org.logo_path = save_image(picture("LOGO", (30, 64, 175), (400, 160)), f"org{org.id}/logo", 600)
    db.add(User(organization_id=org.id, email="demo@flottebox.local", full_name="Jean Démo",
                password_hash=hash_password("demo-flottebox"), role=Role.OWNER))
    vans = [Vehicle(organization_id=org.id, plate=p, brand="Opel", model="Movano", fuel_type="Diesel",
                    parking_address="5 avenue des Lilas, 93100 Montreuil", masterlock_code=code, current_mileage=km,
                    daily_price=77, included_km_per_day=200, extra_km_price=0.48, deposit_amount=1000)
            for p, code, km in [("AB-123-CD", "4821", 101897), ("EF-456-GH", "1937", 54210), ("IJ-789-KL", "7754", 23010)]]
    cust = Customer(organization_id=org.id, is_company=True, company_name="Event Pro SARL", first_name="Camille",
                    last_name="Exemple", phone="0600000000", email="camille@example.com", address="1 rue Fictive",
                    postal_code="75010", city="Paris", birth_date=date(1990, 5, 1), birth_place="Lyon",
                    license_number="12AB34567", license_issue_date=date(2010, 6, 1), license_issue_place="Préfecture du Rhône")
    db.add_all(vans + [cust])
    db.flush()
    start = datetime(2026, 10, 4, 10, 30)
    done = Rental(organization_id=org.id, vehicle_id=vans[0].id, customer_id=cust.id, starts_at=start,
                  ends_at=start.replace(hour=22), price_total=77, included_km=200, extra_km_price=0.48,
                  deposit_amount=1000, protection="Essentielle", status=RentalStatus.RETURNED,
                  created_at=start - timedelta(hours=16), access_code=generate_access_code(),
                  terms_accepted_at=start, terms_accepted_name="Camille Exemple", terms_accepted_ip="192.0.2.10")
    db.add(done)
    db.flush()
    for kind, km, t, damages in [("checkin", 101897, start.replace(minute=40), ""),
                                 ("checkout", 102111, start.replace(hour=19, minute=44), "Rayure légère porte arrière")]:
        insp = Inspection(rental_id=done.id, kind=kind, mileage=km, fuel_level=55, done_at=t, damages=damages)
        for n, angle in enumerate(["Avant", "Arrière", "Côté gauche", "Côté droit", "Intérieur / cabine",
                                   "Compteur & carburant"]):
            rel = save_image(picture(f"{kind} - {angle}", (60 + n * 30, 120, 180 if kind == "checkin" else 90)),
                             f"org{org.id}/rentals/{done.id}/{kind}")
            insp.photos.append(Photo(angle=angle, path=rel, taken_at=t))
        done.inspections.append(insp)
    db.flush()
    generate_and_store(org, done)
    soon = now_local() + timedelta(days=1)
    db.add(Rental(organization_id=org.id, vehicle_id=vans[1].id, customer_id=cust.id, starts_at=soon,
                  ends_at=soon + timedelta(hours=10), price_total=77, included_km=200, extra_km_price=0.48,
                  deposit_amount=1000, access_code=generate_access_code()))
    db.commit()
    print("Démo créée : demo@flottebox.local / demo-flottebox")


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    owner = sub.add_parser("create-owner", help="Crée une société et son compte gérant")
    owner.add_argument("--company", required=True)
    owner.add_argument("--email", required=True)
    owner.add_argument("--name", default="")
    owner.add_argument("--password", help="(sinon demandé de façon interactive)")
    sub.add_parser("scan-fines", help="Traite le dossier de dépôt des contraventions")
    sub.add_parser("send-codes", help="Envoie les codes d'accès SMS arrivés à échéance")
    sub.add_parser("seed-demo", help="Crée une société de démonstration (demo@flottebox.local / demo-flottebox)")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        if args.cmd == "create-owner":
            if db.scalar(select(User).where(User.email == args.email.lower())):
                raise SystemExit("Cet e-mail existe déjà.")
            password = args.password or getpass.getpass("Mot de passe (10 caractères min.) : ")
            if len(password) < 10:
                raise SystemExit("Mot de passe trop court.")
            org = Organization(name=args.company, email=args.email.lower(), representative_name=args.name)
            db.add(org)
            db.flush()
            db.add(User(organization_id=org.id, email=args.email.lower(), full_name=args.name,
                        password_hash=hash_password(password), role=Role.OWNER))
            db.commit()
            print(f"Société #{org.id} et compte gérant créés.")
        elif args.cmd == "scan-fines":
            from app.services.fines import scan_inbox
            print(f"{scan_inbox(db)} fichier(s) traité(s).")
        elif args.cmd == "seed-demo":
            seed_demo(db)
        elif args.cmd == "send-codes":
            from app.services.jobs import send_due_access_codes
            print(f"{send_due_access_codes(db)} SMS envoyé(s).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
