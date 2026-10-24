import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ADMIN_USERNAME, ADMIN_PASSWORD, ADMIN_PASSWORD_INSECURE

# ---------------------------------------------------------------------------
# Password hashing (stdlib scrypt — no extra dependency)
# ---------------------------------------------------------------------------
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1
HASH_PREFIX = "scrypt$"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"{HASH_PREFIX}{_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(digest)}"


def is_hashed(stored: str) -> bool:
    return stored.startswith(HASH_PREFIX)


def verify_password(password: str, stored: str) -> bool:
    if not is_hashed(stored):
        # Legacy plaintext (only until init_db has migrated it)
        return hmac.compare_digest(password.encode(), stored.encode())
    try:
        _, n, r, p, salt, digest = stored.split("$")
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def generate_password(words: int = 3) -> str:
    """Readable one-time password, e.g. 'sol-alt-kanon-42'."""
    pool = ["sol", "alt", "bas", "tone", "kor", "dur", "mol", "kanon", "sang", "takt", "node",
            "hymne", "vals", "fuga", "rondo", "lyd", "klang", "puls", "vers", "omkvaed", "stemme"]
    return "-".join(secrets.choice(pool) for _ in range(words)) + f"-{secrets.randbelow(90) + 10}"


def verify_admin(username: str, password: str) -> bool:
    if ADMIN_PASSWORD_INSECURE:
        return False
    user_ok = hmac.compare_digest(username.encode(), ADMIN_USERNAME.encode())
    pass_ok = hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode())
    return user_ok and pass_ok


# ---------------------------------------------------------------------------
# Login throttling (in-memory, per IP + username)
# ---------------------------------------------------------------------------
_MAX_FAILURES = 8
_WINDOW_SECONDS = 15 * 60
_failures: dict[str, deque] = defaultdict(deque)


def _throttle_key(request: Request, username: str) -> str:
    ip = request.client.host if request.client else "?"
    return f"{ip}|{username.strip().lower()}"


def login_blocked(request: Request, username: str) -> int:
    """Return seconds until the next attempt is allowed (0 = allowed)."""
    q = _failures.get(_throttle_key(request, username))
    if not q:
        return 0
    now = time.monotonic()
    while q and now - q[0] > _WINDOW_SECONDS:
        q.popleft()
    if len(q) >= _MAX_FAILURES:
        return int(_WINDOW_SECONDS - (now - q[0])) + 1
    return 0


def register_failure(request: Request, username: str) -> None:
    _failures[_throttle_key(request, username)].append(time.monotonic())


def clear_failures(request: Request, username: str) -> None:
    _failures.pop(_throttle_key(request, username), None)


# ---------------------------------------------------------------------------
# CSRF — synchronizer token stored in the session
# ---------------------------------------------------------------------------
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def verify_csrf(request: Request):
    """Router-level dependency: every state-changing request must carry the session token."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    expected = request.session.get("csrf")
    sent = request.headers.get(CSRF_HEADER)
    if not sent:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()  # cached by Starlette, reused by Form() params
            sent = form.get(CSRF_FIELD)
    if not expected or not sent or not hmac.compare_digest(str(sent), expected):
        raise HTTPException(status_code=403, detail="Sikkerhedstjek fejlede — genindlæs siden og prøv igen.")


# ---------------------------------------------------------------------------
# Access guards & flash messages
# ---------------------------------------------------------------------------
class LoginRequired(Exception):
    """Raised by role dependencies; main.py turns it into a redirect to /login."""


def admin_required(request: Request):
    """Choir-leader guard used by the handlers (the router dependency has already validated the session)."""
    if request.session.get("role") != "admin" or not request.session.get("admin_id"):
        return RedirectResponse("/login", status_code=303)
    return None


def admin_id(request: Request) -> int:
    return request.session["admin_id"]


async def require_admin(request: Request):
    """Router dependency: the session must belong to a choir leader that still exists."""
    from app.database import async_session
    from app.models import Admin

    if request.session.get("role") != "admin" or not request.session.get("admin_id"):
        raise LoginRequired
    async with async_session() as db:
        if not await db.scalar(select(Admin.id).where(Admin.id == request.session["admin_id"])):
            request.session.clear()
            raise LoginRequired


async def require_superadmin(request: Request):
    if request.session.get("role") != "superadmin":
        raise LoginRequired


async def load_member(request: Request, db: AsyncSession):
    """Return the logged-in member, or None if the session is stale (user deleted)."""
    from app.models import User

    if request.session.get("role") != "user":
        return None
    user_id = request.session.get("user_id")
    user = await db.scalar(select(User).where(User.id == user_id)) if user_id else None
    if user is None:
        request.session.clear()
    return user


def flash(request: Request, message: str, kind: str = "success", **extra) -> None:
    request.session["flash"] = {"message": message, "kind": kind, **extra}


def pop_flash(request: Request):
    data = request.session.pop("flash", None)
    if isinstance(data, str):  # sessions from before the upgrade
        data = {"message": data, "kind": "success"}
    return data
