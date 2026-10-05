# Bento plugin: quick start and send runbook

The full reference (install, credentials, every tool, the safety model, settings, disclosure) is the
[README](../README.md). Once the plugin is listed in the Hermes catalog, the same README renders at
`https://hermes-agent.nousresearch.com/docs/plugins/bento` (the page is generated from the catalog entry and shows the README at the pinned commit).

## 5-minute setup

1. `hermes plugins install shagghiesuperstar/hermes-plugin-bento` then `hermes plugins enable bento`.
2. Put `BENTO_PUBLISHABLE_KEY`, `BENTO_SECRET_KEY`, `BENTO_SITE_UUID` in the profile's `.env` (one profile per Bento site).
3. `hermes config set plugins.entries.bento.settings.allowed_senders '["news@yourbrand.com"]'` (a verified Bento Author address).
4. `hermes bento status` should print `status: OK`. `hermes bento doctor` lints settings offline.

## Runbook: sending a transactional email

```text
# 1. preview (default; sends nothing)
bento_email_send {"from": "news@yourbrand.com",
                  "emails": [{"to": "jane@example.com", "subject": "Your order",
                              "html_body": "<p>Thanks! {{ visitor.unsubscribe_url }}</p>"}]}
  -> {"dry_run": true, "sent": false, "request": {"body": {...exact payload...}},
      "warnings": [...], "context": {...}, "confirm_token": "v1....", "confirm_token_expires_at": "..."}

# 2. review the exact payload with the user, then repeat IDENTICAL parameters plus:
bento_email_send {... same parameters ..., "dry_run": false, "confirm_token": "v1...."}
  -> Hermes shows a human approval prompt (recipients, sender, subject, payload hash)
  -> approved: {"ok": true, "sent": true, "bento_response": {"results": 1}}
  -> denied / timed out / no human present: nothing is sent
```

Refusals are explicit and safe to retry after fixing the input, for example
`refused: no confirm_token. Run this call with dry_run=true first ...` or `refused: payload changed since preview`.

## Runbook: scheduling a broadcast

1. `bento_tag_list` (or a segment id) to pick a real audience. A tag audience has unknown size: add `acknowledge_unknown_audience=true` on the real call.
2. `bento_broadcast_schedule` with `send_at` (UTC, at least 30 minutes ahead), `batch_size_per_hour`, `from_email` (in `allowed_senders`): dry run is the default.
3. Same token + approval flow as above. The documented Bento API has no cancel/edit endpoint: cancelling a scheduled broadcast is dashboard-only.
4. To prepare content for a human to send instead, use `bento_broadcast_create` (draft only) and hand over the returned `dashboard_url`.

## Multi-brand

One Hermes profile per Bento site: `hermes -p brand-a bento status`. Caches, confirm tokens, the duplicate ledger and the audit log never cross profiles.
