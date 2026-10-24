from datetime import datetime, timezone

from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from contextlib import asynccontextmanager
from sqlalchemy import select, func

from app.config import SECRET_KEY, SESSION_HTTPS_ONLY, SESSION_MAX_AGE
from app.database import init_db, async_session
from app.models import User, Admin
from app.auth import (verify_admin, verify_password, verify_csrf, login_blocked,
                      register_failure, clear_failures, require_admin, require_superadmin, LoginRequired)
from app.routers import admin, user, superadmin
from app.templating import render, templates, asset, ASSET_VERSION, PRECACHE


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Noder", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; media-src 'self' blob:; frame-src 'self'; object-src 'self'; "
        "frame-ancestors 'self'; base-uri 'self'; form-action 'self'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "SAMEORIGIN")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if h.get("content-type", "").startswith("text/html"):
        h.setdefault("Content-Security-Policy", _CSP)
        h.setdefault("Cache-Control", "no-store")
    if SESSION_HTTPS_ONLY:
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


# Added last so it runs first: the security middleware can rely on the session
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="noder_session",
    max_age=SESSION_MAX_AGE,
    same_site="lax",
    https_only=SESSION_HTTPS_ONLY,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(admin.router, prefix="/admin", dependencies=[Depends(verify_csrf), Depends(require_admin)])
app.include_router(superadmin.router, prefix="/super", dependencies=[Depends(verify_csrf), Depends(require_superadmin)])
app.include_router(user.router, prefix="/user", dependencies=[Depends(verify_csrf)])


@app.exception_handler(LoginRequired)
async def login_required(request: Request, exc: LoginRequired):
    if "text/html" not in request.headers.get("accept", ""):
        return JSONResponse({"detail": "Log ind igen"}, status_code=401)
    request.session.clear()  # ugyldig session må ikke sende /login tilbage til dashboardet (redirect-løkke)
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if "text/html" not in request.headers.get("accept", ""):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return render(request, "error.html", status_code=exc.status_code, status=exc.status_code, detail=exc.detail)


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    role = request.session.get("role")
    if role == "admin":
        return RedirectResponse("/admin/dashboard", status_code=303)
    elif role == "superadmin":
        return RedirectResponse("/super/", status_code=303)
    elif role == "user":
        return RedirectResponse("/user/dashboard", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ---------------------------------------------------------------------------
# PWA: manifest, service worker (served from / so its scope is the whole app), offline page
# ---------------------------------------------------------------------------
@app.get("/manifest.webmanifest")
async def manifest():
    icon = lambda src, size, purpose="any": {"src": src, "sizes": size, "type": "image/png", "purpose": purpose}  # noqa: E731
    return JSONResponse(
        {
            "id": "/",
            "name": "Noder",
            "short_name": "Noder",
            "description": "Noder og stemmer til dit kor",
            "lang": "da",
            "start_url": "/?source=pwa",
            "scope": "/",
            "display": "standalone",
            "display_override": ["standalone", "minimal-ui"],
            "orientation": "any",
            "background_color": "#151a2d",
            "theme_color": "#151a2d",
            "categories": ["music", "education"],
            "icons": [
                icon(asset("icons/icon-192.png"), "192x192"),
                icon(asset("icons/icon-512.png"), "512x512"),
                icon(asset("icons/maskable-512.png"), "512x512", "maskable"),
            ],
            "shortcuts": [
                {"name": "Mine sange", "url": "/user/dashboard",
                 "icons": [icon(asset("icons/icon-192.png"), "192x192")]},
            ],
        },
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/sw.js")
async def service_worker(request: Request):
    return templates.TemplateResponse(
        request, "sw.js", {"version": ASSET_VERSION, "precache": PRECACHE},
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.get("/offline", response_class=HTMLResponse)
async def offline(request: Request):
    return render(request, "offline.html")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("role") in ("admin", "superadmin", "user"):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", error=None, username="")


@app.post("/login", dependencies=[Depends(verify_csrf)])
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    username = username.strip()

    wait = login_blocked(request, username)
    if wait:
        minutes = max(1, wait // 60)
        return render(request, "login.html", username=username,
                      error=f"For mange forsøg. Prøv igen om {minutes} min.")

    async with async_session() as db:
        # Superadmin first: if a leader happens to share its name (e.g. a legacy admin migrated from .env),
        # the superadmin can still get in and rename or reset that leader
        if verify_admin(username, password):
            clear_failures(request, username)
            request.session.clear()
            request.session["role"] = "superadmin"
            request.session["display_name"] = username
            return RedirectResponse("/super/", status_code=303)

        leader = await db.scalar(
            select(Admin)
            .where(func.lower(Admin.username) == username.lower())
            .order_by((Admin.username == username).desc())
        )
        if leader and verify_password(password, leader.password):
            clear_failures(request, username)
            leader.last_login = datetime.now(timezone.utc)
            await db.commit()
            request.session.clear()  # new session on privilege change
            request.session["role"] = "admin"
            request.session["admin_id"] = leader.id
            request.session["display_name"] = leader.username
            return RedirectResponse("/admin/dashboard", status_code=303)

        db_user = await db.scalar(
            select(User)
            .where(func.lower(User.username) == username.lower())
            .order_by((User.username == username).desc())  # exact-case match wins
        )
        if db_user and verify_password(password, db_user.password):
            clear_failures(request, username)
            db_user.last_login = datetime.now(timezone.utc)
            await db.commit()
            request.session.clear()
            request.session["role"] = "user"
            request.session["user_id"] = db_user.id
            request.session["choir_id"] = db_user.choir_id
            request.session["display_name"] = db_user.username
            return RedirectResponse("/user/dashboard", status_code=303)

    register_failure(request, username)
    return render(request, "login.html", username=username,
                  error="Forkert brugernavn eller adgangskode")


@app.post("/logout", dependencies=[Depends(verify_csrf)])
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/logout")
async def logout_get(request: Request):
    # Kept for old bookmarks; a GET must not log anyone out (CSRF), so just confirm via the form
    if request.session.get("role"):
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/login", status_code=303)
