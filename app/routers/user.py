from datetime import date
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Song, SongFile, ChoirSong, Choir, Setlist, SetlistItem
from app.auth import load_member, flash, verify_password, hash_password
from app.services import (load_setlist, setlist_sort_key, setlist_parts, setlist_neighbours,
                          export_setlist_response, next_event)
from app.templating import render
from app.utils import serve_file_with_range, download_name

router = APIRouter()


def go(url: str):
    return RedirectResponse(url, status_code=303)


async def _has_song(db: AsyncSession, choir_id: int, song_id: int) -> bool:
    return bool(await db.scalar(
        select(ChoirSong.id).where(ChoirSong.choir_id == choir_id, ChoirSong.song_id == song_id)
    ))


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    sort: str = "title",
    q: str = "",
    db: AsyncSession = Depends(get_db),
):
    member = await load_member(request, db)
    if not member:
        return go("/login")

    choir = await db.get(Choir, member.choir_id)
    today = date.today()

    query = (
        select(Song, ChoirSong)
        .options(selectinload(Song.files))
        .join(ChoirSong, ChoirSong.song_id == Song.id)
        .where(ChoirSong.choir_id == member.choir_id)
    )
    q = q.strip()
    if q:
        like = f"%{q}%"
        query = query.where(Song.title.ilike(like) | Song.composer.ilike(like) | Song.tags.ilike(like))
    if sort == "date":
        query = query.order_by(ChoirSong.assigned_at.desc())
    else:
        query = query.order_by(Song.title.asc())

    rows = (await db.execute(query)).all()

    highlighted, songs = [], []
    for song, cs in rows:
        songs.append((song, cs.assigned_at))
        if cs.highlight_start and cs.highlight_end and cs.highlight_start <= today <= cs.highlight_end:
            highlighted.append((song, cs))

    setlists = (await db.execute(
        select(Setlist)
        .options(selectinload(Setlist.items).selectinload(SetlistItem.song))
        .where(Setlist.choir_id == member.choir_id)
    )).scalars().all()
    setlists = sorted((s for s in setlists if s.items), key=setlist_sort_key)

    return render(
        request, "user/dashboard.html",
        songs=songs, highlighted=highlighted, setlists=setlists, next_setlist=next_event(setlists),
        sort=sort, q=q, choir=choir,
    )


@router.get("/songs/{song_id}", response_class=HTMLResponse)
async def song_detail(request: Request, song_id: int, sl: int = 0, db: AsyncSession = Depends(get_db)):
    member = await load_member(request, db)
    if not member:
        return go("/login")

    if not await _has_song(db, member.choir_id, song_id):
        return go("/user/dashboard")

    song = await db.scalar(
        select(Song).options(selectinload(Song.files), selectinload(Song.links)).where(Song.id == song_id)
    )
    if not song:
        return go("/user/dashboard")

    # Opened from a setlist: offer previous/next navigation
    nav = None
    if sl:
        setlist = await load_setlist(db, sl, member.choir_id)
        if setlist:
            nav = setlist_neighbours(setlist, song_id)

    files = sorted(song.files, key=lambda f: f.id)
    return render(
        request, "user/song_detail.html",
        song=song,
        pdf_files=[f for f in files if f.file_type == "pdf"],
        audio_files=[f for f in files if f.file_type == "audio"],
        nav=nav,
    )


@router.get("/files/{file_id}")
async def file_serve(request: Request, file_id: int, download: int = 0, db: AsyncSession = Depends(get_db)):
    member = await load_member(request, db)
    if not member:
        raise HTTPException(401, "Log ind igen")

    sf = await db.get(SongFile, file_id)
    if not sf or not await _has_song(db, member.choir_id, sf.song_id):
        raise HTTPException(404, "Fil ikke fundet")

    return await serve_file_with_range(sf.file_path, request, as_download=bool(download),
                                       filename=download_name(sf.original_filename, sf.file_path))


@router.get("/setlists/{setlist_id}", response_class=HTMLResponse)
async def setlist_detail(request: Request, setlist_id: int, db: AsyncSession = Depends(get_db)):
    member = await load_member(request, db)
    if not member:
        return go("/login")

    setlist = await load_setlist(db, setlist_id, member.choir_id, with_files=True)
    if not setlist:
        return go("/user/dashboard")

    # Rehearsal playlist: every song with its audio parts, in setlist order
    playlist = []
    for item in setlist.items:
        if not item.song:
            continue
        audio = [f for f in sorted(item.song.files, key=lambda f: f.id) if f.file_type == "audio"]
        playlist.append({
            "id": item.song.id,
            "title": item.song.title,
            "composer": item.song.composer or "",
            "tracks": [{"part": f.part_name or f.original_filename, "url": f"/user/files/{f.id}"} for f in audio],
        })

    return render(
        request, "user/setlist_detail.html",
        setlist=setlist,
        playlist=playlist,
        pdf_parts=setlist_parts(setlist, "pdf"),
        audio_parts=setlist_parts(setlist, "audio"),
        export_url=f"/user/setlists/{setlist.id}/export.pdf",
    )


@router.get("/setlists/{setlist_id}/export.pdf")
async def setlist_export(request: Request, setlist_id: int, db: AsyncSession = Depends(get_db)):
    member = await load_member(request, db)
    if not member:
        return go("/login")

    setlist = await load_setlist(db, setlist_id, member.choir_id, with_files=True)
    if not setlist:
        raise HTTPException(404, "Setlisten findes ikke")
    return await export_setlist_response(setlist, request.query_params)


@router.get("/account", response_class=HTMLResponse)
async def account(request: Request, db: AsyncSession = Depends(get_db)):
    member = await load_member(request, db)
    if not member:
        return go("/login")
    return render(request, "user/account.html", member=member, choir=await db.get(Choir, member.choir_id))


@router.post("/account/password")
async def change_password(
    request: Request,
    current: str = Form(...),
    new: str = Form(...),
    confirm: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    member = await load_member(request, db)
    if not member:
        return go("/login")

    if not verify_password(current, member.password):
        flash(request, "Den nuværende adgangskode er forkert", "error")
    elif len(new) < 8:
        flash(request, "Den nye adgangskode skal være mindst 8 tegn", "error")
    elif new != confirm:
        flash(request, "De to nye adgangskoder er ikke ens", "error")
    else:
        member.password = hash_password(new)
        await db.commit()
        flash(request, "Din adgangskode er ændret")
    return go("/user/account")
