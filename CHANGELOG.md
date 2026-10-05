# Changelog

## 0.1.0 (unreleased)

- Initial release. 15 tools: subscriber get/upsert/tag/consent, tag list/create, field list/create, event track,
  purchase track, broadcast list/create/schedule, transactional email send, stats.
- Safety model enforced in code: dry-run default for T3, HMAC confirm tokens (single use, 15 min, payload and site bound),
  `pre_tool_call` human-approval escalation with payload-bound `rule_key`, hard guards, no POST retries for sends,
  duplicate ledger, audit log, environment guard, untrusted-content sanitising.
- Confirm-token claim is atomic: check, duplicate-ledger reservation and consume run under one in-process lock
  (never held across the HTTP send); credentials are resolved before the token is spent; a definite 4xx rejection
  releases the duplicate reservation. Threaded regression tests in `tests/test_concurrency.py`.
- `hermes bento status|doctor`, bundled `bento-ops` skill.
