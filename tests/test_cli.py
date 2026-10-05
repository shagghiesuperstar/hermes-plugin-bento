"""`hermes bento status|doctor`."""

import argparse
import unittest
from unittest import mock

from support import PK, SITE, SK, BentoTestCase

from bento_hermes.cli import make_command, register_cli, run_doctor, run_status


class CliTests(BentoTestCase):
    def run_cmd(self, fn):
        lines: list[str] = []
        code = fn(self.rt, out=lines.append)
        return code, "\n".join(lines)

    def test_status_ok_uses_one_cheap_non_stats_read_and_prints_no_secret(self):
        code, text = self.run_cmd(run_status)
        self.assertEqual(code, 0, text)
        self.assertIn("status: OK", text)
        self.assertEqual([r["path"] for r in self.server.requests], ["/fetch/fields"])
        for secret in (PK, SK, SITE):
            self.assertNotIn(secret, text)

    def test_status_reports_missing_credentials_without_network(self):
        with mock.patch.dict("os.environ"):
            import os
            os.environ.pop("BENTO_SECRET_KEY")
            code, text = self.run_cmd(run_status)
        self.assertEqual(code, 1)
        self.assertIn("BENTO_SECRET_KEY: MISSING", text)
        self.assertEqual(self.server.requests, [])

    def test_status_reports_unreachable_or_rejected(self):
        self.server.route("GET", "/fetch/fields", {"status": 401, "json": {"error": "Unauthorized"}})
        code, text = self.run_cmd(run_status)
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401", text)
        self.assertNotIn(SK, text)

    def test_status_warns_when_allowlist_empty(self):
        self.ctx.config["allowed_senders"] = []
        _, text = self.run_cmd(run_status)
        self.assertIn("REFUSED", text)

    def test_doctor_is_offline(self):
        code, text = self.run_cmd(run_doctor)
        self.assertEqual(code, 0, text)
        self.assertIn("doctor: OK", text)
        self.assertEqual(self.server.requests, [])

    def test_doctor_flags_setting_problems_and_unwritable_state(self):
        self.ctx.config["bulk_threshold"] = "lots"
        self.ctx.state.fail = True
        with self.assertLogs("bento_hermes.config", level="WARNING"):
            code, text = self.run_cmd(run_doctor)
        self.assertEqual(code, 1)
        self.assertIn("setting problem: bulk_threshold", text)
        self.assertIn("plugin state writable: False", text)

    def test_argparse_wiring(self):
        parser = argparse.ArgumentParser()
        register_cli(parser)
        self.assertEqual(parser.parse_args(["status"]).bento_cmd, "status")
        self.assertEqual(parser.parse_args(["doctor"]).bento_cmd, "doctor")
        self.assertIsNone(parser.parse_args([]).bento_cmd)
        command = make_command(self.rt)
        with mock.patch("builtins.print"):
            self.assertEqual(command(argparse.Namespace(bento_cmd=None)), 0)  # defaults to status


if __name__ == "__main__":
    unittest.main()
