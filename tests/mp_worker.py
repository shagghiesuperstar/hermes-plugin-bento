"""Worker process for ``test_multiprocess.py``: one Hermes-like process sharing a file-backed plugin state.

``FileState`` mimics ``hermes_cli.plugins_state.PluginState``: each ``get`` / ``set`` is atomic (file lock
per call) but a plugin's read-modify-write across calls is NOT. Usage: ``python mp_worker.py '<json spec>'``.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ENV, FakeCtx  # noqa: E402,I001 - puts src/ on sys.path

from bento_hermes.claim import ClaimLock  # noqa: E402
from bento_hermes.config import Runtime  # noqa: E402
from bento_hermes.safety import run_tool  # noqa: E402
import bento_hermes  # noqa: E402


class FileState:
    def __init__(self, data_dir: Path, delay: float = 0.0) -> None:
        self.data_dir = Path(data_dir)
        self.delay = delay
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = ClaimLock(lambda: self.data_dir / "state.lock")  # per-call lock, like PluginState

    @property
    def path(self) -> Path:
        return self.data_dir / "state.json"

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def get(self, key, default=None):
        with self._lock:
            value = self._read().get(key, default)
        if self.delay:
            time.sleep(self.delay)  # widen the read -> write window, as a slow disk would
        return value

    def set(self, key, value) -> None:
        with self._lock:
            data = self._read()
            data[key] = value
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)


def make_runtime(data_dir: Path, base_url: str, delay: float = 0.0) -> Runtime:
    ctx = FakeCtx(data_dir, {"base_url": base_url, "allowed_senders": ["news@example.com"]})
    ctx.state = FileState(data_dir, delay)
    return Runtime(ctx)


def main() -> None:
    spec = json.loads(sys.argv[1])
    os.environ.update(ENV)
    rt = make_runtime(Path(spec["dir"]), spec["base_url"], spec.get("delay", 0.0))
    if spec.get("mode") == "nolock":  # the pre-fix behaviour: no cross-process (or in-process) claim
        rt.gate = contextlib.nullcontext()  # type: ignore[assignment]
    while time.time() < spec["start_at"]:  # release every worker together
        time.sleep(0.001)
    print(run_tool(rt, bento_hermes.REGISTRY[spec["tool"]], spec["args"]))


if __name__ == "__main__":
    main()
