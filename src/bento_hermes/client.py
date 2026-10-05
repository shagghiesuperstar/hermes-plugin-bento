"""Stdlib HTTP client for the Bento REST API (https://app.bentonow.com/api/v1).

Docs relied on (bentonow.com/docs/authentication, /docs/api/conventions, /docs/api/access):
  * Auth is HTTP Basic: username = publishable key, password = secret key.
  * ``site_uuid`` is a query parameter on EVERY request (missing -> 401).
  * A real ``User-Agent`` is mandatory ("Requests without it are blocked by Cloudflare and will
    fail silently or return HTML instead of JSON").
  * Rate limits (use the lower numbers): /fetch + /batch 60/min, /stats 30/hour per IP.
  * "avoid blindly retrying email sends" -> retries are GET-only (or explicitly idempotent POSTs).
"""

from __future__ import annotations

import base64
import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://app.bentonow.com/api/v1"
USER_AGENT = "hermes-plugin-bento/0.1.0"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3
MAX_BACKOFF_SECONDS = 8.0

# bucket name -> (max requests, window seconds). Lower of the conflicting documented numbers.
RATE_LIMITS = {"api": (60, 60.0), "stats": (30, 3600.0)}


@dataclass(frozen=True)
class Credentials:
    publishable_key: str
    secret_key: str
    site_uuid: str

    def secrets(self) -> list[str]:
        basic = base64.b64encode(f"{self.publishable_key}:{self.secret_key}".encode()).decode()
        return [self.publishable_key, self.secret_key, basic]


class BentoError(Exception):
    """Failure talking to Bento. ``str(err)`` is already redacted and safe to return to the model."""

    def __init__(self, message: str, *, status: int | None = None, kind: str = "http", detail: Any = None):
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.detail = detail


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every secret (>= 4 chars) in ``text`` with a marker."""
    out = text
    for secret in secrets:
        if secret and len(secret) >= 4:
            out = out.replace(secret, "[REDACTED]")
    return out


class RateGuard:
    """Client-side sliding-window budget so a runaway loop cannot burn Bento's per-IP quota.

    Refuses (never sleeps) when the budget is spent so a hung agent turn cannot result.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, limits: dict | None = None):
        self._clock = clock
        self._limits = dict(RATE_LIMITS if limits is None else limits)
        self._hits: dict[str, deque[float]] = {}

    def acquire(self, bucket: str) -> None:
        limit = self._limits.get(bucket)
        if limit is None:
            return
        maximum, window = limit
        now = self._clock()
        hits = self._hits.setdefault(bucket, deque())
        while hits and now - hits[0] >= window:
            hits.popleft()
        if len(hits) >= maximum:
            wait = max(1, int(window - (now - hits[0])))
            raise BentoError(
                f"Client-side rate budget for Bento '{bucket}' calls is spent "
                f"({maximum} per {int(window)}s). Retry in about {wait}s.",
                kind="client_rate_limit",
            )
        hits.append(now)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: credentials must not travel to a host we did not configure."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _bucket_for(path: str) -> str:
    return "stats" if path.startswith("/stats/") else "api"


