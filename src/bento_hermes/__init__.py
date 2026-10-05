"""bento_hermes - Hermes Agent plugin for Bento (bentonow.com). Stdlib only."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import tools_read, tools_send, tools_write
from .cli import make_command, register_cli
from .config import REQUIRED_ENV, Runtime
from .safety import ToolSpec, make_pre_tool_call, run_tool

logger = logging.getLogger(__name__)

__version__ = "0.1.0"
TOOLSET = "bento"
SPECS: list[ToolSpec] = [*tools_read.SPECS, *tools_write.SPECS, *tools_send.SPECS]
REGISTRY: dict[str, ToolSpec] = {spec.name: spec for spec in SPECS}
_SKILL_RELATIVE = ("skills", "bento-ops", "SKILL.md")


def _make_handler(rt: Runtime, spec: ToolSpec):
    def handler(args: dict, **kwargs: Any) -> str:
        return run_tool(rt, spec, args)

    return handler


def _skill_path() -> Path | None:
    for base in (Path(__file__).resolve().parents[2], Path(__file__).resolve().parent):
        candidate = base.joinpath(*_SKILL_RELATIVE)
        if candidate.is_file():
            return candidate
    return None


def register(ctx: Any) -> None:
    """Called once by the Hermes plugin loader. Performs no network I/O and no state writes."""
    rt = Runtime(ctx)
    for spec in SPECS:
        ctx.register_tool(
            name=spec.name,
            toolset=TOOLSET,
            schema=spec.schema,
            handler=_make_handler(rt, spec),
            check_fn=rt.check_fn,
            requires_env=list(REQUIRED_ENV),
            emoji=spec.emoji,
        )
    ctx.register_hook("pre_tool_call", make_pre_tool_call(rt, REGISTRY))
    ctx.register_cli_command(
        name="bento",
        help="Bento plugin: status / doctor",
        setup_fn=register_cli,
        handler_fn=make_command(rt),
        description="Check Bento credentials and connectivity (status) or lint plugin configuration offline (doctor).",
    )
    skill = _skill_path()
    if skill is not None:
        try:
            ctx.register_skill("bento-ops", skill, description="Operate Bento safely: dry-run, confirm, send")
        except (ValueError, FileNotFoundError):
            logger.warning("bento: could not register bundled skill", exc_info=True)


__all__ = ["REGISTRY", "SPECS", "register"]
