"""Send tools: draft broadcasts (T1), scheduled broadcasts and transactional email (T3).

Everything here that reaches real people is T3: dry-run by default, hash-bound confirm token,
human approval, hard guards that configuration cannot relax (explicit audience, allowed sender,
lead time, batch cap, recipient cap, duplicate suppression, no POST retries).
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from .cache import email_fingerprint
from .safety import T1, T3, Built, ToolSpec, ValidationError, clean_line
from .schemas import SCHEMAS
from .tools_read import (
    as_email,
    as_id,
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

MAX_SCHEDULE_DAYS = 90
BENTO_EMAIL_CAP = 60
_EMOJI_RE = re.compile("[\U0001f300-\U0001faff\u2600-\u27bf]")
_SHORTENERS = ("bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly", "is.gd", "buff.ly", "rebrand.ly", "cutt.ly")
_BROADCAST_KEYS = {
    "name", "subject", "content", "type", "from_name", "from_email", "inclusive_tags", "segment_id",
    "exclusive_tags", "batch_size_per_hour", "preview_text",
}


def _lint(subject: str, body: str) -> list[str]:
    notes = []
    if _EMOJI_RE.search(subject) or _EMOJI_RE.search(body):
        notes.append("Emoji in subject/body: Bento's docs say risky content can throttle sends.")
    lowered = body.lower()
    if any(s in lowered for s in _SHORTENERS):
        notes.append("URL shortener detected: Bento's docs say these can throttle sends.")
    return notes


def _snippet(html: str) -> str:
    """Tag-stripped start of a message body, shown to the human approver (who otherwise only sees a hash)."""
    return clean_line(re.sub(r"<[^>]+>", " ", html), 70)

# -- broadcasts ----------------------------------------------------------------------------------------------------
def _broadcast_entry(args: dict) -> tuple[dict, dict]:
    inclusive = tag_names(args.get("inclusive_tags"), "inclusive_tags")
    segment = args.get("segment_id")
    if bool(inclusive) == (segment is not None):
        raise ValidationError("an explicit audience is mandatory: provide exactly one of inclusive_tags or segment_id")
    entry: dict[str, Any] = {
        "name": req_str(args, "name", max_len=255),
        "subject": req_str(args, "subject", max_len=998),
        "content": req_str(args, "content", max_len=2_000_000),
        "from": {"name": req_str(args, "from_name", max_len=255), "email": as_email(args.get("from_email"), "from_email")},
    }
    if args.get("type") is not None:
        if args["type"] not in ("plain", "raw"):
            raise ValidationError("type must be 'plain' or 'raw'")
        entry["type"] = args["type"]
    if inclusive:
        entry["inclusive_tags"] = ",".join(inclusive)
    else:
        entry["segment_id"] = as_id(segment, "segment_id")
    exclusive = tag_names(args.get("exclusive_tags"), "exclusive_tags")
    if exclusive:
        entry["exclusive_tags"] = ",".join(exclusive)
    if args.get("batch_size_per_hour") is not None:
        entry["batch_size_per_hour"] = as_int(args["batch_size_per_hour"], "batch_size_per_hour", low=1)
    if (snippet := opt_str(args, "preview_text", max_len=500)) is not None:
        entry["inbox_snippet"] = snippet
    audience = {"tags": inclusive, "segment_id": entry.get("segment_id")}
    return entry, audience


def build_broadcast_create(rt: Any, settings: Any, args: dict) -> Built:
    for forbidden in ("approved", "send_at"):
        if forbidden in args:
            raise ValidationError(f"{forbidden} is not accepted here: this tool only creates drafts. Use bento_broadcast_schedule to schedule a send.")
    reject_unknown(args, _BROADCAST_KEYS)
    entry, audience = _broadcast_entry(args)
    # the schema/body never carries approved or send_at: a draft stays a draft
    return Built("POST", "/batch/broadcasts", {"broadcasts": [entry]}, T1,
                 summary={"name": entry["name"], "subject": entry["subject"], "audience": _audience_text(audience),
                          "body": _snippet(entry["content"])},
                 warnings=["Draft only: a human must review and send it from the Bento dashboard (see dashboard_url in the response).",
                           *_lint(entry["subject"], entry["content"])])


def _audience_text(audience: dict) -> str:
    return f"segment {audience['segment_id']}" if audience.get("segment_id") else "tags " + ",".join(audience["tags"])


def build_broadcast_schedule(rt: Any, settings: Any, args: dict) -> Built:
    reject_unknown(args, _BROADCAST_KEYS | {"send_at", "acknowledge_unknown_audience", "dry_run", "confirm_token"})
    entry, audience = _broadcast_entry(args)
    if not settings.allowed_senders:
        raise ValidationError("refused: allowed_senders is empty. Set plugins.entries.bento.settings.allowed_senders to the verified Bento Author address(es) first.")
    if entry["from"]["email"].lower() not in settings.allowed_senders:
        raise ValidationError(f"refused: from_email {entry['from']['email']!r} is not in allowed_senders")
    if "batch_size_per_hour" not in entry:
        raise ValidationError("batch_size_per_hour is required when scheduling a send")
    if entry["batch_size_per_hour"] > settings.max_batch_size_per_hour:
        raise ValidationError(f"batch_size_per_hour {entry['batch_size_per_hour']} exceeds the configured maximum {settings.max_batch_size_per_hour}")
    send_at = as_iso(args.get("send_at"), "send_at")
    now = datetime.fromtimestamp(rt.clock(), UTC)
    if send_at < now + timedelta(minutes=settings.min_schedule_lead_minutes):
        raise ValidationError(f"send_at must be at least {settings.min_schedule_lead_minutes} minutes in the future (UTC now is {iso_z(now)})")
    if send_at > now + timedelta(days=MAX_SCHEDULE_DAYS):
        raise ValidationError(f"send_at must be within {MAX_SCHEDULE_DAYS} days")
    entry["send_at"] = iso_z(send_at)
    entry["approved"] = True  # docs: send_at is REQUIRED when approved:true; this schedules a real send

    acks: list[str] = []
    if audience["tags"] and args.get("acknowledge_unknown_audience") is not True:
        acks.append("acknowledge_unknown_audience=true (the API cannot report how many people a tag audience contains)")

    def context(rt_: Any) -> dict:
        out: dict[str, Any] = {}
        if audience["segment_id"]:
            data, _ = rt_.cached_get("/stats/segment", {"segment_id": audience["segment_id"]},
                                     ttl=float(rt_.settings().stats_cache_ttl_seconds))
            out["segment_counts"] = data
        else:
            known, _ = rt_.cached_get("/fetch/tags")
            existing = {n.lower() for n in extract_names(known)}
            out["tags_not_defined_in_bento"] = [t for t in audience["tags"] if t.lower() not in existing]
            out["audience_size"] = "unknown: Bento's API cannot count a tag audience"
        out["hours_to_deliver_at_batch_size"] = "audience size / batch_size_per_hour"
        return out

    return Built("POST", "/batch/broadcasts", {"broadcasts": [entry]}, T3,
                 summary={"name": entry["name"], "subject": entry["subject"], "from": entry["from"]["email"],
                          "audience": _audience_text(audience), "send_at": entry["send_at"],
                          "batch_per_hour": entry["batch_size_per_hour"], "body": _snippet(entry["content"])},
                 warnings=["This schedules a REAL send. The documented API has no cancel/edit endpoint: cancelling is dashboard-only.",
                           *_lint(entry["subject"], entry["content"])],
                 acks_missing=acks, context=context)


# -- transactional email -----------------------------------------------------------------------------------------------
def build_email_send(rt: Any, settings: Any, args: dict) -> Built:
    reject_unknown(args, {"from", "emails", "transactional", "reason", "allow_duplicate", "dry_run", "confirm_token"})
    sender = as_email(args.get("from"), "from")
    if not settings.allowed_senders:
        raise ValidationError("refused: allowed_senders is empty. Set plugins.entries.bento.settings.allowed_senders to the verified Bento Author address(es) first.")
    if sender.lower() not in settings.allowed_senders:
        raise ValidationError(f"refused: from {sender!r} is not in allowed_senders")
    raw = args.get("emails")
    if not isinstance(raw, list) or not raw:
        raise ValidationError("emails must be a non-empty list")
    if len(raw) > BENTO_EMAIL_CAP:
        raise ValidationError(f"Bento accepts at most {BENTO_EMAIL_CAP} emails per request (got {len(raw)})")
    if len(raw) > settings.max_recipients_per_call:
        raise ValidationError(f"{len(raw)} recipients exceeds max_recipients_per_call={settings.max_recipients_per_call}; split the call or raise the setting (max {BENTO_EMAIL_CAP})")
    transactional = args.get("transactional", False)
    if not isinstance(transactional, bool):
        raise ValidationError("transactional must be a JSON boolean")
    reason = opt_str(args, "reason", max_len=500)
    if transactional:
        if not settings.allow_transactional_override:
            raise ValidationError("refused: transactional=true bypasses unsubscribe suppression and is disabled (settings.allow_transactional_override=false)")
        if not reason:
            raise ValidationError("reason is required when transactional=true (Bento: do not use this to bypass marketing consent)")

    entries, tos, fingerprints, warnings = [], [], [], []
    missing_unsub = personalized = 0
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValidationError(f"emails[{i}] must be an object")
        unknown = sorted(set(item) - {"to", "subject", "html_body", "personalizations"})
        if unknown:
            raise ValidationError(f"emails[{i}]: unknown key(s) {', '.join(unknown)}")
        to = as_email(item.get("to"), f"emails[{i}].to")
        subject = req_str(item, "subject", max_len=998)
        body = req_str(item, "html_body", max_len=2_000_000)
        entry: dict[str, Any] = {"to": to, "from": sender, "subject": subject, "html_body": body, "transactional": transactional}
        personalizations = flat_dict(item.get("personalizations"), f"emails[{i}].personalizations")
        if personalizations:
            entry["personalizations"] = personalizations
            personalized += 1
        if not transactional and "visitor.unsubscribe_url" not in body:
            missing_unsub += 1
        for note in _lint(subject, body):
            if note not in warnings:
                warnings.append(note)
        entries.append(entry)
        tos.append(to)
        fingerprints.append(email_fingerprint(rt.site_fingerprint(), to, subject, body))
    if missing_unsub:
        warnings.append(f"{missing_unsub} email(s) lack {{{{ visitor.unsubscribe_url }}}}; Bento requires it on non-transactional/marketing-like mail.")
    if personalized:
        warnings.append("personalizations present: user-controlled values (names etc.) can throttle sends to 1/hour per Bento's docs.")
    if transactional:
        warnings.append("transactional=true sends EVEN TO UNSUBSCRIBED people.")
    warnings.append("Bento returns only a queue-accept count; unsubscribed/suppressed recipients are dropped silently. Accepted != delivered.")

    def context(rt_: Any) -> dict:
        return {"duplicates_in_last_24h": len(rt_.ledger.seen(fingerprints)),
                "throttle_eta_minutes": math.ceil(len(entries) / BENTO_EMAIL_CAP),
                "recipient_cap": {"call": len(entries), "max_recipients_per_call": settings.max_recipients_per_call}}

    def precheck(rt_: Any) -> str | None:
        dupes = rt_.ledger.seen(fingerprints)
        if dupes and args.get("allow_duplicate") is not True:
            return f"{len(dupes)} email(s) are identical to ones sent in the last 24h; set allow_duplicate=true to send anyway"
        return None

    def after_send(rt_: Any, accepted_or_unknown: bool) -> None:
        if accepted_or_unknown:  # an ambiguous failure may still have sent: block blind resends too
            rt_.ledger.record(fingerprints)

    shown = ", ".join(tos[:3]) + (f" +{len(tos) - 3} more" if len(tos) > 3 else "")
    summary = {"from": sender, "to": shown, "subject": entries[0]["subject"], "body": _snippet(entries[0]["html_body"]),
               "transactional": transactional}
    if reason:
        summary["reason"] = reason
    return Built("POST", "/batch/emails", {"emails": entries}, T3, count=len(entries), summary=summary, warnings=warnings,
                 recipients=tos, audit_extra={"reason": reason} if reason else {}, context=context,
                 precheck=precheck, after_send=after_send)


SPECS: list[ToolSpec] = [
    ToolSpec("bento_broadcast_create", T1, SCHEMAS["bento_broadcast_create"], build_broadcast_create, emoji="📝"),
    ToolSpec("bento_broadcast_schedule", T3, SCHEMAS["bento_broadcast_schedule"], build_broadcast_schedule, emoji="📣",
             dry_run_default=True, supports_dry_run=True),
    ToolSpec("bento_email_send", T3, SCHEMAS["bento_email_send"], build_email_send, emoji="✉️",
             dry_run_default=True, supports_dry_run=True),
]