class BentoClient:
    def __init__(
        self,
        creds: Credentials,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        guard: RateGuard | None = None,
        sleep: Callable[[float], None] = time.sleep,
        user_agent: str = USER_AGENT,
    ):
        self._creds = creds
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.guard = guard or RateGuard()
        self._sleep = sleep
        self._user_agent = user_agent
        self._opener = urllib.request.build_opener(_NoRedirect)

    # -- public ---------------------------------------------------------------------------------
    def get(self, path: str, params: dict | None = None) -> Any:
        return self._request("GET", path, params=params, body=None, retry=True)

    def post(self, path: str, body: dict, *, idempotent: bool = False, params: dict | None = None) -> Any:
        """POST. Retries happen only when ``idempotent`` (e.g. a $purchase with a unique key)."""
        return self._request("POST", path, params=params, body=body, retry=idempotent)

    # -- internals ------------------------------------------------------------------------------
    def _build(self, method: str, path: str, params: dict | None, body: dict | None):
        query = {k: v for k, v in (params or {}).items() if v is not None}
        query["site_uuid"] = self._creds.site_uuid
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(query)}"
        token = base64.b64encode(f"{self._creds.publishable_key}:{self._creds.secret_key}".encode()).decode()
        req = urllib.request.Request(url, method=method)
        # unredirected: never re-sent anywhere else, even if a handler were to follow a redirect
        req.add_unredirected_header("Authorization", f"Basic {token}")
        req.add_header("User-Agent", self._user_agent)
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        return req

    def _request(self, method: str, path: str, *, params, body, retry: bool) -> Any:
        attempts = MAX_ATTEMPTS if retry else 1
        for attempt in range(attempts):
            self.guard.acquire(_bucket_for(path))
            try:
                return self._once(method, path, params, body)
            except BentoError as err:
                if err.status not in RETRY_STATUSES or attempt == attempts - 1:
                    raise
                self._sleep(self._backoff(attempt, err.detail))
        raise BentoError("retry loop exhausted", kind="internal")  # pragma: no cover

    @staticmethod
    def _backoff(attempt: int, detail: Any) -> float:
        retry_after = detail.get("retry_after") if isinstance(detail, dict) else None
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            return min(float(retry_after), MAX_BACKOFF_SECONDS)
        return min(MAX_BACKOFF_SECONDS, (2**attempt) * 0.5 + random.uniform(0, 0.25))  # noqa: S311

    def _once(self, method: str, path: str, params, body) -> Any:
        req = self._build(method, path, params, body)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                status = resp.status
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as err:
            raw = err.read(MAX_RESPONSE_BYTES + 1) if err.fp else b""
            retry_after = _parse_retry_after(err.headers.get("Retry-After") if err.headers else None)
            raise self._http_error(err.code, raw, retry_after) from None
        except urllib.error.URLError as err:
            raise BentoError(
                self._clean(f"Could not reach Bento: {err.reason}"), kind="network"
            ) from None
        except (TimeoutError, OSError) as err:
            raise BentoError(self._clean(f"Network error talking to Bento: {err}"), kind="network") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise BentoError("Bento response exceeded the 5 MiB safety cap.", kind="too_large")
        if status in (301, 302, 303, 307, 308):
            raise BentoError("Bento answered with a redirect; refusing to follow it.", status=status, kind="redirect")
        return self._parse(raw, status)

    def _parse(self, raw: bytes, status: int) -> Any:
        if not raw.strip():
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            snippet = self._clean(raw[:160].decode("utf-8", "replace"))
            raise BentoError(
                "Bento returned a non-JSON body (HTML usually means the User-Agent was blocked by "
                f"Cloudflare). Snippet: {snippet!r}",
                status=status,
                kind="non_json",
            ) from None

    def _http_error(self, status: int, raw: bytes, retry_after: float | None) -> BentoError:
        detail: Any
        try:
            detail = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            detail = {"body_snippet": raw[:160].decode("utf-8", "replace")}
        if isinstance(detail, dict) and retry_after is not None:
            detail = {**detail, "retry_after": retry_after}
        hint = {
            400: "bad request body",
            401: "authentication failed - check BENTO_PUBLISHABLE_KEY / BENTO_SECRET_KEY and that site_uuid matches",
            403: "forbidden - missing/blocked User-Agent, blocked IP, or the feature is not enabled for this site",
            404: "path or method not found",
            422: "Bento rejected the request (business rule, e.g. sender is not a verified Author)",
            429: "rate limited by Bento",
        }.get(status, "server error" if status >= 500 else "request failed")
        text = self._clean(json.dumps(detail, default=str)[:500])
        return BentoError(f"Bento HTTP {status}: {hint}. Response: {text}", status=status, kind="http", detail=detail)

    def _clean(self, text: str) -> str:
        return redact(text, self._creds.secrets())


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
