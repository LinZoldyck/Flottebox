import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import init_db
from app.routers import api, auth, fleet, ops, renter
from app.security import LoginRequired
from app.web import render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    scheduler = None
    if settings.enable_scheduler:
        from app.services.jobs import start_scheduler
        scheduler = start_scheduler()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="fb_session",
                   max_age=60 * 60 * 12, same_site="lax", https_only=settings.secure_cookies)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

for module in (auth, fleet, ops, renter, api):
    app.include_router(module.router)


@app.exception_handler(LoginRequired)
async def login_required(request: Request, _: LoginRequired):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith(("/api/", "/webhooks/")):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return render(request, "error.html", status_code=exc.status_code, code=exc.status_code, detail=exc.detail)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response
