#!/usr/bin/env python3
"""Robot de pré-remplissage du paiement d'un forfait post-stationnement (FPS).

À lancer sur VOTRE ordinateur (navigateur visible) :

    export FLOTTEBOX_URL=https://flotte.mondomaine.fr FLOTTEBOX_TOKEN=fb_xxx
    python robot/fps_payment.py --list          # FPS restant à payer
    python robot/fps_payment.py --fine 12

Le robot récupère dans l'application le numéro de l'avis de paiement, la clé et le montant,
ouvre le site de paiement et remplit ces champs (d'un seul bloc ou case par case). Il vérifie
que le montant affiché correspond à l'avis. Il NE saisit JAMAIS la carte bancaire et ne valide
pas le paiement : vous le faites vous-même, puis vous confirmez dans le terminal pour marquer
le FPS comme payé dans l'application.

Les étapes et libellés sont décrits dans fps_payment_steps.json (à ajuster si le site change).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from antai_designation import api, locate, lookup, pause

STEPS_FILE = Path(__file__).with_name("fps_payment_steps.json")


def fill_notice(page, field: dict, data: dict) -> None:
    """Remplit le numéro d'avis : champ unique, sinon une case par groupe de chiffres."""
    full = lookup(data, field.get("value", "numero_avis"))
    try:
        loc = locate(page, field["locator"]).first
        loc.wait_for(state="visible", timeout=field.get("timeout", 4000))
        loc.fill(full)
        print(f"   ✔ Numéro d'avis = {full}")
        return
    except Exception:  # noqa: BLE001
        pass
    seg_spec = field.get("segments_locator")
    if seg_spec:
        boxes = locate(page, seg_spec)
        segments = data.get("segments", [])
        if boxes.count() >= len(segments) > 0:
            for i, seg in enumerate(segments):
                boxes.nth(i).fill(seg)
            print(f"   ✔ Numéro d'avis (case par case) = {' | '.join(segments)}")
            return
    print(f"   ✖ Champ du numéro d'avis non trouvé. À saisir : {data.get('numero_avis_imprime') or full}")
    pause("Saisissez le numéro d'avis puis revenez ici.")


def fill_simple(page, field: dict, data: dict) -> None:
    value = lookup(data, field["value"])
    label = field.get("description", field["value"])
    if not value:
        print(f"   – {label} : aucune valeur")
        return
    try:
        loc = locate(page, field["locator"]).first
        loc.wait_for(state="visible", timeout=field.get("timeout", 4000))
        loc.fill(value)
        print(f"   ✔ {label} = {value}")
    except Exception:  # noqa: BLE001
        print(f"   ✖ Champ non trouvé : {label}. Valeur à saisir : {value}")
        pause(f"Saisissez « {value} » dans le champ « {label} » puis revenez ici.")


def check_amount(page, data: dict) -> None:
    amount = data.get("montant")
    if amount is None:
        return
    text = page.inner_text("body")
    euros = f"{amount:.2f}".replace(".", ",")
    candidates = {euros, f"{amount:.2f}", f"{int(amount)} €", f"{int(amount)},00", f"{int(amount)} euros"}
    if any(c in text for c in candidates) or re.search(rf"\b{int(amount)}\s*(?:€|euros?)", text):
        print(f"   ✔ Montant affiché conforme à l'avis ({euros} €)")
    else:
        print(f"   ⚠ Montant de l'avis ({euros} €) non retrouvé sur la page : vérifiez avant de payer "
              "(FPS majoré ? avis différent ?).")


def run(fine_id: int, base: str, token: str, steps_file: Path, slow_mo: int) -> None:
    from playwright.sync_api import sync_playwright

    client = api(base, token)
    resp = client.get(f"/api/fines/{fine_id}/payment")
    if resp.status_code in (404, 409):
        sys.exit(f"✖ {resp.json().get('detail', 'FPS introuvable')}")
    data = resp.raise_for_status().json()
    print(json.dumps(data, indent=2, ensure_ascii=False))
    if data.get("manquants"):
        sys.exit(f"✖ À compléter dans l'application avant de payer : {', '.join(data['manquants'])}")

    config = json.loads(steps_file.read_text(encoding="utf-8"))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=slow_mo)
        page = browser.new_page(locale="fr-FR")
        page.goto(config["start_url"])
        for step in config["steps"]:
            print(f"\n▶ Étape : {step['name']}")
            if step.get("before"):
                pause(step["before"])
            for field in step.get("fields", []):
                if field.get("type") == "notice":
                    fill_notice(page, field, data)
                else:
                    fill_simple(page, field, data)
            if step.get("check_amount"):
                check_amount(page, data)
            if step.get("manual"):
                pause(step["manual"])
            elif step.get("next"):
                try:
                    locate(page, step["next"]).first.click(timeout=5000)
                    page.wait_for_load_state("networkidle")
                except Exception:  # noqa: BLE001
                    pause("Bouton « suivant » non trouvé : cliquez-le vous-même.")
        if input("\nLe paiement a-t-il été accepté ? (o/N) ").lower() == "o":
            client.post(f"/api/fines/{fine_id}/paid").raise_for_status()
            print("✔ FPS marqué comme payé dans l'application. Pensez à le refacturer au locataire.")
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fine", type=int, help="Identifiant du FPS dans l'application")
    parser.add_argument("--list", action="store_true", help="Lister les FPS restant à payer")
    parser.add_argument("--url", default=os.environ.get("FLOTTEBOX_URL", "http://localhost:8000"))
    parser.add_argument("--token", default=os.environ.get("FLOTTEBOX_TOKEN", ""))
    parser.add_argument("--steps", type=Path, default=STEPS_FILE)
    parser.add_argument("--slow-mo", type=int, default=150)
    args = parser.parse_args()
    if not args.token:
        sys.exit("Jeton manquant : --token ou variable FLOTTEBOX_TOKEN.")
    if args.list or not args.fine:
        rows = api(args.url, args.token).get("/api/fines", params={"type": "fps"}).raise_for_status().json()
        for f in rows:
            print(f"#{f['id']:>4}  {f['numero_avis'] or '-':<28} {f['immatriculation']:<10} {f['date']}  {f['note']}")
        if not rows:
            print("Aucun FPS à payer.")
        return
    run(args.fine, args.url, args.token, args.steps, args.slow_mo)


if __name__ == "__main__":
    main()
