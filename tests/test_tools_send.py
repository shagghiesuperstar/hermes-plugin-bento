"""T3 tools: bento_broadcast_create/schedule and bento_email_send. Includes the refused-send proofs."""

import json
import unittest

from support import PK, SK, BentoTestCase, good_email, good_schedule


class BroadcastCreateTests(BentoTestCase):
    def test_draft_body_never_contains_approved_or_send_at(self):
        out = self.call("bento_broadcast_create", name="October news", subject="Hello", content="<p>hi</p>",
                        from_name="Shop", from_email="news@example.com", inclusive_tags=["newsletter", "vip"],
                        exclusive_tags=["churned"], type="raw", batch_size_per_hour=500, preview_text="peek")
        self.assertTrue(out["ok"], out)
        (req,) = self.server.hits("POST", "/batch/broadcasts")
        self.assertEqual(req["body"], {"broadcasts": [{
            "name": "October news", "subject": "Hello", "content": "<p>hi</p>", "type": "raw",
            "from": {"name": "Shop", "email": "news@example.com"}, "inclusive_tags": "newsletter,vip",
            "exclusive_tags": "churned", "batch_size_per_hour": 500, "inbox_snippet": "peek"}]})
        self.assertNotIn("approved", json.dumps(req["body"]))
        self.assertNotIn("send_at", json.dumps(req["body"]))

    def test_approved_and_send_at_are_rejected_not_forwarded(self):
        for extra in ({"approved": True}, {"send_at": "2026-10-20T14:00:00Z"}):
            out = self.call("bento_broadcast_create", name="n", subject="s", content="c", from_name="f",
                            from_email="news@example.com", inclusive_tags=["a"], **extra)
            self.assertFalse(out["ok"])
            self.assertIn("only creates drafts", out["error"])
        self.assertEqual(self.server.posts(), [])

    def test_explicit_audience_mandatory_and_exclusive(self):
        base = {"name": "n", "subject": "s", "content": "c", "from_name": "f", "from_email": "news@example.com"}
        self.assertIn("explicit audience", self.call("bento_broadcast_create", **base)["error"])
        both = self.call("bento_broadcast_create", **base, inclusive_tags=["a"], segment_id="seg1")
        self.assertIn("exactly one", both["error"])
        self.assertEqual(self.server.posts(), [])

    def test_type_enum_follows_docs(self):
        out = self.call("bento_broadcast_create", name="n", subject="s", content="c", from_name="f",
                        from_email="news@example.com", inclusive_tags=["a"], type="html")
        self.assertIn("plain", out["error"])


