from datetime import date
from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Song, SongFile, ChoirSong, Choir, Setlist, SetlistItem
from app.auth import login_required
from app.utils import serve_file_with_range

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def user_required(request: Request):
    if request.session.get("role") != "user":
        return RedirectResponse("/login", status_code=303)
    return None


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    sort: str = "title",
    q: str = "",
    db: AsyncSession = Depends(get_db),
):
    redirect = user_required(request)
    if redirect:
        return redirect

    choir_id = request.session.get("choir_id")

    # Get choir name
    choir_result = await db.execute(select(Choir).where(Choir.id == choir_id))
    choir = choir_result.scalar_one_or_none()

    today = date.today()

    # Get songs assigned to user's choir (with highlight info)
    query = (
        select(Song, ChoirSong)
        .join(ChoirSong, ChoirSong.song_id == Song.id)
        .where(ChoirSong.choir_id == choir_id)
    )

    if q:
        query = query.where(Song.title.ilike(f"%{q}%"))

    if sort == "date":
        query = query.order_by(ChoirSong.assigned_at.desc())
    else:
        query = query.order_by(Song.title.asc())

    result = await db.execute(query)
    rows = result.all()

    highlighted = []
    songs = []
    for song, cs in rows:
        songs.append((song, cs.assigned_at))
        is_hl = (
            cs.highlight_start and cs.highlight_end
            and cs.highlight_start <= today <= cs.highlight_end
        )
        if is_hl:
            highlighted.append(song)

    # Setlists for this choir
    sl_result = await db.execute(
        select(Setlist)
        .options(selectinload(Setlist.items).selectinload(SetlistItem.song))
        .where(Setlist.choir_id == choir_id)
        .order_by(Setlist.name)
    )
    setlists = sl_result.scalars().all()

    return templates.TemplateResponse(
        "user/dashboard.html",
        {
            "request": request,
            "songs": songs,
            "highlighted": highlighted,
            "setlists": setlists,
            "sort": sort,
            "q": q,
            "choir": choir,
        },
    )


@router.get("/songs/{song_id}", response_class=HTMLResponse)
async def song_detail(request: Request, song_id: int, db: AsyncSession = Depends(get_db)):
    redirect = user_required(request)
    if redirect:
        return redirect

    choir_id = request.session.get("choir_id")

    # Check song is assigned to user's choir
    cs_result = await db.execute(
        select(ChoirSong).where(ChoirSong.choir_id == choir_id, ChoirSong.song_id == song_id)
    )
    if not cs_result.scalar_one_or_none():
        return RedirectResponse("/user/dashboard", status_code=303)

    result = await db.execute(
        select(Song).options(selectinload(Song.files), selectinload(Song.links)).where(Song.id == song_id)
    )
    song = result.scalar_one_or_none()
    if not song:
        return RedirectResponse("/user/dashboard", status_code=303)

    pdf_files = [f for f in song.files if f.file_type == "pdf"]
    audio_files = [f for f in song.files if f.file_type == "audio"]

    return templates.TemplateResponse(
        "user/song_detail.html",
        {
            "request": request,
            "song": song,
            "pdf_files": pdf_files,
            "audio_files": audio_files,
        },
    )


@router.get("/files/{file_id}")
async def file_serve(
    request: Request, file_id: int, download: int = 0, db: AsyncSession = Depends(get_db)
):
    redirect = user_required(request)
    if redirect:
        return redirect

    choir_id = request.session.get("choir_id")

    # Get file and check access
    result = await db.execute(
        select(SongFile).where(SongFile.id == file_id)
    )
    sf = result.scalar_one_or_none()
    if not sf:
        return RedirectResponse("/user/dashboard", status_code=303)

    # Verify user's choir has access to this song
    cs_result = await db.execute(
        select(ChoirSong).where(ChoirSong.choir_id == choir_id, ChoirSong.song_id == sf.song_id)
    )
    if not cs_result.scalar_one_or_none():
        return RedirectResponse("/user/dashboard", status_code=303)

    return await serve_file_with_range(sf.file_path, request, as_download=bool(download))


@router.get("/setlists/{setlist_id}", response_class=HTMLResponse)
async def setlist_detail(request: Request, setlist_id: int, db: AsyncSession = Depends(get_db)):
    redirect = user_required(request)
    if redirect:
        return redirect

    choir_id = request.session.get("choir_id")

    result = await db.execute(
        select(Setlist)
        .options(selectinload(Setlist.items).selectinload(SetlistItem.song))
        .where(Setlist.id == setlist_id, Setlist.choir_id == choir_id)
    )
    setlist = result.scalar_one_or_none()
    if not setlist:
        return RedirectResponse("/user/dashboard", status_code=303)

    return templates.TemplateResponse(
        "user/setlist_detail.html",
        {"request": request, "setlist": setlist},
    )
