"""Settings, credential resolution and the per-plugin Runtime shared by tools and hook."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cache import DedupeLedger, TTLCache
from .client import DEFAULT_BASE_URL, BentoClient, BentoError, Credentials, RateGuard
from .safety import AuditLog, TokenService

logger = logging.getLogger(__name__)

ENV_PUBLISHABLE = "BENTO_PUBLISHABLE_KEY"
ENV_SECRET = "BENTO_SECRET_KEY"
ENV_SITE = "BENTO_SITE_UUID"
REQUIRED_ENV = (ENV_PUBLISHABLE, ENV_SECRET, ENV_SITE)

_SITE_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
ENVIRONMENTS = ("production", "staging", "disabled")
TAG_FIELD_CACHE_TTL = 300.0


def resolve_secret(name: str) -> str | None:
    """Resolve a credential through Hermes's secret scope when importable, else ``os.environ``.

    ``agent.secret_scope`` is an internal module (not documented plugin API), hence the guarded
    import. Any error from it (e.g. an unscoped read under a multiplexing gateway) fails CLOSED.
    """
    try:
        from agent.secret_scope import get_secret  # type: ignore[import-not-found]
    except ImportError:
        value = os.environ.get(name)
    else:
        try:
            value = get_secret(name)
        except Exception:  # noqa: BLE001 - UnscopedSecretError etc.: no credential rather than a wrong one
            logger.warning("bento: secret scope refused a read of %s; treating it as unset", name)
            return None
    value = (value or "").strip()
    return value or None


def validate_base_url(url: str) -> str | None:
    """Return a problem description, or ``None`` when ``url`` is an acceptable API base."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return "base_url is not a valid URL"
    if parts.username or parts.password:
        return "base_url must not embed credentials"
    if parts.scheme == "https" and (host == "bentonow.com" or host.endswith(".bentonow.com")):
        return None
    if parts.scheme == "http" and host in LOOPBACK_HOSTS:
        return None
    return "base_url must be https://*.bentonow.com or http://localhost (credentials are never sent elsewhere)"


def _int(raw: Any, default: int, low: int, high: int, name: str, problems: list[str]) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        problems.append(f"{name}: expected an integer; using {default}")
        return default
    try:
        value = int(float(raw))
    except ValueError:
        problems.append(f"{name}: {raw!r} is not an integer; using {default}")
        return default
    if not low <= value <= high:
        clamped = min(max(value, low), high)
        problems.append(f"{name}: {value} outside {low}..{high}; using {clamped}")
        return clamped
    return value


def _bool(raw: Any, default: bool, name: str, problems: list[str]) -> bool:
    if isinstance(raw, bool):
        return raw
    problems.append(f"{name}: expected true/false; using {default}")
    return default


@dataclass(frozen=True)
class Settings:
    environment: str = "production"
    allowed_senders: tuple[str, ...] = ()
    bulk_threshold: int = 25
    tier2: str = "approve"
    min_schedule_lead_minutes: int = 30
    max_batch_size_per_hour: int = 5000
    max_recipients_per_call: int = 10
    allow_transactional_override: bool = False
    stats_cache_ttl_seconds: int = 900
    request_timeout_seconds: int = 30
    log_emails: bool = False
    base_url: str = ""
    problems: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def load(cls, get: Callable[[str, Any], Any]) -> Settings:
        problems: list[str] = []
        env = get("environment", "production")
        if env not in ENVIRONMENTS:
            problems.append(f"environment: {env!r} is not one of {ENVIRONMENTS}; failing closed to 'disabled'")
            env = "disabled"
        tier2 = get("safety.tier2", "approve")
        if tier2 not in ("approve", "token_only"):
            problems.append(f"safety.tier2: {tier2!r} invalid; using 'approve'")
            tier2 = "approve"
        senders_raw = get("allowed_senders", [])
        if isinstance(senders_raw, str):
            senders_raw = [s for s in senders_raw.split(",")]
        senders: list[str] = []
        for item in senders_raw if isinstance(senders_raw, (list, tuple)) else []:
            email = str(item).strip().lower()
            if _EMAIL_RE.match(email):
                senders.append(email)
            elif email:
                problems.append(f"allowed_senders: ignoring invalid address {email!r}")
        base_url = str(get("base_url", "") or "").strip()
        if base_url:
            problem = validate_base_url(base_url)
            if problem:
                problems.append(problem)
        return cls(
            environment=env,
            allowed_senders=tuple(dict.fromkeys(senders)),
            bulk_threshold=_int(get("bulk_threshold", 25), 25, 1, 1000, "bulk_threshold", problems),
            tier2=tier2,
            min_schedule_lead_minutes=_int(get("min_schedule_lead_minutes", 30), 30, 5, 10080,
                                           "min_schedule_lead_minutes", problems),
            max_batch_size_per_hour=_int(get("max_batch_size_per_hour", 5000), 5000, 1, 100000,
                                         "max_batch_size_per_hour", problems),
            max_recipients_per_call=_int(get("max_recipients_per_call", 10), 10, 1, 60,
                                         "max_recipients_per_call", problems),
            allow_transactional_override=_bool(get("allow_transactional_override", False), False,
                                               "allow_transactional_override", problems),
            stats_cache_ttl_seconds=_int(get("stats_cache_ttl_seconds", 900), 900, 0, 86400,
                                         "stats_cache_ttl_seconds", problems),
            request_timeout_seconds=_int(get("request_timeout_seconds", 30), 30, 1, 120,
                                         "request_timeout_seconds", problems),
            log_emails=_bool(get("audit.log_emails", False), False, "audit.log_emails", problems),
            base_url=base_url if base_url and not validate_base_url(base_url) else "",
            problems=tuple(problems),
        )


