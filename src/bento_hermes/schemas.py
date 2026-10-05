"""Tool JSON schemas. Descriptions tell the model when and how to call each tool."""

from __future__ import annotations

_EMAIL = {"type": "string", "description": "Email address."}
_DRY = {"type": "boolean", "description": "Preview only: returns the exact request and a confirm_token; sends nothing."}
_TOKEN = {"type": "string", "description": "confirm_token returned by the dry run of these IDENTICAL parameters."}
_ISO = {"type": "string", "description": "ISO-8601 timestamp, e.g. 2026-10-20T14:00:00Z."}
_FLAT = {"type": "object", "description": "Flat key/value object (string/number/boolean values; no nested objects or arrays)."}
_TAGS = {"type": "array", "items": {"type": "string"}, "description": "Tag names (no commas)."}

_AUDIENCE = {
    "inclusive_tags": {**_TAGS, "description": "Audience: send to subscribers having these tags. Provide this or segment_id."},
    "segment_id": {"type": "string", "description": "Audience: Bento segment id. Provide this or inclusive_tags."},
    "exclusive_tags": {**_TAGS, "description": "Exclude subscribers having any of these tags."},
}


def _schema(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False},
    }


def _broadcast_props() -> dict:
    return {
        "name": {"type": "string", "description": "Internal broadcast name."},
        "subject": {"type": "string"},
        "content": {"type": "string", "description": "Broadcast body (Liquid allowed)."},
        "type": {"type": "string", "enum": ["plain", "raw"], "description": "plain = Bento styling; raw = your full HTML document."},
        "from_name": {"type": "string"},
        "from_email": {"type": "string", "description": "Must be a verified Bento Author."},
        **_AUDIENCE,
        "batch_size_per_hour": {"type": "integer", "minimum": 1, "description": "Send pacing."},
        "preview_text": {"type": "string", "description": "Inbox preview snippet."},
    }


_BROADCAST_REQUIRED = ["name", "subject", "content", "from_name", "from_email"]

