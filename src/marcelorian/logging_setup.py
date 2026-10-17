"""Logging with redaction (spec §2 "Local UI security").

Never logged: bootstrap tokens, OAuth codes/tokens, API keys, email bodies,
raw CV text, or full email addresses (masked). The filter is a safety net;
code must still avoid passing such values to loggers in the first place.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

# Every pattern below must stay linear on hostile input (long hyphen- or dot-joined
# runs from URLs, headers or third-party exception text): a match may start only at
# the beginning of a character run (negative lookbehind), and runs are possessive.
#
# An address, also in URL-encoded form ("%40"), as Gmail API request URLs carry it.
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._+'-])([A-Za-z0-9._+'-])[A-Za-z0-9._+'-]*+(?:@|%40)"
    r"([A-Za-z0-9])[A-Za-z0-9.-]*\.([A-Za-z]{2,})\b"
)
_KEY_WORDS = r"(?:token|code|key|secret|password|passwd|session|csrf|cookie|authorization)"
#: (pattern, replacement) pairs, applied in order after addresses are masked.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"), r"\1 [REDACTED]"),
    # key=value, key: value, "key": "value", 'key': 'value' (JSON, dict reprs, query strings)
    (
        re.compile(
            rf"(?i)([\"']?)(?<![a-z_-])((?=[a-z_-]*{_KEY_WORDS})[a-z_-]++)\1"
            rf"(\s*[=:]\s*)([\"']?)[^\s\"',;&}}\]]+"
        ),
        r"\1\2\1\3\4[REDACTED]",
    ),
    (re.compile(r"\b(sk-ant-)[A-Za-z0-9_-]+"), r"\1[REDACTED]"),
    (re.compile(r"\b(ya29\.)[A-Za-z0-9._-]+"), r"\1[REDACTED]"),  # Google access tokens
    (re.compile(r"\b(1//)[A-Za-z0-9._-]+"), r"\1[REDACTED]"),  # Google refresh tokens
    (re.compile(r"\b(GOCSPX-)[A-Za-z0-9_-]+"), r"\1[REDACTED]"),  # OAuth client secrets
    (re.compile(r"\b(4/0)[A-Za-z0-9_-]+"), r"\1[REDACTED]"),  # OAuth authorization codes
    (re.compile(r"\b(eyJ)[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"), r"\1[REDACTED]"),  # JWT
    (re.compile(r"(#t=)[A-Za-z0-9_-]+"), r"\1[REDACTED]"),  # bootstrap fragment
)


#: Percent-escapes of characters that occur in addresses (+ - . ' _ @). They are decoded
#: before masking so a URL-encoded address (urlencode turns "+" into "%2B") is masked
#: whole instead of only after the last escape. Nothing else is decoded, so no control
#: characters or line breaks can be introduced into the log.
_PCT_ADDRESS_CHAR = re.compile(r"%(2[BbDdEe7]|5[Ff]|40)")

#: Keyword arguments for uvicorn.Config / uvicorn.run. Without log_config=None uvicorn
#: re-applies its own dictConfig when the server is configured, which restores an
#: unredacted handler on "uvicorn" (propagate=False) and re-enables the access log,
#: undoing configure_logging(). The launcher must pass these.
UVICORN_LOG_KWARGS: dict[str, object] = {"log_config": None, "access_log": False}


def mask_email(match: re.Match[str]) -> str:
    return f"{match.group(1)}***@{match.group(2)}***.{match.group(3)}"


def redact(text: str) -> str:
    text = _PCT_ADDRESS_CHAR.sub(lambda m: chr(int(m.group(1), 16)), text)
    text = _EMAIL.sub(mask_email, text)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """Redacts the formatted message early; never raises into the logging caller."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            # Mismatched format arguments: keep the template, drop the arguments.
            try:
                message = f"{record.msg} [unformattable log arguments]"
            except Exception:  # the message object itself cannot be rendered
                message = f"[unprintable {type(record.msg).__name__} log message]"
        record.msg = redact(str(message))
        record.args = None
        return True


class RedactingFormatter(logging.Formatter):
    """Redacts the *complete* output, including exception tracebacks and stack info.

    The filter only sees the message; tracebacks are appended by the formatter
    afterwards, and third-party exception text (SQL parameters, request URLs)
    travels that way.
    """

    def format(self, record: logging.LogRecord) -> str:
        try:
            return redact(super().format(record))
        except Exception:
            return redact(f"{record.levelname} {record.name} [log record could not be formatted]")


def configure_logging(log_dir: Path | None, level: int = logging.INFO) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_dir / "marcelorian.log", encoding="utf-8"))
    redactor = RedactingFilter()
    for handler in handlers:
        handler.addFilter(redactor)
        handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers = handlers
    root.setLevel(level)
    # Route uvicorn's loggers through the redacting root handlers. This holds only if the
    # server is then started with UVICORN_LOG_KWARGS (log_config=None); otherwise uvicorn's
    # own dictConfig, applied when the server is configured, would undo it.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    # uvicorn access logs would echo request lines; keep them off entirely.
    logging.getLogger("uvicorn.access").disabled = True
