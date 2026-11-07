"""Génération du contrat de location PDF (logo, parties, véhicule, états des lieux, photos avant/après)."""
from __future__ import annotations

from pathlib import Path

from fpdf import FPDF

from app.models import Organization, Rental
from app.services.storage import absolute, save_file
from app.utils import euros, fuel_label, now_local

FONT_CANDIDATES = [
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
]

DEFAULT_TERMS = """1. Objet. Le loueur met à disposition du locataire le véhicule utilitaire désigné ci-dessus pour la période indiquée. Le locataire reconnaît l'avoir reçu en bon état, conforme à l'état des lieux de départ et aux photos jointes.
2. Conducteurs. Seul le conducteur désigné au contrat, titulaire d'un permis B valide depuis au moins 2 ans, est autorisé à conduire le véhicule.
3. Utilisation. Le véhicule doit être utilisé en bon père de famille, dans le respect du Code de la route et de la charge utile maximale. Sous-location, transport rémunéré de personnes, compétitions et déplacements hors France métropolitaine sont interdits sauf accord écrit.
4. Clés et boîte à clés. Les clés sont remises et restituées via la boîte à code (cadenas) fixée au véhicule. Le code est personnel et ne doit pas être communiqué. Le locataire s'engage à refermer la boîte et à verrouiller le véhicule à la restitution.
5. Kilométrage et carburant. Le forfait kilométrique est indiqué ci-dessus ; chaque kilomètre supplémentaire est facturé au tarif prévu. Le véhicule doit être restitué avec le même niveau de carburant qu'au départ, à défaut le complément est facturé.
6. Dépôt de garantie et dommages. Le dépôt de garantie couvre les dommages, frais de remise en état, franchise d'assurance et frais impayés. Tout dommage non signalé à l'état des lieux de départ est réputé survenu pendant la location.
7. Restitution. Le véhicule doit être restitué à l'adresse et à l'heure prévues, propre, avec l'état des lieux de retour (photos, kilométrage, carburant) réalisé dans l'application. Tout retard non autorisé peut être facturé.
8. Contraventions. Le locataire est responsable des infractions commises pendant la location. Conformément à l'article L.121-2 du Code de la route, le loueur désignera le conducteur auprès de l'ANTAI ; des frais de gestion peuvent être facturés.
9. Sinistre. En cas d'accident, de vol ou de panne, le locataire prévient immédiatement le loueur et remplit un constat amiable. Aucun remorquage ou réparation ne peut être engagé sans accord du loueur.
10. Données personnelles. Les données collectées sont utilisées pour l'exécution du contrat, la facturation et les obligations légales (dont la désignation du conducteur). Le locataire dispose d'un droit d'accès et de rectification auprès du loueur."""