class BroadcastScheduleTests(BentoTestCase):
    def test_dry_run_is_default_sends_nothing_and_returns_exact_payload(self):
        out = self.call("bento_broadcast_schedule", **good_schedule())
        self.assertTrue(out["ok"])
        self.assertTrue(out["dry_run"])
        self.assertFalse(out["sent"])
        self.assertEqual(self.server.posts(), [], "dry run must not POST")
        body = out["request"]["body"]["broadcasts"][0]
        self.assertTrue(body["approved"])
        self.assertEqual(body["send_at"], "2026-10-20T14:00:00Z")
        self.assertEqual(body["inclusive_tags"], "newsletter")
        self.assertEqual(out["tier"], "T3")
        self.assertTrue(out["confirm_token"].startswith("v1."))
        self.assertEqual(out["required_on_real_call"], [])  # ack supplied

    def test_REFUSED_send_without_confirm_token(self):
        out = self.call("bento_broadcast_schedule", **good_schedule(), dry_run=False)
        self.assertFalse(out["ok"])
        self.assertTrue(out["refused"])
        self.assertFalse(out["sent"])
        self.assertIn("confirm_token", out["error"])
        self.assertEqual(self.server.posts(), [], "refused send must never reach Bento")

    def test_refused_when_parameters_changed_after_preview(self):
        token = self.dry_then_token("bento_broadcast_schedule", **good_schedule())
        out = self.call("bento_broadcast_schedule", **good_schedule(subject="Different subject"), dry_run=False, confirm_token=token)
        self.assertTrue(out["refused"])
        self.assertIn("payload changed", out["error"])
        self.assertEqual(self.server.posts(), [])

    def test_refused_when_token_expired(self):
        token = self.dry_then_token("bento_broadcast_schedule", **good_schedule())
        self.clock.advance(15 * 60 + 5)
        out = self.call("bento_broadcast_schedule", **good_schedule(), dry_run=False, confirm_token=token)
        self.assertTrue(out["refused"])
        self.assertIn("expired", out["error"])

    def test_confirmed_send_goes_out_once_and_token_cannot_be_replayed(self):
        token = self.dry_then_token("bento_broadcast_schedule", **good_schedule())
        out = self.call("bento_broadcast_schedule", **good_schedule(), dry_run=False, confirm_token=token)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["sent"])
        (req,) = self.server.hits("POST", "/batch/broadcasts")
        self.assertTrue(req["body"]["broadcasts"][0]["approved"])
        self.assertEqual(req["body"]["broadcasts"][0]["send_at"], "2026-10-20T14:00:00Z")
        replay = self.call("bento_broadcast_schedule", **good_schedule(), dry_run=False, confirm_token=token)
        self.assertTrue(replay["refused"])
        self.assertIn("already used", replay["error"])
        self.assertEqual(len(self.server.hits("POST", "/batch/broadcasts")), 1)

    def test_garbage_tokens_refused(self):
        for token in ("", "nope", "v1.1.2", "v1.99999999999.deadbeef", None, 12):
            out = self.call("bento_broadcast_schedule", **good_schedule(), dry_run=False, confirm_token=token)
            self.assertTrue(out["refused"], token)
        self.assertEqual(self.server.posts(), [])

    def test_dry_run_must_be_a_real_boolean(self):
        out = self.call("bento_broadcast_schedule", **good_schedule(), dry_run="false")
        self.assertFalse(out["ok"])
        self.assertIn("boolean", out["error"])
        self.assertEqual(self.server.posts(), [])

    def test_unknown_audience_needs_acknowledgement_on_real_call(self):
        args = good_schedule()
        args.pop("acknowledge_unknown_audience")
        dry = self.call("bento_broadcast_schedule", **args)
        self.assertEqual(len(dry["required_on_real_call"]), 1)
        out = self.call("bento_broadcast_schedule", **args, dry_run=False, confirm_token=dry["confirm_token"])
        self.assertTrue(out["refused"])
        self.assertIn("acknowledge_unknown_audience", out["error"])
        # the ack is NOT part of the previewed payload: dry-run token still works once it is supplied
        ok = self.call("bento_broadcast_schedule", **args, acknowledge_unknown_audience=True, dry_run=False,
                       confirm_token=dry["confirm_token"])
        self.assertTrue(ok["ok"], ok)

    def test_segment_audience_dry_run_reports_segment_size_via_get_only(self):
        args = good_schedule(segment_id="seg_1")
        args.pop("inclusive_tags")
        args.pop("acknowledge_unknown_audience")
        out = self.call("bento_broadcast_schedule", **args)
        self.assertEqual(out["context"]["segment_counts"]["subscriber_count"], 9)
        self.assertEqual(out["required_on_real_call"], [])
        self.assertEqual(self.server.posts(), [])

    def test_hard_guards(self):
        cases = {
            "not in allowed_senders": good_schedule(from_email="rogue@example.com"),
            "at least 30 minutes": good_schedule(send_at="2026-10-05T12:10:00Z"),
            "within 90 days": good_schedule(send_at="2027-02-01T00:00:00Z"),
            "exceeds the configured maximum": good_schedule(batch_size_per_hour=5001),
            "must include a timezone": good_schedule(send_at="2026-10-20T14:00:00"),
            "explicit audience": {k: v for k, v in good_schedule().items() if k != "inclusive_tags"},
            "batch_size_per_hour is required": {k: v for k, v in good_schedule().items() if k != "batch_size_per_hour"},
        }
        for needle, args in cases.items():
            for dry in (True, False):
                out = self.call("bento_broadcast_schedule", **args, dry_run=dry)
                self.assertFalse(out["ok"], needle)
                self.assertIn(needle, out["error"], needle)
        self.assertEqual(self.server.posts(), [])

    def test_unknown_params_rejected(self):
        out = self.call("bento_broadcast_schedule", **good_schedule(), approved=True, dry_run=True)
        self.assertIn("unknown parameter", out["error"])


class AllowedSendersEmptyTests(BentoTestCase):
    settings = {"allowed_senders": []}

    def test_empty_allow_list_refuses_all_t3(self):
        for tool, args in (("bento_broadcast_schedule", good_schedule()), ("bento_email_send", good_email())):
            out = self.call(tool, **args)
            self.assertFalse(out["ok"])
            self.assertIn("allowed_senders is empty", out["error"])
        self.assertEqual(self.server.posts(), [])