class Runtime:
    """Everything a handler needs: settings, credentials, client, cache, tokens, audit, state."""

    def __init__(self, ctx: Any, *, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.ctx = ctx
        self.clock = clock
        self._sleep = sleep
        self._monotonic = monotonic
        self.cache = TTLCache(monotonic)
        self._guards: dict[str, RateGuard] = {}
        self.tokens = TokenService(self.state_get, self.state_set, clock)
        self.ledger = DedupeLedger(self.state_get, self.state_set, clock)
        self.audit = AuditLog(self._audit_path, clock)

    # -- plugin state / config (all failures degrade, never raise into a tool call) -----------------
    def _config(self, key: str, default: Any) -> Any:
        try:
            return self.ctx.get_config(key, default)
        except (ValueError, OSError, RuntimeError):
            logger.warning("bento: could not read config key %s; using default", key, exc_info=True)
            return default

    def state_get(self, key: str, default: Any = None) -> Any:
        try:
            return self.ctx.state.get(key, default)
        except (ValueError, OSError, RuntimeError, AttributeError):
            logger.warning("bento: plugin state read failed for %s", key, exc_info=True)
            return default

    def state_set(self, key: str, value: Any) -> bool:
        try:
            self.ctx.state.set(key, value)
            return True
        except (ValueError, OSError, RuntimeError, AttributeError):
            logger.warning("bento: plugin state write failed for %s", key, exc_info=True)
            return False

    def _audit_path(self) -> Path | None:
        try:
            data_dir = getattr(self.ctx.state, "data_dir", None)
        except (ValueError, OSError, RuntimeError, AttributeError):
            return None
        return Path(data_dir) / "audit.jsonl" if data_dir else None

    def settings(self) -> Settings:
        return Settings.load(self._config)

    def audit_event(self, event: dict, recipients: list[str] | None = None) -> bool:
        return self.audit.write(event, log_emails=self.settings().log_emails, recipients=recipients)

    # -- credentials / client --------------------------------------------------------------------------
    def credentials(self) -> Credentials | None:
        pk, sk, site = (resolve_secret(n) for n in REQUIRED_ENV)
        if not (pk and sk and site):
            return None
        if not _SITE_RE.match(site):
            logger.warning("bento: %s does not look like a site UUID; treating credentials as unset", ENV_SITE)
            return None
        return Credentials(pk, sk, site)

    def check_fn(self) -> bool:
        return self.credentials() is not None

    def site_fingerprint(self) -> str:
        creds = self.credentials()
        return hashlib.sha256(creds.site_uuid.encode()).hexdigest()[:16] if creds else ""

    def client(self) -> BentoClient:
        creds = self.credentials()
        if creds is None:
            raise BentoError(
                f"Bento is not configured: set {ENV_PUBLISHABLE}, {ENV_SECRET} and {ENV_SITE} in this profile's .env.",
                kind="config",
            )
        settings = self.settings()
        guard = self._guards.setdefault(creds.site_uuid, RateGuard(self._monotonic))
        return BentoClient(creds, base_url=settings.base_url or DEFAULT_BASE_URL,
                           timeout=float(settings.request_timeout_seconds), guard=guard, sleep=self._sleep)

    # -- cached reads --------------------------------------------------------------------------------------
    def cached_get(self, path: str, params: dict | None = None, ttl: float = TAG_FIELD_CACHE_TTL) -> tuple[Any, float | None]:
        """GET through the TTL cache; the key carries the site UUID.

        Returns ``(data, age_seconds)`` on a cache hit and ``(data, None)`` after a live fetch.
        """
        site = self.site_fingerprint()
        key = (site, path, tuple(sorted((params or {}).items())))
        hit = self.cache.get(key, ttl)
        if hit is not None:
            return hit
        data = self.client().get(path, params)
        self.cache.set(key, data)
        return data, None

    def invalidate(self, path: str) -> None:
        self.cache.invalidate(lambda key: isinstance(key, tuple) and len(key) > 1 and key[1] == path)
