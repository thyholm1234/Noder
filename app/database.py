from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text
import logging

from app.config import DATABASE_URL

log = logging.getLogger("noder")

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        yield session


# Columns added after the first release (idempotent)
_MIGRATIONS = [
    "ALTER TABLE choir_songs ADD COLUMN IF NOT EXISTS highlight_start DATE",
    "ALTER TABLE choir_songs ADD COLUMN IF NOT EXISTS highlight_end DATE",
    "ALTER TABLE setlist_items ADD COLUMN IF NOT EXISTS placeholder_text VARCHAR(255)",
    "ALTER TABLE setlist_items ALTER COLUMN song_id DROP NOT NULL",
    "ALTER TABLE songs ADD COLUMN IF NOT EXISTS tags VARCHAR(500)",
    "ALTER TABLE songs ADD COLUMN IF NOT EXISTS description TEXT",
    "ALTER TABLE setlists ADD COLUMN IF NOT EXISTS event_date DATE",
    "ALTER TABLE setlists ADD COLUMN IF NOT EXISTS notes TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMPTZ",
    # Multiple choir leaders: choirs and songs belong to one (admins table is made by create_all)
    "ALTER TABLE choirs ADD COLUMN IF NOT EXISTS owner_id INTEGER REFERENCES admins(id) ON DELETE CASCADE",
    "ALTER TABLE songs ADD COLUMN IF NOT EXISTS owner_id INTEGER REFERENCES admins(id) ON DELETE CASCADE",
    "ALTER TABLE admins ADD COLUMN IF NOT EXISTS quota_bytes BIGINT",
    "ALTER TABLE choirs DROP CONSTRAINT IF EXISTS choirs_name_key",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_choirs_owner_name ON choirs (owner_id, name)",
]


async def init_db():
    async with engine.begin() as conn:
        from app import models  # noqa: F401
        await conn.run_sync(Base.metadata.create_all)
        for stmt in _MIGRATIONS:
            await conn.execute(text(stmt))

    await _hash_legacy_passwords()
    await _adopt_legacy_data()


async def _hash_legacy_passwords():
    """Older versions stored member passwords in plaintext — hash them in place."""
    from sqlalchemy import select
    from app.models import User
    from app.auth import hash_password, HASH_PREFIX

    async with async_session() as db:
        result = await db.execute(select(User).where(~User.password.startswith(HASH_PREFIX)))
        users = result.scalars().all()
        for user in users:
            user.password = hash_password(user.password)
        if users:
            await db.commit()


async def _adopt_legacy_data():
    """Before v3 the single env admin owned everything. Turn that admin into a regular choir leader.

    Runs while choirs/songs without an owner exist. The leader is created from ADMIN_USERNAME /
    ADMIN_PASSWORD, so deploy once with the old .env, then put the superadmin's credentials there.
    """
    from sqlalchemy import select, update
    from app.models import Admin, Choir, Song
    from app.auth import hash_password
    from app.config import ADMIN_USERNAME, ADMIN_PASSWORD, ADMIN_PASSWORD_INSECURE

    async with async_session() as db:
        orphans = (await db.scalar(select(Choir.id).where(Choir.owner_id.is_(None)).limit(1)) is not None
                   or await db.scalar(select(Song.id).where(Song.owner_id.is_(None)).limit(1)) is not None)
        if not orphans:
            return

        leader = await db.scalar(select(Admin).order_by(Admin.id).limit(1))
        if leader is None:
            if ADMIN_PASSWORD_INSECURE:
                log.warning("Der er kor/sange uden korleder, men ADMIN_PASSWORD er ikke sat — "
                            "kan ikke oprette den eksisterende korleder. Sæt de gamle login-oplysninger og start igen.")
                return
            leader = Admin(username=ADMIN_USERNAME, password=hash_password(ADMIN_PASSWORD))
            db.add(leader)
            await db.flush()
            log.warning("Eksisterende administrator er oprettet som korleder '%s'. Skift nu ADMIN_USERNAME / "
                        "ADMIN_PASSWORD i .env til superadminens oplysninger.", ADMIN_USERNAME)
        await db.execute(update(Choir).where(Choir.owner_id.is_(None)).values(owner_id=leader.id))
        await db.execute(update(Song).where(Song.owner_id.is_(None)).values(owner_id=leader.id))
        await db.commit()
