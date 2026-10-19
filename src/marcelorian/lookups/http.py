"""A small JSON-over-HTTPS client restricted to the public research APIs.

* Fixed allowlist of hosts (no user-supplied URLs, so no SSRF surface).
* Timeouts, a response size cap, and polite retries on 429/5xx (read-only calls only,
  spec §15). Partial results are the caller's to keep.
* Uses the system proxy settings, so it works behind a university proxy.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

ALLOWED_HOSTS = frozenset({"eutils.ncbi.nlm.nih.gov", "api.openalex.org", "api.reporter.nih.gov"})
USER_AGENT = "Marcelorian/0.1 (applicant research tool; read-only)"
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT_SECONDS = 20
RETRIES = 3


class LookupFailed(RuntimeError):
    """A lookup failed; the message is safe to show (no keys, no full URLs)."""


class Fetcher(Protocol):
    def get_json(self, url: str, params: dict[str, str]) -> Any: ...

    def post_json(self, url: str, body: dict[str, Any]) -> Any: ...


class HttpFetcher:
    def __init__(
        self,
        sleep: Callable[[float], None] = time.sleep,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self._sleep = sleep
        self._open = opener

    def get_json(self, url: str, params: dict[str, str]) -> Any:
        query = urllib.parse.urlencode(params)
        return self._request(f"{url}?{query}" if query else url, None)

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        return self._request(url, json.dumps(body).encode("utf-8"))

    def _request(self, url: str, data: bytes | None) -> Any:
        host = urllib.parse.urlsplit(url).hostname or ""
        if urllib.parse.urlsplit(url).scheme != "https" or host not in ALLOWED_HOSTS:
            raise LookupFailed(f"host not allowed: {host}")
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        delay = 1.0
        for attempt in range(1, RETRIES + 1):
            request = urllib.request.Request(  # noqa: S310 (https, fixed hosts)
                url, data=data, headers=headers, method="POST" if data is not None else "GET"
            )
            try:
                with self._open(request, timeout=TIMEOUT_SECONDS) as response:
                    raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise LookupFailed(f"{host}: response too large")
                return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < RETRIES:
                    self._sleep(delay)
                    delay *= 2
                    continue
                raise LookupFailed(f"{host}: HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < RETRIES:
                    self._sleep(delay)
                    delay *= 2
                    continue
                raise LookupFailed(f"{host}: not reachable ({type(exc).__name__})") from None
            except (ValueError, UnicodeDecodeError):
                raise LookupFailed(f"{host}: unexpected response") from None
        raise LookupFailed(f"{host}: not reachable")  # pragma: no cover