class ContractPDF(FPDF):
    def __init__(self, org: Organization, reference: str):
        super().__init__(format="A4")
        self.org = org
        self.reference = reference
        self.font_name = "Helvetica"
        for candidate in FONT_CANDIDATES:
            if candidate.exists():
                self.add_font("DejaVu", "", str(candidate))
                bold = candidate.with_name("DejaVuSans-Bold.ttf")
                self.add_font("DejaVu", "B", str(bold if bold.exists() else candidate))
                self.font_name = "DejaVu"
                break
        self.set_auto_page_break(auto=True, margin=18)
        self.set_margins(15, 15, 15)

    def t(self, text: str) -> str:
        """Les polices de base ne gèrent que le latin-1 : on remplace les caractères non supportés."""
        if self.font_name == "DejaVu":
            return text
        return text.replace("€", "EUR").replace("’", "'").encode("latin-1", "replace").decode("latin-1")

    def header(self):
        logo = self.org.logo_path
        if logo:
            try:
                self.image(str(absolute(logo)), x=15, y=10, h=18)
            except Exception:  # noqa: BLE001 - logo invalide : on continue sans
                pass
        self.set_font(self.font_name, "B", 13)
        self.set_xy(60, 10)
        self.cell(0, 6, self.t(self.org.name), align="R", new_x="LMARGIN", new_y="NEXT")
        self.set_font(self.font_name, "", 8)
        lines = [
            f"{self.org.address} {self.org.postal_code} {self.org.city}".strip(),
            " · ".join(x for x in [f"SIRET {self.org.siret}" if self.org.siret else "", self.org.phone, self.org.email] if x),
        ]
        for line in lines:
            if line:
                self.set_x(60)
                self.cell(0, 4, self.t(line), align="R", new_x="LMARGIN", new_y="NEXT")
        self.set_y(32)
        self.set_draw_color(30, 64, 175)
        self.set_line_width(0.6)
        self.line(15, 31, 195, 31)

    def footer(self):
        self.set_y(-12)
        self.set_font(self.font_name, "", 7)
        self.set_text_color(120)
        self.cell(0, 5, self.t(f"Contrat {self.reference} — page {self.page_no()}/{{nb}}"), align="C")
        self.set_text_color(0)

    # --- helpers de mise en page ---
    def section(self, title: str):
        self.ln(3)
        self.set_font(self.font_name, "B", 10)
        self.set_fill_color(232, 238, 252)
        self.cell(0, 7, self.t(title), fill=True, new_x="LMARGIN", new_y="NEXT")
        self.ln(1)
        self.set_font(self.font_name, "", 9)

    def kv(self, rows: list[tuple[str, str]], col=55):
        for k, v in rows:
            size = 9.0
            self.set_font(self.font_name, "B", size)
            while self.get_string_width(self.t(k)) > col - 2 and size > 6.5:
                size -= 0.5
                self.set_font(self.font_name, "B", size)
            self.cell(col, 5, self.t(k))
            self.set_font(self.font_name, "", 9)
            self.multi_cell(0, 5, self.t(v or "-"), new_x="LMARGIN", new_y="NEXT")

    def photo_grid(self, photos, title: str):
        self.section(title)
        if not photos:
            self.cell(0, 6, self.t("Aucune photo."), new_x="LMARGIN", new_y="NEXT")
            return
        w, h, gap = 58, 43, 3
        x0 = self.l_margin
        col = 0
        for photo in photos:
            if col == 0 and self.get_y() + h + 8 > self.h - self.b_margin:
                self.add_page()
            x = x0 + col * (w + gap)
            y = self.get_y()
            try:
                self.image(str(absolute(photo.path)), x=x, y=y, w=w, h=h, keep_aspect_ratio=True)
            except Exception:  # noqa: BLE001
                self.rect(x, y, w, h)
            self.set_xy(x, y + h)
            self.set_font(self.font_name, "", 7)
            self.cell(w, 4, self.t(f"{photo.angle} — {photo.taken_at:%d/%m/%Y %H:%M}"), align="C")
            col += 1
            if col == 3:
                col = 0
                self.set_xy(x0, y + h + 6)
            else:
                self.set_xy(x + w + gap, y)
        if col:
            self.set_xy(x0, self.get_y() + h + 6)


FR_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
FR_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
             "septembre", "octobre", "novembre", "décembre"]


def fr_datetime(d) -> str:
    """dimanche 04 octobre 2026 à 10:30 (format du contrat Getaround)."""
    if not d:
        return "-"
    return f"{FR_DAYS[d.weekday()]} {d:%d} {FR_MONTHS[d.month - 1]} {d:%Y} à {d:%H:%M}"


def fr_amount(value: float | None) -> str:
    if value is None:
        return "-"
    return (f"{value:,.0f} €" if float(value).is_integer() else euros(value)).replace(",", " ")