class EnvironmentDisabledTests(BentoTestCase):
    settings = {"environment": "disabled"}

    def test_disabled_blocks_every_write_and_send(self):
        for tool, args in (("bento_broadcast_schedule", good_schedule()), ("bento_email_send", good_email()),
                           ("bento_event_track", {"events": [{"type": "x", "email": "a@example.com"}]})):
            out = self.call(tool, **args)
            self.assertTrue(out.get("refused"), tool)
            self.assertIn("environment=disabled", out["error"])
        self.assertEqual(self.server.posts(), [])

    def test_disabled_still_allows_reads(self):
        self.assertTrue(self.call("bento_tag_list")["ok"])


class EmailSendTests(BentoTestCase):
    def test_dry_run_default_exact_payload_transactional_false(self):
        out = self.call("bento_email_send", **good_email())
        self.assertTrue(out["dry_run"])
        entry = out["request"]["body"]["emails"][0]
        self.assertEqual(entry, {"to": "jane@example.com", "from": "news@example.com", "subject": "Your order",
                                 "html_body": "<p>Hi {{ visitor.unsubscribe_url }}</p>", "transactional": False})
        self.assertEqual(out["context"]["throttle_eta_minutes"], 1)
        self.assertEqual(self.server.posts(), [])

    def test_REFUSED_send_without_confirm_token(self):
        out = self.call("bento_email_send", **good_email(), dry_run=False)
        self.assertTrue(out["refused"])
        self.assertFalse(out["sent"])
        self.assertEqual(self.server.posts(), [])

    def test_full_flow_sends_once(self):
        token = self.dry_then_token("bento_email_send", **good_email())
        out = self.call("bento_email_send", **good_email(), dry_run=False, confirm_token=token)
        self.assertTrue(out["ok"] and out["sent"], out)
        (req,) = self.server.hits("POST", "/batch/emails")
        self.assertEqual(req["body"]["emails"][0]["transactional"], False)
        self.assertEqual(req["query"]["site_uuid"], ["11111111-2222-3333-4444-555555555555"])

    def test_cap_61_rejected_even_when_config_allows_60(self):
        self.ctx.config["max_recipients_per_call"] = 60
        many = [{"to": f"u{i}@example.com", "subject": "s", "html_body": "b"} for i in range(61)]
        out = self.call("bento_email_send", **good_email(emails=many))
        self.assertIn("at most 60", out["error"])
        ok = self.call("bento_email_send", **good_email(emails=many[:60]))
        self.assertTrue(ok["ok"])

    def test_per_call_recipient_cap_default_10(self):
        many = [{"to": f"u{i}@example.com", "subject": "s", "html_body": "b"} for i in range(11)]
        out = self.call("bento_email_send", **good_email(emails=many))
        self.assertIn("max_recipients_per_call=10", out["error"])

    def test_sender_must_be_allowed(self):
        out = self.call("bento_email_send", **good_email(**{"from": "rogue@example.com"}))
        self.assertIn("not in allowed_senders", out["error"])

    def test_transactional_true_needs_setting_and_reason(self):
        out = self.call("bento_email_send", **good_email(transactional=True, reason="password reset"))
        self.assertIn("allow_transactional_override=false", out["error"])
        self.ctx.config["allow_transactional_override"] = True
        no_reason = self.call("bento_email_send", **good_email(transactional=True))
        self.assertIn("reason is required", no_reason["error"])
        ok = self.call("bento_email_send", **good_email(transactional=True, reason="password reset"))
        self.assertTrue(ok["ok"])
        self.assertTrue(ok["request"]["body"]["emails"][0]["transactional"])
        self.assertTrue(any("EVEN TO UNSUBSCRIBED" in w for w in ok["warnings"]))

    def test_duplicate_ledger_blocks_identical_resend_unless_allowed(self):
        token = self.dry_then_token("bento_email_send", **good_email())
        self.assertTrue(self.call("bento_email_send", **good_email(), dry_run=False, confirm_token=token)["ok"])
        dry = self.call("bento_email_send", **good_email())
        self.assertEqual(dry["context"]["duplicates_in_last_24h"], 1)
        refused = self.call("bento_email_send", **good_email(), dry_run=False, confirm_token=dry["confirm_token"])
        self.assertTrue(refused["refused"])
        self.assertIn("allow_duplicate", refused["error"])
        dry2 = self.call("bento_email_send", **good_email())
        allowed = self.call("bento_email_send", **good_email(), allow_duplicate=True, dry_run=False, confirm_token=dry2["confirm_token"])
        self.assertTrue(allowed["ok"])
        self.assertEqual(len(self.server.hits("POST", "/batch/emails")), 2)

    def test_duplicate_window_expires_after_24h(self):
        token = self.dry_then_token("bento_email_send", **good_email())
        self.call("bento_email_send", **good_email(), dry_run=False, confirm_token=token)
        self.clock.advance(24 * 3600 + 5)
        self.assertEqual(self.call("bento_email_send", **good_email())["context"]["duplicates_in_last_24h"], 0)

    def test_send_is_never_retried_and_ambiguous_failure_is_reported_unknown(self):
        self.server.route("POST", "/batch/emails", {"status": 503, "json": {"error": "busy"}})
        token = self.dry_then_token("bento_email_send", **good_email())
        out = self.call("bento_email_send", **good_email(), dry_run=False, confirm_token=token)
        self.assertFalse(out["ok"])
        self.assertEqual(out["sent"], "unknown")
        self.assertIn("Do NOT retry blindly", out["warning"])
        self.assertEqual(len(self.server.hits("POST", "/batch/emails")), 1)
        self.assertEqual(self.sleeps, [])
        # an ambiguous failure still counts for duplicate suppression
        dry = self.call("bento_email_send", **good_email())
        self.assertEqual(dry["context"]["duplicates_in_last_24h"], 1)

    def test_422_rejection_is_definite_and_not_recorded_as_sent(self):
        self.server.route("POST", "/batch/emails", {"status": 422, "json": {"errors": [{"field": "from", "message": "is not an author"}]}})
        token = self.dry_then_token("bento_email_send", **good_email())
        out = self.call("bento_email_send", **good_email(), dry_run=False, confirm_token=token)
        self.assertEqual(out["sent"], False)
        self.assertEqual(out["status"], 422)
        self.assertEqual(self.call("bento_email_send", **good_email())["context"]["duplicates_in_last_24h"], 0)

    def test_lint_warnings(self):
        emails = [{"to": "a@example.com", "subject": "Sale 🎉", "html_body": "<a href='https://bit.ly/x'>x</a>",
                   "personalizations": {"name": "Ann"}}]
        out = self.call("bento_email_send", **good_email(emails=emails))
        joined = " ".join(out["warnings"])
        self.assertIn("Emoji", joined)
        self.assertIn("URL shortener", joined)
        self.assertIn("unsubscribe_url", joined)
        self.assertIn("personalizations", joined)
        self.assertIn("Accepted != delivered", joined)

    def test_validation(self):
        bad = [good_email(emails=[]), good_email(emails=[{"to": "not-an-email", "subject": "s", "html_body": "b"}]),
               good_email(emails=[{"to": "a@example.com", "subject": "s"}]),
               good_email(emails=[{"to": "a@example.com", "subject": "s", "html_body": "b", "cc": "x@example.com"}]),
               good_email(transactional="yes"),
               good_email(emails=[{"to": "a@example.com", "subject": "s", "html_body": "b", "personalizations": {"k": {"nested": 1}}}])]
        for args in bad:
            self.assertFalse(self.call("bento_email_send", **args)["ok"], args)
        self.assertEqual(self.server.posts(), [])


