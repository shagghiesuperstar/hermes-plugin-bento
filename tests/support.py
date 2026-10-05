"""Shared test harness: loopback fake Bento server, fake Hermes ctx, base TestCase.

Stdlib only so the suite runs with ``python -m unittest`` and under pytest alike.
"""

from __future__ import annotations

import calendar
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import bento_hermes  # noqa: E402
from bento_hermes.config import Runtime  # noqa: E402
from bento_hermes.safety import make_pre_tool_call, run_tool  # noqa: E402

# Fake credentials. Obviously fake, never real.
PK, SK, SITE = "bento-test-publishable-FAKE1", "bento-test-secret-FAKE1", "11111111-2222-3333-4444-555555555555"
ENV = {"BENTO_PUBLISHABLE_KEY": PK, "BENTO_SECRET_KEY": SK, "BENTO_SITE_UUID": SITE}
NOW = calendar.timegm((2026, 10, 5, 12, 0, 0))  # 2026-10-05T12:00:00Z


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeBento:
    """Loopback HTTP server that records every request and replies from a route table."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.routes: dict[tuple[str, str], list[dict]] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                return

            def _handle(self, method: str) -> None:
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw
                path = parts.path.removeprefix("/api/v1")
                outer.requests.append({"method": method, "path": path, "query": parse_qs(parts.query),
                                       "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                queue = outer.routes.get((method, path))
                if not queue:
                    resp = {"status": 404, "json": {"error": "no route"}}
                else:
                    resp = queue.pop(0) if len(queue) > 1 else queue[0]
                if resp.get("delay"):
                    threading.Event().wait(resp["delay"])
                payload = resp["raw"] if "raw" in resp else json.dumps(resp.get("json", {})).encode()
                try:
                    self.send_response(resp.get("status", 200))
                    self.send_header("Content-Type", resp.get("content_type", "application/json"))
                    self.send_header("Content-Length", str(len(payload)))
                    for k, v in resp.get("headers", {}).items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v1"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def route(self, method: str, path: str, *responses: dict) -> None:
        self.routes[(method, path)] = list(responses) or [{"json": {}}]

    def defaults(self) -> None:
        self.route("GET", "/fetch/subscribers", {"json": fixture("subscriber")})
        self.route("GET", "/fetch/tags", {"json": fixture("tags")})
        self.route("GET", "/fetch/fields", {"json": fixture("fields")})
        self.route("GET", "/fetch/broadcasts", {"json": fixture("broadcasts_sent")})
        self.route("GET", "/stats/site", {"json": fixture("stats_site")})
        self.route("GET", "/stats/segment", {"json": fixture("stats_segment")})
        self.route("POST", "/batch/subscribers", {"json": fixture("batch_results")})
        self.route("POST", "/batch/events", {"json": fixture("batch_results")})
        self.route("POST", "/fetch/commands", {"json": fixture("commands_results")})
        self.route("POST", "/fetch/tags", {"json": {"data": {}}})
        self.route("POST", "/fetch/fields", {"json": {"data": {}}})
        self.route("POST", "/batch/broadcasts", {"json": fixture("broadcast_create_response")})
        self.route("POST", "/batch/emails", {"json": fixture("emails_results")})

    def hits(self, method: str | None = None, path: str | None = None) -> list[dict]:
        return [r for r in self.requests if (method is None or r["method"] == method) and (path is None or r["path"] == path)]

    def posts(self) -> list[dict]:
        return self.hits("POST")


class FakeState:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.store: dict = {}
        self.fail = False

    def get(self, key, default=None):
        if self.fail:
            raise RuntimeError("state unavailable")
        return self.store.get(key, default)

    def set(self, key, value) -> None:
        if self.fail:
            raise RuntimeError("state unavailable")
        self.store[key] = json.loads(json.dumps(value))


class FakeCtx:
    """The slice of Hermes's PluginContext this plugin touches."""

    def __init__(self, data_dir: Path, config: dict | None = None) -> None:
        self.config = dict(config or {})
        self.state = FakeState(data_dir)
        self.tools: dict[str, dict] = {}
        self.hooks: dict[str, list] = {}
        self.cli: dict[str, dict] = {}
        self.skills: dict[str, dict] = {}
        self.set_config_calls: list = []

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def set_config(self, key, value) -> None:  # the plugin must never need this
        self.set_config_calls.append((key, value))

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)

    def register_cli_command(self, **kwargs):
        self.cli[kwargs["name"]] = kwargs

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills[name] = {"path": Path(path), "description": description}


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def mono(self) -> float:
        return self.now - NOW + 1000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


class BentoTestCase(unittest.TestCase):
    """Fake server + scrubbed env + fake ctx + Runtime. ``settings`` overrides plugin config."""

    settings: dict = {}

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.server = FakeBento()
        self.server.defaults()
        self.server.start()
        self.addCleanup(self.server.stop)
        patcher = mock.patch.dict(os.environ, ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in [n for n in os.environ if n.startswith("BENTO_") and n not in ENV]:
            os.environ.pop(name)
        self.clock = Clock()
        self.sleeps: list[float] = []
        config = {"base_url": self.server.base_url, "allowed_senders": ["news@example.com"], **self.settings}
        self.ctx = FakeCtx(self.tmp / "plugin-data" / "bento", config)
        self.rt = Runtime(self.ctx, clock=self.clock, monotonic=self.clock.mono, sleep=self.sleeps.append)
        self.registry = bento_hermes.REGISTRY
        self.hook_fn = make_pre_tool_call(self.rt, self.registry)

    # -- helpers ------------------------------------------------------------------------------------
    def call(self, tool: str, **args) -> dict:
        raw = run_tool(self.rt, self.registry[tool], args)
        self.assertIsInstance(raw, str)
        return json.loads(raw)

    def hook(self, tool: str, **args):
        return self.hook_fn(tool, args, task_id="t1")

    def dry_then_token(self, tool: str, **args) -> str:
        out = self.call(tool, **{**args, "dry_run": True})
        self.assertTrue(out.get("ok"), out)
        return out["confirm_token"]

    def audit_lines(self) -> list[dict]:
        path = self.ctx.state.data_dir / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def good_email(**overrides) -> dict:
    base = {"from": "news@example.com", "emails": [
        {"to": "jane@example.com", "subject": "Your order", "html_body": "<p>Hi {{ visitor.unsubscribe_url }}</p>"}]}
    base.update(overrides)
    return base


def good_schedule(**overrides) -> dict:
    base = {"name": "October news", "subject": "Hello", "content": "<p>hi</p>", "from_name": "Shop", "from_email": "news@example.com",
            "inclusive_tags": ["newsletter"], "batch_size_per_hour": 1000, "send_at": "2026-10-20T14:00:00Z",
            "acknowledge_unknown_audience": True}
    base.update(overrides)
    return base
