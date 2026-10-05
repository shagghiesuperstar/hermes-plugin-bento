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
2. **Confirm token.** `HMAC-SHA256(per-profile key, tool + site + exact request + expiry + nonce)`. 15 minute TTL, single use.
   The real call must repeat the *identical* parameters with `dry_run=false` and the token. Change anything and it is refused
   (`payload changed since preview`), so a token cannot be used to send a different payload than the one previewed.
   **What "single use" guarantees, and what it does not.**
   - The token check, the token spend and the duplicate-fingerprint reservation are one critical section under an exclusive
     OS file lock (`claims.lock` in `ctx.state.data_dir`, i.e. `<HERMES_HOME>/plugin-data/agent-plugin-bento-4243b479/` for this plugin id) plus an in-process lock. Every process on the profile
     (gateway, CLI, cron) takes the same lock, so two processes or threads presenting the same token, or two different tokens
     for the same email content, cannot both pass: one wins the claim, the others are refused. The lock is never held across the HTTP send.
   - If the claim cannot be taken or persisted (lock timeout, plugin state unreadable or unwritable, no data dir), the send is
     refused and the token is not spent.
   - A token is spent *before* the POST. If the POST then fails ambiguously (timeout, 5xx) the result is `sent: "unknown"`, the token
     is gone, and the duplicate reservation is kept. A later fresh dry run plus approval for the same email content is refused for 24 h
     unless `allow_duplicate=true`; that is how an unknown outcome is kept from turning into a second send by accident, not a proof of one POST.
   - Duplicate suppression covers `bento_email_send` only. `bento_broadcast_schedule` is protected by the token alone: two separate
     dry runs give two valid tokens, and each can schedule the same broadcast (check the Bento dashboard before re-running).
   - The lock is local to one `HERMES_HOME`. It does not coordinate separate machines or profiles, and it depends on the OS file lock
     working on that filesystem (some network filesystems do not honour it).
   - Bento's API has no idempotency key for these endpoints, so the plugin cannot make Bento itself deduplicate.
3. **Human approval.** The plugin registers a `pre_tool_call` hook that returns `{"action": "approve"}` for real T3 calls (and for T2
   calls unless `safety.tier2` is `token_only`). Hermes then asks a person, showing the sender, up to three recipients, the subject,
   the start of the body (or the audience and send time for a broadcast) and the payload hash.
   Denial, timeout or a gate error fails closed. The approval `rule_key` is one-shot: it carries the checked confirm token's nonce (a fresh random
   id for T2 calls, which ignore any confirm token) and never the payload hash. Hermes stores "[a]lways" and "[s]ession" approvals under that key, so
   choosing "always" approves this one call only; a later dry run of an identical payload gets a new nonce and asks the human again.
   The approval text is stripped of control and invisible characters, so message content cannot forge the prompt.
4. **Hard guards for T3 (config cannot relax them):** an explicit audience (`inclusive_tags` or `segment_id`, never "all");
   `from` must be listed in `allowed_senders` (empty list = T3 refused); `send_at` must be at least `min_schedule_lead_minutes`
   (floor 5, default 30) ahead and within 90 days; `batch_size_per_hour` explicit and <= `max_batch_size_per_hour`;
   at most `max_recipients_per_call` recipients (default 10, hard cap 60); `transactional=true` only with
   `allow_transactional_override: true` **and** a reason; duplicate suppression (24 h ledger) unless `allow_duplicate=true`;
   tag audiences need `acknowledge_unknown_audience=true` because the API cannot report their size.
5. **No automatic retries of anything that sends.** Reads retry on 429/5xx with backoff; `$purchase` retries because its
   `unique.key` makes it idempotent; email and broadcast POSTs never retry, and an ambiguous failure is reported as `sent: "unknown"`.
6. **Environment guard.** `environment: disabled` makes every T2/T3 tool refuse. `environment: staging` does **not** block anything: the plugin
   treats it exactly like `production`, and a confirmed T3 send under `staging` still emails real people. The plugin has no sandbox or test mode
   (Bento has none either); use `disabled` where sends must not happen (Bento advises against sending data from local or CI).