class AuditTests(BentoTestCase):
    def test_audit_records_hashed_recipients_and_no_bodies_or_keys(self):
        token = self.dry_then_token("bento_email_send", **good_email())
        self.call("bento_email_send", **good_email(), dry_run=False, confirm_token=token)
        text = (self.ctx.state.data_dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("jane@example.com", text)
        self.assertNotIn("Your order", text)
        self.assertNotIn("unsubscribe_url", text)
        for secret in (PK, SK):
            self.assertNotIn(secret, text)
        stages = [(r["stage"], r.get("outcome")) for r in self.audit_lines()]
        self.assertEqual(stages, [("dry_run", None), ("attempt", None), ("result", "accepted")])

    def test_log_emails_setting_stores_raw_addresses(self):
        self.ctx.config["audit.log_emails"] = True
        self.call("bento_email_send", **good_email())
        self.assertIn("jane@example.com", (self.ctx.state.data_dir / "audit.jsonl").read_text(encoding="utf-8"))

    def test_t3_send_refused_when_audit_cannot_be_written(self):
        token = self.dry_then_token("bento_email_send", **good_email())
        audit = self.ctx.state.data_dir / "audit.jsonl"
        audit.unlink()
        audit.mkdir()  # a directory where the log file should be: open(..., "a") now fails
        with self.assertLogs("bento_hermes.safety", level="ERROR"):
            out = self.call("bento_email_send", **good_email(), dry_run=False, confirm_token=token)
        self.assertTrue(out["refused"])
        self.assertIn("audit log", out["error"])
        self.assertEqual(self.server.posts(), [])


if __name__ == "__main__":
    unittest.main()
