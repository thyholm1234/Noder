from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text
from app.config import DATABASE_URL

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
]


async def init_db():
    async with engine.begin() as conn:
        from app import models  # noqa: F401
        await conn.run_sync(Base.metadata.create_all)
        for stmt in _MIGRATIONS:
            await conn.execute(text(stmt))

    await _hash_legacy_passwords()


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
