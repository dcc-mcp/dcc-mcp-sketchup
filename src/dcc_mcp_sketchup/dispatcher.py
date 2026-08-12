"""Core execution adapter for bridge-backed SketchUp skill wrappers."""

from __future__ import annotations

from typing import Any, Callable


class SketchupBridgeDispatcher:
    """Run wrappers inline; the Ruby extension owns the actual UI-thread hop."""

    def dispatch_callable(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        for key in (
            "affinity",
            "context",
            "action_name",
            "skill_name",
            "execution",
            "timeout_hint_secs",
            "thread_affinity",
        ):
            kwargs.pop(key, None)
        return func(*args, **kwargs)
