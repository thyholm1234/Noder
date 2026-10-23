"""Queries and helpers shared by the admin and member routers."""
from datetime import date

from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Setlist, SetlistItem, Song
from app.pdf_export import ExportEntry, ExportOptions, build_setlist_pdf, pick_pdfs, safe_filename
from app.utils import content_disposition


def setlist_sort_key(sl: Setlist, today: date | None = None):
    """Upcoming events first (soonest first), then undated, then past events (newest first)."""
    today = today or date.today()
    if sl.event_date and sl.event_date >= today:
        return (0, sl.event_date.toordinal(), sl.name.lower())
    if not sl.event_date:
        return (1, 0, sl.name.lower())
    return (2, -sl.event_date.toordinal(), sl.name.lower())


def next_event(setlists, today: date | None = None):
    today = today or date.today()
    upcoming = [s for s in setlists if s.event_date and s.event_date >= today]
    return min(upcoming, key=lambda s: s.event_date) if upcoming else None


async def load_setlist(db: AsyncSession, setlist_id: int, choir_id: int | None = None, with_files=False):
    song_load = selectinload(Setlist.items).selectinload(SetlistItem.song)
    if with_files:
        song_load = song_load.selectinload(Song.files)
    stmt = select(Setlist).options(song_load, selectinload(Setlist.choir)).where(Setlist.id == setlist_id)
    if choir_id is not None:
        stmt = stmt.where(Setlist.choir_id == choir_id)
    return await db.scalar(stmt)


def setlist_parts(setlist: Setlist, file_type: str) -> list[str]:
    """Distinct part names across a setlist's files, in first-seen order."""
    seen: dict[str, str] = {}
    for item in setlist.items:
        if not item.song:
            continue
        for f in sorted(item.song.files, key=lambda f: f.id):
            if f.file_type == file_type and f.part_name:
                seen.setdefault(f.part_name.strip().lower(), f.part_name.strip())
    return list(seen.values())


def setlist_neighbours(setlist: Setlist, song_id: int):
    """(index, total, previous song, next song) for song_id within the setlist's songs."""
    songs = [i.song for i in setlist.items if i.song]
    ids = [s.id for s in songs]
    if song_id not in ids:
        return None
    i = ids.index(song_id)
    return {
        "setlist": setlist,
        "index": i + 1,
        "total": len(songs),
        "prev": songs[i - 1] if i > 0 else None,
        "next": songs[i + 1] if i + 1 < len(songs) else None,
    }


def _flag(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.lower() in ("1", "true", "on", "yes")


async def export_setlist_response(setlist: Setlist, params) -> Response:
    """Build the merged PDF for a setlist (loaded with files) from query parameters."""
    mode = params.get("mode", "first")
    part = params.get("part", "")
    options = ExportOptions(
        cover=_flag(params.get("cover"), True),
        duplex=_flag(params.get("duplex"), False),
        page_numbers=_flag(params.get("numbers"), False),
    )

    entries = []
    for item in setlist.items:
        if item.song:
            files = pick_pdfs(item.song.files, mode, part)
            entries.append(ExportEntry("song", item.song.title, item.song.composer, [f.file_path for f in files]))
        elif item.placeholder_text:
            entries.append(ExportEntry("separator", item.placeholder_text))

    pdf, _warnings = await run_in_threadpool(
        build_setlist_pdf,
        setlist.name,
        setlist.choir.name if setlist.choir else None,
        setlist.event_date,
        setlist.notes,
        entries,
        options,
    )
    filename = safe_filename(setlist.name) + (f"-{safe_filename(part)}" if mode == "part" and part else "") + ".pdf"
    disposition = "attachment" if _flag(params.get("download"), True) else "inline"
    return Response(
        pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": content_disposition(disposition, filename),
            "Cache-Control": "private, no-store",
        },
    )
