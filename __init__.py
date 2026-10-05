"""Hermes plugin entry point for bento.

Thin shim: Hermes' native (git-clone) plugin loader imports ``<plugin-dir>/__init__.py``
directly; the implementation lives in ``src/bento_hermes``. Put ``src`` on ``sys.path`` first
so the package's absolute imports resolve exactly as they do under a pip install (which
resolves the ``hermes_agent.plugins`` entry point in ``pyproject.toml`` and never touches
this file).
"""

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from bento_hermes import register  # noqa: E402

__all__ = ["register"]