def build_contract(org: Organization, rental: Rental) -> bytes:
    """Mise en page alignée sur le contrat Getaround (Réservation / début / fin) + logo, photos et CGL."""
    c = rental.customer
    v = rental.vehicle
    start, end = rental.inspection("checkin"), rental.inspection("checkout")
    pdf = ContractPDF(org, rental.reference)
    pdf.alias_nb_pages()
    pdf.add_page()

    pdf.set_font(pdf.font_name, "B", 15)
    pdf.cell(0, 9, pdf.t(f"Contrat de location n°{rental.reference}"), new_x="LMARGIN", new_y="NEXT")

    pdf.section("Réservation")
    pdf.kv([
        ("Date de réservation", fr_datetime(rental.created_at)),
        ("Horaire de début", fr_datetime(rental.starts_at)),
        ("Horaire de fin", fr_datetime(rental.ends_at)),
        ("Prix de la réservation", fr_amount(rental.price_total)),
        ("Distance incluse", f"{rental.included_km} km"),
        ("Prix additionnel par km", euros(rental.extra_km_price)),
        ("Dépôt de garantie", fr_amount(rental.deposit_amount)),
        ("Nom du propriétaire", f"{org.name} {org.legal_form}".strip()),
        ("Protection", rental.protection or "-"),
    ], col=82)

    pdf.section("Informations de début de location")
    rows = [
        ("Plaque d'immatriculation", v.plate.replace("-", "")),
        ("Marque et modèle", f"{v.brand} {v.model}".strip()),
        ("Type de carburant", v.fuel_type),
    ]
    if c:
        if c.is_company and c.company_name:
            rows.append(("Société locataire", f"{c.company_name}" + (f" (SIRET {c.siret})" if c.siret else "")))
        rows += [
            ("Nom du locataire", c.driver_name),
            ("Date de naissance du locataire", f"{c.birth_date:%d/%m/%Y}" if c.birth_date else "-"),
            ("Numéro du permis de conduire du locataire", c.license_number),
            ("Date d'obtention du permis de conduire du locataire",
             f"{c.license_issue_date:%d/%m/%Y}" if c.license_issue_date else "-"),
            ("Adresse du locataire", f"{c.address} , {c.postal_code}, {c.city}, {c.country}"),
        ]
    else:
        rows.append(("Nom du locataire", "Non renseigné"))
    if start:
        rows += [
            ("Kilométrage", f"{start.mileage:,} km".replace(",", " ")),
            ("Niveau de carburant (relevé du locataire)", fuel_label(start.fuel_level)),
            ("Heure de début", fr_datetime(start.done_at)),
            ("Dommages déclarés", start.damages or "Aucun"),
            ("Évaluation de la propreté intérieure", f"{start.cleanliness_score}/5"),
            ("Problèmes de propreté intérieure", start.cleanliness_issues or "Aucun"),
        ]
    else:
        rows.append(("État des lieux de départ", "Non réalisé"))
    pdf.kv(rows, col=82)

    pdf.section("Informations de fin de location")
    if end:
        dist = rental.distance_km
        pdf.kv([
            ("Distance parcourue", f"{dist} km" if dist is not None else "-"),
            ("Kilomètres au-delà du forfait", f"{rental.extra_km} km"),
            ("Kilométrage", f"{end.mileage:,} km".replace(",", " ")),
            ("Niveau de carburant (relevé du locataire)", fuel_label(end.fuel_level)),
            ("Heure de fin", fr_datetime(end.done_at)),
            ("Dommages déclarés", end.damages or "Aucun"),
            ("Évaluation de la propreté intérieure", f"{end.cleanliness_score}/5"),
            ("Problèmes de propreté intérieure", end.cleanliness_issues or "Aucun"),
        ], col=82)
    else:
        pdf.kv([("État des lieux de retour", "Non réalisé")], col=82)

    pdf.ln(2)
    pdf.set_font(pdf.font_name, "", 7.5)
    pdf.multi_cell(0, 4, pdf.t("Les informations fournies par le locataire peuvent, le cas échéant, être corrigées "
                               "en fonction d'éléments découverts ultérieurement."), new_x="LMARGIN", new_y="NEXT")

    pdf.section("Acceptation")
    if rental.terms_accepted_at:
        pdf.multi_cell(0, 5, pdf.t(
            f"Contrat et conditions générales acceptés électroniquement par {rental.terms_accepted_name} le "
            f"{fr_datetime(rental.terms_accepted_at)} (adresse IP {rental.terms_accepted_ip}), "
            "après authentification par code personnel envoyé par SMS."), new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.cell(90, 22, pdf.t("Le loueur"), border=1)
        pdf.cell(90, 22, pdf.t("Le locataire (lu et approuvé)"), border=1, new_x="LMARGIN", new_y="NEXT")

    pdf.add_page()
    pdf.photo_grid(start.photos if start else [], "Photos — état des lieux de départ (avant)")
    pdf.photo_grid(end.photos if end else [], "Photos — état des lieux de retour (après)")

    pdf.add_page()
    pdf.section("Conditions générales de location")
    pdf.set_font(pdf.font_name, "", 7.5)
    pdf.multi_cell(0, 3.8, pdf.t(org.contract_terms or DEFAULT_TERMS), new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


def generate_and_store(org: Organization, rental: Rental) -> str:
    data = build_contract(org, rental)
    rel = save_file(data, f"org{org.id}/contracts", ".pdf")
    rental.contract_path = rel
    return rel
