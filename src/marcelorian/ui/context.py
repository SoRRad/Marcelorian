"""Shared objects for UI route modules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker

from marcelorian.clock import Clock, PracticeClock
from marcelorian.lookups.http import Fetcher
from marcelorian.platform.secrets import SecretStore, open_secret_store
from marcelorian.services.ai import AnthropicClient, ClientFactory
from marcelorian.services.gateway import SendGateway
from marcelorian.ui.security import UISession

SLOGAN = "May the Force Hire You"


@dataclass(frozen=True)
class UIContext:
    templates: Jinja2Templates
    session_factory: sessionmaker[Session]
    clock: Clock
    current_session: Callable[[Request], UISession]
    fetcher: Fetcher
    campaign_clock: PracticeClock
    gateway: SendGateway | None = None
    #: Opened only when a key is saved, removed or used (never on page loads).
    secrets: Callable[[], SecretStore] = open_secret_store
    ai_client: ClientFactory = AnthropicClient

    def render(self, request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
        """Render with the slogan and this session's CSRF token always present."""
        merged = {
            "slogan": SLOGAN,
            "csrf_token": self.current_session(request).csrf_token,
            **context,
        }
        return self.templates.TemplateResponse(request, name, merged)
