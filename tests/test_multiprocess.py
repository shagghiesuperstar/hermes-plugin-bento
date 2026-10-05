"""Two real OS processes sharing one profile (gateway + cron) must not both spend a token or both send.

The workers share a file-backed state whose ``get``/``set`` are individually atomic (like Hermes's
``PluginState``), with a slow ``get`` so the read -> write window is wide. The ``nolock`` runs disable the
claim lock and MUST double-send: they prove the harness can see the bug, so the locked runs mean something.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from mp_worker import make_runtime  # noqa: I001 - tests dir is on sys.path
from support import ENV, FakeBento, good_email

from bento_hermes import REGISTRY
from bento_hermes.claim import ClaimError, ClaimLock
from bento_hermes.safety import run_tool

WORKER = Path(__file__).resolve().parent / "mp_worker.py"
STATE_DELAY = 0.05
START_LEAD = 2.0


class TwoProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name) / "plugin-data" / "bento"
        self.server = FakeBento()
        self.server.defaults()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.rt = make_runtime(self.data_dir, self.server.base_url)  # the "previewing" process

    def dry_token(self, args: dict) -> str:
        out = json.loads(run_tool(self.rt, REGISTRY["bento_email_send"], {**args, "dry_run": True}))
        self.assertTrue(out["ok"], out)
        return out["confirm_token"]

    def run_processes(self, arg_sets: list[dict], mode: str) -> list[dict]:
        start_at = time.time() + START_LEAD
        env = {**os.environ, **ENV}
        procs = []
        for args in arg_sets:
            spec = {"dir": str(self.data_dir), "base_url": self.server.base_url, "tool": "bento_email_send",
                    "args": {**args, "dry_run": False}, "start_at": start_at, "mode": mode, "delay": STATE_DELAY}
            procs.append(subprocess.Popen([sys.executable, str(WORKER), json.dumps(spec)], env=env,  # noqa: S603
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err)
            results.append(json.loads(out.strip().splitlines()[-1]))
        return results

    def posts(self) -> int:
        return len(self.server.hits("POST", "/batch/emails"))

    def same_token_calls(self, tag: str) -> list[dict]:
        args = good_email(emails=[{"to": "jane@example.com", "subject": f"Same token {tag}", "html_body": "<p>x</p>"}])
        token = self.dry_token(args)
        return [{**args, "confirm_token": token}] * 2

    def two_token_calls(self, tag: str) -> list[dict]:
        args = good_email(emails=[{"to": "jane@example.com", "subject": f"Two tokens {tag}", "html_body": "<p>x</p>"}])
        tokens = [self.dry_token(args) for _ in range(2)]
        self.assertNotEqual(*tokens)
        return [{**args, "confirm_token": t} for t in tokens]

    # -- with the cross-process claim ---------------------------------------------------------------
    def test_same_token_in_two_processes_sends_once(self):
        results = self.run_processes(self.same_token_calls("locked"), "lock")
        self.assertEqual(self.posts(), 1, results)
        self.assertEqual(sorted(r.get("sent") for r in results), [False, True], results)
        self.assertTrue(all(r.get("refused") for r in results if r.get("sent") is False), results)

    def test_two_tokens_for_one_email_in_two_processes_send_once(self):
        results = self.run_processes(self.two_token_calls("locked"), "lock")
        self.assertEqual(self.posts(), 1, results)
        self.assertEqual(sorted(r.get("sent") for r in results), [False, True], results)
        self.assertTrue(any("identical" in r.get("error", "") for r in results), results)

    # -- harness self-check: WITHOUT the claim the same runs double-send ------------------------------
    def test_without_the_claim_same_token_double_sends(self):
        self.run_processes(self.same_token_calls("nolock"), "nolock")
        self.assertEqual(self.posts(), 2, "harness cannot see the race; the locked tests prove nothing")

    def test_without_the_claim_two_tokens_double_send(self):
        self.run_processes(self.two_token_calls("nolock"), "nolock")
        self.assertEqual(self.posts(), 2, "harness cannot see the race; the locked tests prove nothing")


class ClaimNotPersistedTests(unittest.TestCase):
    """If the claim cannot be persisted the send is refused and the token is not treated as spent."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name) / "bento"
        self.server = FakeBento()
        self.server.defaults()
        self.server.start()
        self.addCleanup(self.server.stop)
        os.environ.update(ENV)
        self.rt = make_runtime(self.data_dir, self.server.base_url)
        self.args = good_email()
        out = json.loads(run_tool(self.rt, REGISTRY["bento_email_send"], {**self.args, "dry_run": True}))
        self.token = out["confirm_token"]

    def send(self) -> dict:
        return json.loads(run_tool(self.rt, REGISTRY["bento_email_send"],
                                   {**self.args, "dry_run": False, "confirm_token": self.token}))

    def test_unwritable_used_tokens_refuses_and_does_not_spend(self):
        real_set = self.rt.ctx.state.set

        def failing_set(key, value):
            if key == "used_tokens":
                raise OSError("disk full")
            real_set(key, value)

        self.rt.ctx.state.set = failing_set
        with self.assertLogs("bento_hermes.config", level="WARNING"):
            out = self.send()
        self.assertTrue(out["refused"] and out["sent"] is False, out)
        self.assertIn("not spent", out["error"])
        self.assertEqual(self.server.posts(), [])
        self.rt.ctx.state.set = real_set  # disk recovered: the SAME token still works
        again = self.send()
        self.assertTrue(again["sent"], again)

    def test_unwritable_ledger_refuses_and_leaves_the_token_unspent(self):
        real_set = self.rt.ctx.state.set

        def failing_set(key, value):
            if key == "email_ledger":
                raise OSError("disk full")
            real_set(key, value)

        self.rt.ctx.state.set = failing_set
        with self.assertLogs("bento_hermes.config", level="WARNING"):
            out = self.send()
        self.assertTrue(out["refused"], out)
        self.assertEqual(self.server.posts(), [])
        self.rt.ctx.state.set = real_set
        self.assertTrue(self.send()["sent"])

    def test_unreadable_state_refuses(self):
        self.rt.ctx.state.get = lambda key, default=None: (_ for _ in ()).throw(RuntimeError("corrupt"))
        with self.assertLogs("bento_hermes.config", level="WARNING"):
            out = self.send()
        self.assertTrue(out["refused"] and out["sent"] is False, out)
        self.assertEqual(self.server.posts(), [])

    def test_lock_unavailable_refuses(self):
        self.rt.gate = ClaimLock(lambda: None)  # no data dir -> no cross-process lock -> no send
        with self.assertLogs("bento_hermes.safety", level="WARNING"):
            out = self.send()
        self.assertTrue(out["refused"] and out["sent"] is False, out)
        self.assertEqual(self.server.posts(), [])

    def test_lock_times_out_while_another_holder_has_it(self):
        gate = ClaimLock(lambda: self.data_dir / "claims.lock", timeout=0.2)
        other = ClaimLock(lambda: self.data_dir / "claims.lock", timeout=0.2)
        with gate:
            with self.assertRaises(ClaimError), other:
                pass
        with other:  # released: acquirable again
            pass


if __name__ == "__main__":
    unittest.main()
