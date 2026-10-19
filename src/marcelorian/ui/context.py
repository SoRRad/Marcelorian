"""Shared objects for UI route modules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker

from marcelorian.clock import Clock
from marcelorian.ui.security import UISession

SLOGAN = "May the Force Hire You"


@dataclass(frozen=True)
class UIContext:
    templates: Jinja2Templates
    session_factory: sessionmaker[Session]
    clock: Clock
    current_session: Callable[[Request], UISession]

    def render(self, request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
        """Render with the slogan and this session's CSRF token always present."""
        merged = {
            "slogan": SLOGAN,
            "csrf_token": self.current_session(request).csrf_token,
            **context,
        }
        return self.templates.TemplateResponse(request, name, merged)
