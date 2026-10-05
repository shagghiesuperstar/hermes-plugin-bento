"""``hermes bento status|doctor``.

status: confirms credentials are present (never prints them) and makes ONE cheap read
        (GET /fetch/fields; deliberately not /stats/*, which is 30 requests/hour).
doctor: offline configuration lint; makes no network calls.
"""

from __future__ import annotations

import argparse
from typing import Any

from .client import BentoError
from .config import REQUIRED_ENV, Runtime, resolve_secret

_SUBCOMMANDS = ("status", "doctor")


def register_cli(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="bento_cmd")
    sub.add_parser("status", help="Check credentials and reach Bento with one read-only call")
    sub.add_parser("doctor", help="Offline lint of settings, secret scope, state and audit log (no network)")


def _settings_report(rt: Runtime) -> list[str]:
    s = rt.settings()
    lines = [
        f"environment: {s.environment}",
        f"allowed_senders: {len(s.allowed_senders)} configured"
        + ("" if s.allowed_senders else "  (empty: broadcast scheduling and email sending are REFUSED)"),
        f"safety.tier2: {s.tier2}",
        f"bulk_threshold: {s.bulk_threshold}  max_recipients_per_call: {s.max_recipients_per_call}",
        f"allow_transactional_override: {s.allow_transactional_override}",
    ]
    lines += [f"setting problem: {p}" for p in s.problems]
    return lines


def _credential_report() -> tuple[list[str], bool]:
    lines, ok = [], True
    for name in REQUIRED_ENV:
        present = resolve_secret(name) is not None
        ok = ok and present
        lines.append(f"{name}: {'set' if present else 'MISSING'}")
    return lines, ok


def run_status(rt: Runtime, out: Any = print) -> int:
    lines, creds_ok = _credential_report()
    for line in [*lines, *_settings_report(rt)]:
        out(line)
    if not creds_ok:
        out("status: NOT READY - set the missing variables in this profile's .env")
        return 1
    try:
        rt.client().get("/fetch/fields")
    except BentoError as err:
        out(f"status: Bento unreachable or rejected the call: {err}")
        return 1
    out("status: OK - Bento reachable with the configured keys and site")
    return 0


def run_doctor(rt: Runtime, out: Any = print) -> int:
    lines, creds_ok = _credential_report()
    problems = 0
    for line in [*lines, *_settings_report(rt)]:
        out(line)
    problems += 0 if creds_ok else 1
    problems += len(rt.settings().problems)
    state_ok = rt.state_set("doctor_probe", True)
    out(f"plugin state writable: {state_ok}")
    out(f"audit log path available: {rt.audit.available()}  (T3 sends are refused without it)")
    problems += 0 if state_ok and rt.audit.available() else 1
    try:
        import agent.secret_scope  # noqa: F401  (internal module; see README 'Secrets')
        out("agent.secret_scope: importable (multiplex-safe credential reads)")
    except ImportError:
        out("agent.secret_scope: not importable; falling back to os.environ")
    out("doctor: OK" if problems == 0 else f"doctor: {problems} problem(s)")
    return 0 if problems == 0 else 1


def make_command(rt: Runtime):
    def bento_command(args: argparse.Namespace) -> int:
        cmd = getattr(args, "bento_cmd", None) or "status"
        return run_doctor(rt) if cmd == "doctor" else run_status(rt)

    return bento_command
