"""Smoke test against the REAL Hermes plugin loader and approval gate (not part of unittest discovery).

Run with Hermes's own interpreter, in a throwaway HERMES_HOME (nothing under ~/.hermes is touched):

    ~/.hermes/hermes-agent/venv/bin/python tests/real_hermes_smoke.py

Uses a loopback fake Bento server; never contacts Bento.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))
# Hermes's own checkout provides hermes_cli/tools; override with HERMES_AGENT_DIR for another checkout.
HERMES_DIR = Path(os.environ.get("HERMES_AGENT_DIR") or Path.home() / ".hermes" / "hermes-agent")
sys.path.insert(1, str(HERMES_DIR))

from support import ENV, FakeBento  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    server = FakeBento()
    server.defaults()
    server.start()
    home = Path(tempfile.mkdtemp(prefix="bento-smoke-"))
    os.environ["HERMES_HOME"] = str(home)
    os.environ.update(ENV)
    try:
        shutil.copytree(REPO, home / "plugins" / "bento", ignore=shutil.ignore_patterns(".git", "__pycache__", "tests"))
        import yaml

        (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["bento"], "entries": {"bento": {"settings": {
            "base_url": server.base_url, "allowed_senders": ["news@example.com"]}}}}}))

        from hermes_cli.plugins import (  # type: ignore[import-not-found]
            discover_plugins,
            get_plugin_manager,
            get_pre_tool_call_directive,
            resolve_pre_tool_block,
        )
        from tools.registry import registry  # type: ignore[import-not-found]

        discover_plugins(force=True)
        loaded = get_plugin_manager()._plugins
        key = next((k for k in loaded if k.endswith("bento")), None)
        check("plugin loaded by real loader", key is not None and loaded[key].enabled and not loaded[key].error,
              str(getattr(loaded.get(key), "error", None)))
        tools = [n for n in (e for e in registry.get_all_tool_names()) if n.startswith("bento_")] if hasattr(registry, "get_all_tool_names") else []
        check("15 bento tools registered", len(tools) == 15, str(len(tools)))
        entry = registry.get_entry("bento_email_send")
        check("check_fn true with env set", bool(entry and entry.check_fn()))

        email = {"from": "news@example.com", "emails": [{"to": "jane@example.com", "subject": "Hi", "html_body": "<p>x {{ visitor.unsubscribe_url }}</p>"}]}
        dry = json.loads(registry.dispatch("bento_email_send", dict(email)))
        check("dry run default, nothing posted", dry.get("dry_run") is True and not server.posts())
        refused = json.loads(registry.dispatch("bento_email_send", {**email, "dry_run": False}))
        check("real call without token refused", refused.get("refused") is True and not server.posts())

        real = {**email, "dry_run": False, "confirm_token": dry["confirm_token"]}
        directive, message = get_pre_tool_call_directive("bento_email_send", real)
        check("hook -> approve for real T3 call", directive == "approve" and "bento_email_send" in (message or ""), str((directive, message)))
        d2, _ = get_pre_tool_call_directive("bento_email_send", {**email, "dry_run": False})
        check("hook -> block for tokenless real call", d2 == "block")
        d3, _ = get_pre_tool_call_directive("bento_email_send", dict(email))
        check("hook -> nothing for dry run", d3 is None)
        d4, _ = get_pre_tool_call_directive("bento_tag_list", {})
        check("hook -> nothing for reads", d4 is None)

        blocked = resolve_pre_tool_block("bento_email_send", real)
        check("Hermes approval gate fails closed with no human present", bool(blocked) and not server.posts(), str(blocked))
        print("  gate message:", str(blocked)[:160])

        sent = json.loads(registry.dispatch("bento_email_send", real))
        check("handler sends once when called with the valid token", sent.get("ok") and len(server.posts()) == 1, json.dumps(sent)[:200])
        audit = home / "plugin-data"
        files = list(audit.rglob("audit.jsonl"))
        check("audit log written under plugin-data", len(files) == 1 and "jane@example.com" not in files[0].read_text())
        check("signing key persisted in real PluginState", any("hmac_key" in p.read_text() for p in audit.rglob("state.json")))
        listed = json.loads(registry.dispatch("bento_tag_list", {}))
        check("read tool works through registry", listed.get("ok") and "vip" in listed["data"]["tags"])
    finally:
        server.stop()
        shutil.rmtree(home, ignore_errors=True)
    print("FAILURES:", failures if failures else "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
