"""Registration contract: manifest == registered tools/hooks, schemas, check_fn gating, stdlib-only."""

import ast
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

from support import ENV, ROOT, SRC, BentoTestCase, FakeCtx

import bento_hermes
from bento_hermes.safety import run_tool


def manifest_list(key: str) -> list[str]:
    """Items of a top-level YAML list in plugin.yaml (stdlib has no YAML parser)."""
    items, active = [], False
    for line in (ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        if re.match(rf"^{key}:\s*$", line):
            active = True
        elif active and re.match(r"^\S", line):
            break
        elif active:
            m = re.match(r"^\s+- (?:name: )?(\S+)\s*$", line)
            if m:
                items.append(m.group(1))
    return items


class RegisterTests(BentoTestCase):
    def setUp(self):
        super().setUp()
        self.reg = FakeCtx(self.tmp / "reg")
        bento_hermes.register(self.reg)

    def test_registers_all_fifteen_tools_matching_manifest_exactly(self):
        declared = manifest_list("provides_tools")
        self.assertEqual(len(declared), 15)
        self.assertEqual(sorted(self.reg.tools), sorted(declared))

    def test_hook_cli_and_skill_registered_and_declared(self):
        self.assertEqual(manifest_list("provides_hooks"), ["pre_tool_call"])
        self.assertEqual(list(self.reg.hooks), ["pre_tool_call"])
        self.assertIn("bento", self.reg.cli)
        self.assertIn("bento-ops", self.reg.skills)
        self.assertTrue(self.reg.skills["bento-ops"]["path"].is_file())

    def test_tool_registration_details(self):
        for name, reg in self.reg.tools.items():
            self.assertEqual(reg["toolset"], "bento")
            self.assertEqual(reg["requires_env"], list(ENV))
            self.assertEqual(reg["schema"]["name"], name)
            self.assertTrue(callable(reg["check_fn"]) and callable(reg["handler"]))
            self.assertFalse(reg.get("override", False))

    def test_requires_env_declared_equals_the_three_bento_vars(self):
        self.assertEqual(manifest_list("requires_env"), sorted(ENV, key=list(ENV).index))
        text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.assertRegex(text, r"name: BENTO_SITE_UUID\n(?:.*\n)*?\s+secret: false")

    def test_config_schema_covers_every_setting_the_code_reads(self):
        text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        for key in ("environment", "allowed_senders", "bulk_threshold", "safety.tier2", "min_schedule_lead_minutes",
                    "max_batch_size_per_hour", "max_recipients_per_call", "allow_transactional_override",
                    "stats_cache_ttl_seconds", "request_timeout_seconds", "audit.log_emails", "base_url"):
            self.assertRegex(text, rf"(?m)^  {re.escape(key)}:", key)
        for key in re.findall(r'get\("([a-z_.0-9]+)", ', (SRC / "bento_hermes" / "config.py").read_text(encoding="utf-8")):
            self.assertRegex(text, rf"(?m)^  {re.escape(key)}:", key)

    def test_check_fn_hides_tools_without_all_three_values(self):
        check = self.reg.tools["bento_tag_list"]["check_fn"]
        self.assertTrue(check())
        for missing in ENV:
            with mock.patch.dict(os.environ):
                os.environ.pop(missing)
                self.assertFalse(check(), missing)
        with mock.patch.dict(os.environ, {"BENTO_SECRET_KEY": "   "}):
            self.assertFalse(check())
        with mock.patch.dict(os.environ, {"BENTO_SITE_UUID": "not a uuid!"}):
            self.assertFalse(check())

    def test_schemas_are_wellformed_and_required_subset_of_properties(self):
        for name, reg in self.reg.tools.items():
            schema = reg["schema"]
            self.assertTrue(schema["description"].strip())
            params = schema["parameters"]
            self.assertEqual(params["type"], "object")
            self.assertTrue(set(params["required"]) <= set(params["properties"]), name)
            json.dumps(schema)
            for prop, spec in params["properties"].items():
                self.assertIn("type", spec, f"{name}.{prop}")

    def test_credentials_are_never_tool_parameters(self):
        for reg in self.reg.tools.values():
            props = " ".join(reg["schema"]["parameters"]["properties"]).lower()
            for banned in ("secret", "password", "publishable", "site_uuid", "api_key", "authorization", "credential"):
                self.assertNotIn(banned, props, reg["schema"]["name"])

    def test_draft_schema_has_no_approved_or_send_at(self):
        props = self.reg.tools["bento_broadcast_create"]["schema"]["parameters"]["properties"]
        self.assertNotIn("approved", props)
        self.assertNotIn("send_at", props)

    def test_t3_schemas_default_dry_run_documented(self):
        for name in ("bento_broadcast_schedule", "bento_email_send"):
            self.assertIn("Defaults to TRUE", self.reg.tools[name]["schema"]["parameters"]["properties"]["dry_run"]["description"])

    def test_handlers_never_raise_and_always_return_json_strings(self):
        garbage = [None, [], "x", 5, {"x": 1}, {"email": None}, {"emails": "x"}, {"subscribers": {}}, {"kind": []}]
        for name, spec in self.registry.items():
            for args in garbage:
                raw = run_tool(self.rt, spec, args)
                self.assertIsInstance(raw, str, name)
                self.assertIsInstance(json.loads(raw), dict, name)
            handler = self.reg.tools[name]["handler"]
            self.assertIsInstance(handler({"bogus": 1}, task_id="t", session_id="s"), str)
        self.assertEqual(self.server.posts(), [], "garbage input must never reach Bento as a write")

    def test_register_does_no_network_and_no_state_writes(self):
        fresh = FakeCtx(self.tmp / "fresh")
        before = len(self.server.requests)
        bento_hermes.register(fresh)
        self.assertEqual(len(self.server.requests), before)
        self.assertEqual(fresh.state.store, {})
        self.assertFalse((self.tmp / "fresh").exists())


class RepoHygieneTests(unittest.TestCase):
    def test_runtime_is_stdlib_only(self):
        stdlib = set(sys.stdlib_module_names)
        for path in SRC.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mods = [node.module.split(".")[0]]
                for mod in mods:
                    self.assertTrue(mod in stdlib or mod in {"agent"}, f"{path.name} imports non-stdlib {mod}")

    def test_only_documented_hermes_internal_import_is_secret_scope(self):
        internal = set()
        for path in SRC.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                elif isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                else:
                    continue
                internal |= {n for n in names if n.split(".")[0] in {"agent", "hermes_cli", "tools", "plugins", "gateway"}}
        self.assertEqual(internal, {"agent.secret_scope"})

    def test_no_subprocess_shell_environ_mutation_or_self_update(self):
        text = "\n".join(p.read_text(encoding="utf-8") for p in SRC.rglob("*.py"))
        for banned in ("subprocess", "os.system", "os.popen", "eval(", "exec(", "pickle", "os.environ[", "os.putenv",
                       "setdefault(\"BENTO", "pip install", "git pull", "__import__("):
            self.assertNotIn(banned, text, banned)

    def test_only_documented_host_is_hardcoded(self):
        text = "\n".join(p.read_text(encoding="utf-8") for p in SRC.rglob("*.py"))
        hosts = set(re.findall(r"https?://([A-Za-z0-9.-]+)", text))
        self.assertTrue(hosts <= {"app.bentonow.com", "bentonow.com", "127.0.0.1", "localhost"}, hosts)

    def test_skill_description_within_upstream_limit(self):
        text = (ROOT / "skills" / "bento-ops" / "SKILL.md").read_text(encoding="utf-8")
        desc = re.search(r"^description: (.*)$", text, re.MULTILINE).group(1)
        self.assertLessEqual(len(desc), 60)
        self.assertTrue(desc.endswith("."))

    def test_expected_tree_files_exist(self):
        for rel in ("plugin.yaml", "__init__.py", "pyproject.toml", "README.md", "CHANGELOG.md", "LICENSE",
                    "src/bento_hermes/__init__.py", "src/bento_hermes/config.py", "src/bento_hermes/client.py",
                    "src/bento_hermes/schemas.py", "src/bento_hermes/tools_read.py", "src/bento_hermes/tools_write.py",
                    "src/bento_hermes/tools_send.py", "src/bento_hermes/safety.py", "src/bento_hermes/cache.py",
                    "src/bento_hermes/cli.py", "skills/bento-ops/SKILL.md", "tests/conftest.py", "docs/index.md",
                    ".github/workflows/test.yml", ".github/workflows/validate.yml"):
            self.assertTrue((ROOT / rel).is_file(), rel)

    def test_shim_imports_register(self):
        text = (ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("from bento_hermes import register", text)
        self.assertTrue(Path(ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License"))


if __name__ == "__main__":
    unittest.main()