7. **Audit log.** Append-only JSONL at `audit.jsonl` in `ctx.state.data_dir` (`<HERMES_HOME>/plugin-data/agent-plugin-bento-4243b479/` for this plugin id): tool, tier, payload hash, outcome, counts.
   No keys, no message bodies; recipient emails are hashed unless `audit.log_emails` is on. If the log cannot be written, T3 sends are refused.
8. **Bento-served content is untrusted, in every tool result.** Subscriber fields, tag names, broadcast bodies, dry-run context, the
   `bento_response` of a write or send, and the body text inside Bento error messages are all marked `untrusted_content: true` with an
   `untrusted_fields` list naming where the Bento text is (read tools wrap it in a `data` envelope). Each is stripped of control and
   invisible/bidi characters and length-capped, so a poisoned field is data, not instructions. Error bodies are redacted of the API keys
   first and cut to length afterwards, so a key straddling the cut cannot leak a prefix. Even a successful injection cannot send: sends need the token and the human approval above.
9. **Client-side budgets.** 60 requests/min for fetch+batch and 30/hour for stats (Bento's documented lower numbers) are enforced locally;
   when spent, a call is refused immediately instead of hanging the agent. Redirects are never followed, so credentials cannot be sent to another host.

**What the plugin cannot do:** it cannot make Hermes ask a human on surfaces that auto-approve (for example YOLO mode, or a cron job with
`approvals.cron_mode: approve`). In those modes the token and the hard guards still apply but a human is not in the loop. Do not run this plugin with auto-approval.
A surface that skips `pre_tool_call` hooks entirely would still hit the token requirement in the handler.

## Settings

Set with `hermes config set plugins.entries.bento.settings.<key> <value>` (the Desktop Plugins tab renders the same form).

| Key | Default | Meaning |
|---|---|---|
| `environment` | `production` | `production` / `staging` / `disabled`. Only `disabled` refuses T2/T3 tools; `staging` behaves like `production` and **still emails real people**. Unknown values fail closed to `disabled`. |
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
- **Storage:** only `ctx.state.data_dir` (`<HERMES_HOME>/plugin-data/agent-plugin-bento-4243b479/` for this plugin id; state: signing key, used tokens, duplicate ledger; audit log; `claims.lock`, an empty lock file for the cross-process claim). Credentials stay in the profile `.env`.
- **Reads outside the plugin's own data:** the three `BENTO_*` variables via Hermes's secret scope / environment, plugin settings. Nothing else.
- **No** telemetry, usage reporting, self-update, subprocesses, shell commands, background processes, or changes to Hermes core.
- **Internal Hermes API used:** `agent.secret_scope.get_secret` (internal, not a documented plugin API), imported defensively with an `os.environ` fallback.
- **Approval behaviour:** relies on `pre_tool_call` -> `approve` failing closed. Under cron this holds only while `approvals.cron_mode` is not `approve` (and likewise `single_query_mode` / `unattended_mode` for those surfaces). See "What the plugin cannot do".

## Known gaps

Not yet verified against a live Bento site (no account when this was built); these Bento facts are inconsistent in Bento's own docs and are
handled conservatively: the broadcast `type` enum (`plain|raw` per docs; the plugin accepts only those), the `/batch/emails` cap (60 per docs, 100 in an SDK comment: 60 used),
the `POST /fetch/tags|fields` body wrappers (from the Node SDK), the `cart.items[].price` key (from Bento's JS docs, in cents like `value.amount`), and `approved: true` + `send_at` on broadcasts (docs.bentonow.com/broadcasts).
The single-use lock and the duplicate ledger are shared by every process on one `HERMES_HOME` through an OS file lock (see the Safety model); they do
not coordinate separate machines or profiles, and `bento_broadcast_schedule` has no duplicate ledger (token only). The cross-process claim is tested with
two real processes on a file-backed state, not against a live gateway plus a live cron job; the file-lock path on Windows is implemented (`msvcrt.locking`) but not yet exercised there.
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
