"""Parallel tool calls: a confirm token is single-use and a duplicate email is sent once, even under threads.

Hermes can run several tool calls from one assistant turn on parallel threads. Each test loops many
trials because a single racy run can pass by luck. ``audit_event`` is slowed a little (a slow disk) so
the window between "token valid" and "token used" is wide enough to catch a missing lock reliably.
"""

import threading
import time
import unittest
from unittest import mock

from support import BentoTestCase, good_email, good_schedule  # noqa: I001 - puts src/ on sys.path

from bento_hermes.client import BentoError  # noqa: E402

TRIALS = 25


class ConcurrentReplayTests(BentoTestCase):
    def setUp(self) -> None:
        super().setUp()
        real_audit = self.rt.audit_event

        def slow_audit(event, recipients=None):
            time.sleep(0.003)
            return real_audit(event, recipients=recipients)

        patcher = mock.patch.object(self.rt, "audit_event", slow_audit)
        patcher.start()
        self.addCleanup(patcher.stop)

    def race(self, calls: list[tuple[str, dict]]) -> list[dict]:
        """Run every (tool, args) call on its own thread, released together by a barrier."""
        barrier = threading.Barrier(len(calls))
        results: list[dict | None] = [None] * len(calls)
        errors: list[BaseException] = []

        def work(i: int, tool: str, args: dict) -> None:
            try:
                barrier.wait(timeout=10)
                results[i] = self.call(tool, **args)
            except BaseException as err:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(err)

        threads = [threading.Thread(target=work, args=(i, t, a)) for i, (t, a) in enumerate(calls)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        return results  # type: ignore[return-value]

    def assert_exactly_one_send(self, path: str, results: list[dict], posts_before: int, label: str) -> None:
        sent = [r for r in results if r.get("sent") is True]
        refused = [r for r in results if r.get("refused") is True and r.get("sent") is False]
        self.assertEqual(len(sent), 1, f"{label}: {results}")
        self.assertEqual(len(refused), len(results) - 1, f"{label}: {results}")
        self.assertEqual(len(self.server.hits("POST", path)) - posts_before, 1, f"{label}: more than one POST")

    def test_same_token_across_threads_sends_one_email(self):
        for trial in range(TRIALS):
            for threads in (2, 4):
                args = good_email()
                args["emails"][0]["subject"] = f"Order {trial}-{threads}"
                token = self.dry_then_token("bento_email_send", **args)
                before = len(self.server.hits("POST", "/batch/emails"))
                results = self.race([("bento_email_send", {**args, "dry_run": False, "confirm_token": token})] * threads)
                self.assert_exactly_one_send("/batch/emails", results, before, f"trial {trial} x{threads}")

    def test_same_token_across_threads_schedules_one_broadcast(self):
        for trial in range(TRIALS):
            for threads in (2, 4):
                args = good_schedule(name=f"News {trial}-{threads}")
                token = self.dry_then_token("bento_broadcast_schedule", **args)
                before = len(self.server.hits("POST", "/batch/broadcasts"))
                results = self.race([("bento_broadcast_schedule", {**args, "dry_run": False, "confirm_token": token})] * threads)
                self.assert_exactly_one_send("/batch/broadcasts", results, before, f"trial {trial} x{threads}")

    def test_distinct_tokens_for_identical_email_send_once(self):
        """Two dry runs give two valid tokens for the same content; the duplicate ledger must still hold."""
        for trial in range(TRIALS):
            args = good_email()
            args["emails"][0]["subject"] = f"Dup {trial}"
            tokens = [self.dry_then_token("bento_email_send", **args) for _ in range(2)]
            self.assertNotEqual(*tokens)
            before = len(self.server.hits("POST", "/batch/emails"))
            results = self.race([("bento_email_send", {**args, "dry_run": False, "confirm_token": t}) for t in tokens])
            self.assert_exactly_one_send("/batch/emails", results, before, f"trial {trial}")
            self.assertTrue(any("identical" in r.get("error", "") for r in results), results)


class ReservationAndClientOrderTests(BentoTestCase):
    def test_definite_rejection_releases_the_duplicate_reservation(self):
        self.server.route("POST", "/batch/emails", {"status": 400, "json": {"error": "bad"}}, {"json": {"results": 1}})
        args = good_email()
        out = self.call("bento_email_send", **args, dry_run=False, confirm_token=self.dry_then_token("bento_email_send", **args))
        self.assertFalse(out["ok"])
        self.assertIs(out["sent"], False)
        again = self.call("bento_email_send", **args, dry_run=False, confirm_token=self.dry_then_token("bento_email_send", **args))
        self.assertTrue(again["ok"] and again["sent"], again)

    def test_release_never_removes_an_earlier_genuine_send(self):
        args = good_email()
        first = self.call("bento_email_send", **args, dry_run=False, confirm_token=self.dry_then_token("bento_email_send", **args))
        self.assertTrue(first["sent"], first)
        self.server.route("POST", "/batch/emails", {"status": 400, "json": {"error": "bad"}})
        forced = {**args, "allow_duplicate": True}
        self.call("bento_email_send", **forced, dry_run=False, confirm_token=self.dry_then_token("bento_email_send", **forced))
        blocked = self.call("bento_email_send", **args, dry_run=False, confirm_token=self.dry_then_token("bento_email_send", **args))
        self.assertTrue(blocked["refused"], blocked)

    def test_missing_client_does_not_burn_the_token(self):
        token = self.dry_then_token("bento_email_send", **good_email())
        with mock.patch.object(self.rt, "client", side_effect=BentoError("not configured", kind="config")):
            out = self.call("bento_email_send", **good_email(), dry_run=False, confirm_token=token)
        self.assertFalse(out["ok"])
        self.assertEqual(self.server.posts(), [])
        retry = self.call("bento_email_send", **good_email(), dry_run=False, confirm_token=token)
        self.assertTrue(retry["ok"] and retry["sent"], retry)


if __name__ == "__main__":
    unittest.main()
