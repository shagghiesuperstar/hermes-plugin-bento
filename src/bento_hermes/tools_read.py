"""Read-only (T0) tools plus the argument validators shared by every tool module."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from .safety import T0, ToolSpec, ValidationError, untrusted_envelope
from .schemas import SCHEMAS

EMAIL_RE = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
BROADCAST_STATUSES = ("sent", "draft", "sending", "scheduled", "paused", "canceled")
STATS_KINDS = ("site", "segment", "report", "ads")
ADS_DIMENSIONS = ("source", "campaign", "medium", "content", "ad")
REVENUE_MODES = ("all_revenue", "first_revenue", "subscriptions")


# -- validators (raise ValidationError; messages are returned to the model) -----------------------------
def reject_unknown(args: dict, allowed: Iterable[str]) -> None:
    unknown = sorted(set(args) - set(allowed))
    if unknown:
        raise ValidationError(f"unknown parameter(s): {', '.join(unknown)}")


def req_str(args: dict, key: str, *, max_len: int = 1000) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{key} is required and must be a non-empty string")
    if len(value) > max_len:
        raise ValidationError(f"{key} is longer than {max_len} characters")
    return value


def opt_str(args: dict, key: str, *, max_len: int = 1000) -> str | None:
    if args.get(key) is None:
        return None
    return req_str(args, key, max_len=max_len)


def as_email(value: Any, name: str = "email") -> str:
    if not isinstance(value, str) or len(value) > 254 or not EMAIL_RE.match(value.strip()):
        raise ValidationError(f"{name} is not a valid email address")
    return value.strip()


def as_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValidationError(f"{name} must be 1-64 characters of letters, digits, '-' or '_'")
    return value


def as_int(value: Any, name: str, *, low: int = 0, high: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} must be an integer (got {type(value).__name__}); money is integer cents")
    if value < low or (high is not None and value > high):
        raise ValidationError(f"{name} must be between {low} and {high if high is not None else 'unbounded'}")
    return value


def as_currency(value: Any, name: str = "currency") -> str:
    code = value.strip().upper() if isinstance(value, str) else ""
    if not _CURRENCY_RE.match(code):
        raise ValidationError(f"{name} must be a 3-letter ISO-4217 code such as USD")
    return code


def as_iso(value: Any, name: str) -> datetime:
    """Parse ISO-8601 with an explicit timezone; returns an aware UTC datetime."""
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be an ISO-8601 string with a timezone, e.g. 2026-10-20T14:00:00Z")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError(f"{name} is not valid ISO-8601 (e.g. 2026-10-20T14:00:00Z)") from None
    if parsed.tzinfo is None:
        raise ValidationError(f"{name} must include a timezone (Z or an offset)")
    return parsed.astimezone(UTC)


def iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def flat_dict(value: Any, name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be an object")
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValidationError(f"{name} keys must be non-empty strings")
        if isinstance(item, (dict, list)):
            raise ValidationError(f"{name}.{key} must be a flat value (no nested objects or arrays)")
    return dict(value)


def tag_names(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(t, str) and t.strip() for t in value):
        raise ValidationError(f"{name} must be a list of non-empty strings")
    for tag in value:
        if "," in tag:
            raise ValidationError(f"{name}: tag {tag!r} contains a comma (Bento takes comma-separated tag lists)")
    return [t.strip() for t in value]


def extract_names(data: Any) -> list[str]:
    """Pull ``name``/``key`` strings out of a Bento list response (docs: ``data[].name``)."""
    items = data.get("data") if isinstance(data, dict) else data
    names: list[str] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        name = item.get("name") or item.get("key") or attrs.get("name") or attrs.get("key")
        if isinstance(name, str) and name:
            names.append(name)
    return names


# -- handlers ----------------------------------------------------------------------------------------------
def subscriber_get(rt: Any, args: dict) -> str:
    reject_unknown(args, {"email", "uuid"})
    email, uuid = args.get("email"), args.get("uuid")
    if (email is None) == (uuid is None):
        raise ValidationError("provide exactly one of email or uuid")
    params = {"email": as_email(email)} if email is not None else {"uuid": as_id(uuid, "uuid")}
    return untrusted_envelope(rt.client().get("/fetch/subscribers", params))


def tag_list(rt: Any, args: dict) -> str:
    reject_unknown(args, set())
    data, age = rt.cached_get("/fetch/tags")
    return untrusted_envelope({"tags": extract_names(data)}, cached=age is not None, cached_age_seconds=_round(age))


def field_list(rt: Any, args: dict) -> str:
    reject_unknown(args, set())
    data, age = rt.cached_get("/fetch/fields")
    return untrusted_envelope({"fields": extract_names(data)}, cached=age is not None, cached_age_seconds=_round(age))


def _round(age: float | None) -> float | None:
    return None if age is None else round(age, 1)


def broadcast_list(rt: Any, args: dict) -> str:
    reject_unknown(args, {"status", "page"})
    status = args.get("status")
    if status is not None and status not in BROADCAST_STATUSES:
        raise ValidationError(f"status must be one of {', '.join(BROADCAST_STATUSES)}")
    params: dict[str, Any] = {}
    if status:
        params["status"] = status
    if args.get("page") is not None:
        params["page"] = as_int(args["page"], "page", low=1, high=100000)
    note = None if status else "No status given: Bento returns only SENT broadcasts. Pass status=draft to find drafts."
    return untrusted_envelope(rt.client().get("/fetch/broadcasts", params), note=note)


def stats_params(args: dict) -> tuple[str, dict]:
    kind = args.get("kind")
    if kind not in STATS_KINDS:
        raise ValidationError(f"kind must be one of {', '.join(STATS_KINDS)}")
    if kind == "site":
        return "/stats/site", {}
    if kind == "segment":
        return "/stats/segment", {"segment_id": as_id(args.get("segment_id"), "segment_id")}
    if kind == "report":
        return "/stats/report", {"report_id": as_id(args.get("report_id"), "report_id")}
    params: dict[str, Any] = {}
    if args.get("dimension") is not None:
        if args["dimension"] not in ADS_DIMENSIONS:
            raise ValidationError(f"dimension must be one of {', '.join(ADS_DIMENSIONS)}")
        params["dimension"] = args["dimension"]
    if args.get("revenue_mode") is not None:
        if args["revenue_mode"] not in REVENUE_MODES:
            raise ValidationError(f"revenue_mode must be one of {', '.join(REVENUE_MODES)}")
        params["revenue_mode"] = args["revenue_mode"]
    for key in ("value", "start_date", "end_date"):
        text = opt_str(args, key, max_len=200)
        if text is not None:
            params[key] = text
    return "/stats/ads", params


def stats(rt: Any, args: dict) -> str:
    reject_unknown(args, {"kind", "segment_id", "report_id", "dimension", "value", "start_date", "end_date", "revenue_mode"})
    path, params = stats_params(args)
    ttl = float(rt.settings().stats_cache_ttl_seconds)
    data, age = rt.cached_get(path, params, ttl=ttl)
    return untrusted_envelope(data, cached=age is not None, cached_age_seconds=_round(age),
                              note="Bento allows 30 stats requests/hour/IP; money values are cents.")


def _spec(name: str, handler: Any, emoji: str) -> ToolSpec:
    return ToolSpec(name=name, tier=T0, schema=SCHEMAS[name], build=None, handler=handler, emoji=emoji)


SPECS: list[ToolSpec] = [
    _spec("bento_subscriber_get", subscriber_get, "🔎"),
    _spec("bento_tag_list", tag_list, "🏷️"),
    _spec("bento_field_list", field_list, "🧾"),
    _spec("bento_broadcast_list", broadcast_list, "📋"),
    _spec("bento_stats", stats, "📊"),
]

__all__ = ["SPECS"]
