"""
HTML page routes: / and /login.
Templates live in templates/ and use {{ current_user }} as the only placeholder.
"""
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, RedirectResponse

from auth import get_current_user

router = APIRouter()

TEMPLATES = Path(__file__).parent.parent / "templates"


def _render(name: str, **ctx: str) -> str:
    html = (TEMPLATES / name).read_text(encoding="utf-8")
    for key, value in ctx.items():
        html = html.replace("{{ " + key + " }}", value)
    return html


@router.get("/", response_class=HTMLResponse)
async def index(current_user: Optional[str] = Depends(get_current_user)):
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    return _render("index.html", current_user=current_user)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(current_user: Optional[str] = Depends(get_current_user)):
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    return _render("settings.html", current_user=current_user)


@router.get("/login", response_class=HTMLResponse)
async def login_page():
    return _render("login.html")
