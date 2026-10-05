"""BentoClient: auth, headers, retries, redirects, rate budget, redaction."""

import base64
import time
import unittest
from urllib.parse import urlsplit

from support import PK, SITE, SK, BentoTestCase

from bento_hermes.client import BentoClient, BentoError, Credentials, RateGuard, redact

CREDS = Credentials(PK, SK, SITE)


class ClientTests(BentoTestCase):
    def client(self, **kw) -> BentoClient:
        kw.setdefault("sleep", self.sleeps.append)
        return BentoClient(CREDS, base_url=self.server.base_url, **kw)

    def test_basic_auth_site_uuid_user_agent_and_accept(self):
        self.client().get("/fetch/tags")
        (req,) = self.server.hits("GET", "/fetch/tags")
        expected = "Basic " + base64.b64encode(f"{PK}:{SK}".encode()).decode()
        self.assertEqual(req["headers"]["authorization"], expected)
        self.assertEqual(req["query"]["site_uuid"], [SITE])
        self.assertRegex(req["headers"]["user-agent"], r"^hermes-plugin-bento/\d")
        self.assertEqual(req["headers"]["accept"], "application/json")

    def test_post_sends_json_content_type_and_compact_body(self):
        self.client().post("/batch/events", {"events": [{"type": "x", "email": "a@example.com"}]})
        (req,) = self.server.hits("POST", "/batch/events")
        self.assertEqual(req["headers"]["content-type"], "application/json")
        self.assertEqual(req["body"], {"events": [{"type": "x", "email": "a@example.com"}]})

    def test_site_uuid_is_added_even_when_caller_params_present(self):
        self.client().get("/fetch/subscribers", {"email": "a@example.com", "page": None})
        (req,) = self.server.hits("GET", "/fetch/subscribers")
        self.assertEqual(sorted(req["query"]), ["email", "site_uuid"])

    def test_get_retries_429_then_succeeds_honoring_retry_after(self):
        self.server.route("GET", "/fetch/tags", {"status": 429, "json": {"error": "slow"}, "headers": {"Retry-After": "2"}},
                          {"json": {"data": []}})
        self.assertEqual(self.client().get("/fetch/tags"), {"data": []})
        self.assertEqual(len(self.server.hits("GET", "/fetch/tags")), 2)
        self.assertEqual(self.sleeps, [2.0])

    def test_get_gives_up_after_three_attempts_with_backoff(self):
        self.server.route("GET", "/fetch/tags", {"status": 503, "json": {"error": "down"}})
        with self.assertRaises(BentoError) as caught:
            self.client().get("/fetch/tags")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(len(self.server.hits("GET", "/fetch/tags")), 3)
        self.assertEqual(len(self.sleeps), 2)
        self.assertTrue(all(0 < s <= 8 for s in self.sleeps))

    def test_post_is_never_retried_by_default(self):
        self.server.route("POST", "/batch/emails", {"status": 503, "json": {}})
        with self.assertRaises(BentoError):
            self.client().post("/batch/emails", {"emails": []})
        self.assertEqual(len(self.server.hits("POST", "/batch/emails")), 1)
        self.assertEqual(self.sleeps, [])

    def test_idempotent_post_is_retried(self):
        self.server.route("POST", "/batch/events", {"status": 502, "json": {}}, {"json": {"results": 1}})
        self.assertEqual(self.client().post("/batch/events", {"events": []}, idempotent=True), {"results": 1})
        self.assertEqual(len(self.server.hits("POST", "/batch/events")), 2)

    def test_4xx_is_not_retried_and_message_is_clean(self):
        self.server.route("GET", "/fetch/tags", {"status": 422, "json": {"errors": [{"field": "x", "message": "bad"}]}})
        with self.assertRaises(BentoError) as caught:
            self.client().get("/fetch/tags")
        self.assertEqual(len(self.server.hits("GET", "/fetch/tags")), 1)
        self.assertIn("HTTP 422", str(caught.exception))
        self.assertIn("sender is not a verified Author", str(caught.exception))

    def test_401_hint_mentions_site_uuid(self):
        self.server.route("GET", "/fetch/tags", {"status": 401, "json": {"error": "Unauthorized"}})
        with self.assertRaises(BentoError) as caught:
            self.client().get("/fetch/tags")
        self.assertIn("site_uuid", str(caught.exception))

    def test_html_instead_of_json_points_at_user_agent(self):
        self.server.route("GET", "/fetch/tags", {"status": 200, "raw": b"<html>Blocked by Cloudflare</html>", "content_type": "text/html"})
        with self.assertRaises(BentoError) as caught:
            self.client().get("/fetch/tags")
        self.assertEqual(caught.exception.kind, "non_json")
        self.assertIn("User-Agent", str(caught.exception))

    def test_secrets_never_appear_in_error_text_even_when_server_echoes_them(self):
        echo = {"error": f"bad creds {PK} / {SK}", "auth": "Basic " + base64.b64encode(f"{PK}:{SK}".encode()).decode()}
        self.server.route("GET", "/fetch/tags", {"status": 400, "json": echo})
        with self.assertRaises(BentoError) as caught:
            self.client().get("/fetch/tags")
        text = str(caught.exception)
        for secret in (PK, SK, base64.b64encode(f"{PK}:{SK}".encode()).decode()):
            self.assertNotIn(secret, text)
        self.assertIn("[REDACTED]", text)

    def test_redirects_are_never_followed_so_credentials_cannot_leak(self):
        self.server.route("GET", "/fetch/tags", {"status": 302, "json": {}, "headers": {"Location": "/api/v1/elsewhere"}})
        self.server.route("GET", "/elsewhere", {"json": {"leak": True}})
        with self.assertRaises(BentoError):
            self.client().get("/fetch/tags")
        self.assertEqual(self.server.hits("GET", "/elsewhere"), [])

    def test_timeout_is_enforced(self):
        self.server.route("GET", "/fetch/tags", {"json": {}, "delay": 1.5})
        started = time.monotonic()
        with self.assertRaises(BentoError) as caught:
            self.client(timeout=0.2).get("/fetch/tags")
        self.assertLess(time.monotonic() - started, 1.4 * 4)
        self.assertEqual(caught.exception.kind, "network")

    def test_connection_refused_is_a_clean_network_error(self):
        dead = BentoClient(CREDS, base_url="http://127.0.0.1:9/api/v1", timeout=1, sleep=self.sleeps.append)
        with self.assertRaises(BentoError) as caught:
            dead.get("/fetch/tags")
        self.assertEqual(caught.exception.kind, "network")

    def test_empty_body_is_ok(self):
        self.server.route("GET", "/fetch/tags", {"raw": b""})
        self.assertEqual(self.client().get("/fetch/tags"), {})

    def test_client_side_stats_budget_refuses_without_a_request(self):
        t = [0.0]
        guard = RateGuard(clock=lambda: t[0])
        client = self.client(guard=guard)
        for _ in range(30):
            client.get("/stats/site")
        with self.assertRaises(BentoError) as caught:
            client.get("/stats/site")
        self.assertEqual(caught.exception.kind, "client_rate_limit")
        self.assertEqual(len(self.server.hits("GET", "/stats/site")), 30)
        t[0] = 3601.0
        client.get("/stats/site")
        self.assertEqual(len(self.server.hits("GET", "/stats/site")), 31)

    def test_client_side_api_budget_is_60_per_minute(self):
        guard = RateGuard(clock=lambda: 0.0)
        for _ in range(60):
            guard.acquire("api")
        with self.assertRaises(BentoError):
            guard.acquire("api")

    def test_base_url_host_matches_loopback_only_in_tests(self):
        self.assertEqual(urlsplit(self.server.base_url).hostname, "127.0.0.1")


class RedactTests(unittest.TestCase):
    def test_redact_ignores_short_or_empty_secrets(self):
        self.assertEqual(redact("abc", ["", "ab"]), "abc")
        self.assertEqual(redact("key=sk_12345", ["sk_12345"]), "key=[REDACTED]")


if __name__ == "__main__":
    unittest.main()
