import os
import re
from datetime import date
from typing import Optional
from fastapi import APIRouter, Request, Form, UploadFile, File, Depends, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy import select, func, delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ADMIN_USERNAME
from app.database import get_db
from app.models import Admin, Choir, User, Song, SongFile, ChoirSong, Setlist, SetlistItem, SongLink
from app.auth import admin_required, admin_id, flash, hash_password, generate_password
from app.services import (load_setlist, setlist_sort_key, setlist_parts, export_setlist_response, next_event)
from app.templating import render
from app.utils import (save_upload, serve_file_with_range, detect_file_type, part_name_from_filename,
                       safe_external_url, remove_stored_file, download_name, UploadError,
                       leader_quota, leader_usage)

router = APIRouter()


def go(url: str):
    return RedirectResponse(url, status_code=303)


async def _quota_room(db: AsyncSession, request: Request) -> int:
    """Bytes the logged-in choir leader can still upload within their own quota."""
    leader = await db.get(Admin, admin_id(request))
    return max(await leader_quota(db, leader) - sum((await leader_usage(db, leader.id)).values()), 0)


def parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def clean_tags(value: str) -> str | None:
    tags = []
    for t in value.split(","):
        t = t.strip()
        if t and t.lower() not in (x.lower() for x in tags):
            tags.append(t)
    return ", ".join(tags)[:500] or None


# --- Ownership: a choir leader only ever sees and touches their own choirs, songs and members ---
async def own_choir(db: AsyncSession, request: Request, choir_id: int) -> Choir | None:
    return await db.scalar(select(Choir).where(Choir.id == choir_id, Choir.owner_id == admin_id(request)))


async def own_song(db: AsyncSession, request: Request, song_id: int) -> Song | None:
    return await db.scalar(select(Song).where(Song.id == song_id, Song.owner_id == admin_id(request)))


async def own_user(db: AsyncSession, request: Request, user_id: int) -> User | None:
    return await db.scalar(
        select(User).join(Choir, Choir.id == User.choir_id).where(User.id == user_id, Choir.owner_id == admin_id(request))
    )


async def own_file(db: AsyncSession, request: Request, file_id: int) -> SongFile | None:
    return await db.scalar(
        select(SongFile).join(Song, Song.id == SongFile.song_id).where(SongFile.id == file_id, Song.owner_id == admin_id(request))
    )


async def own_song_ids(db: AsyncSession, request: Request, ids) -> set[int]:
    if not ids:
        return set()
    return set((await db.execute(select(Song.id).where(Song.id.in_(ids), Song.owner_id == admin_id(request)))).scalars())


async def own_choir_ids(db: AsyncSession, request: Request, ids) -> set[int]:
    if not ids:
        return set()
    return set((await db.execute(select(Choir.id).where(Choir.id.in_(ids), Choir.owner_id == admin_id(request)))).scalars())


async def taken_usernames(db: AsyncSession) -> set[str]:
    """Login names that can't be given to a member: other members, choir leaders and the superadmin."""
    names = set((await db.execute(select(func.lower(User.username)))).scalars())
    names |= set((await db.execute(select(func.lower(Admin.username)))).scalars())
    names.add(ADMIN_USERNAME.lower())
    return names


async def _choir_setlist(db: AsyncSession, request: Request, choir_id: int, setlist_id: int) -> Setlist:
    if not await own_choir(db, request, choir_id):
        raise HTTPException(404, "Koret findes ikke")
    setlist = await db.scalar(select(Setlist).where(Setlist.id == setlist_id, Setlist.choir_id == choir_id))
    if not setlist:
        raise HTTPException(404, "Setlisten findes ikke")
    return setlist


async def _unassign(db: AsyncSession, choir_id: int, song_id: int) -> None:
    """Remove a song from a choir — and from that choir's setlists, where members could no longer open it."""
    await db.execute(delete(ChoirSong).where(ChoirSong.choir_id == choir_id, ChoirSong.song_id == song_id))
    await db.execute(
        delete(SetlistItem).where(
            SetlistItem.song_id == song_id,
            SetlistItem.setlist_id.in_(select(Setlist.id).where(Setlist.choir_id == choir_id)),
        )
    )
    await db.commit()


