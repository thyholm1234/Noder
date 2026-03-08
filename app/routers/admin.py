import os
from datetime import date
from typing import Optional
from fastapi import APIRouter, Request, Form, UploadFile, File, Depends
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, delete, update
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Choir, User, Song, SongFile, ChoirSong, Setlist, SetlistItem, SongLink
from app.auth import admin_required
from app.utils import save_upload, serve_file_with_range, AUDIO_EXTENSIONS, check_quota

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    redirect = admin_required(request)
    if redirect:
        return redirect

    choir_count = await db.scalar(select(func.count(Choir.id)))
    user_count = await db.scalar(select(func.count(User.id)))
    song_count = await db.scalar(select(func.count(Song.id)))
 
    # Recent songs
    result = await db.execute(select(Song).order_by(Song.created_at.desc()).limit(5))
    recent_songs = result.scalars().all()

    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "choir_count": choir_count,
            "user_count": user_count,
            "song_count": song_count,
            "recent_songs": recent_songs,
        },
    )


# ---------------------------------------------------------------------------
# Choirs
# ---------------------------------------------------------------------------
@router.get("/choirs", response_class=HTMLResponse)
async def choirs_list(request: Request, db: AsyncSession = Depends(get_db)):
    redirect = admin_required(request)
    if redirect:
        return redirect

    result = await db.execute(
        select(Choir).options(selectinload(Choir.users), selectinload(Choir.choir_songs)).order_by(Choir.name)
    )
    choirs = result.scalars().all()
    flash = request.session.pop("flash", None)

    return templates.TemplateResponse(
        "admin/choirs.html", {"request": request, "choirs": choirs, "flash": flash}
    )


@router.post("/choirs")
async def choir_create(request: Request, name: str = Form(...), db: AsyncSession = Depends(get_db)):
    redirect = admin_required(request)
    if redirect:
        return redirect

    choir = Choir(name=name.strip())
    db.add(choir)
    await db.commit()
    request.session["flash"] = f'Kor "{name}" oprettet'
    return RedirectResponse("/admin/choirs", status_code=303)


@router.get("/choirs/{choir_id}", response_class=HTMLResponse)
async def choir_detail(request: Request, choir_id: int, db: AsyncSession = Depends(get_db)):
    redirect = admin_required(request)
    if redirect:
        return redirect

    result = await db.execute(
        select(Choir)
        .options(
            selectinload(Choir.users),
            selectinload(Choir.choir_songs).selectinload(ChoirSong.song),
            selectinload(Choir.setlists).selectinload(Setlist.items).selectinload(SetlistItem.song),
        )
        .where(Choir.id == choir_id)
    )
    choir = result.scalar_one_or_none()
    if not choir:
        return RedirectResponse("/admin/choirs", status_code=303)

    # Get songs NOT yet assigned to this choir
    assigned_ids = [cs.song_id for cs in choir.choir_songs]
    if assigned_ids:
        avail_result = await db.execute(select(Song).where(Song.id.notin_(assigned_ids)).order_by(Song.title))
    else:
        avail_result = await db.execute(select(Song).order_by(Song.title))
    available_songs = avail_result.scalars().all()

    # All assigned songs (for setlist management)
    assigned_songs = [cs.song for cs in choir.choir_songs]

    flash = request.session.pop("flash", None)

    return templates.TemplateResponse(
        "admin/choir_detail.html",
        {
            "request": request,
            "choir": choir,
            "available_songs": available_songs,
            "assigned_songs": assigned_songs,
            "flash": flash,
            "today": date.today(),
        },
    )


@router.post("/choirs/{choir_id}/delete")
async def choir_delete(request: Request, choir_id: int, db: AsyncSession = Depends(get_db)):
    redirect = admin_required(request)
    if redirect:
        return redirect

    await db.execute(delete(Choir).where(Choir.id == choir_id))
    await db.commit()
    request.session["flash"] = "Kor slettet"
    return RedirectResponse("/admin/choirs", status_code=303)


