"""Rendu des gabarits HTML et protection CSRF des formulaires."""
from __future__ import annotations

import hmac
import secrets
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app.config import get_settings
from app.security import pop_flashes
from app.utils import euros, fuel_label

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["euros"] = euros
templates.env.filters["fuel"] = fuel_label
templates.env.filters["dt"] = lambda d: d.strftime("%d/%m/%Y %H:%M") if d else "-"
templates.env.filters["d"] = lambda d: d.strftime("%d/%m/%Y") if d else "-"
templates.env.globals["app_name"] = get_settings().app_name


def csrf_token(request: Request) -> str:
    token = request.session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["_csrf"] = token
    return token


async def csrf_protect(request: Request) -> None:
    """Dépendance de routeur : vérifie le jeton CSRF sur toutes les requêtes POST."""
    if request.method != "POST":
        return
    form = await request.form()
    sent = str(form.get("csrf_token", ""))
    expected = request.session.get("_csrf", "")
    if not expected or not hmac.compare_digest(sent, expected):
        raise HTTPException(status_code=400, detail="Formulaire expiré, rechargez la page.")


def render(request: Request, template: str, status_code: int = 200, **context):
    context.setdefault("user", getattr(request.state, "user", None))
    context["flashes"] = pop_flashes(request)
    token = csrf_token(request)
    context["csrf_token"] = token
    context["csrf"] = Markup(f'<input type="hidden" name="csrf_token" value="{token}">')
    return templates.TemplateResponse(request, template, context, status_code=status_code)
