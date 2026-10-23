import logging
import os
import secrets
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("noder")

_INSECURE_VALUES = {"", "admin123", "skift-mig", "skift-til-en-lang-tilfaeldig-streng",
                    "skift-denne-til-en-tilfaeldig-nogle-i-produktion"}

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://noder:noder_secret_2024@db:5432/noder_db")

SECRET_KEY = os.getenv("SECRET_KEY", "")
if SECRET_KEY in _INSECURE_VALUES or len(SECRET_KEY) < 16:
    # Never fall back to a well-known key: sessions could be forged.
    # A random key is safe, but logs everyone out on each restart.
    log.warning("SECRET_KEY mangler eller er usikker — bruger en tilfældig nøgle. "
                "Sæt SECRET_KEY i .env for at bevare logins på tværs af genstarter.")
    SECRET_KEY = secrets.token_urlsafe(48)

# Default/template passwords are rejected outright; short ones only warn.
ADMIN_PASSWORD_INSECURE = ADMIN_PASSWORD in _INSECURE_VALUES
if ADMIN_PASSWORD_INSECURE:
    log.warning("ADMIN_PASSWORD er ikke sat eller er en standardværdi — admin-login er slået fra.")
elif len(ADMIN_PASSWORD) < 10:
    log.warning("ADMIN_PASSWORD er kort — overvej mindst 10 tegn.")

# Set to true when served over HTTPS (e.g. behind a reverse proxy with TLS)
SESSION_HTTPS_ONLY = os.getenv("SESSION_HTTPS_ONLY", "false").lower() in ("1", "true", "yes")
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE_DAYS", "30")) * 24 * 3600


def _parse_quota(value: str) -> int:
    """Parse a human-readable size like '4G' or '500M' to bytes."""
    value = value.strip().upper()
    units = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    if value[-1] in units:
        return int(float(value[:-1]) * units[value[-1]])
    return int(value)


QUOTA_BYTES = _parse_quota(os.getenv("QUOTA", "4G"))
MAX_UPLOAD_BYTES = _parse_quota(os.getenv("MAX_UPLOAD", "200M"))
