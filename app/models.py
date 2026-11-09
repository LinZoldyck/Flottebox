"""Modèle de données. Chaque objet métier est rattaché à une Organisation (multi-société / SaaS)."""
from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.utils import now_local


class Role(str, enum.Enum):
    OWNER = "owner"  # gérant : tout, y compris utilisateurs et intégrations
    MANAGER = "manager"  # opérationnel : véhicules, locations, PV, factures
    VIEWER = "viewer"  # lecture seule

    @property
    def label(self) -> str:
        return {"owner": "Gérant", "manager": "Gestionnaire", "viewer": "Lecture seule"}[self.value]


class RentalSource(str, enum.Enum):
    DIRECT = "direct"
    GETAROUND = "getaround"


class RentalStatus(str, enum.Enum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    RETURNED = "returned"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        return {
            "draft": "Brouillon",
            "confirmed": "Confirmée",
            "in_progress": "En cours",
            "returned": "Terminée",
            "cancelled": "Annulée",
        }[self.value]


class FineStatus(str, enum.Enum):
    NEW = "new"
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    DESIGNATED = "designated"
    REBILLED = "rebilled"
    IGNORED = "ignored"

    @property
    def label(self) -> str:
        return {
            "new": "À analyser",
            "matched": "Conducteur identifié",
            "unmatched": "Non rapprochée",
            "designated": "Désignation faite",
            "rebilled": "Refacturé au locataire",
            "ignored": "Ignorée",
        }[self.value]


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    legal_form: Mapped[str] = mapped_column(String(50), default="")
    siret: Mapped[str] = mapped_column(String(20), default="")
    vat_number: Mapped[str] = mapped_column(String(30), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    postal_code: Mapped[str] = mapped_column(String(10), default="")
    city: Mapped[str] = mapped_column(String(100), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    representative_name: Mapped[str] = mapped_column(String(200), default="")  # signataire / désignation ANTAI
    logo_path: Mapped[str] = mapped_column(String(300), default="")
    contract_terms: Mapped[str] = mapped_column(Text, default="")
    assistant_knowledge: Mapped[str] = mapped_column(Text, default="")  # FAQ pour l'assistant IA
    assistant_mode: Mapped[str] = mapped_column(String(10), default="draft")  # off | draft | auto
    getaround_token: Mapped[str] = mapped_column(String(500), default="")
    getaround_webhook_secret: Mapped[str] = mapped_column(String(200), default="")
    pennylane_token: Mapped[str] = mapped_column(String(500), default="")
    invoice_vat_rate: Mapped[str] = mapped_column(String(10), default="FR_200")
    api_token_hash: Mapped[str] = mapped_column(String(200), default="")  # robot de désignation
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)

    users: Mapped[list[User]] = relationship(back_populates="organization")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.MANAGER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    organization: Mapped[Organization] = relationship(back_populates="users")


class Vehicle(Base):
    __tablename__ = "vehicles"
    __table_args__ = (UniqueConstraint("organization_id", "plate"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    plate: Mapped[str] = mapped_column(String(20))  # normalisée : AB-123-CD
    brand: Mapped[str] = mapped_column(String(100), default="")
    model: Mapped[str] = mapped_column(String(100), default="")
    vin: Mapped[str] = mapped_column(String(30), default="")
    fuel_type: Mapped[str] = mapped_column(String(30), default="Diesel")
    parking_address: Mapped[str] = mapped_column(String(300), default="")
    masterlock_code: Mapped[str] = mapped_column(String(20), default="")
    getaround_car_id: Mapped[str] = mapped_column(String(50), default="")
    current_mileage: Mapped[int] = mapped_column(Integer, default=0)
    daily_price: Mapped[float] = mapped_column(Float, default=0)  # € TTC / jour
    included_km_per_day: Mapped[int] = mapped_column(Integer, default=100)
    extra_km_price: Mapped[float] = mapped_column(Float, default=0.30)  # € TTC / km
    deposit_amount: Mapped[float] = mapped_column(Float, default=1000)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    @property
    def label(self) -> str:
        return f"{self.brand} {self.model} ({self.plate})".strip()


class Customer(Base):
    """Locataire / conducteur. Contient les données nécessaires au contrat et à la désignation ANTAI."""

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    is_company: Mapped[bool] = mapped_column(Boolean, default=True)
    company_name: Mapped[str] = mapped_column(String(200), default="")
    siret: Mapped[str] = mapped_column(String(20), default="")
    civility: Mapped[str] = mapped_column(String(5), default="M")  # M | Mme
    first_name: Mapped[str] = mapped_column(String(100), default="")
    last_name: Mapped[str] = mapped_column(String(100), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    postal_code: Mapped[str] = mapped_column(String(10), default="")
    city: Mapped[str] = mapped_column(String(100), default="")
    country: Mapped[str] = mapped_column(String(2), default="FR")
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    birth_place: Mapped[str] = mapped_column(String(100), default="")
    license_number: Mapped[str] = mapped_column(String(30), default="")
    license_issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    license_issue_place: Mapped[str] = mapped_column(String(100), default="")
    license_category: Mapped[str] = mapped_column(String(10), default="B")
    pennylane_customer_id: Mapped[str] = mapped_column(String(50), default="")
    getaround_user_id: Mapped[str] = mapped_column(String(50), default="")

    @property
    def driver_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def display_name(self) -> str:
        if self.is_company and self.company_name:
            return f"{self.company_name} ({self.driver_name})" if self.driver_name else self.company_name
        return self.driver_name or self.email or f"Client #{self.id}"

    @property
    def missing_designation_fields(self) -> list[str]:
        required = {
            "Nom": self.last_name,
            "Prénom": self.first_name,
            "Adresse": self.address,
            "Code postal": self.postal_code,
            "Ville": self.city,
            "Date de naissance": self.birth_date,
            "Lieu de naissance": self.birth_place,
            "N° de permis": self.license_number,
            "Date de délivrance du permis": self.license_issue_date,
        }
        return [k for k, v in required.items() if not v]


class Rental(Base):
    __tablename__ = "rentals"
    __table_args__ = (UniqueConstraint("organization_id", "source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    source: Mapped[RentalSource] = mapped_column(Enum(RentalSource), default=RentalSource.DIRECT)
    external_id: Mapped[str | None] = mapped_column(String(50), nullable=True)  # id Getaround
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[RentalStatus] = mapped_column(Enum(RentalStatus), default=RentalStatus.CONFIRMED)
    price_total: Mapped[float] = mapped_column(Float, default=0)  # € TTC hors km sup.
    included_km: Mapped[int] = mapped_column(Integer, default=0)
    extra_km_price: Mapped[float] = mapped_column(Float, default=0)
    deposit_amount: Mapped[float] = mapped_column(Float, default=0)
    protection: Mapped[str] = mapped_column(String(100), default="")  # assurance / protection incluse
    notes: Mapped[str] = mapped_column(Text, default="")
    access_code: Mapped[str] = mapped_column(String(12), default="", index=True)
    access_code_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    terms_accepted_ip: Mapped[str] = mapped_column(String(64), default="")
    terms_accepted_name: Mapped[str] = mapped_column(String(200), default="")
    contract_path: Mapped[str] = mapped_column(String(300), default="")
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # données brutes Getaround
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)

    vehicle: Mapped[Vehicle] = relationship()
    customer: Mapped[Customer | None] = relationship()
    inspections: Mapped[list[Inspection]] = relationship(back_populates="rental", cascade="all, delete-orphan")
    invoices: Mapped[list[Invoice]] = relationship(back_populates="rental")

    @property
    def reference(self) -> str:
        prefix = "GA" if self.source == RentalSource.GETAROUND else "LD"
        return f"{prefix}-{self.starts_at:%Y%m}-{self.id:04d}"

    def inspection(self, kind: str) -> Inspection | None:
        return next((i for i in self.inspections if i.kind == kind), None)

    @property
    def days(self) -> int:
        seconds = (self.ends_at - self.starts_at).total_seconds()
        return max(1, int(-(-seconds // 86400)))

    @property
    def distance_km(self) -> int | None:
        start, end = self.inspection("checkin"), self.inspection("checkout")
        if start and end and end.mileage >= start.mileage:
            return end.mileage - start.mileage
        return None

    @property
    def extra_km(self) -> int:
        d = self.distance_km
        return max(0, d - self.included_km) if d is not None else 0


class Inspection(Base):
    """État des lieux de départ (checkin) ou de retour (checkout)."""

    __tablename__ = "inspections"
    __table_args__ = (UniqueConstraint("rental_id", "kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    rental_id: Mapped[int] = mapped_column(ForeignKey("rentals.id"))
    kind: Mapped[str] = mapped_column(String(10))  # checkin | checkout
    mileage: Mapped[int] = mapped_column(Integer)
    fuel_level: Mapped[int] = mapped_column(Integer)  # en % (0..100)
    cleanliness_score: Mapped[int] = mapped_column(Integer, default=5)  # propreté intérieure 1..5
    cleanliness_issues: Mapped[str] = mapped_column(Text, default="")
    damages: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    done_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)
    done_by_ip: Mapped[str] = mapped_column(String(64), default="")

    rental: Mapped[Rental] = relationship(back_populates="inspections")
    photos: Mapped[list[Photo]] = relationship(back_populates="inspection", cascade="all, delete-orphan")


class Photo(Base):
    __tablename__ = "photos"

    id: Mapped[int] = mapped_column(primary_key=True)
    inspection_id: Mapped[int] = mapped_column(ForeignKey("inspections.id"))
    angle: Mapped[str] = mapped_column(String(30), default="")
    path: Mapped[str] = mapped_column(String(300))
    taken_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)

    inspection: Mapped[Inspection] = relationship(back_populates="photos")


class Fine(Base):
    """Avis de contravention déposé dans l'application."""

    __tablename__ = "fines"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    file_path: Mapped[str] = mapped_column(String(300))
    original_name: Mapped[str] = mapped_column(String(300), default="")
    # contravention : amende, on désigne le conducteur (ANTAI)
    # fps : forfait post-stationnement, non désignable -> le titulaire paie puis refacture le locataire
    kind: Mapped[str] = mapped_column(String(20), default="contravention")
    notice_number: Mapped[str] = mapped_column(String(40), default="")
    plate: Mapped[str] = mapped_column(String(20), default="")
    offense_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    location: Mapped[str] = mapped_column(String(300), default="")
    offense_type: Mapped[str] = mapped_column(String(300), default="")
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    designation_deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    extraction_method: Mapped[str] = mapped_column(String(20), default="")
    raw_text: Mapped[str] = mapped_column(Text, default="")
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("vehicles.id"), nullable=True)
    rental_id: Mapped[int | None] = mapped_column(ForeignKey("rentals.id"), nullable=True)
    status: Mapped[FineStatus] = mapped_column(Enum(FineStatus), default=FineStatus.NEW)
    match_note: Mapped[str] = mapped_column(Text, default="")
    designated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)

    vehicle: Mapped[Vehicle | None] = relationship()
    rental: Mapped[Rental | None] = relationship()

    @property
    def is_fps(self) -> bool:
        return self.kind == "fps"


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    rental_id: Mapped[int] = mapped_column(ForeignKey("rentals.id"))
    kind: Mapped[str] = mapped_column(String(20), default="rental")  # rental | fps (refacturation)
    lines: Mapped[list] = mapped_column(JSON, default=list)
    total_ttc: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | sent | error
    pennylane_id: Mapped[str] = mapped_column(String(50), default="")
    pennylane_url: Mapped[str] = mapped_column(String(300), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)

    rental: Mapped[Rental] = relationship(back_populates="invoices")


class Message(Base):
    """Message d'une conversation Getaround et suggestion de l'assistant."""

    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("organization_id", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    rental_id: Mapped[int | None] = mapped_column(ForeignKey("rentals.id"), nullable=True)
    external_id: Mapped[str] = mapped_column(String(50))
    direction: Mapped[str] = mapped_column(String(10))  # in | out
    body: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)
    # Traitement IA (messages entrants)
    ai_status: Mapped[str] = mapped_column(String(20), default="")  # "" | draft | sent | escalated | ignored | error
    ai_category: Mapped[str] = mapped_column(String(50), default="")
    ai_reply: Mapped[str] = mapped_column(Text, default="")
    ai_reason: Mapped[str] = mapped_column(Text, default="")
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    rental: Mapped[Rental | None] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)
