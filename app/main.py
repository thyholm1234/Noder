from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from contextlib import asynccontextmanager
from sqlalchemy import select

from app.config import SECRET_KEY
from app.database import init_db, async_session
from app.models import User
from app.auth import verify_admin
from app.routers import admin, user
from app.utils import get_storage_usage


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Noder", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# Make storage usage available in all templates
templates.env.globals["get_storage"] = get_storage_usage

# Also inject into admin/user template instances
admin.templates.env.globals["get_storage"] = get_storage_usage
user.templates.env.globals["get_storage"] = get_storage_usage

app.include_router(admin.router, prefix="/admin")
app.include_router(user.router, prefix="/user")


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    role = request.session.get("role")
    if role == "admin":
        return RedirectResponse("/admin/dashboard", status_code=303)
    elif role == "user":
        return RedirectResponse("/user/dashboard", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    # Check admin credentials
    if verify_admin(username, password):
        request.session["role"] = "admin"
        request.session["user_id"] = None
        return RedirectResponse("/admin/dashboard", status_code=303)

    # Check regular user credentials
    async with async_session() as db:
        result = await db.execute(select(User).where(User.username == username))
        db_user = result.scalar_one_or_none()
        if db_user and db_user.password == password:
            request.session["role"] = "user"
            request.session["user_id"] = db_user.id
            request.session["choir_id"] = db_user.choir_id
            request.session["display_name"] = db_user.username
            return RedirectResponse("/user/dashboard", status_code=303)

    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Forkert brugernavn eller adgangskode"},
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
