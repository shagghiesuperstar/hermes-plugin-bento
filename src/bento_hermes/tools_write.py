"""Write tools: subscribers, tags, fields, events, purchases (tiers T1/T2).

Builders are PURE (no network): they validate and normalize arguments into the exact request that
would be sent. The safety layer hashes that request, previews it, gates it, then sends it.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .safety import T1, T2, Built, ToolSpec, ValidationError, sanitize_untrusted
from .schemas import SCHEMAS
from .tools_read import (
    as_currency,
    as_email,
    as_int,
    as_iso,
    extract_names,
    flat_dict,
    iso_z,
    opt_str,
    reject_unknown,
    req_str,
    tag_names,
)

_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PURCHASE_TYPES = frozenset({"$purchase", "purchase", "order", "order_complete", "event_sale"})
_SUBSCRIBER_KEYS = {
    "email", "first_name", "last_name", "tags", "remove_tags", "fields", "utm_source", "utm_medium",
    "utm_campaign", "utm_content", "utm_term", "created_at", "lifetime_value_currency", "lifetime_value_amount",
}
_RESERVED = _SUBSCRIBER_KEYS - {"fields"}
_UTM = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")


def _normalize(name: str) -> str:
    return re.sub(r"[\s_-]+", "", name.lower())


# -- subscribers ---------------------------------------------------------------------------------------------
def _subscriber_entry(raw: Any, index: int) -> dict:
    if not isinstance(raw, dict):
        raise ValidationError(f"subscribers[{index}] must be an object")
    unknown = sorted(set(raw) - _SUBSCRIBER_KEYS)
    if unknown:
        raise ValidationError(f"subscribers[{index}]: unknown key(s) {', '.join(unknown)}")
    entry: dict[str, Any] = {"email": as_email(raw.get("email"), f"subscribers[{index}].email")}
    for key in ("first_name", "last_name", *_UTM):
        value = opt_str(raw, key, max_len=255)
        if value is not None:
            entry[key] = value
    for key in ("tags", "remove_tags"):
        names = tag_names(raw.get(key), f"subscribers[{index}].{key}")
        if names:
            entry[key] = ",".join(names)  # docs: comma-separated STRING
    if raw.get("created_at") is not None:
        entry["created_at"] = iso_z(as_iso(raw["created_at"], f"subscribers[{index}].created_at"))
    has_cur, has_amt = raw.get("lifetime_value_currency") is not None, raw.get("lifetime_value_amount") is not None
    if has_cur != has_amt:
        raise ValidationError(f"subscribers[{index}]: lifetime_value_currency and lifetime_value_amount go together")
    if has_cur:
        entry["lifetime_value_currency"] = as_currency(raw["lifetime_value_currency"], "lifetime_value_currency")
        entry["lifetime_value_amount"] = as_int(raw["lifetime_value_amount"], "lifetime_value_amount")
    for key, value in flat_dict(raw.get("fields"), f"subscribers[{index}].fields").items():
        if key in _RESERVED or key == "fields":
            raise ValidationError(f"subscribers[{index}].fields: {key!r} collides with a reserved subscriber attribute")
        entry[key] = value
    return entry


def build_subscriber_upsert(rt: Any, settings: Any, args: dict) -> Built:
    reject_unknown(args, {"subscribers", "dry_run", "confirm_token"})
    raw = args.get("subscribers")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 1000:
        raise ValidationError("subscribers must be a list of 1-1000 objects")
    entries = [_subscriber_entry(item, i) for i, item in enumerate(raw)]
    emails = [e["email"] for e in entries]
    tags = sorted({t for e in entries for t in e.get("tags", "").split(",") if t})
    warnings = ["Import is queued: it can take up to 5 minutes to appear. It does not trigger Flows or Automations."]
    dupes = [e for e, n in Counter(x.lower() for x in emails).items() if n > 1]
    if dupes:
        warnings.append(f"{len(dupes)} email(s) appear more than once in this batch.")
    if len(entries) > 300:
        warnings.append("Bento's docs recommend 200-300 subscribers per call for large syncs; timeouts are likelier above that.")
    tier = T1 if len(entries) <= settings.bulk_threshold else T2

    def context(rt_: Any) -> dict:
        known, _ = rt_.cached_get("/fetch/tags")
        existing = {n.lower() for n in extract_names(known)}
        unknown = [t for t in tags if t.lower() not in existing]
        return {"tags_not_yet_defined": unknown, "note": "Unknown tags are created implicitly by the import."}

    return Built("POST", "/batch/subscribers", {"subscribers": entries}, tier, count=len(entries),
                 summary={"subscribers": len(entries), "tags_added": ",".join(tags[:10]) or "-", "bulk": tier == T2},
                 warnings=warnings, recipients=emails, context=context)


def build_subscriber_tag(rt: Any, settings: Any, args: dict) -> Built:
    reject_unknown(args, {"email", "action", "tag"})
    email = as_email(args.get("email"))
    action = args.get("action")
    if action not in ("add", "remove"):
        raise ValidationError("action must be 'add' or 'remove'")
    tag = req_str(args, "tag", max_len=128).strip()
    if "," in tag:
        raise ValidationError("tag must not contain a comma")
    body = {"command": {"command": f"{action}_tag", "email": email, "query": tag}}
    return Built("POST", "/fetch/commands", body, T1, summary={"action": f"{action}_tag", "tag": tag},
                 warnings=["Async command: Bento returns only a queued count, not the updated subscriber."],
                 recipients=[email])


def build_subscriber_consent(rt: Any, settings: Any, args: dict) -> Built:
    reject_unknown(args, {"email", "action", "reason", "dry_run", "confirm_token"})
    email = as_email(args.get("email"))
    action = args.get("action")
    if action not in ("subscribe", "unsubscribe"):
        raise ValidationError("action must be 'subscribe' or 'unsubscribe'")
    reason = opt_str(args, "reason", max_len=500)
    if action == "subscribe" and not reason:
        raise ValidationError("reason is required for subscribe: record where this person's consent came from")
    warnings = ["Async command: Bento returns only a queued count."]
    if action == "subscribe":
        warnings.append("subscribe RE-SUBSCRIBES a previously unsubscribed address. Confirm the person opted back in.")

    def context(rt_: Any) -> dict:
        data = rt_.client().get("/fetch/subscribers", {"email": email})
        attrs = (data.get("data") or {}).get("attributes", {}) if isinstance(data, dict) else {}
        return {"currently_unsubscribed_at": attrs.get("unsubscribed_at") if isinstance(attrs, dict) else None}

    summary: dict[str, Any] = {"action": action, "email": email}
    if reason:
        summary["reason"] = reason
    return Built("POST", "/fetch/commands", {"command": {"command": action, "email": email}}, T2, summary=summary,
                 warnings=warnings, recipients=[email], audit_extra={"reason": reason} if reason else {}, context=context)


# -- tags / fields ------------------------------------------------------------------------------------------------
def _definition_builder(kind: str, list_path: str, post_path: str, wrapper: str, key: str, pattern: re.Pattern, hint: str):
    def build(rt: Any, settings: Any, args: dict) -> Built:
        reject_unknown(args, {key})
        value = req_str(args, key, max_len=64).strip()
        if not pattern.match(value):
            raise ValidationError(f"{kind} {key} {value!r} is not allowed: {hint}")

        def precheck(rt_: Any) -> str | dict | None:
            existing, _ = rt_.cached_get(list_path, ttl=60.0)
            names = extract_names(existing)
            if value in names:
                return {"ok": True, "tool": f"bento_{kind}_create", "created": False, "exists": True, "sent": False,
                        "note": f"{kind} {value!r} already exists; nothing was sent."}
            near = [n for n in names if _normalize(n) == _normalize(value)]
            if near:
                return f"near-duplicate of existing {kind}(s) {near}; use the existing one"
            return None

        def after_send(rt_: Any, ok: bool) -> None:
            rt_.invalidate(list_path)

        return Built("POST", post_path, {wrapper: {key: value}}, T1, summary={key: value},
                     warnings=[f"Creates the {kind} DEFINITION only; it is not applied to anyone."],
                     precheck=precheck, after_send=after_send)

    return build


build_tag_create = _definition_builder(
    "tag", "/fetch/tags", "/fetch/tags", "tag", "name", _TAG_RE, "use lowercase letters, digits, '-' or '_' (max 64)")
build_field_create = _definition_builder(
    "field", "/fetch/fields", "/fetch/fields", "field", "key", _FIELD_RE, "use lowercase_snake_case starting with a letter (max 64)")


# -- events / purchases ---------------------------------------------------------------------------------------------
def build_event_track(rt: Any, settings: Any, args: dict) -> Built:
    reject_unknown(args, {"events", "dry_run", "confirm_token"})
    raw = args.get("events")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 1000:
        raise ValidationError("events must be a list of 1-1000 objects")
    events, emails = [], []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValidationError(f"events[{i}] must be an object")
        unknown = sorted(set(item) - {"type", "email", "fields", "details", "date"})
        if unknown:
            raise ValidationError(f"events[{i}]: unknown key(s) {', '.join(unknown)}")
        etype = req_str(item, "type", max_len=255).strip()
        if etype.lower() in PURCHASE_TYPES:
            raise ValidationError(f"events[{i}]: purchase events must go through bento_purchase_track (real order id, integer cents)")
        event: dict[str, Any] = {"type": etype, "email": as_email(item.get("email"), f"events[{i}].email")}
        fields = flat_dict(item.get("fields"), f"events[{i}].fields")
        if fields:
            event["fields"] = fields
        if item.get("details") is not None:
            if not isinstance(item["details"], dict):
                raise ValidationError(f"events[{i}].details must be an object")
            event["details"] = item["details"]
        if item.get("date") is not None:
            event["date"] = iso_z(as_iso(item["date"], f"events[{i}].date"))
        events.append(event)
        emails.append(event["email"])
    types = Counter(e["type"] for e in events)
    return Built("POST", "/batch/events", {"events": events}, T2, count=len(events),
                 summary={"events": len(events), "types": ", ".join(f"{t} x{n}" for t, n in list(types.items())[:6])},
                 warnings=["Events create unknown subscribers and CAN TRIGGER FLOWS that send real email."],
                 recipients=emails)


def build_purchase_track(rt: Any, settings: Any, args: dict) -> Built:
    reject_unknown(args, {"email", "order_id", "amount_cents", "currency", "items", "fields", "date", "dry_run", "confirm_token"})
    email = as_email(args.get("email"))
    order_id = req_str(args, "order_id", max_len=255).strip()
    amount = as_int(args.get("amount_cents"), "amount_cents")
    currency = as_currency(args.get("currency"))
    details: dict[str, Any] = {"unique": {"key": order_id}, "value": {"currency": currency, "amount": amount}}
    raw_items = args.get("items")
    if raw_items is not None:
        if not isinstance(raw_items, list):
            raise ValidationError("items must be a list")
        items = []
        for i, item in enumerate(raw_items):
            if not isinstance(item, dict):
                raise ValidationError(f"items[{i}] must be an object")
            unknown = sorted(set(item) - {"product_sku", "product_name", "quantity", "price_cents"})
            if unknown:
                raise ValidationError(f"items[{i}]: unknown key(s) {', '.join(unknown)}")
            entry: dict[str, Any] = {}
            for key in ("product_sku", "product_name"):
                if (text := opt_str(item, key, max_len=255)) is not None:
                    entry[key] = text
            if item.get("quantity") is not None:
                entry["quantity"] = as_int(item["quantity"], f"items[{i}].quantity", low=1)
            if item.get("price_cents") is not None:
                # Bento's JS docs show cart.items[].price in the same cents unit as value.amount
                entry["price"] = as_int(item["price_cents"], f"items[{i}].price_cents")
            items.append(entry)
        if items:
            details["cart"] = {"items": items}
    event: dict[str, Any] = {"type": "$purchase", "email": email, "details": details}
    fields = flat_dict(args.get("fields"), "fields")
    if fields:
        event["fields"] = fields
    if args.get("date") is not None:
        event["date"] = iso_z(as_iso(args["date"], "date"))
    return Built("POST", "/batch/events", {"events": [event]}, T2, idempotent=True,
                 summary={"order_id": order_id, "amount": f"{amount} cents {currency}", "email": email},
                 warnings=["order_id is the dedupe key (details.unique.key): reusing it cannot double-count revenue."],
                 recipients=[email])


# -- registry ----------------------------------------------------------------------------------------------------------
def _spec(name: str, tier: int, build: Any, emoji: str, *, dry: bool = False) -> ToolSpec:
    return ToolSpec(name=name, tier=tier, schema=SCHEMAS[name], build=build, emoji=emoji, supports_dry_run=dry)


SPECS: list[ToolSpec] = [
    _spec("bento_subscriber_upsert", T2, build_subscriber_upsert, "📥", dry=True),
    _spec("bento_subscriber_tag", T1, build_subscriber_tag, "🏷️"),
    _spec("bento_subscriber_consent", T2, build_subscriber_consent, "✅", dry=True),
    _spec("bento_tag_create", T1, build_tag_create, "➕"),
    _spec("bento_field_create", T1, build_field_create, "🧱"),
    _spec("bento_event_track", T2, build_event_track, "⚡", dry=True),
    _spec("bento_purchase_track", T2, build_purchase_track, "💳", dry=True),
]

__all__ = ["SPECS", "sanitize_untrusted"]
