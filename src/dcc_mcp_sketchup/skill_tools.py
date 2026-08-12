"""Small reusable adapter between DCC-MCP skill scripts and the SketchUp bridge."""

from __future__ import annotations

from typing import Any, Callable

from dcc_mcp_core.skill import skill_entry, skill_success

from .bridge import get_bridge


def bridge_main(method: str, message: str) -> Callable[..., dict[str, Any]]:
    """Create a decorated tool entry point for one bounded host command."""

    @skill_entry
    def main(**kwargs: Any) -> dict[str, Any]:
        result = get_bridge().call(method, **kwargs)
        if isinstance(result, dict):
            return skill_success(message, **result)
        return skill_success(message, result=result)

    return main
