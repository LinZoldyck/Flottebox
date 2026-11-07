"""Facturation via l'API Pennylane v2 (https://pennylane.readme.io).

- Jeton : Pennylane > Paramètres > Connectivité > Développeurs (scopes customers:all + customer_invoices:all)
- Clients : POST /company_customers ou /individual_customers (recherche par external_reference)
- Factures : POST /customer_invoices (créées en brouillon pour validation dans Pennylane)
L'API ne déduplique pas : on n'envoie pas deux fois une facture déjà transmise (Invoice.pennylane_id).
"""
from __future__ import annotations

import json
from datetime import timedelta

import httpx

from app.config import get_settings
from app.models import Customer, Invoice, Organization, Rental
from app.utils import now_local


class PennylaneError(Exception):
    pass


def build_lines(rental: Rental) -> list[dict]:
    """Lignes de facture (montants TTC) : location + kilomètres supplémentaires."""
    v = rental.vehicle
    lines = [{
        "label": f"Location {v.brand} {v.model} ({v.plate}) du {rental.starts_at:%d/%m/%Y %H:%M} "
                 f"au {rental.ends_at:%d/%m/%Y %H:%M} — contrat {rental.reference}",
        "quantity": 1,
        "unit_price_ttc": round(rental.price_total, 2),
    }]
    if rental.extra_km:
        lines.append({
            "label": f"Kilomètres supplémentaires ({rental.extra_km} km au-delà des {rental.included_km} km inclus)",
            "quantity": rental.extra_km,
            "unit_price_ttc": round(rental.extra_km_price, 2),
        })
    return lines


class PennylaneClient:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        if not token:
            raise PennylaneError("Jeton API Pennylane non configuré (Paramètres > Intégrations).")
        self.http = httpx.Client(base_url=get_settings().pennylane_base_url,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                                 timeout=30, transport=transport)

    def _req(self, method: str, path: str, **kw) -> dict:
        r = self.http.request(method, path, **kw)
        if r.status_code >= 300:
            raise PennylaneError(f"{method} {path} : {r.status_code} {r.text[:400]}")
        return r.json() if r.content else {}

    def ensure_customer(self, customer: Customer) -> str:
        if customer.pennylane_customer_id:
            return customer.pennylane_customer_id
        ref = f"flottebox-{customer.organization_id}-{customer.id}"
        found = self._req("GET", "/customers", params={"filter": json.dumps(
            [{"field": "external_reference", "operator": "eq", "value": ref}])})
        items = found.get("items") or []
        if items:
            customer.pennylane_customer_id = str(items[0]["id"])
            return customer.pennylane_customer_id
        address = {"address": customer.address or "-", "postal_code": customer.postal_code or "-",
                   "city": customer.city or "-", "country_alpha2": customer.country or "FR"}
        common = {"external_reference": ref, "billing_address": address,
                  "emails": [customer.email] if customer.email else [], "phone": customer.phone or None}
        if customer.is_company and customer.company_name:
            created = self._req("POST", "/company_customers",
                                json={**common, "name": customer.company_name, "reg_no": customer.siret or None})
        else:
            created = self._req("POST", "/individual_customers",
                                json={**common, "first_name": customer.first_name or "-",
                                      "last_name": customer.last_name or "-"})
        customer.pennylane_customer_id = str(created["id"])
        return customer.pennylane_customer_id

    def create_invoice(self, org: Organization, invoice: Invoice) -> dict:
        rental = invoice.rental
        if not rental.customer:
            raise PennylaneError("La location n'a pas de client.")
        customer_id = self.ensure_customer(rental.customer)
        today = now_local().date()
        payload = {
            "customer_id": int(customer_id) if customer_id.isdigit() else customer_id,
            "date": today.isoformat(),
            "deadline": (today + timedelta(days=30)).isoformat(),
            "draft": True,
            "currency": "EUR",
            "label": f"Location {rental.reference}",
            "external_reference": f"flottebox-invoice-{invoice.id}",
            "invoice_lines": [{
                "label": line["label"],
                "quantity": line["quantity"],
                "unit": "piece",
                "raw_currency_unit_price": f"{line['unit_price_ttc']:.2f}",
                "vat_rate": org.invoice_vat_rate or "FR_200",
            } for line in invoice.lines],
        }
        return self._req("POST", "/customer_invoices", json=payload)


def push_invoice(org: Organization, invoice: Invoice, client: PennylaneClient | None = None) -> Invoice:
    if invoice.pennylane_id:
        return invoice  # déjà envoyée : pas de doublon
    try:
        client = client or PennylaneClient(org.pennylane_token)
        result = client.create_invoice(org, invoice)
        invoice.pennylane_id = str(result.get("id", ""))
        invoice.pennylane_url = str(result.get("public_file_url") or result.get("file_url") or "")
        invoice.status = "sent"
        invoice.error = ""
    except (PennylaneError, httpx.HTTPError) as exc:
        invoice.status = "error"
        invoice.error = str(exc)
    return invoice


def create_invoice_for_rental(org: Organization, rental: Rental) -> Invoice:
    lines = build_lines(rental)
    total = round(sum(line["quantity"] * line["unit_price_ttc"] for line in lines), 2)
    return Invoice(organization_id=org.id, rental_id=rental.id, rental=rental, lines=lines, total_ttc=total)
