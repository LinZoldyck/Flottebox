from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import AuditLog, Organization, Role, User
from app.security import (client_ip, flash, get_current_user, hash_password, login_limiter, require_role,
                          verify_password)
from app.utils import now_local
from app.web import csrf_protect, render

router = APIRouter(dependencies=[Depends(csrf_protect)])


@router.get("/login")
def login_page(request: Request):
    return render(request, "login.html", allow_signup=get_settings().allow_signup)


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    key = f"{client_ip(request)}:{email.lower()}"
    if not login_limiter.hit(key):
        flash(request, "Trop de tentatives. Réessayez dans 15 minutes.", "error")
        return RedirectResponse("/login", status_code=303)
    user = db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        flash(request, "Identifiants incorrects.", "error")
        return RedirectResponse("/login", status_code=303)
    login_limiter.reset(key)
    request.session.clear()
    request.session["user_id"] = user.id
    user.last_login_at = now_local()
    db.add(AuditLog(organization_id=user.organization_id, user_id=user.id, action="login", detail=client_ip(request)))
    db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/signup")
def signup_page(request: Request):
    if not get_settings().allow_signup:
        return RedirectResponse("/login", status_code=303)
    return render(request, "signup.html")


@router.post("/signup")
def signup(request: Request, company: str = Form(...), full_name: str = Form(...), email: str = Form(...),
           password: str = Form(...), db: Session = Depends(get_db)):
    if not get_settings().allow_signup:
        return RedirectResponse("/login", status_code=303)
    email = email.strip().lower()
    if len(password) < 10:
        flash(request, "Le mot de passe doit contenir au moins 10 caractères.", "error")
        return RedirectResponse("/signup", status_code=303)
    if db.scalar(select(User).where(func.lower(User.email) == email)):
        flash(request, "Un compte existe déjà avec cet e-mail.", "error")
        return RedirectResponse("/signup", status_code=303)
    org = Organization(name=company.strip(), email=email, representative_name=full_name.strip())
    db.add(org)
    db.flush()
    user = User(organization_id=org.id, email=email, full_name=full_name.strip(),
                password_hash=hash_password(password), role=Role.OWNER)
    db.add(user)
    db.commit()
    request.session.clear()
    request.session["user_id"] = user.id
    flash(request, "Bienvenue ! Complétez les informations de votre société puis ajoutez vos véhicules.", "success")
    return RedirectResponse("/settings", status_code=303)


# --- Gestion des utilisateurs (gérant) ---

@router.get("/users")
def users_page(request: Request, user: User = Depends(require_role(Role.OWNER)), db: Session = Depends(get_db)):
    users = db.scalars(select(User).where(User.organization_id == user.organization_id).order_by(User.email)).all()
    return render(request, "users.html", user=user, users=users, roles=list(Role))


@router.post("/users")
def create_user(request: Request, email: str = Form(...), full_name: str = Form(""), role: Role = Form(...),
                password: str = Form(...), user: User = Depends(require_role(Role.OWNER)), db: Session = Depends(get_db)):
    email = email.strip().lower()
    if len(password) < 10:
        flash(request, "Mot de passe trop court (10 caractères minimum).", "error")
    elif db.scalar(select(User).where(func.lower(User.email) == email)):
        flash(request, "Cet e-mail est déjà utilisé.", "error")
    else:
        db.add(User(organization_id=user.organization_id, email=email, full_name=full_name,
                    password_hash=hash_password(password), role=role))
        db.add(AuditLog(organization_id=user.organization_id, user_id=user.id, action="user.create", detail=email))
        db.commit()
        flash(request, f"Utilisateur {email} créé.", "success")
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}")
def update_user(request: Request, user_id: int, role: Role = Form(...), is_active: bool = Form(False),
                user: User = Depends(require_role(Role.OWNER)), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if not target or target.organization_id != user.organization_id:
        flash(request, "Utilisateur introuvable.", "error")
    elif target.id == user.id:
        flash(request, "Vous ne pouvez pas modifier votre propre rôle.", "error")
    else:
        target.role, target.is_active = role, is_active
        db.add(AuditLog(organization_id=user.organization_id, user_id=user.id, action="user.update",
                        detail=f"{target.email} {role.value} actif={is_active}"))
        db.commit()
        flash(request, "Utilisateur mis à jour.", "success")
    return RedirectResponse("/users", status_code=303)


@router.get("/account")
def account_page(request: Request, user: User = Depends(get_current_user)):
    return render(request, "account.html", user=user)


@router.post("/account/password")
def change_password(request: Request, current: str = Form(...), new: str = Form(...),
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not verify_password(current, user.password_hash):
        flash(request, "Mot de passe actuel incorrect.", "error")
    elif len(new) < 10:
        flash(request, "Le nouveau mot de passe doit contenir au moins 10 caractères.", "error")
    else:
        user.password_hash = hash_password(new)
        db.commit()
        flash(request, "Mot de passe modifié.", "success")
    return RedirectResponse("/account", status_code=303)
