# Changelog

## 0.1.0 (unreleased)

- Initial release. 15 tools: subscriber get/upsert/tag/consent, tag list/create, field list/create, event track,
  purchase track, broadcast list/create/schedule, transactional email send, stats.
- Safety model enforced in code: dry-run default for T3, HMAC confirm tokens (single use, 15 min, payload and site bound),
  `pre_tool_call` human-approval escalation with a one-shot (token-nonce) `rule_key`, hard guards, no POST retries for sends,
  duplicate ledger, audit log, environment guard, untrusted-content sanitising.
- Confirm-token claim: check, duplicate-ledger reservation and consume run under one cross-process critical section (OS file lock
  `claims.lock` + thread lock, never held across the HTTP send); credentials are resolved before the token is spent; a claim that cannot be
  persisted refuses the send and leaves the token unspent; a definite 4xx rejection releases the duplicate reservation. Regression tests in
  `tests/test_concurrency.py` (threads) and `tests/test_multiprocess.py` (two real processes, with a no-lock control that double-sends).
- `[a]lways` at the approval prompt approves one call only: the `rule_key` is bound to the token nonce, not the payload hash.
- Every result carrying Bento-served text (dry-run context, `bento_response`, error bodies) is marked `untrusted_content`; error bodies are
  redacted before they are truncated.
- Docs state the real send guarantee and that only `environment: disabled` (not `staging`) refuses sends.
- `hermes bento status|doctor`, bundled `bento-ops` skill.
