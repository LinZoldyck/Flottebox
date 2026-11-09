#!/usr/bin/env python3
"""Robot d'aide à la désignation du conducteur sur le site de l'ANTAI.

À lancer sur VOTRE ordinateur (il ouvre un vrai navigateur visible) :

    pip install -r robot/requirements.txt && playwright install chromium
    export FLOTTEBOX_URL=https://flotte.mondomaine.fr
    export FLOTTEBOX_TOKEN=fb_xxx            # Paramètres > Jeton du robot
    python robot/antai_designation.py --fine 12      # ou --list pour voir les PV prêts

Fonctionnement : le robot récupère les données de la contravention et du conducteur dans
l'application, ouvre le site de l'ANTAI et remplit chaque étape décrite dans antai_steps.json.
Il NE résout PAS le captcha et NE valide PAS la désignation : c'est vous qui le faites, puis vous
confirmez dans le terminal pour marquer la contravention comme désignée.

Les libellés des champs du site ANTAI peuvent évoluer : ajustez antai_steps.json si un champ
n'est pas trouvé (le robot vous le signale et vous laisse le remplir à la main).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import httpx

STEPS_FILE = Path(__file__).with_name("antai_steps.json")


def api(base: str, token: str) -> httpx.Client:
    return httpx.Client(base_url=base.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=30)


def lookup(data: dict, dotted: str) -> str:
    cur: object = data
    for part in dotted.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return "" if cur is None else str(cur)


def locate(page, spec: dict):
    if "css" in spec:
        return page.locator(spec["css"])
    if "label" in spec:
        return page.get_by_label(re.compile(spec["label"], re.I))
    if "placeholder" in spec:
        return page.get_by_placeholder(re.compile(spec["placeholder"], re.I))
    if "role" in spec:
        return page.get_by_role(spec["role"], name=re.compile(spec.get("name", ""), re.I))
    if "text" in spec:
        return page.get_by_text(re.compile(spec["text"], re.I))
    raise ValueError(f"Localisateur inconnu : {spec}")


def pause(msg: str) -> None:
    input(f"\n⏸  {msg}\n   Appuyez sur Entrée pour continuer… ")


def fill_field(page, field: dict, data: dict) -> None:
    value = lookup(data, field["value"]) if "value" in field else field.get("const", "")
    label = field.get("description", field["value"] if "value" in field else str(field["locator"]))
    if not value and field.get("type", "fill") != "click":
        print(f"   – {label} : aucune valeur (ignoré)")
        return
    loc = locate(page, field["locator"]).first
    try:
        loc.wait_for(state="visible", timeout=field.get("timeout", 4000))
        kind = field.get("type", "fill")
        if kind == "fill":
            loc.fill(value)
        elif kind == "select":
            try:
                loc.select_option(label=value)
            except Exception:  # noqa: BLE001
                loc.select_option(value=value)
        elif kind == "check":
            loc.check()
        elif kind == "click":
            loc.click()
        print(f"   ✔ {label} = {value}")
    except Exception:  # noqa: BLE001 - champ introuvable : on laisse l'utilisateur faire
        print(f"   ✖ Champ non trouvé : {label}. Valeur à saisir : {value}")
        pause(f"Saisissez « {value} » dans le champ « {label} » puis revenez ici.")


def run(fine_id: int, base: str, token: str, steps_file: Path, slow_mo: int) -> None:
    from playwright.sync_api import sync_playwright

    client = api(base, token)
    resp = client.get(f"/api/fines/{fine_id}/designation")
    if resp.status_code == 409:
        sys.exit(f"✖ {resp.json().get('detail')}")
    data = resp.raise_for_status().json()
    print(json.dumps(data, indent=2, ensure_ascii=False))
    if data.get("manquants"):
        print(f"\n⚠ Informations manquantes : {', '.join(data['manquants'])}")
        if not data.get("conducteur") or input("Continuer quand même ? (o/N) ").lower() != "o":
            sys.exit(1)

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
                fill_field(page, field, data)
            if step.get("manual"):
                pause(step["manual"])
            elif step.get("next"):
                try:
                    locate(page, step["next"]).first.click(timeout=5000)
                    page.wait_for_load_state("networkidle")
                except Exception:  # noqa: BLE001
                    pause("Bouton « suivant » non trouvé : cliquez-le vous-même.")
        if input("\nLa désignation a-t-elle été validée sur le site de l'ANTAI ? (o/N) ").lower() == "o":
            client.post(f"/api/fines/{fine_id}/designated").raise_for_status()
            print("✔ Contravention marquée comme désignée dans l'application.")
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fine", type=int, help="Identifiant de la contravention")
    parser.add_argument("--list", action="store_true", help="Lister les contraventions prêtes à désigner")
    parser.add_argument("--url", default=os.environ.get("FLOTTEBOX_URL", "http://localhost:8000"))
    parser.add_argument("--token", default=os.environ.get("FLOTTEBOX_TOKEN", ""))
    parser.add_argument("--steps", type=Path, default=STEPS_FILE)
    parser.add_argument("--slow-mo", type=int, default=150, help="Ralentit les actions (ms) pour suivre à l'écran")
    args = parser.parse_args()
    if not args.token:
        sys.exit("Jeton manquant : --token ou variable FLOTTEBOX_TOKEN.")
    if args.list or not args.fine:
        for f in api(args.url, args.token).get("/api/fines").raise_for_status().json():
            print(f"#{f['id']:>4}  {f['numero_avis'] or '-':<16} {f['immatriculation']:<10} {f['date']}  {f['note']}")
        return
    run(args.fine, args.url, args.token, args.steps, args.slow_mo)


if __name__ == "__main__":
    main()
