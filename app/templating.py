import hashlib
import os
from datetime import date

from fastapi.templating import Jinja2Templates

from app.auth import csrf_token, pop_flash
from app.pdf_export import danish_date
from app.utils import get_storage_usage

STATIC_DIR = "app/static"
templates = Jinja2Templates(directory="app/templates")


def _asset_version() -> str:
    """Fingerprint of all static files — changes whenever any of them do (cache busting + SW cache name)."""
    h = hashlib.sha1()
    for root, _, files in sorted(os.walk(STATIC_DIR)):
        for name in sorted(files):
            path = os.path.join(root, name)
            h.update(path.encode())
            h.update(str(os.path.getmtime(path)).encode())
    return h.hexdigest()[:10]


ASSET_VERSION = _asset_version()


def asset(path: str) -> str:
    return f"/static/{path}?v={ASSET_VERSION}"


# Assets the service worker pre-caches so the app shell opens instantly and offline
PRECACHE = [asset(p) for p in ("style.css", "app.js", "player.js", "vendor/Sortable.min.js",
                                "favicon.svg", "icons/icon-192.png")] + ["/offline"]

templates.env.globals.update(
    get_storage=get_storage_usage,
    csrf_token=csrf_token,
    get_flash=pop_flash,
    today=date.today,
    asset=asset,
)
templates.env.filters["danish_date"] = danish_date


def render(request, name: str, status_code: int = 200, **context):
    return templates.TemplateResponse(request, name, context, status_code=status_code)
