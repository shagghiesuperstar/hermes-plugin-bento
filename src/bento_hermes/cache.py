"""Small in-process TTL cache and a persistent 24h duplicate-send ledger (shared across processes)."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import Any

from .claim import ClaimError

LEDGER_TTL_SECONDS = 24 * 3600
LEDGER_MAX_ENTRIES = 5000


class TTLCache:
    """Monotonic-clock TTL cache. Keys must include the site UUID so sites never share entries."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._items: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any, ttl: float) -> tuple[Any, float] | None:
        """Return ``(value, age_seconds)`` when fresh, else ``None``."""
        item = self._items.get(key)
        if item is None:
            return None
        stored_at, value = item
        age = self._clock() - stored_at
        if ttl <= 0 or age >= ttl:
            self._items.pop(key, None)
            return None
        return value, age

    def set(self, key: Any, value: Any) -> None:
        self._items[key] = (self._clock(), value)

    def invalidate(self, predicate: Callable[[Any], bool]) -> None:
        for key in [k for k in self._items if predicate(k)]:
            self._items.pop(key, None)


def email_fingerprint(site_uuid: str, to: str, subject: str, body: str) -> str:
    material = "\x1f".join([site_uuid, to.strip().lower(), subject, body])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class DedupeLedger:
    """24h ledger of (site, to, subject, body) hashes kept in ``ctx.state`` (profile scoped).

    ``state_get`` must raise ``ClaimError`` when the stored ledger cannot be read, and every write that
    fails to persist raises ``ClaimError``: a reservation that is not on disk would not stop a second
    process. ``reserve``/``release`` must run under the cross-process claim lock (``Runtime.gate``).
    """

    KEY = "email_ledger"

    def __init__(self, state_get: Callable[[str, Any], Any], state_set: Callable[[str, Any], bool],
                 clock: Callable[[], float] = time.time):
        self._get = state_get
        self._set = state_set
        self._clock = clock

    def _load(self) -> dict[str, float]:
        stored = self._get(self.KEY, None)
        merged = dict(stored) if isinstance(stored, dict) else {}
        cutoff = self._clock() - LEDGER_TTL_SECONDS
        return {k: v for k, v in merged.items() if isinstance(v, (int, float)) and v >= cutoff}

    def seen(self, fingerprints: list[str]) -> list[str]:
        ledger = self._load()
        return [f for f in fingerprints if f in ledger]

    def reserve(self, fingerprints: list[str]) -> list[str]:
        """Record fingerprints as sent-or-in-flight; return only the ones that were not already present."""
        present = self._load()
        fresh = [f for f in dict.fromkeys(fingerprints) if f not in present]
        self.record(fingerprints)
        return fresh

    def release(self, fingerprints: list[str]) -> None:
        """Undo a ``reserve`` after a definite non-send (never touches earlier, genuinely sent entries)."""
        if not fingerprints:
            return
        ledger = self._load()
        for fp in fingerprints:
            ledger.pop(fp, None)
        self._persist(ledger)

    def record(self, fingerprints: list[str]) -> None:
        ledger = self._load()
        now = self._clock()
        for fp in fingerprints:
            ledger[fp] = now
        if len(ledger) > LEDGER_MAX_ENTRIES:
            newest = sorted(ledger.items(), key=lambda kv: kv[1], reverse=True)[:LEDGER_MAX_ENTRIES]
            ledger = dict(newest)
        self._persist(ledger)

    def _persist(self, ledger: dict[str, float]) -> None:
        if not self._set(self.KEY, ledger):
            raise ClaimError("could not persist the duplicate-send ledger")
