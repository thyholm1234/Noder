from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text
from app.config import DATABASE_URL

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        from app import models  # noqa: F401
        await conn.run_sync(Base.metadata.create_all)

        # Add columns that may not exist yet on older databases
        await conn.execute(
            text(
                "ALTER TABLE choir_songs ADD COLUMN IF NOT EXISTS highlight_start DATE"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE choir_songs ADD COLUMN IF NOT EXISTS highlight_end DATE"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE setlist_items ADD COLUMN IF NOT EXISTS placeholder_text VARCHAR(255)"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE setlist_items ALTER COLUMN song_id DROP NOT NULL"
            )
        )
        await conn.execute(
            text("ALTER TABLE songs ADD COLUMN IF NOT EXISTS tags VARCHAR(500)")
        )
        await conn.execute(
            text("ALTER TABLE songs ADD COLUMN IF NOT EXISTS description TEXT")
        )