SCHEMAS: dict[str, dict] = {
    "bento_subscriber_get": _schema(
        "bento_subscriber_get",
        "Look up ONE Bento subscriber by email or uuid. Returns fields, tags and unsubscribed_at (null = currently subscribed). Read-only.",
        {"email": _EMAIL, "uuid": {"type": "string", "description": "Bento subscriber uuid (alternative to email)."}},
    ),
    "bento_subscriber_upsert": _schema(
        "bento_subscriber_upsert",
        "Import/upsert subscribers (1-1000) with names, tags and custom fields. Does NOT trigger Flows. Async: can take up to 5 minutes "
        "to appear. Never use to re-subscribe people who opted out. Batches above the bulk threshold need human approval; "
        "use dry_run=true to preview.",
        {
            "subscribers": {"type": "array", "minItems": 1, "maxItems": 1000, "items": {
                "type": "object",
                "properties": {
                    "email": _EMAIL, "first_name": {"type": "string"}, "last_name": {"type": "string"},
                    "tags": _TAGS, "remove_tags": _TAGS, "fields": _FLAT,
                    "utm_source": {"type": "string"}, "utm_medium": {"type": "string"}, "utm_campaign": {"type": "string"},
                    "utm_content": {"type": "string"}, "utm_term": {"type": "string"},
                    "created_at": _ISO,
                    "lifetime_value_currency": {"type": "string", "description": "3-letter ISO-4217 code."},
                    "lifetime_value_amount": {"type": "integer", "minimum": 0, "description": "Integer CENTS."},
                },
                "required": ["email"], "additionalProperties": False}},
            "dry_run": _DRY, "confirm_token": _TOKEN,
        },
        ["subscribers"],
    ),
    "bento_subscriber_tag": _schema(
        "bento_subscriber_tag",
        "Add or remove ONE tag on ONE subscriber (async command; returns only a queued count). Does not trigger automations.",
        {"email": _EMAIL, "action": {"type": "string", "enum": ["add", "remove"]}, "tag": {"type": "string"}},
        ["email", "action", "tag"],
    ),
    "bento_subscriber_consent": _schema(
        "bento_subscriber_consent",
        "Subscribe or unsubscribe ONE person. 'subscribe' RE-SUBSCRIBES people who opted out - a compliance hazard - so it needs a "
        "written reason and human approval. Never call it from a sync loop. Use dry_run=true first.",
        {"email": _EMAIL, "action": {"type": "string", "enum": ["subscribe", "unsubscribe"]},
         "reason": {"type": "string", "description": "Required for subscribe: where the person's consent came from."},
         "dry_run": _DRY, "confirm_token": _TOKEN},
        ["email", "action"],
    ),
    "bento_tag_list": _schema("bento_tag_list", "List all tag names in the Bento site (cached 5 min). Always list before creating a tag.", {}),
    "bento_tag_create": _schema(
        "bento_tag_create",
        "Create a tag DEFINITION (does not apply it to anyone). Lowercase letters, digits, hyphen/underscore only. "
        "Refuses if the tag exists or a near-duplicate exists.",
        {"name": {"type": "string"}}, ["name"],
    ),
    "bento_field_list": _schema("bento_field_list", "List all custom field names in the Bento site (cached 5 min). Always list before creating a field.", {}),
    "bento_field_create": _schema(
        "bento_field_create",
        "Create a custom field DEFINITION (no values). Key must be lowercase_snake_case. Refuses if it exists.",
        {"key": {"type": "string"}}, ["key"],
    ),
    "bento_event_track": _schema(
        "bento_event_track",
        "Track custom events (1-1000). Creates unknown subscribers, updates their fields, and CAN TRIGGER FLOWS that send real email, "
        "so it needs human approval; use dry_run=true first. Be very restrictive about event types. For purchases use "
        "bento_purchase_track, not this tool.",
        {"events": {"type": "array", "minItems": 1, "maxItems": 1000, "items": {
            "type": "object",
            "properties": {"type": {"type": "string", "description": "Event type, e.g. $signed_up."}, "email": _EMAIL,
                           "fields": _FLAT, "details": {"type": "object", "description": "Event metadata (may be nested)."},
                           "date": _ISO},
            "required": ["type", "email"], "additionalProperties": False}},
         "dry_run": _DRY, "confirm_token": _TOKEN},
        ["events"],
    ),
    "bento_purchase_track": _schema(
        "bento_purchase_track",
        "Record ONE purchase ($purchase) so revenue/LTV update. order_id is the dedupe key: pass the real order id, never invent one. "
        "Amounts are integer CENTS. Triggers purchase Flows, so it needs human approval; use dry_run=true first.",
        {"email": _EMAIL, "order_id": {"type": "string", "description": "Stable order id (becomes details.unique.key)."},
         "amount_cents": {"type": "integer", "minimum": 0}, "currency": {"type": "string", "description": "ISO-4217, e.g. USD."},
         "items": {"type": "array", "items": {"type": "object", "properties": {
             "product_sku": {"type": "string"}, "product_name": {"type": "string"},
             "quantity": {"type": "integer", "minimum": 1}, "price_cents": {"type": "integer", "minimum": 0}},
             "additionalProperties": False}},
         "fields": _FLAT, "date": _ISO, "dry_run": _DRY, "confirm_token": _TOKEN},
        ["email", "order_id", "amount_cents", "currency"],
    ),
    "bento_broadcast_list": _schema(
        "bento_broadcast_list",
        "List broadcasts by status (omitting status returns only SENT ones; pass status=draft to find drafts). Read-only.",
        {"status": {"type": "string", "enum": ["sent", "draft", "sending", "scheduled", "paused", "canceled"]},
         "page": {"type": "integer", "minimum": 1}},
    ),
    "bento_broadcast_create": _schema(
        "bento_broadcast_create",
        "Create a DRAFT broadcast for a human to review and send from the Bento dashboard (response has dashboard_url). "
        "This tool cannot send or schedule. An explicit audience (inclusive_tags or segment_id) is mandatory.",
        _broadcast_props(), _BROADCAST_REQUIRED,
    ),
    "bento_broadcast_schedule": _schema(
        "bento_broadcast_schedule",
        "Create AND APPROVE a broadcast for sending at send_at - real email to real people, not cancellable via API. Always "
        "dry_run first (default). The real call needs dry_run=false + confirm_token from that dry run + human approval. "
        "from_email must be in the allowed_senders setting. Tag audiences have unknown size: the real call also needs "
        "acknowledge_unknown_audience=true.",
        {**_broadcast_props(),
         "send_at": {**_ISO, "description": "UTC start time, at least the configured lead time in the future, max 90 days out."},
         "batch_size_per_hour": {"type": "integer", "minimum": 1, "description": "REQUIRED send pacing."},
         "acknowledge_unknown_audience": {"type": "boolean", "description": "Real call only: confirm you accept sending to a tag audience whose size the API cannot report."},
         "dry_run": {**_DRY, "description": "Defaults to TRUE. Preview only; returns the exact payload and confirm_token."},
         "confirm_token": _TOKEN},
        [*_BROADCAST_REQUIRED, "send_at", "batch_size_per_hour"],
    ),
    "bento_email_send": _schema(
        "bento_email_send",
        "Send transactional email through Bento (max 60 per call, plugin cap lower by default). Real email to real people. "
        "Always dry_run first (default). The real call needs dry_run=false + confirm_token + human approval. 'from' must be a "
        "verified Bento Author listed in allowed_senders. transactional=false (default) honors unsubscribes; true bypasses them "
        "and is refused unless enabled in settings and a reason is given. Accepted != delivered.",
        {"from": {"type": "string", "description": "Verified Author email."},
         "emails": {"type": "array", "minItems": 1, "maxItems": 60, "items": {
             "type": "object",
             "properties": {"to": _EMAIL, "subject": {"type": "string"},
                            "html_body": {"type": "string", "description": "HTML (Liquid ok). Marketing-like mail must include {{ visitor.unsubscribe_url }}."},
                            "personalizations": _FLAT},
             "required": ["to", "subject", "html_body"], "additionalProperties": False}},
         "transactional": {"type": "boolean", "description": "Default false. true sends even to unsubscribed people."},
         "reason": {"type": "string", "description": "Required when transactional=true."},
         "allow_duplicate": {"type": "boolean", "description": "Real call only: send even if an identical email went out in the last 24h."},
         "dry_run": {**_DRY, "description": "Defaults to TRUE. Preview only; returns the exact payload and confirm_token."},
         "confirm_token": _TOKEN},
        ["from", "emails"],
    ),
    "bento_stats": _schema(
        "bento_stats",
        "Read Bento stats: site counts, a segment's counts, a saved report, or ads attribution. Bento allows only 30 stats calls/hour "
        "so results are cached (default 15 min). Money values are cents.",
        {"kind": {"type": "string", "enum": ["site", "segment", "report", "ads"]},
         "segment_id": {"type": "string", "description": "Required for kind=segment."},
         "report_id": {"type": "string", "description": "Required for kind=report."},
         "dimension": {"type": "string", "enum": ["source", "campaign", "medium", "content", "ad"]},
         "value": {"type": "string", "description": "ads: dimension value to filter."},
         "start_date": {"type": "string"}, "end_date": {"type": "string"},
         "revenue_mode": {"type": "string", "enum": ["all_revenue", "first_revenue", "subscriptions"]}},
        ["kind"],
    ),
}
