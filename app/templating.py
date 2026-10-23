from datetime import date

from fastapi.templating import Jinja2Templates

from app.auth import csrf_token, pop_flash
from app.pdf_export import danish_date
from app.utils import get_storage_usage

templates = Jinja2Templates(directory="app/templates")

templates.env.globals.update(
    get_storage=get_storage_usage,
    csrf_token=csrf_token,
    get_flash=pop_flash,
    today=date.today,
)
templates.env.filters["danish_date"] = danish_date


def render(request, name: str, status_code: int = 200, **context):
    return templates.TemplateResponse(request, name, context, status_code=status_code)
