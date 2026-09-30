"""Small reusable adapter between DCC-MCP skill scripts and the SketchUp bridge."""

from __future__ import annotations

from typing import Any, Callable

from dcc_mcp_core.skill import skill_entry, skill_success

from .bridge import get_bridge
from .compat import classify_host, load_matrix
from .write_contract import MUTATING, WriteVerificationError, classify, require_verification


def bridge_main(method: str, message: str) -> Callable[..., dict[str, Any]]:
    """Create a decorated tool entry point for one bounded host command.

    For a mutating method the returned entry point enforces the post-write
    read-back contract: the Ruby side performs the read-back and returns the
    evidence, and this wrapper proves the evidence actually arrived before the
    tool is allowed to report success. See ``write_contract.py`` for why the
    check has to happen on both sides of the boundary.
    """

    # Classified at import time, so a method added to the Ruby command map
    # without a read-back decision fails every skill that touches it rather
    # than silently skipping the contract.
    kind = classify(method)

    @skill_entry
    def main(**kwargs: Any) -> dict[str, Any]:
        bridge = get_bridge()
        try:
            result = bridge.call(method, **kwargs)
        except WriteVerificationError as exc:
            # A read-back disagreement is raised by bridge.call as soon as the
            # Ruby side reports it, so it never reaches require_verification.
            # It is the same failure class and owes the same host context.
            raise _with_host_context(bridge, exc) from None
        if kind != MUTATING:
            if isinstance(result, dict):
                return skill_success(message, **result)
            return skill_success(message, result=result)

        try:
            verification = require_verification(method, result)
        except WriteVerificationError as exc:
            raise _with_host_context(bridge, exc) from None
        payload = dict(result) if isinstance(result, dict) else {"result": result}
        payload["verification"] = verification
        return skill_success(message, **payload)

    return main


def _with_host_context(bridge: Any, error: WriteVerificationError) -> WriteVerificationError:
    """Attach the host version and matrix verdict to a read-back failure.

    Only runs on the failure path. Both failure sources funnel through here:
    a Ruby payload that reached Python without evidence, and a Ruby read-back
    that disagreed and was raised on the way out of ``bridge.call``.

    The Ruby side stamps its own host version into the payloads it builds, so
    the extra round trip is paid only when the payload arrived without one.
    That is also the case that needs it most: a payload with no host version is
    a failure rebuilt from the bare error envelope rather than produced by the
    running host.

    A failure to resolve the host degrades to the original error rather than
    masking it -- the read-back disagreement is the finding, and a missing
    version only makes it less reproducible.
    """
    payload = dict(error.payload)
    version = payload.get("host_version")
    if not version:
        try:
            runtime = bridge.status()
        except Exception:
            return error
        version = runtime.get("sketchup_version") if isinstance(runtime, dict) else None
        if not version:
            return error
        payload["host_version"] = version
    try:
        host_matrix = classify_host(version, load_matrix())
    except Exception:
        host_matrix = None
    payload["host_matrix"] = host_matrix
    return WriteVerificationError.from_payload(payload)