async def _next_position(db: AsyncSession, setlist_id: int) -> int:
    max_pos = await db.scalar(
        select(func.coalesce(func.max(SetlistItem.position), -1)).where(SetlistItem.setlist_id == setlist_id)
    )
    return max_pos + 1


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    me = admin_id(request)
    choir_count = await db.scalar(select(func.count(Choir.id)).where(Choir.owner_id == me))
    user_count = await db.scalar(select(func.count(User.id)).join(Choir, Choir.id == User.choir_id).where(Choir.owner_id == me))
    song_count = await db.scalar(select(func.count(Song.id)).where(Song.owner_id == me))
    setlist_count = await db.scalar(
        select(func.count(Setlist.id)).join(Choir, Choir.id == Setlist.choir_id).where(Choir.owner_id == me))

    recent_songs = (await db.execute(
        select(Song).where(Song.owner_id == me).order_by(Song.created_at.desc()).limit(5))).scalars().all()

    upcoming = (await db.execute(
        select(Setlist)
        .join(Choir, Choir.id == Setlist.choir_id)
        .options(selectinload(Setlist.choir), selectinload(Setlist.items))
        .where(Choir.owner_id == me, Setlist.event_date >= date.today())
        .order_by(Setlist.event_date)
        .limit(6)
    )).scalars().all()

    # Songs that members can see but that have no sheet music yet
    has_pdf = select(SongFile.song_id).where(SongFile.file_type == "pdf")
    missing_pdf = (await db.execute(
        select(Song)
        .where(Song.owner_id == me, Song.id.notin_(has_pdf), Song.id.in_(select(ChoirSong.song_id)))
        .order_by(Song.title)
        .limit(8)
    )).scalars().all()

    return render(
        request, "admin/dashboard.html",
        choir_count=choir_count, user_count=user_count, song_count=song_count, setlist_count=setlist_count,
        recent_songs=recent_songs, upcoming=upcoming, missing_pdf=missing_pdf,
    )


