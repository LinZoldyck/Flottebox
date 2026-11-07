"""Authentification (sessions), rôles, jetons d'API et limitation des tentatives."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Organization, Role, User

ROLE_RANK = {Role.VIEWER: 0, Role.MANAGER: 1, Role.OWNER: 2}


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_api_token() -> str:
    return "fb_" + secrets.token_urlsafe(32)


class LoginRequired(Exception):
    """Levée quand une page nécessite une session ; convertie en redirection vers /login."""


class RateLimiter:
    """Limiteur en mémoire (suffisant pour une instance unique sur Raspberry Pi)."""

    def __init__(self, max_attempts: int, window_seconds: int):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str) -> bool:
        """Enregistre une tentative ; renvoie False si la limite est dépassée."""
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max_attempts:
            return False
        q.append(now)
        return True

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


login_limiter = RateLimiter(max_attempts=10, window_seconds=15 * 60)
renter_code_limiter = RateLimiter(max_attempts=8, window_seconds=15 * 60)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise LoginRequired()
    user = db.get(User, user_id)
    if not user or not user.is_active:
        request.session.clear()
        raise LoginRequired()
    return user


def require_role(minimum: Role):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if ROLE_RANK[user.role] < ROLE_RANK[minimum]:
            raise HTTPException(status_code=403, detail="Droits insuffisants pour cette action.")
        return user

    return dependency


def get_api_org(request: Request, db: Session = Depends(get_db)) -> Organization:
    """Authentification par jeton (en-tête Authorization: Bearer ...) pour le robot de désignation."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Jeton manquant")
    digest = hash_token(auth[7:].strip())
    for org in db.scalars(select(Organization).where(Organization.api_token_hash != "")):
        if hmac.compare_digest(org.api_token_hash, digest):
            return org
    raise HTTPException(status_code=401, detail="Jeton invalide")


def flash(request: Request, message: str, category: str = "info") -> None:
    request.session.setdefault("_flashes", []).append({"message": message, "category": category})


def pop_flashes(request: Request) -> list[dict]:
    return request.session.pop("_flashes", [])
