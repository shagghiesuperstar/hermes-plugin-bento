"""Secrets: resolution order, scope fail-closed, redaction everywhere, multi-site (profile) isolation, settings."""

import json
import os
import re
import sys
import types
import unittest
from contextlib import contextmanager
from unittest import mock

from support import ENV, PK, ROOT, SITE, SK, SRC, BentoTestCase, good_email

from bento_hermes.config import Settings, resolve_secret, validate_base_url
from bento_hermes.client import DEFAULT_BASE_URL


@contextmanager
def fake_secret_scope(mapping_ref: dict, raise_on: str | None = None):
    """Install a fake ``agent.secret_scope`` whose get_secret reads ``mapping_ref`` (a stand-in for a profile scope)."""
    agent = types.ModuleType("agent")
    scope = types.ModuleType("agent.secret_scope")

    def get_secret(name, default=None):
        if name == raise_on:
            raise RuntimeError("UnscopedSecretError stand-in")
        return mapping_ref.get(name, default)

    scope.get_secret = get_secret
    agent.secret_scope = scope
    with mock.patch.dict(sys.modules, {"agent": agent, "agent.secret_scope": scope}):
        yield


class ResolveSecretTests(unittest.TestCase):
    def test_env_fallback_when_scope_module_absent(self):
        with mock.patch.dict(os.environ, {"BENTO_SECRET_KEY": "  abc123456  "}), mock.patch.dict(sys.modules, {"agent": None, "agent.secret_scope": None}):
            self.assertEqual(resolve_secret("BENTO_SECRET_KEY"), "abc123456")

    def test_scope_is_authoritative_and_does_not_fall_back_to_os_environ(self):
        with mock.patch.dict(os.environ, {"BENTO_SECRET_KEY": "from-environ-xxx"}), fake_secret_scope({"BENTO_SECRET_KEY": "from-scope-xxx"}):
            self.assertEqual(resolve_secret("BENTO_SECRET_KEY"), "from-scope-xxx")
        with mock.patch.dict(os.environ, {"BENTO_SECRET_KEY": "from-environ-xxx"}), fake_secret_scope({}):
            self.assertIsNone(resolve_secret("BENTO_SECRET_KEY"))  # multiplex: another profile's environ must not leak in

    def test_scope_errors_fail_closed(self):
        with mock.patch.dict(os.environ, {"BENTO_SECRET_KEY": "from-environ-xxx"}), fake_secret_scope({}, raise_on="BENTO_SECRET_KEY"):
            with self.assertLogs("bento_hermes.config", level="WARNING"):
                self.assertIsNone(resolve_secret("BENTO_SECRET_KEY"))

    def test_blank_values_are_unset(self):
        with mock.patch.dict(os.environ, {"BENTO_SECRET_KEY": "   "}), mock.patch.dict(sys.modules, {"agent": None, "agent.secret_scope": None}):
            self.assertIsNone(resolve_secret("BENTO_SECRET_KEY"))


