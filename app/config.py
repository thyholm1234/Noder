import os
from dotenv import load_dotenv

load_dotenv()

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://noder:noder_secret_2024@db:5432/noder_db")
SECRET_KEY = os.getenv("SECRET_KEY", "skift-denne-til-en-tilfaeldig-nogle-i-produktion")


def _parse_quota(value: str) -> int:
    """Parse a human-readable size like '4G' or '500M' to bytes."""
    value = value.strip().upper()
    units = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    if value[-1] in units:
        return int(float(value[:-1]) * units[value[-1]])
    return int(value)


QUOTA_BYTES = _parse_quota(os.getenv("QUOTA", "4G"))