# ---------------------------------------------------------------------------
# Choirs
# ---------------------------------------------------------------------------
@router.get("/choirs", response_class=HTMLResponse)
async def choirs_list(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    result = await db.execute(
        select(Choir)
        .options(selectinload(Choir.users), selectinload(Choir.choir_songs), selectinload(Choir.setlists))
        .where(Choir.owner_id == admin_id(request))
        .order_by(Choir.name)
    )
    return render(request, "admin/choirs.html", choirs=result.scalars().all())


@router.post("/choirs")
async def choir_create(request: Request, name: str = Form(...), db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    name = name.strip()[:255]
    if not name:
        flash(request, "Koret skal have et navn", "error")
        return go("/admin/choirs")
    choir = Choir(name=name, owner_id=admin_id(request))
    db.add(choir)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        flash(request, f'Der findes allerede et kor ved navn "{name}"', "error")
        return go("/admin/choirs")
    flash(request, f'Kor "{name}" oprettet')
    return go(f"/admin/choirs/{choir.id}")


@router.get("/choirs/{choir_id}", response_class=HTMLResponse)
async def choir_detail(request: Request, choir_id: int, db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    result = await db.execute(
        select(Choir)
        .options(
            selectinload(Choir.users),
            selectinload(Choir.choir_songs).selectinload(ChoirSong.song),
            selectinload(Choir.setlists).selectinload(Setlist.items).selectinload(SetlistItem.song)
            .selectinload(Song.files),
        )
        .where(Choir.id == choir_id, Choir.owner_id == admin_id(request))
    )
    choir = result.scalar_one_or_none()
    if not choir:
        return go("/admin/choirs")

    assigned_ids = {cs.song_id for cs in choir.choir_songs}
    all_songs = (await db.execute(
        select(Song).where(Song.owner_id == admin_id(request)).order_by(Song.title))).scalars().all()
    available_songs = [s for s in all_songs if s.id not in assigned_ids]
    assigned_songs = sorted((cs.song for cs in choir.choir_songs), key=lambda s: s.title.lower())
    choir_songs = sorted(choir.choir_songs, key=lambda cs: cs.song.title.lower())
    setlists = sorted(choir.setlists, key=setlist_sort_key)

    return render(
        request, "admin/choir_detail.html",
        choir=choir,
        users=sorted(choir.users, key=lambda u: u.username.lower()),
        choir_songs=choir_songs,
        available_songs=available_songs,
        assigned_songs=assigned_songs,
        setlists=setlists,
        setlist_pdf_parts={sl.id: setlist_parts(sl, "pdf") for sl in setlists},
        other_choirs=(await db.execute(
            select(Choir).where(Choir.id != choir_id, Choir.owner_id == admin_id(request)).order_by(Choir.name))).scalars().all(),
    )


@router.post("/choirs/{choir_id}/rename")
async def choir_rename(request: Request, choir_id: int, name: str = Form(...), db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    choir = await own_choir(db, request, choir_id)
    name = name.strip()[:255]
    if choir and name:
        choir.name = name
        try:
            await db.commit()
            flash(request, "Koret er omdøbt")
        except IntegrityError:
            await db.rollback()
            flash(request, f'Der findes allerede et kor ved navn "{name}"', "error")
    return go(f"/admin/choirs/{choir_id}")


@router.post("/choirs/{choir_id}/delete")
async def choir_delete(request: Request, choir_id: int, db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    await db.execute(delete(Choir).where(Choir.id == choir_id, Choir.owner_id == admin_id(request)))
    await db.commit()
    flash(request, "Kor slettet")
    return go("/admin/choirs")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
_SPLIT = re.compile(r"\s*[;\t]\s*")


@router.post("/choirs/{choir_id}/add-user")
async def choir_add_user(
    request: Request,
    choir_id: int,
    users: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Add one user, or many at once: one per line, optionally 'navn; adgangskode'."""
    if redirect := admin_required(request):
        return redirect

    if not await own_choir(db, request, choir_id):
        return go("/admin/choirs")

    wanted: list[tuple[str, str]] = []
    if username.strip():
        wanted.append((username.strip(), password))
    for line in users.splitlines():
        if line.strip():
            name, pw = (_SPLIT.split(line.strip(), maxsplit=1) + [""])[:2]
            wanted.append((name.strip(), pw.strip()))

    existing = await taken_usernames(db)
    created, skipped = [], []
    for name, pw in wanted:
        name = name[:255]
        if not name or name.lower() in existing:
            skipped.append(name)
            continue
        pw = pw or generate_password()
        db.add(User(username=name, password=hash_password(pw), choir_id=choir_id))
        existing.add(name.lower())
        created.append({"username": name, "password": pw})

    await db.commit()
    if created:
        msg = f"{len(created)} bruger{'e' if len(created) != 1 else ''} oprettet — gem adgangskoderne nu, de vises kun denne ene gang."
        if skipped:
            msg += f" Sprunget over (findes allerede): {', '.join(skipped)}."
        flash(request, msg, "secret", credentials=created)
    else:
        flash(request, "Ingen brugere oprettet — brugernavnene findes allerede" if skipped else "Angiv mindst ét brugernavn", "error")
    return go(f"/admin/choirs/{choir_id}#members")


@router.post("/users/{user_id}/reset-password")
async def user_reset_password(
    request: Request, user_id: int, password: str = Form(""), db: AsyncSession = Depends(get_db)
):
    if redirect := admin_required(request):
        return redirect

    user = await own_user(db, request, user_id)
    if not user:
        return go("/admin/choirs")
    pw = password.strip() or generate_password()
    user.password = hash_password(pw)
    await db.commit()
    flash(request, f'Ny adgangskode til "{user.username}" — vises kun denne ene gang.', "secret",
          credentials=[{"username": user.username, "password": pw}])
    return go(f"/admin/choirs/{user.choir_id}#members")


@router.post("/users/{user_id}/rename")
async def user_rename(request: Request, user_id: int, username: str = Form(...), db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    user = await own_user(db, request, user_id)
    if not user:
        return go("/admin/choirs")
    new = username.strip()[:255]
    clash = new.lower() in (await taken_usernames(db)) - {user.username.lower()} or (
        await db.scalar(select(User.id).where(func.lower(User.username) == new.lower(), User.id != user_id)))
    if not new or clash:
        flash(request, f'Brugernavnet "{new}" er optaget', "error")
    else:
        user.username = new
        await db.commit()
        flash(request, f'Bruger omdøbt til "{new}"')
    return go(f"/admin/choirs/{user.choir_id}#members")


@router.post("/users/{user_id}/delete")
async def user_delete(request: Request, user_id: int, db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    user = await own_user(db, request, user_id)
    if not user:
        return go("/admin/choirs")
    choir_id = user.choir_id
    await db.delete(user)
    await db.commit()
    flash(request, "Bruger slettet")
    return go(f"/admin/choirs/{choir_id}#members")


# ---------------------------------------------------------------------------
# Choir ↔ song assignment
# ---------------------------------------------------------------------------
@router.post("/choirs/{choir_id}/assign-song")
async def choir_assign_song(request: Request, choir_id: int, db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    if not await own_choir(db, request, choir_id):
        return go("/admin/choirs")
    form = await request.form()
    song_ids = await own_song_ids(db, request, {int(v) for v in form.getlist("song_id") if str(v).isdigit()})
    already = set((await db.execute(select(ChoirSong.song_id).where(ChoirSong.choir_id == choir_id))).scalars())
    new_ids = song_ids - already
    for song_id in new_ids:
        db.add(ChoirSong(choir_id=choir_id, song_id=song_id))
    await db.commit()
    if new_ids:
        flash(request, f"{len(new_ids)} sang{'e' if len(new_ids) != 1 else ''} tildelt")
    return go(f"/admin/choirs/{choir_id}#songs")


@router.post("/choirs/{choir_id}/unassign-song/{song_id}")
async def choir_unassign_song(request: Request, choir_id: int, song_id: int, db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    if await own_choir(db, request, choir_id):
        await _unassign(db, choir_id, song_id)
        flash(request, "Sang fjernet fra koret")
    return go(f"/admin/choirs/{choir_id}#songs")


@router.post("/choirs/{choir_id}/highlight/{song_id}")
async def choir_highlight_song(
    request: Request,
    choir_id: int,
    song_id: int,
    highlight_start: str = Form(""),
    highlight_end: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if redirect := admin_required(request):
        return redirect

    cs = await db.scalar(
        select(ChoirSong).join(Choir, Choir.id == ChoirSong.choir_id)
        .where(ChoirSong.choir_id == choir_id, ChoirSong.song_id == song_id, Choir.owner_id == admin_id(request)))
    if not cs:
        return go(f"/admin/choirs/{choir_id}")

    start, end = parse_date(highlight_start), parse_date(highlight_end)
    if start and not end:
        end = start
    if start and end and end < start:
        start, end = end, start
    cs.highlight_start, cs.highlight_end = start, end
    await db.commit()
    flash(request, "Fokus opdateret" if start else "Fokus fjernet")
    return go(f"/admin/choirs/{choir_id}#songs")


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
    if redirect := admin_required(request):
        return redirect

    q = q.strip()
    filter_clause = Song.owner_id == admin_id(request)
    if q:
        like = f"%{q}%"
        filter_clause = filter_clause & (Song.title.ilike(like) | Song.composer.ilike(like) | Song.tags.ilike(like))

    count_stmt = select(func.count(Song.id)).where(filter_clause)
    total = await db.scalar(count_stmt)
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(1, min(page, total_pages))

    sort_map = {"title": Song.title, "composer": Song.composer, "tags": Song.tags, "created": Song.created_at}
    sort_col = sort_map.get(sort, Song.title)
    sort_expr = sort_col.desc().nulls_last() if order == "desc" else sort_col.asc().nulls_last()

    data_stmt = (
        select(Song)
        .options(selectinload(Song.files), selectinload(Song.choir_songs))
        .order_by(sort_expr, Song.id)
        .offset((page - 1) * PER_PAGE)
        .limit(PER_PAGE)
    )
    data_stmt = data_stmt.where(filter_clause)

    songs = (await db.execute(data_stmt)).scalars().all()

    return render(
        request, "admin/songs.html",
        songs=songs, q=q, sort=sort, order=order, page=page, total_pages=total_pages, total=total,
    )


@router.post("/songs")
async def song_create(
    request: Request,
    title: str = Form(...),
    composer: str = Form(""),
    tags: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if redirect := admin_required(request):
        return redirect

    title = title.strip()[:255]
    if not title:
        flash(request, "Sangen skal have en titel", "error")
        return go("/admin/songs")
    song = Song(title=title, composer=composer.strip()[:255] or None, tags=clean_tags(tags), owner_id=admin_id(request))
    db.add(song)
    await db.commit()
    flash(request, f'Sang "{title}" oprettet — upload noder og stemmer herunder')
    return go(f"/admin/songs/{song.id}")


@router.get("/songs/{song_id}", response_class=HTMLResponse)
async def song_detail(request: Request, song_id: int, db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    result = await db.execute(
        select(Song)
        .options(
            selectinload(Song.files),
            selectinload(Song.choir_songs).selectinload(ChoirSong.choir),
            selectinload(Song.links),
        )
        .where(Song.id == song_id, Song.owner_id == admin_id(request))
    )
    song = result.scalar_one_or_none()
    if not song:
        return go("/admin/songs")

    assigned_ids = {cs.choir_id for cs in song.choir_songs}
    all_choirs = (await db.execute(
        select(Choir).where(Choir.owner_id == admin_id(request)).order_by(Choir.name))).scalars().all()

    used_in = (await db.execute(
        select(Setlist)
        .options(selectinload(Setlist.choir))
        .join(SetlistItem, SetlistItem.setlist_id == Setlist.id)
        .where(SetlistItem.song_id == song_id)
    )).scalars().all()

    files = sorted(song.files, key=lambda f: f.id)
    return render(
        request, "admin/song_detail.html",
        song=song,
        pdf_files=[f for f in files if f.file_type == "pdf"],
        audio_files=[f for f in files if f.file_type == "audio"],
        available_choirs=[c for c in all_choirs if c.id not in assigned_ids],
        used_in=sorted(used_in, key=setlist_sort_key),
    )


@router.post("/songs/{song_id}/upload")
async def song_upload_file(
    request: Request,
    song_id: int,
    file_type: str = Form("auto"),
    part_name: str = Form(""),
    file: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
):
    if redirect := admin_required(request):
        return redirect

    if not await own_song(db, request, song_id):
        return go("/admin/songs")

    uploads = [f for f in file if f.filename]
    if not uploads:
        flash(request, "Ingen fil valgt", "error")
        return go(f"/admin/songs/{song_id}")

    room = await _quota_room(db, request)
    saved, errors = [], []
    for up in uploads:
        kind = detect_file_type(up.filename) if file_type == "auto" else file_type
        if kind is None:
            errors.append(f"{up.filename}: filtypen understøttes ikke")
            continue
        try:
            path = await save_upload(up, kind, room)
        except UploadError as e:
            errors.append(f"{up.filename}: {e}")
            continue
        room -= os.path.getsize(path)
        name = part_name.strip() if (part_name.strip() and len(uploads) == 1) else part_name_from_filename(up.filename)
        db.add(SongFile(song_id=song_id, file_type=kind, part_name=name or None,
                        file_path=path, original_filename=up.filename[:255]))
        saved.append(up.filename)
        await db.commit()

    if saved and not errors:
        flash(request, f"{len(saved)} fil{'er' if len(saved) != 1 else ''} uploadet")
    elif saved:
        flash(request, f"{len(saved)} uploadet, {len(errors)} fejlede: " + "; ".join(errors), "error")
    else:
        flash(request, "Upload fejlede: " + "; ".join(errors), "error")
    return go(f"/admin/songs/{song_id}#files")


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
    if redirect := admin_required(request):
        return redirect

    song = await own_song(db, request, song_id)
    if song and title.strip():
        song.title = title.strip()[:255]
        song.composer = composer.strip()[:255] or None
        song.tags = clean_tags(tags)
        song.description = description.strip() or None
        await db.commit()
        flash(request, "Sanginformation gemt")
    return go(f"/admin/songs/{song_id}")


@router.post("/songs/{song_id}/links")
async def song_link_add(
    request: Request,
    song_id: int,
    url: str = Form(...),
    label: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if redirect := admin_required(request):
        return redirect

    if not await own_song(db, request, song_id):
        return go("/admin/songs")
    safe = safe_external_url(url)
    if not safe:
        flash(request, "Linket skal starte med http:// eller https://", "error")
        return go(f"/admin/songs/{song_id}#links")
    db.add(SongLink(song_id=song_id, url=safe, label=label.strip()[:255] or None))
    await db.commit()
    flash(request, "Link tilføjet")
    return go(f"/admin/songs/{song_id}#links")


@router.post("/songs/{song_id}/links/{link_id}/delete")
async def song_link_delete(request: Request, song_id: int, link_id: int, db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    if not await own_song(db, request, song_id):
        return go("/admin/songs")
    await db.execute(delete(SongLink).where(SongLink.id == link_id, SongLink.song_id == song_id))
    await db.commit()
    flash(request, "Link slettet")
    return go(f"/admin/songs/{song_id}#links")


@router.post("/songs/{song_id}/assign")
async def song_assign_choir(request: Request, song_id: int, db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    if not await own_song(db, request, song_id):
        return go("/admin/songs")
    form = await request.form()
    choir_ids = await own_choir_ids(db, request, {int(v) for v in form.getlist("choir_id") if str(v).isdigit()})
    already = set((await db.execute(select(ChoirSong.choir_id).where(ChoirSong.song_id == song_id))).scalars())
    new_ids = choir_ids - already
    for choir_id in new_ids:
        db.add(ChoirSong(choir_id=choir_id, song_id=song_id))
    if new_ids:
        await db.commit()
        flash(request, f"Tildelt {len(new_ids)} kor")
    return go(f"/admin/songs/{song_id}#choirs")


@router.post("/songs/{song_id}/unassign/{choir_id}")
async def song_unassign_choir(request: Request, song_id: int, choir_id: int, db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    if await own_song(db, request, song_id) and await own_choir(db, request, choir_id):
        await _unassign(db, choir_id, song_id)
        flash(request, "Kor fjernet fra sang")
    return go(f"/admin/songs/{song_id}#choirs")


@router.post("/songs/{song_id}/delete")
async def song_delete(request: Request, song_id: int, db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    if not await own_song(db, request, song_id):
        return go("/admin/songs")
    paths = (await db.execute(select(SongFile.file_path).where(SongFile.song_id == song_id))).scalars().all()
    await db.execute(delete(Song).where(Song.id == song_id))
    await db.commit()
    for path in paths:  # only after the commit succeeded
        remove_stored_file(path)
    flash(request, "Sang slettet")
    return go("/admin/songs")


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
@router.post("/files/{file_id}/delete")
async def file_delete(request: Request, file_id: int, db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    sf = await own_file(db, request, file_id)
    if not sf:
        return go("/admin/songs")
    song_id, path = sf.song_id, sf.file_path
    await db.delete(sf)
    await db.commit()
    remove_stored_file(path)
    flash(request, "Fil slettet")
    return go(f"/admin/songs/{song_id}#files")


@router.post("/files/{file_id}/edit")
async def file_edit(
    request: Request,
    file_id: int,
    display_name: str = Form(""),
    file: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db),
):
    if redirect := admin_required(request):
        return redirect

    sf = await own_file(db, request, file_id)
    if not sf:
        return go("/admin/songs")

    sf.part_name = display_name.strip()[:255] or None

    old_path = None
    if file and file.filename:
        try:
            room = await _quota_room(db, request) + (os.path.getsize(sf.file_path) if os.path.isfile(sf.file_path) else 0)
            new_path = await save_upload(file, sf.file_type, room)
        except UploadError as e:
            flash(request, f"Fejl ved upload: {e}", "error")
            return go(f"/admin/songs/{sf.song_id}#files")
        old_path, sf.file_path, sf.original_filename = sf.file_path, new_path, file.filename[:255]

    await db.commit()
    if old_path:
        remove_stored_file(old_path)
    flash(request, "Fil opdateret")
    return go(f"/admin/songs/{sf.song_id}#files")


@router.get("/files/{file_id}")
async def file_serve(request: Request, file_id: int, download: int = 0, db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    sf = await own_file(db, request, file_id)
    if not sf:
        raise HTTPException(404, "Fil ikke fundet")
    return await serve_file_with_range(sf.file_path, request, as_download=bool(download),
                                       filename=download_name(sf.original_filename, sf.file_path))


# ---------------------------------------------------------------------------
# Setlists
# ---------------------------------------------------------------------------
@router.post("/choirs/{choir_id}/setlists")
async def setlist_create(
    request: Request,
    choir_id: int,
    name: str = Form(...),
    event_date: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if redirect := admin_required(request):
        return redirect

    name = name.strip()[:255]
    if not await own_choir(db, request, choir_id):
        return go("/admin/choirs")
    if not name:
        flash(request, "Setlisten skal have et navn", "error")
        return go(f"/admin/choirs/{choir_id}#setlists")
    setlist = Setlist(choir_id=choir_id, name=name, event_date=parse_date(event_date))
    db.add(setlist)
    await db.commit()
    flash(request, f'Setliste "{name}" oprettet')
    return go(f"/admin/choirs/{choir_id}#setlist-{setlist.id}")


@router.post("/choirs/{choir_id}/setlists/{setlist_id}/update")
async def setlist_update(
    request: Request,
    choir_id: int,
    setlist_id: int,
    name: str = Form(...),
    event_date: str = Form(""),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if redirect := admin_required(request):
        return redirect

    setlist = await _choir_setlist(db, request, choir_id, setlist_id)
    if name.strip():
        setlist.name = name.strip()[:255]
    setlist.event_date = parse_date(event_date)
    setlist.notes = notes.strip() or None
    await db.commit()
    flash(request, "Setliste gemt")
    return go(f"/admin/choirs/{choir_id}#setlist-{setlist_id}")


@router.post("/choirs/{choir_id}/setlists/{setlist_id}/duplicate")
async def setlist_duplicate(
    request: Request,
    choir_id: int,
    setlist_id: int,
    target_choir_id: int = Form(0),
    db: AsyncSession = Depends(get_db),
):
    """Copy a setlist — within the choir, or to another choir (songs get assigned there too)."""
    if redirect := admin_required(request):
        return redirect

    if not await own_choir(db, request, choir_id):
        raise HTTPException(404, "Koret findes ikke")
    source = await load_setlist(db, setlist_id, choir_id)
    if not source:
        raise HTTPException(404, "Setlisten findes ikke")
    target = target_choir_id or choir_id
    if not await own_choir(db, request, target):
        raise HTTPException(404, "Koret findes ikke")

    copy = Setlist(choir_id=target, name=(f"{source.name} (kopi)" if target == choir_id else source.name)[:255],
                   event_date=source.event_date, notes=source.notes)
    db.add(copy)
    await db.flush()

    assigned = set((await db.execute(select(ChoirSong.song_id).where(ChoirSong.choir_id == target))).scalars())
    for item in source.items:
        db.add(SetlistItem(setlist_id=copy.id, song_id=item.song_id,
                           placeholder_text=item.placeholder_text, position=item.position))
        if item.song_id and item.song_id not in assigned:
            db.add(ChoirSong(choir_id=target, song_id=item.song_id))
            assigned.add(item.song_id)
    await db.commit()
    flash(request, "Setliste kopieret")
    return go(f"/admin/choirs/{target}#setlist-{copy.id}")


@router.post("/choirs/{choir_id}/setlists/{setlist_id}/add")
async def setlist_add_song(request: Request, choir_id: int, setlist_id: int, db: AsyncSession = Depends(get_db)):
    """Add one or more songs. Songs not yet assigned to the choir are assigned automatically."""
    if redirect := admin_required(request):
        return redirect

    await _choir_setlist(db, request, choir_id, setlist_id)
    form = await request.form()
    song_ids = [int(v) for v in form.getlist("song_id") if str(v).isdigit()]

    in_list = set((await db.execute(select(SetlistItem.song_id).where(SetlistItem.setlist_id == setlist_id))).scalars())
    assigned = set((await db.execute(select(ChoirSong.song_id).where(ChoirSong.choir_id == choir_id))).scalars())
    pos = await _next_position(db, setlist_id)
    added, auto_assigned = 0, 0
    for song_id in dict.fromkeys(song_ids):
        if song_id in in_list or not await own_song(db, request, song_id):
            continue
        if song_id not in assigned:
            db.add(ChoirSong(choir_id=choir_id, song_id=song_id))
            assigned.add(song_id)
            auto_assigned += 1
        db.add(SetlistItem(setlist_id=setlist_id, song_id=song_id, position=pos))
        pos += 1
        added += 1
    await db.commit()

    if added:
        msg = f"{added} sang{'e' if added != 1 else ''} tilføjet"
        if auto_assigned:
            msg += f" ({auto_assigned} også tildelt koret)"
        flash(request, msg)
    return go(f"/admin/choirs/{choir_id}#setlist-{setlist_id}")


@router.post("/choirs/{choir_id}/setlists/{setlist_id}/add-placeholder")
async def setlist_add_placeholder(
    request: Request, choir_id: int, setlist_id: int, title: str = Form(""), db: AsyncSession = Depends(get_db)
):
    if redirect := admin_required(request):
        return redirect

    await _choir_setlist(db, request, choir_id, setlist_id)
    db.add(SetlistItem(setlist_id=setlist_id, song_id=None, placeholder_text=title.strip()[:255] or "Pause",
                       position=await _next_position(db, setlist_id)))
    await db.commit()
    flash(request, "Separator tilføjet")
    return go(f"/admin/choirs/{choir_id}#setlist-{setlist_id}")


@router.post("/choirs/{choir_id}/setlists/{setlist_id}/items/{item_id}/rename")
async def setlist_rename_placeholder(
    request: Request, choir_id: int, setlist_id: int, item_id: int, title: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if redirect := admin_required(request):
        return redirect

    await _choir_setlist(db, request, choir_id, setlist_id)
    await db.execute(
        update(SetlistItem)
        .where(SetlistItem.id == item_id, SetlistItem.setlist_id == setlist_id, SetlistItem.song_id.is_(None))
        .values(placeholder_text=title.strip()[:255] or "Pause")
    )
    await db.commit()
    return {"ok": True}


@router.post("/choirs/{choir_id}/setlists/{setlist_id}/remove-item/{item_id}")
async def setlist_remove_item(
    request: Request, choir_id: int, setlist_id: int, item_id: int, db: AsyncSession = Depends(get_db)
):
    if redirect := admin_required(request):
        return redirect

    await _choir_setlist(db, request, choir_id, setlist_id)
    await db.execute(delete(SetlistItem).where(SetlistItem.id == item_id, SetlistItem.setlist_id == setlist_id))
    await db.commit()
    flash(request, "Fjernet fra setlisten")
    return go(f"/admin/choirs/{choir_id}#setlist-{setlist_id}")


@router.post("/choirs/{choir_id}/setlists/{setlist_id}/reorder")
async def setlist_reorder(request: Request, choir_id: int, setlist_id: int, db: AsyncSession = Depends(get_db)):
    await _choir_setlist(db, request, choir_id, setlist_id)
    body = await request.json()
    order = body.get("order", []) if isinstance(body, dict) else []
    try:
        ids = [int(i) for i in order]
    except (TypeError, ValueError):
        raise HTTPException(400, "Ugyldig rækkefølge")
    for pos, item_id in enumerate(ids):
        await db.execute(
            update(SetlistItem)
            .where(SetlistItem.id == item_id, SetlistItem.setlist_id == setlist_id)
            .values(position=pos)
        )
    await db.commit()
    return {"ok": True}


@router.post("/choirs/{choir_id}/setlists/{setlist_id}/delete")
async def setlist_delete(request: Request, choir_id: int, setlist_id: int, db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    if not await own_choir(db, request, choir_id):
        return go("/admin/choirs")
    await db.execute(delete(Setlist).where(Setlist.id == setlist_id, Setlist.choir_id == choir_id))
    await db.commit()
    flash(request, "Setliste slettet")
    return go(f"/admin/choirs/{choir_id}#setlists")


@router.get("/choirs/{choir_id}/setlists/{setlist_id}/export.pdf")
async def setlist_export(request: Request, choir_id: int, setlist_id: int, db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    setlist = None
    if await own_choir(db, request, choir_id):
        setlist = await load_setlist(db, setlist_id, choir_id, with_files=True)
    if not setlist:
        raise HTTPException(404, "Setlisten findes ikke")
    return await export_setlist_response(setlist, request.query_params)


# ---------------------------------------------------------------------------
# Admin preview of choir member dashboard
# ---------------------------------------------------------------------------
@router.get("/choirs/{choir_id}/preview", response_class=HTMLResponse)
async def choir_preview(request: Request, choir_id: int, db: AsyncSession = Depends(get_db)):
    if redirect := admin_required(request):
        return redirect

    choir = await own_choir(db, request, choir_id)
    if not choir:
        return go("/admin/choirs")

    today = date.today()
    rows = (await db.execute(
        select(Song, ChoirSong)
        .join(ChoirSong, ChoirSong.song_id == Song.id)
        .where(ChoirSong.choir_id == choir_id)
        .order_by(Song.title)
    )).all()

    songs, highlighted = [], []
    for song, cs in rows:
        songs.append((song, cs.assigned_at))
        if cs.highlight_start and cs.highlight_end and cs.highlight_start <= today <= cs.highlight_end:
            highlighted.append((song, cs))

    setlists = (await db.execute(
        select(Setlist)
        .options(selectinload(Setlist.items).selectinload(SetlistItem.song))
        .where(Setlist.choir_id == choir_id)
    )).scalars().all()
    setlists = sorted(setlists, key=setlist_sort_key)

    return render(
        request, "admin/choir_preview.html",
        choir=choir, songs=songs, highlighted=highlighted, setlists=setlists, next_setlist=next_event(setlists),
    )