class NoSecretLeaksTests(BentoTestCase):
    def all_outputs(self) -> str:
        outs = [self.call("bento_tag_list"), self.call("bento_subscriber_get", email="jane@example.com"),
                self.call("bento_email_send", **good_email()), self.call("bento_stats", kind="site")]
        self.server.route("GET", "/fetch/fields", {"status": 401, "json": {"error": f"{PK}:{SK} rejected for {SITE}"}})
        outs.append(self.call("bento_field_list"))
        self.server.route("POST", "/batch/emails", {"status": 500, "raw": f"boom {SK}".encode()})
        token = self.dry_then_token("bento_email_send", **good_email(emails=[{"to": "z@example.com", "subject": "s", "html_body": "b"}]))
        outs.append(self.call("bento_email_send", **good_email(emails=[{"to": "z@example.com", "subject": "s", "html_body": "b"}]),
                              dry_run=False, confirm_token=token))
        return json.dumps(outs)

    def test_keys_never_in_results_errors_audit_state_or_config(self):
        blob = self.all_outputs()
        audit = (self.ctx.state.data_dir / "audit.jsonl").read_text(encoding="utf-8")
        state = json.dumps(self.ctx.state.store)
        config = json.dumps(self.ctx.config)
        for secret in (PK, SK):
            for name, text in (("results", blob), ("audit", audit), ("state", state), ("config", config)):
                self.assertNotIn(secret, text, f"{secret} leaked into {name}")
        self.assertEqual(self.ctx.set_config_calls, [])

    def test_keys_never_on_the_wire_except_in_the_basic_auth_header(self):
        self.call("bento_tag_list")
        for req in self.server.requests:
            self.assertNotIn(SK, json.dumps(req["query"]))
            self.assertNotIn(SK, json.dumps(req["body"]))

    def test_logs_contain_no_secrets(self):
        with self.assertLogs(level="DEBUG") as logs:
            import logging
            logging.getLogger("probe").debug("probe")  # assertLogs requires at least one record
            self.server.route("GET", "/fetch/tags", {"status": 401, "json": {"error": f"{SK}"}})
            self.call("bento_tag_list")
            self.rt.state_get("x")
        text = "\n".join(logs.output)
        self.assertNotIn(SK, text)
        self.assertNotIn(PK, text)

    def test_repo_contains_no_key_shaped_strings(self):
        pattern = re.compile(r"\b(?:sk|pk)_(?:live|prod|[A-Za-z0-9]{20,})\b|-----BEGIN [A-Z ]*PRIVATE KEY")
        for base in (SRC, ROOT / "docs", ROOT / "skills"):
            for path in base.rglob("*"):
                if path.is_file():
                    self.assertIsNone(pattern.search(path.read_text(encoding="utf-8", errors="ignore")), str(path))
        for name in ("README.md", "plugin.yaml", "CHANGELOG.md"):
            self.assertIsNone(pattern.search((ROOT / name).read_text(encoding="utf-8")), name)


class MultiSiteProfileTests(BentoTestCase):
    """One Hermes profile == one Bento site: separate scopes must never share caches, tokens or UUIDs."""

    def test_two_profiles_two_sites_do_not_cross(self):
        site_a, site_b = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"
        scope = {"BENTO_PUBLISHABLE_KEY": PK, "BENTO_SECRET_KEY": SK, "BENTO_SITE_UUID": site_a}
        with fake_secret_scope(scope):
            self.assertTrue(self.call("bento_stats", kind="site")["ok"])
            token_a = self.dry_then_token("bento_email_send", **good_email())
            scope["BENTO_SITE_UUID"] = site_b  # switch "profile"
            self.assertTrue(self.call("bento_stats", kind="site")["ok"])
            refused = self.call("bento_email_send", **good_email(), dry_run=False, confirm_token=token_a)
        sites = [r["query"]["site_uuid"][0] for r in self.server.hits("GET", "/stats/site")]
        self.assertEqual(sites, [site_a, site_b], "cache must be keyed per site")
        self.assertTrue(refused["refused"])
        self.assertIn("payload changed", refused["error"])
        self.assertEqual(self.server.posts(), [])

    def test_duplicate_ledger_is_per_site(self):
        site_a, site_b = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"
        scope = {**ENV, "BENTO_SITE_UUID": site_a}
        with fake_secret_scope(scope):
            token = self.dry_then_token("bento_email_send", **good_email())
            self.call("bento_email_send", **good_email(), dry_run=False, confirm_token=token)
            scope["BENTO_SITE_UUID"] = site_b
            self.assertEqual(self.call("bento_email_send", **good_email())["context"]["duplicates_in_last_24h"], 0)

    def test_unconfigured_profile_returns_clean_error_not_a_crash(self):
        with fake_secret_scope({}):
            out = self.call("bento_tag_list")
        self.assertFalse(out["ok"])
        self.assertEqual(out["kind"], "config")
        self.assertIn("BENTO_SITE_UUID", out["error"])
        self.assertEqual(self.server.requests, [])


