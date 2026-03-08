import secrets
from fastapi import Request
from fastapi.responses import RedirectResponse
from app.config import ADMIN_USERNAME, ADMIN_PASSWORD


def verify_admin(username: str, password: str) -> bool:
    return secrets.compare_digest(username, ADMIN_USERNAME) and secrets.compare_digest(
        password, ADMIN_PASSWORD
    )


def admin_required(request: Request):
    if request.session.get("role") != "admin":
        return RedirectResponse("/login", status_code=303)
    return None


def login_required(request: Request):
    role = request.session.get("role")
    if role not in ("admin", "user"):
        return RedirectResponse("/login", status_code=303)
    return None
