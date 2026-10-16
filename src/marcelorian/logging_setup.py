"""Logging with redaction (spec §2 "Local UI security").

Never logged: bootstrap tokens, OAuth codes/tokens, API keys, email bodies,
raw CV text, or full email addresses (masked). The filter is a safety net;
code must still avoid passing such values to loggers in the first place.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

_EMAIL = re.compile(
    r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9])[A-Za-z0-9.-]*\.([A-Za-z]{2,})\b"
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)\b([a-z_]*(?:token|code|key|secret|password|session|csrf)[a-z_]*)"
        r"(\s*[=:]\s*)[^\s&\"',;]+"
    ),
    re.compile(r"\b(sk-ant-)[A-Za-z0-9_-]+"),
    re.compile(r"\b(ya29\.)[A-Za-z0-9._-]+"),  # Google OAuth access tokens
    re.compile(r"\b(1//)[A-Za-z0-9._-]+"),  # Google refresh tokens
    re.compile(r"(#t=)[A-Za-z0-9_-]+"),  # bootstrap fragment
)


def mask_email(match: re.Match[str]) -> str:
    return f"{match.group(1)}***@{match.group(2)}***.{match.group(3)}"


def redact(text: str) -> str:
    text = _EMAIL.sub(mask_email, text)
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
        else:
            text = pattern.sub(lambda m: f"{m.group(1)}[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = redact(message)
        record.args = None
        return True


def configure_logging(log_dir: Path | None, level: int = logging.INFO) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_dir / "marcelorian.log", encoding="utf-8"))
    redactor = RedactingFilter()
    for handler in handlers:
        handler.addFilter(redactor)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers = handlers
    root.setLevel(level)
    # uvicorn access logs would echo request lines; keep them off entirely.
    logging.getLogger("uvicorn.access").disabled = True