class SettingsTests(BentoTestCase):
    def test_defaults_match_the_documented_values(self):
        s = Settings.load(lambda k, d: d)
        self.assertEqual((s.environment, s.bulk_threshold, s.tier2, s.min_schedule_lead_minutes, s.max_batch_size_per_hour,
                          s.max_recipients_per_call, s.allow_transactional_override, s.stats_cache_ttl_seconds,
                          s.request_timeout_seconds, s.log_emails, s.allowed_senders),
                         ("production", 25, "approve", 30, 5000, 10, False, 900, 30, False, ()))
        self.assertEqual(s.problems, ())

    def test_invalid_values_degrade_safely_and_are_reported(self):
        cfg = {"environment": "yolo", "safety.tier2": "none", "bulk_threshold": "lots", "max_recipients_per_call": 500,
               "min_schedule_lead_minutes": 0, "allow_transactional_override": "true", "allowed_senders": ["bad", "ok@example.com", 5]}
        s = Settings.load(lambda k, d: cfg.get(k, d))
        self.assertEqual(s.environment, "disabled")  # fail closed
        self.assertEqual(s.tier2, "approve")
        self.assertEqual(s.bulk_threshold, 25)
        self.assertEqual(s.max_recipients_per_call, 60)
        self.assertEqual(s.min_schedule_lead_minutes, 5)  # floor: the lead-time guard cannot be configured away
        self.assertFalse(s.allow_transactional_override)
        self.assertEqual(s.allowed_senders, ("ok@example.com",))
        self.assertGreaterEqual(len(s.problems), 6)

    def test_allowed_senders_accepts_comma_string_and_dedupes_case(self):
        s = Settings.load(lambda k, d: {"allowed_senders": "A@example.com, a@example.com ,b@example.com"}.get(k, d))
        self.assertEqual(s.allowed_senders, ("a@example.com", "b@example.com"))

    def test_sender_comparison_is_case_insensitive(self):
        out = self.call("bento_email_send", **good_email(**{"from": "NEWS@Example.com"}))
        self.assertTrue(out["ok"], out)

    def test_config_read_failure_uses_defaults(self):
        with mock.patch.object(self.ctx, "get_config", side_effect=ValueError("x")):
            with self.assertLogs("bento_hermes.config", level="WARNING"):
                self.assertEqual(self.rt.settings().environment, "production")


class BaseUrlTests(unittest.TestCase):
    def test_accepts_only_bento_https_or_loopback_http(self):
        for ok in ("https://app.bentonow.com/api/v1", "https://bentonow.com", "https://eu.app.bentonow.com/api/v1",
                   "http://localhost:8080/api/v1", "http://127.0.0.1:9/x", "http://[::1]:9/x"):
            self.assertIsNone(validate_base_url(ok), ok)
        for bad in ("https://evil.com", "https://bentonow.com.evil.com/api", "https://evilbentonow.com", "http://app.bentonow.com",
                    "http://169.254.169.254/latest", "https://user:pw@app.bentonow.com", "ftp://app.bentonow.com", "app.bentonow.com",
                    "https://app.bentonow.com@evil.com", "http://localhost.evil.com", ""):
            self.assertIsNotNone(validate_base_url(bad), bad)

    def test_invalid_override_is_ignored_and_default_host_used(self):
        class Ctx:
            state = None

            def get_config(self, key, default=None):
                return {"base_url": "https://evil.example.com/api/v1"}.get(key, default)

        from bento_hermes.config import Runtime
        with mock.patch.dict(os.environ, ENV):
            rt = Runtime(Ctx())
            settings = rt.settings()
            self.assertEqual(settings.base_url, "")
            self.assertTrue(any("base_url" in p for p in settings.problems))
            self.assertEqual(rt.client().base_url, DEFAULT_BASE_URL)


if __name__ == "__main__":
    unittest.main()
