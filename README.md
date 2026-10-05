# hermes-plugin-bento

A native [Hermes Agent](https://hermes-agent.nousresearch.com) plugin for [Bento](https://bentonow.com) (email marketing).
Fifteen `bento_*` tools let an agent read and manage subscribers, tags, fields, events (including `$purchase`),
stats, draft and scheduled broadcasts and transactional email, **with the safety rules enforced in code, not in prompts**:
every send is a dry run first, bound to a single-use confirm token, and then goes through Hermes's human-approval gate.

Python, standard library only. No telemetry. No self-update. MIT licensed.

> Status: v0.1.0. Built against the documented Bento REST API and tested against recorded-shape fixtures and a local
> fake server. It has **not** yet been run against a live Bento site; see [Known gaps](#known-gaps).

## Install

Requires Hermes `>= 0.21.5`.

```bash
# from GitHub (works today)
hermes plugins install shagghiesuperstar/hermes-plugin-bento

# from the Hermes plugin catalog (after the catalog entry merges)
hermes plugins install bento
```

## Enable

```bash
hermes plugins enable bento     # general plugins are opt-in
hermes tools                    # turn the "bento" toolset on for the platforms you use, if it is not already
hermes bento status             # checks credentials and makes one read-only call to Bento
```

## Credentials

Three values, all from your Bento account (Settings -> API Keys, <https://app.bentonow.com/account/teams>).
Hermes prompts for them at install time and stores them in the profile's `.env`:

| Variable | What |
|---|---|
| `BENTO_PUBLISHABLE_KEY` | publishable key (`pk_...`) |
| `BENTO_SECRET_KEY` | secret key (`sk_...`) |
| `BENTO_SITE_UUID` | the site every call targets |

Keys never go in `config.yaml`, in tool parameters, in the repo, in logs, in audit records or in error messages. They are read
through Hermes's secret scope when available (`agent.secret_scope.get_secret`, which fails closed under a multiplexing gateway)
and from the process environment otherwise.

Bento keys are user-level and "grant access to all teams + sites", so treat them like a root credential.

### One Bento site per brand: use Hermes profiles

A Bento key set targets one site. For several brands, create one Hermes profile per site, each with its own `.env`:

```bash
hermes profile create brand-a          # then put brand A's three values in that profile's .env
hermes -p brand-a bento status
hermes -p brand-b bento status
```

Cache, confirm tokens, the duplicate-send ledger and the audit log are all per profile (and the cache and tokens are also
bound to the site UUID), so a token from one site can never authorise a send on another.

## Tools

| Tool | Tier | What it does | Bento endpoint |
|---|---|---|---|
| `bento_subscriber_get` | T0 | look up one subscriber | `GET /fetch/subscribers` |
| `bento_tag_list` / `bento_field_list` | T0 | list tags / fields (cached 5 min) | `GET /fetch/tags`, `/fetch/fields` |
| `bento_broadcast_list` | T0 | list broadcasts by status | `GET /fetch/broadcasts` |
| `bento_stats` | T0 | site / segment / report / ads stats (cached) | `GET /stats/*` |
| `bento_subscriber_upsert` | T1 (T2 above `bulk_threshold`) | import/upsert 1-1000 subscribers, no Flows | `POST /batch/subscribers` |
| `bento_subscriber_tag` | T1 | add/remove one tag on one person | `POST /fetch/commands` |
| `bento_tag_create` / `bento_field_create` | T1 | create a definition; refuses duplicates and near-duplicates | `POST /fetch/tags`, `/fetch/fields` |
| `bento_broadcast_create` | T1 | create a **draft** only (cannot send) | `POST /batch/broadcasts` |
| `bento_subscriber_consent` | T2 | subscribe / unsubscribe one person (reason required to subscribe) | `POST /fetch/commands` |
| `bento_event_track` | T2 | track events (can trigger Flows) | `POST /batch/events` |
| `bento_purchase_track` | T2 | record one `$purchase` with a real order id, integer cents | `POST /batch/events` |
| `bento_broadcast_schedule` | **T3** | create **and approve** a broadcast for `send_at` | `POST /batch/broadcasts` (`send_at`, `approved: true`) |
| `bento_email_send` | **T3** | send transactional email (<= 60/request) | `POST /batch/emails` |

Tiers: **T0** read, **T1** low-risk reversible write, **T2** can fire Flows, change consent or revenue, **T3** outbound email to real people.
A Hermes CLI command is also provided: `hermes bento status` (one cheap read, prints no secrets) and `hermes bento doctor` (offline config lint).

## Safety model

All of this is code (`src/bento_hermes/safety.py`) with tests, not instructions to the model.

1. **Dry run first.** `bento_broadcast_schedule` and `bento_email_send` default to `dry_run=true`. A dry run performs no POST.
   It returns the exact request body, validation results, lint warnings, read-only context (tag existence, segment size,
   duplicate check, throttle ETA) and a `confirm_token`. T2 tools accept `dry_run=true` too.
2. **Confirm token.** `HMAC-SHA256(per-profile key, tool + site + exact request + expiry + nonce)`. 15 minute TTL, single use. "Single use" is atomic: token check, duplicate check and token consumption run under one lock
   (never held across the send), so parallel tool calls with the same token produce exactly one POST.
   The real call must repeat the *identical* parameters with `dry_run=false` and the token. Change anything and it is refused
   (`payload changed since preview`). Preview A, send B is impossible; replay is impossible.
3. **Human approval.** The plugin registers a `pre_tool_call` hook that returns `{"action": "approve"}` for real T3 calls (and for T2
   calls unless `safety.tier2` is `token_only`). Hermes then asks a person, showing the sender, up to three recipients, the subject,
   the start of the body (or the audience and send time for a broadcast) and the payload hash.
   Denial, timeout or a gate error fails closed. The `rule_key` embeds the payload hash, so choosing "always" at the prompt can only
   ever re-approve that one identical, single-use payload, never "all Bento sends". The approval text is stripped of control and
   invisible characters, so message content cannot forge the prompt.
4. **Hard guards for T3 (config cannot relax them):** an explicit audience (`inclusive_tags` or `segment_id`, never "all");
   `from` must be listed in `allowed_senders` (empty list = T3 refused); `send_at` must be at least `min_schedule_lead_minutes`
   (floor 5, default 30) ahead and within 90 days; `batch_size_per_hour` explicit and <= `max_batch_size_per_hour`;
   at most `max_recipients_per_call` recipients (default 10, hard cap 60); `transactional=true` only with
   `allow_transactional_override: true` **and** a reason; duplicate suppression (24 h ledger) unless `allow_duplicate=true`;
   tag audiences need `acknowledge_unknown_audience=true` because the API cannot report their size.
5. **No automatic retries of anything that sends.** Reads retry on 429/5xx with backoff; `$purchase` retries because its
   `unique.key` makes it idempotent; email and broadcast POSTs never retry, and an ambiguous failure is reported as `sent: "unknown"`.
6. **Environment guard.** `environment: disabled` makes every T2/T3 tool refuse (Bento advises against sending data from local or CI).
7. **Audit log.** Append-only JSONL at `<HERMES_HOME>/plugin-data/bento/audit.jsonl`: tool, tier, payload hash, outcome, counts.
   No keys, no message bodies; recipient emails are hashed unless `audit.log_emails` is on. If the log cannot be written, T3 sends are refused.
8. **Bento-served content is untrusted.** Subscriber fields, tag names and broadcast bodies are returned in an envelope marked
   `untrusted_content: true`, stripped of control and invisible/bidi characters and length-capped, so a poisoned field is data, not instructions.
   Even a successful injection cannot send: sends need the token and the human approval above.
9. **Client-side budgets.** 60 requests/min for fetch+batch and 30/hour for stats (Bento's documented lower numbers) are enforced locally;
   when spent, a call is refused immediately instead of hanging the agent. Redirects are never followed, so credentials cannot be sent to another host.

**What the plugin cannot do:** it cannot make Hermes ask a human on surfaces that auto-approve (for example YOLO mode, or a cron job with
`approvals.cron_mode: approve`). In those modes the token and the hard guards still apply but a human is not in the loop. Do not run this plugin with auto-approval.
A surface that skips `pre_tool_call` hooks entirely would still hit the token requirement in the handler.

## Settings

Set with `hermes config set plugins.entries.bento.settings.<key> <value>` (the Desktop Plugins tab renders the same form).

| Key | Default | Meaning |
|---|---|---|
| `environment` | `production` | `production` / `staging` / `disabled`. Unknown values fail closed to `disabled`. |
| `allowed_senders` | `[]` | verified Bento Author addresses allowed as From. Empty = T3 refused. |
| `bulk_threshold` | `25` | upserts above this size are T2 |
| `safety.tier2` | `approve` | `approve` = human approval for T2; `token_only` = dry-run token only |
| `min_schedule_lead_minutes` | `30` | minimum lead time (floor 5) |
| `max_batch_size_per_hour` | `5000` | cap for broadcast pacing |
| `max_recipients_per_call` | `10` | cap per `bento_email_send` (hard max 60) |
| `allow_transactional_override` | `false` | permit `transactional=true` (bypasses unsubscribes) |
| `stats_cache_ttl_seconds` | `900` | stats cache lifetime |
| `request_timeout_seconds` | `30` | per-request timeout |
| `audit.log_emails` | `false` | store raw recipient emails in the audit log |
| `base_url` | `""` | override; only `https://*.bentonow.com` or `http://localhost`/`127.0.0.1` accepted |

Example: `hermes config set plugins.entries.bento.settings.allowed_senders '["news@yourbrand.com"]'`

## Rate limits and quirks (from Bento's docs)

- `/fetch/*` and `/batch/*` about 60 requests/min per IP; `/stats/*` 30/hour per IP; `/batch/emails` 60 emails per request, 60/min.
- A real `User-Agent` is mandatory; the plugin sends `hermes-plugin-bento/<version>`.
- `site_uuid` is sent on every request.
- Imports are async (up to 5 minutes to appear) and do not trigger Flows. Events do.
- `subscribe` re-subscribes people who opted out. Never use it from a sync.
- The documented API has no endpoint to cancel or edit a scheduled broadcast: cancellation is dashboard-only.
- "Accepted" by `/batch/emails` is not "delivered": suppressed recipients are dropped silently.

## Troubleshooting

| Symptom | Cause |
|---|---|
| tools missing in the agent | plugin not enabled, `bento` toolset off in `hermes tools`, or one of the three env vars missing (`hermes bento status`) |
| `non-JSON body ... User-Agent` | Cloudflare blocked the request (HTML instead of JSON) |
| `HTTP 401 ... site_uuid` | wrong keys or `BENTO_SITE_UUID` does not belong to them |
| `HTTP 422 ... verified Author` | `from` is not a verified Author in that Bento site |
| `refused: allowed_senders is empty` | set `allowed_senders` first |
| `refused: payload changed since preview` | re-run the dry run; parameters must be identical |
| `client-side rate budget ... spent` | the local 60/min or 30/hour budget; wait as stated |

## Disclosure

- **Sends real email.** `bento_email_send` and `bento_broadcast_schedule` cause Bento to email real people; `bento_event_track`
  and `bento_purchase_track` can trigger Bento Flows that email people. They are gated as described above.
- **Network:** only `https://app.bentonow.com/api/v1`, with your keys, nothing else. Optional `base_url` override accepts only `*.bentonow.com` or loopback.
- **Data:** subscriber PII you ask it to read or write is exchanged with Bento (Bento is the processor).
- **Storage:** only `<HERMES_HOME>/plugin-data/bento/` (state: signing key, used tokens, duplicate ledger; audit log). Credentials stay in the profile `.env`.
- **Reads outside the plugin's own data:** the three `BENTO_*` variables via Hermes's secret scope / environment, plugin settings. Nothing else.
- **No** telemetry, usage reporting, self-update, subprocesses, shell commands, background processes, or changes to Hermes core.
- **Internal Hermes API used:** `agent.secret_scope.get_secret` (internal, not a documented plugin API), imported defensively with an `os.environ` fallback.
- **Approval behaviour:** relies on `pre_tool_call` -> `approve` failing closed. See "What the plugin cannot do".

## Known gaps

Not yet verified against a live Bento site (no account when this was built); these Bento facts are inconsistent in Bento's own docs and are
handled conservatively: the broadcast `type` enum (`plain|raw` per docs; the plugin accepts only those), the `/batch/emails` cap (60 per docs, 100 in an SDK comment: 60 used),
the `POST /fetch/tags|fields` body wrappers (from the Node SDK), the `cart.items[].price` key (from Bento's JS docs, in cents like `value.amount`), and `approved: true` + `send_at` on broadcasts (docs.bentonow.com/broadcasts).
The single-use lock and the duplicate ledger are per Hermes process; two separate processes sharing one profile (e.g. a gateway and a
concurrent CLI/cron run) are not serialised against each other, so do not run the same send from two processes at once.
Smoke-test on a throwaway Bento site (`hermes bento status`, then a dry run of every T3 tool) before pointing it at a real list.

## Development

```bash
python -m unittest discover -s tests          # stdlib only, no network (loopback fake server)
python -m pytest                              # same tests under pytest, if installed
hermes plugins doctor . --ci
hermes plugins validate . --install-deps
```

Tests never contact Bento and never touch `~/.hermes`. See `docs/index.md` for the docs page and `CHANGELOG.md` for releases.

## License

MIT. Not affiliated with or endorsed by Bento or Nous Research.