@router.post("/choirs/{choir_id}/add-user")
async def choir_add_user(
    request: Request,
    choir_id: int,
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    user = User(username=username.strip(), password=password, choir_id=choir_id)
    db.add(user)
    await db.commit()
    request.session["flash"] = f'Bruger "{username}" oprettet'
    return RedirectResponse(f"/admin/choirs/{choir_id}", status_code=303)


@router.post("/choirs/{choir_id}/assign-song")
async def choir_assign_song(
    request: Request,
    choir_id: int,
    song_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    cs = ChoirSong(choir_id=choir_id, song_id=song_id)
    db.add(cs)
    await db.commit()
    request.session["flash"] = "Sang tildelt"
    return RedirectResponse(f"/admin/choirs/{choir_id}", status_code=303)


@router.post("/choirs/{choir_id}/unassign-song/{song_id}")
async def choir_unassign_song(
    request: Request, choir_id: int, song_id: int, db: AsyncSession = Depends(get_db)
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    await db.execute(
        delete(ChoirSong).where(ChoirSong.choir_id == choir_id, ChoirSong.song_id == song_id)
    )
    await db.commit()
    request.session["flash"] = "Sang fjernet fra kor"
    return RedirectResponse(f"/admin/choirs/{choir_id}", status_code=303)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
@router.post("/users/{user_id}/delete")
async def user_delete(request: Request, user_id: int, db: AsyncSession = Depends(get_db)):
    redirect = admin_required(request)
    if redirect:
        return redirect

    await db.execute(delete(User).where(User.id == user_id))
    await db.commit()
    request.session["flash"] = "Bruger slettet"
    referer = request.headers.get("referer", "/admin/choirs")
    return RedirectResponse(referer, status_code=303)


# ---------------------------------------------------------------------------
# Songs
# ---------------------------------------------------------------------------
PER_PAGE = 25


@router.get("/songs", response_class=HTMLResponse)
async def songs_list(
    request: Request,
    q: str = "",
    sort: str = "title",
    order: str = "asc",
    page: int = 1,
    db: AsyncSession = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    # Filter predicate (reused for count + data query)
    filter_clause = None
    if q:
        filter_clause = (
            Song.title.ilike(f"%{q}%") |
            Song.composer.ilike(f"%{q}%") |
            Song.tags.ilike(f"%{q}%")
        )

    # Total count for pagination
    count_stmt = select(func.count(Song.id))
    if filter_clause is not None:
        count_stmt = count_stmt.where(filter_clause)
    total = await db.scalar(count_stmt)
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(1, min(page, total_pages))

    # Sorting
    sort_map = {
        "title": Song.title,
        "composer": Song.composer,
        "tags": Song.tags,
        "created": Song.created_at,
    }
    sort_col = sort_map.get(sort, Song.title)
    sort_expr = sort_col.desc() if order == "desc" else sort_col.asc()

    data_stmt = (
        select(Song)
        .options(selectinload(Song.files), selectinload(Song.choir_songs))
        .order_by(sort_expr)
        .offset((page - 1) * PER_PAGE)
        .limit(PER_PAGE)
    )
    if filter_clause is not None:
        data_stmt = data_stmt.where(filter_clause)

    result = await db.execute(data_stmt)
    songs = result.scalars().all()
    flash = request.session.pop("flash", None)

    return templates.TemplateResponse(
        "admin/songs.html",
        {
            "request": request,
            "songs": songs,
            "flash": flash,
            "q": q,
            "sort": sort,
            "order": order,
            "page": page,
            "total_pages": total_pages,
            "total": total,
        },
    )


@router.post("/songs")
async def song_create(
    request: Request,
    title: str = Form(...),
    composer: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    song = Song(title=title.strip(), composer=composer.strip() or None)
    db.add(song)
    await db.commit()
    await db.refresh(song)
    request.session["flash"] = f'Sang "{title}" oprettet'
    return RedirectResponse(f"/admin/songs/{song.id}", status_code=303)


@router.get("/songs/{song_id}", response_class=HTMLResponse)
async def song_detail(request: Request, song_id: int, db: AsyncSession = Depends(get_db)):
    redirect = admin_required(request)
    if redirect:
        return redirect

    result = await db.execute(
        select(Song)
        .options(
            selectinload(Song.files),
            selectinload(Song.choir_songs).selectinload(ChoirSong.choir),
            selectinload(Song.links),
        )
        .where(Song.id == song_id)
    )
    song = result.scalar_one_or_none()
    if not song:
        return RedirectResponse("/admin/songs", status_code=303)

    # All choirs — show unassigned ones in the assign form
    assigned_ids = [cs.choir_id for cs in song.choir_songs]
    avail_result = await db.execute(select(Choir).order_by(Choir.name))
    all_choirs = avail_result.scalars().all()
    available_choirs = [c for c in all_choirs if c.id not in assigned_ids]

    flash = request.session.pop("flash", None)

    pdf_files = [f for f in song.files if f.file_type == "pdf"]
    audio_files = [f for f in song.files if f.file_type == "audio"]

    return templates.TemplateResponse(
        "admin/song_detail.html",
        {
            "request": request,
            "song": song,
            "pdf_files": pdf_files,
            "audio_files": audio_files,
            "available_choirs": available_choirs,
            "flash": flash,
        },
    )


@router.post("/songs/{song_id}/upload")
async def song_upload_file(
    request: Request,
    song_id: int,
    file_type: str = Form(...),
    part_name: str = Form(""),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    if not file.filename:
        request.session["flash"] = "Ingen fil valgt"
        return RedirectResponse(f"/admin/songs/{song_id}", status_code=303)

    # Read file content once for quota check
    content = await file.read()
    if check_quota(len(content)):
        request.session["flash"] = "Ikke nok plads — slet filer eller forhøj kvota"
        return RedirectResponse(f"/admin/songs/{song_id}", status_code=303)
    await file.seek(0)

    try:
        file_path = await save_upload(file, file_type)
    except RuntimeError as e:
        request.session["flash"] = f"Fejl ved konvertering: {e}"
        return RedirectResponse(f"/admin/songs/{song_id}", status_code=303)

    song_file = SongFile(
        song_id=song_id,
        file_type=file_type,
        part_name=part_name.strip() or None,
        file_path=file_path,
        original_filename=file.filename,
    )
    db.add(song_file)
    await db.commit()
    request.session["flash"] = f'Fil "{file.filename}" uploadet'
    return RedirectResponse(f"/admin/songs/{song_id}", status_code=303)


@router.post("/songs/{song_id}/update")
async def song_update(
    request: Request,
    song_id: int,
    title: str = Form(...),
    composer: str = Form(""),
    tags: str = Form(""),
    description: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    result = await db.execute(select(Song).where(Song.id == song_id))
    song = result.scalar_one_or_none()
    if song:
        song.title = title.strip()
        song.composer = composer.strip() or None
        song.tags = tags.strip() or None
        song.description = description.strip() or None
        await db.commit()
        request.session["flash"] = "Sanginformation opdateret"
    return RedirectResponse(f"/admin/songs/{song_id}", status_code=303)


@router.post("/songs/{song_id}/links")
async def song_link_add(
    request: Request,
    song_id: int,
    url: str = Form(...),
    label: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    link = SongLink(song_id=song_id, url=url.strip(), label=label.strip() or None)
    db.add(link)
    await db.commit()
    request.session["flash"] = "Link tilføjet"
    return RedirectResponse(f"/admin/songs/{song_id}", status_code=303)


@router.post("/songs/{song_id}/links/{link_id}/delete")
async def song_link_delete(
    request: Request,
    song_id: int,
    link_id: int,
    db: AsyncSession = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    await db.execute(delete(SongLink).where(SongLink.id == link_id, SongLink.song_id == song_id))
    await db.commit()
    request.session["flash"] = "Link slettet"
    return RedirectResponse(f"/admin/songs/{song_id}", status_code=303)


@router.post("/songs/{song_id}/assign")
async def song_assign_choir(
    request: Request,
    song_id: int,
    db: AsyncSession = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    form = await request.form()
    choir_ids = [int(v) for v in form.getlist("choir_id") if v]
    for choir_id in choir_ids:
        cs = ChoirSong(choir_id=choir_id, song_id=song_id)
        db.add(cs)
    if choir_ids:
        await db.commit()
        request.session["flash"] = f"{len(choir_ids)} kor tildelt"
    return RedirectResponse(f"/admin/songs/{song_id}", status_code=303)


@router.post("/songs/{song_id}/unassign/{choir_id}")
async def song_unassign_choir(
    request: Request, song_id: int, choir_id: int, db: AsyncSession = Depends(get_db)
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    await db.execute(
        delete(ChoirSong).where(ChoirSong.choir_id == choir_id, ChoirSong.song_id == song_id)
    )
    await db.commit()
    request.session["flash"] = "Kor fjernet fra sang"
    return RedirectResponse(f"/admin/songs/{song_id}", status_code=303)


@router.post("/songs/{song_id}/delete")
async def song_delete(request: Request, song_id: int, db: AsyncSession = Depends(get_db)):
    redirect = admin_required(request)
    if redirect:
        return redirect

    # Delete associated files from disk
    result = await db.execute(select(SongFile).where(SongFile.song_id == song_id))
    for sf in result.scalars().all():
        if os.path.exists(sf.file_path):
            os.remove(sf.file_path)

    await db.execute(delete(Song).where(Song.id == song_id))
    await db.commit()
    request.session["flash"] = "Sang slettet"
    return RedirectResponse("/admin/songs", status_code=303)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
@router.post("/files/{file_id}/delete")
async def file_delete(request: Request, file_id: int, db: AsyncSession = Depends(get_db)):
    redirect = admin_required(request)
    if redirect:
        return redirect

    result = await db.execute(select(SongFile).where(SongFile.id == file_id))
    sf = result.scalar_one_or_none()
    if sf:
        if os.path.exists(sf.file_path):
            os.remove(sf.file_path)
        song_id = sf.song_id
        await db.delete(sf)
        await db.commit()
        request.session["flash"] = "Fil slettet"
        return RedirectResponse(f"/admin/songs/{song_id}", status_code=303)

    return RedirectResponse("/admin/songs", status_code=303)


@router.post("/files/{file_id}/edit")
async def file_edit(
    request: Request,
    file_id: int,
    display_name: str = Form(""),
    file: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    result = await db.execute(select(SongFile).where(SongFile.id == file_id))
    sf = result.scalar_one_or_none()
    if not sf:
        return RedirectResponse("/admin/songs", status_code=303)

    sf.part_name = display_name.strip() or None

    if file and file.filename:
        file_content = await file.read()
        if check_quota(len(file_content)):
            request.session["flash"] = "Ikke nok plads — slet filer eller forhøj kvota"
            return RedirectResponse(f"/admin/songs/{sf.song_id}", status_code=303)
        await file.seek(0)
        try:
            new_path = await save_upload(file, sf.file_type)
            if os.path.exists(sf.file_path):
                os.remove(sf.file_path)
            sf.file_path = new_path
            sf.original_filename = file.filename
        except RuntimeError as e:
            request.session["flash"] = f"Fejl ved upload: {e}"
            return RedirectResponse(f"/admin/songs/{sf.song_id}", status_code=303)

    await db.commit()
    request.session["flash"] = "Fil opdateret"
    return RedirectResponse(f"/admin/songs/{sf.song_id}", status_code=303)


@router.get("/files/{file_id}")
async def file_serve(request: Request, file_id: int, download: int = 0, db: AsyncSession = Depends(get_db)):
    redirect = admin_required(request)
    if redirect:
        return redirect

    result = await db.execute(select(SongFile).where(SongFile.id == file_id))
    sf = result.scalar_one_or_none()
    if not sf:
        return RedirectResponse("/admin/songs", status_code=303)

    return await serve_file_with_range(sf.file_path, request, as_download=bool(download))


# ---------------------------------------------------------------------------
# Highlight songs (per choir)
# ---------------------------------------------------------------------------
@router.post("/choirs/{choir_id}/highlight/{song_id}")
async def choir_highlight_song(
    request: Request,
    choir_id: int,
    song_id: int,
    highlight_start: str = Form(""),
    highlight_end: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    result = await db.execute(
        select(ChoirSong).where(ChoirSong.choir_id == choir_id, ChoirSong.song_id == song_id)
    )
    cs = result.scalar_one_or_none()
    if not cs:
        return RedirectResponse(f"/admin/choirs/{choir_id}", status_code=303)

    cs.highlight_start = date.fromisoformat(highlight_start) if highlight_start else None
    cs.highlight_end = date.fromisoformat(highlight_end) if highlight_end else None
    await db.commit()
    request.session["flash"] = "Highlight opdateret"
    return RedirectResponse(f"/admin/choirs/{choir_id}", status_code=303)


# ---------------------------------------------------------------------------
# Setlists
# ---------------------------------------------------------------------------
@router.post("/choirs/{choir_id}/setlists")
async def setlist_create(
    request: Request,
    choir_id: int,
    name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    setlist = Setlist(choir_id=choir_id, name=name.strip())
    db.add(setlist)
    await db.commit()
    request.session["flash"] = f'Setliste "{name}" oprettet'
    return RedirectResponse(f"/admin/choirs/{choir_id}", status_code=303)


@router.post("/choirs/{choir_id}/setlists/{setlist_id}/add")
async def setlist_add_song(
    request: Request,
    choir_id: int,
    setlist_id: int,
    song_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    # Get max position
    max_pos = await db.scalar(
        select(func.coalesce(func.max(SetlistItem.position), -1)).where(SetlistItem.setlist_id == setlist_id)
    )
    item = SetlistItem(setlist_id=setlist_id, song_id=song_id, position=max_pos + 1)
    db.add(item)
    await db.commit()
    request.session["flash"] = "Sang tilføjet til setliste"
    return RedirectResponse(f"/admin/choirs/{choir_id}", status_code=303)


@router.post("/choirs/{choir_id}/setlists/{setlist_id}/remove-item/{item_id}")
async def setlist_remove_item(
    request: Request,
    choir_id: int,
    setlist_id: int,
    item_id: int,
    db: AsyncSession = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    await db.execute(
        delete(SetlistItem).where(SetlistItem.id == item_id, SetlistItem.setlist_id == setlist_id)
    )
    await db.commit()
    request.session["flash"] = "Fjernet fra setliste"
    return RedirectResponse(f"/admin/choirs/{choir_id}", status_code=303)


@router.post("/choirs/{choir_id}/setlists/{setlist_id}/add-placeholder")
async def setlist_add_placeholder(
    request: Request,
    choir_id: int,
    setlist_id: int,
    title: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    max_pos = await db.scalar(
        select(func.coalesce(func.max(SetlistItem.position), -1)).where(SetlistItem.setlist_id == setlist_id)
    )
    item = SetlistItem(setlist_id=setlist_id, song_id=None, placeholder_text=title.strip(), position=max_pos + 1)
    db.add(item)
    await db.commit()
    request.session["flash"] = "Separator tilføjet"
    return RedirectResponse(f"/admin/choirs/{choir_id}", status_code=303)


@router.post("/choirs/{choir_id}/setlists/{setlist_id}/reorder")
async def setlist_reorder(
    request: Request,
    choir_id: int,
    setlist_id: int,
    db: AsyncSession = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    body = await request.json()
    order = body.get("order", [])
    for pos, item_id in enumerate(order):
        await db.execute(
            update(SetlistItem)
            .where(SetlistItem.id == int(item_id), SetlistItem.setlist_id == setlist_id)
            .values(position=pos)
        )
    await db.commit()
    return {"ok": True}


@router.post("/choirs/{choir_id}/setlists/{setlist_id}/delete")
async def setlist_delete(
    request: Request,
    choir_id: int,
    setlist_id: int,
    db: AsyncSession = Depends(get_db),
):
    redirect = admin_required(request)
    if redirect:
        return redirect

    await db.execute(delete(Setlist).where(Setlist.id == setlist_id))
    await db.commit()
    request.session["flash"] = "Setliste slettet"
    return RedirectResponse(f"/admin/choirs/{choir_id}", status_code=303)


# ---------------------------------------------------------------------------
# Admin preview of choir member dashboard
# ---------------------------------------------------------------------------
@router.get("/choirs/{choir_id}/preview", response_class=HTMLResponse)
async def choir_preview(request: Request, choir_id: int, db: AsyncSession = Depends(get_db)):
    redirect = admin_required(request)
    if redirect:
        return redirect

    choir_result = await db.execute(select(Choir).where(Choir.id == choir_id))
    choir = choir_result.scalar_one_or_none()
    if not choir:
        return RedirectResponse("/admin/choirs", status_code=303)

    today = date.today()

    # All songs assigned to choir with highlight info
    result = await db.execute(
        select(Song, ChoirSong)
        .join(ChoirSong, ChoirSong.song_id == Song.id)
        .where(ChoirSong.choir_id == choir_id)
        .order_by(Song.title)
    )
    rows = result.all()

    highlighted = []
    songs = []
    for song, cs in rows:
        is_hl = (
            cs.highlight_start and cs.highlight_end
            and cs.highlight_start <= today <= cs.highlight_end
        )
        songs.append((song, cs.assigned_at))
        if is_hl:
            highlighted.append(song)

    # Setlists
    sl_result = await db.execute(
        select(Setlist)
        .options(selectinload(Setlist.items).selectinload(SetlistItem.song))
        .where(Setlist.choir_id == choir_id)
        .order_by(Setlist.name)
    )
    setlists = sl_result.scalars().all()

    return templates.TemplateResponse(
        "admin/choir_preview.html",
        {
            "request": request,
            "choir": choir,
            "songs": songs,
            "highlighted": highlighted,
            "setlists": setlists,
        },
    )
