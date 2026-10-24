"""Superadmin portal: create and manage choir leaders (korledere)."""
import os

from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import flash, hash_password, generate_password
from app.config import ADMIN_USERNAME, QUOTA_BYTES, _parse_quota
from app.database import get_db
from app.models import Admin, Choir, User, Song, SongFile
from app.templating import render
from app.utils import remove_stored_file, allocated_quota, _fmt

router = APIRouter()


def _fmt_input(b: int | None) -> str:
    """Quota as typed in the form (e.g. '500M'); empty = default."""
    if b is None:
        return ""
    for suffix, size in (("T", 1024**4), ("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if b % size == 0:
            return f"{b // size}{suffix}"
    return str(b)


def go(url: str):
    return RedirectResponse(url, status_code=303)


async def _username_problem(db: AsyncSession, name: str, exclude_id: int | None = None) -> str | None:
    """Why this login name can't be used for a choir leader (None = fine). Names are shared with members."""
    if not name:
        return "Angiv et brugernavn"
    low = name.lower()
    if low == ADMIN_USERNAME.lower():
        return f'Brugernavnet "{name}" er reserveret til superadministratoren'
    clash = select(Admin.id).where(func.lower(Admin.username) == low)
    if exclude_id:
        clash = clash.where(Admin.id != exclude_id)
    if await db.scalar(clash) or await db.scalar(select(User.id).where(func.lower(User.username) == low)):
        return f'Brugernavnet "{name}" er optaget'
    return None


@router.get("/", response_class=HTMLResponse)
async def leaders(request: Request, db: AsyncSession = Depends(get_db)):
    admins = (await db.execute(select(Admin).order_by(func.lower(Admin.username)))).scalars().all()

    choirs = dict((await db.execute(select(Choir.owner_id, func.count(Choir.id)).group_by(Choir.owner_id))).all())
    songs = dict((await db.execute(select(Song.owner_id, func.count(Song.id)).group_by(Song.owner_id))).all())
    members = dict((await db.execute(
        select(Choir.owner_id, func.count(User.id)).join(User, User.choir_id == Choir.id).group_by(Choir.owner_id)
    )).all())
    used: dict[int, int] = {}
    for owner_id, path in (await db.execute(
        select(Song.owner_id, SongFile.file_path).join(SongFile, SongFile.song_id == Song.id)
    )).all():
        try:
            used[owner_id] = used.get(owner_id, 0) + os.path.getsize(path)
        except OSError:
            pass

    pool = max(QUOTA_BYTES - await allocated_quota(db), 0)
    rows = [{"admin": a, "choirs": choirs.get(a.id, 0), "members": members.get(a.id, 0),
             "songs": songs.get(a.id, 0), "storage": _fmt(used.get(a.id, 0)), "quota": _fmt(min(a.quota_bytes, QUOTA_BYTES) if a.quota_bytes is not None else pool),
             "quota_input": _fmt_input(a.quota_bytes)} for a in admins]
    return render(request, "super/leaders.html", rows=rows, max_quota=_fmt(QUOTA_BYTES), free_quota=_fmt(pool), allocated=_fmt(QUOTA_BYTES - pool))


@router.post("/leaders")
async def leader_create(request: Request, username: str = Form(""), password: str = Form(""),
                        db: AsyncSession = Depends(get_db)):
    name = username.strip()[:255]
    if problem := await _username_problem(db, name):
        flash(request, problem, "error")
        return go("/super/")
    pw = password.strip() or generate_password()
    db.add(Admin(username=name, password=hash_password(pw)))
    await db.commit()
    flash(request, f'Korleder "{name}" oprettet — giv dem adgangskoden nu, den vises kun denne ene gang.', "secret",
          credentials=[{"username": name, "password": pw}])
    return go("/super/")


@router.post("/leaders/{admin_id}/rename")
async def leader_rename(request: Request, admin_id: int, username: str = Form(""), db: AsyncSession = Depends(get_db)):
    leader = await db.get(Admin, admin_id)
    if not leader:
        return go("/super/")
    name = username.strip()[:255]
    if problem := await _username_problem(db, name, exclude_id=admin_id):
        flash(request, problem, "error")
    else:
        leader.username = name
        await db.commit()
        flash(request, f'Korleder omdøbt til "{name}"')
    return go("/super/")


@router.post("/leaders/{admin_id}/reset-password")
async def leader_reset_password(request: Request, admin_id: int, password: str = Form(""),
                                db: AsyncSession = Depends(get_db)):
    leader = await db.get(Admin, admin_id)
    if not leader:
        return go("/super/")
    pw = password.strip() or generate_password()
    leader.password = hash_password(pw)
    await db.commit()
    flash(request, f'Ny adgangskode til "{leader.username}" — vises kun denne ene gang.', "secret",
          credentials=[{"username": leader.username, "password": pw}])
    return go("/super/")


@router.post("/leaders/{admin_id}/quota")
async def leader_quota_set(request: Request, admin_id: int, quota: str = Form(""), db: AsyncSession = Depends(get_db)):
    """Set a leader's storage limit within the QUOTA from .env. Empty = the full QUOTA."""
    leader = await db.get(Admin, admin_id)
    if not leader:
        return go("/super/")
    value = quota.strip()
    new = None
    if value:
        try:
            new = _parse_quota(value)
        except (ValueError, IndexError):
            flash(request, 'Ugyldig kvote — skriv fx "500M" eller "2G"', "error")
            return go("/super/")
        free = QUOTA_BYTES - await allocated_quota(db, exclude_id=admin_id)
        if new <= 0 or new > free:
            flash(request, f"Kvoten skal være mellem 1 B og {_fmt(max(free, 0))} — det er hvad der er tilbage af den "
                           f"samlede kvote på {_fmt(QUOTA_BYTES)} efter de andre korlederes kvoter", "error")
            return go("/super/")
    leader.quota_bytes = new
    await db.commit()
    flash(request, f'Kvote for "{leader.username}": {_fmt(new) if new else "resten af den samlede kvote"}')
    return go("/super/")


@router.post("/leaders/{admin_id}/delete")
async def leader_delete(request: Request, admin_id: int, db: AsyncSession = Depends(get_db)):
    """Removes the leader together with their choirs, members, songs and uploaded files."""
    leader = await db.get(Admin, admin_id)
    if not leader:
        return go("/super/")
    name = leader.username
    paths = (await db.execute(
        select(SongFile.file_path).join(Song, Song.id == SongFile.song_id).where(Song.owner_id == admin_id)
    )).scalars().all()
    await db.execute(delete(Admin).where(Admin.id == admin_id))  # FK cascades do the rest
    await db.commit()
    for path in paths:  # only after the commit succeeded
        remove_stored_file(path)
    flash(request, f'Korleder "{name}" er slettet sammen med deres kor, sange og filer')
    return go("/super/")
