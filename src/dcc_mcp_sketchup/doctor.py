"""Standalone SketchUp doctor and host compatibility report.

Host support is decided by the machine-readable matrix in ``compat_matrix.json``
(see ``compat.py``), never by a hardcoded minimum: an undeclared SketchUp
version is rejected with an explicit error code instead of being assumed
compatible.

What this report does and does not prove
----------------------------------------
SketchUp is a licensed desktop application that cannot be installed on a hosted
CI runner, and the bridge is a Ruby extension that only runs inside it. So the
doctor distinguishes two things that a naive report would conflate:

* **Installed** -- a SketchUp executable and a matching versioned user profile
  exist on this machine. Read from the filesystem; no process involved.
* **Live** -- a SketchUp process is running with the extension loaded and
  answered ``bridge.health``. This is the only state in which the host version
  and the Ruby API surface are observed rather than inferred.

``directly_usable`` requires the live state. An installed-but-not-running host
is reported as a failure with a concrete next step, because "SketchUp is on the
disk" is not evidence that the adapter works.

The host matrix verdict never claims more than it has. Every range in
``compat_matrix.json`` carries ``evidence_level: "contract"``, and the bound is
echoed into the report so a consumer that reads ``supported`` also reads why.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

from dcc_mcp_core import __version__ as running_core_version

from . import install as install_lifecycle
from .__version__ import __version__
from .bridge import STATUS_TIMEOUT_SECS, BridgeError, SketchupBridge
from .compat import (
    SUPPORTED,
    TOO_NEW,
    UNKNOWN,
    UNLISTED,
    classify_host,
    load_matrix,
    missing_required_api,
    normalize_version,
    product_year,
    required_api_surface,
    supported_range_labels,
    unsupported_reason,
)
from .write_contract import MUTATING_TOOLS, READ_ONLY_TOOLS

MIN_CORE_VERSION = "0.20.36"

# Machine-readable error codes for host support verdicts.
ERROR_HOST_TOO_OLD = "sketchup_host_version_unsupported"
ERROR_HOST_TOO_NEW = "sketchup_host_version_unverified"
ERROR_HOST_UNLISTED = "sketchup_host_version_unlisted"
ERROR_HOST_UNKNOWN = "sketchup_host_version_unparsable"
ERROR_HOST_NOT_FOUND = "sketchup_host_not_found"
ERROR_HOST_NOT_RUNNING = "sketchup_host_not_running"

_HOST_ERROR_CODES = {
    TOO_NEW: ERROR_HOST_TOO_NEW,
    UNLISTED: ERROR_HOST_UNLISTED,
    UNKNOWN: ERROR_HOST_UNKNOWN,
}

EXIT_OK = install_lifecycle.EXIT_OK
EXIT_PREFLIGHT = install_lifecycle.EXIT_PREFLIGHT
EXIT_VERIFY = install_lifecycle.EXIT_VERIFY
SCHEMA_VERSION = install_lifecycle.SCHEMA_VERSION


def _host_error_code(status: str) -> str:
    return _HOST_ERROR_CODES.get(status, ERROR_HOST_TOO_OLD)


def _version_tuple(value: str) -> Optional[tuple[int, int, int]]:
    return normalize_version(value)


def _command_step(
    identifier: str, description: str, command: list[str], why: str
) -> dict[str, Any]:
    return {"id": identifier, "description": description, "command": command, "why": why}


def _start_sketchup_step() -> dict[str, Any]:
    return _command_step(
        "start-sketchup",
        "Start the supported SketchUp version and let the DCC-MCP extension load",
        ["dcc-mcp-sketchup", "doctor", "--json"],
        (
            "The adapter is verified only against a live SketchUp process; an installed "
            "executable that is not running cannot answer bridge.health, so host support "
            "would be an assumption rather than an observation."
        ),
    )


def _install_sketchup_step() -> dict[str, Any]:
    ranges = ", ".join(supported_range_labels(load_matrix())) or "a supported version"
    return _command_step(
        "install-sketchup",
        "Install a SketchUp Desktop version in the supported range: %s" % ranges,
        [sys.executable, "-m", "webbrowser", "https://www.sketchup.com/products/sketchup-desktop"],
        "The SketchUp bridge is a Ruby extension that only loads inside the SketchUp desktop app",
    )


def _report(
    verb: str,
    checks: dict[str, Any],
    steps: list[dict[str, Any]],
    exit_code: int,
    failure_stage: Optional[str] = None,
    failure_reason: Optional[str] = None,
    next_steps: Optional[list[dict[str, Any]]] = None,
    error_code: Optional[str] = None,
) -> dict[str, Any]:
    directly_usable = exit_code == EXIT_OK
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok" if directly_usable else "failed",
        "error_code": error_code,
        "dcc_type": "sketchup",
        "verb": verb,
        "adapter_version": __version__,
        "core_version": running_core_version,
        "min_core_version": MIN_CORE_VERSION,
        "checks": checks,
        "steps": steps,
        "next_steps": list(next_steps or ()),
        "verify": {
            "directly_usable": directly_usable,
            "failure_stage": failure_stage,
            "failure_reason": failure_reason,
        },
        "_exit_code": exit_code,
    }


def _configuration_check() -> tuple[dict[str, Any], Optional[str]]:
    """Validate the bridge environment without requiring a live host.

    An unset token or port is normal for a bare ``doctor`` run -- the sidecar
    only has those once SketchUp has launched it -- so an unset value is
    reported as ``configured: false`` rather than treated as a misconfiguration.
    """
    port_text = os.environ.get("DCC_MCP_SKETCHUP_BRIDGE_PORT", "").strip()
    token = os.environ.get("DCC_MCP_SKETCHUP_BRIDGE_TOKEN", "").strip()
    host = os.environ.get("DCC_MCP_SKETCHUP_BRIDGE_HOST", "127.0.0.1").strip()
    port: Any = None
    if port_text:
        try:
            port = int(port_text)
        except ValueError:
            port = port_text
    port_valid = port is None or (isinstance(port, int) and 1 <= port <= 65535)
    host_valid = host.casefold() in {"127.0.0.1"}
    success = port_valid and host_valid
    reason = None
    if not port_valid:
        reason = "DCC_MCP_SKETCHUP_BRIDGE_PORT must be an integer from 1 through 65535"
    elif not host_valid:
        reason = "DCC_MCP_SKETCHUP_BRIDGE_HOST must be 127.0.0.1"
    check = {
        "success": success,
        "host": host,
        "port": port,
        "token_present": bool(token),
        "configured": bool(port_text) and bool(token),
        "status_timeout_secs": STATUS_TIMEOUT_SECS,
    }
    return check, reason


def doctor_report(verb: str = "doctor", timeout: float = 30.0) -> dict[str, Any]:
    """Build the machine-readable doctor/verify report."""
    checks: dict[str, Any] = {}
    steps: list[dict[str, Any]] = []

    configuration, reason = _configuration_check()
    checks["configuration"] = configuration
    if not configuration["success"]:
        steps.append({"id": "validate-configuration", "status": "failed", "message": reason})
        return _report(
            verb,
            checks,
            steps,
            EXIT_PREFLIGHT,
            "configuration",
            reason,
            [
                _command_step(
                    "review-configuration",
                    "Correct the SketchUp bridge environment configuration",
                    ["dcc-mcp-sketchup", verb, "--json"],
                    reason or "invalid bridge configuration",
                )
            ],
        )
    steps.append({"id": "validate-configuration", "status": "ok"})

    core_release = _version_tuple(running_core_version)
    minimum_core = _version_tuple(MIN_CORE_VERSION)
    core_compatible = (
        core_release is not None and minimum_core is not None and core_release >= minimum_core
    )
    checks["core"] = {
        "success": core_compatible,
        "version": running_core_version,
        "minimum": MIN_CORE_VERSION,
    }
    if not core_compatible:
        reason = "dcc-mcp-core %s is unsupported; %s or newer is required" % (
            running_core_version,
            MIN_CORE_VERSION,
        )
        steps.append({"id": "validate-core", "status": "failed", "message": reason})
        return _report(
            verb,
            checks,
            steps,
            EXIT_PREFLIGHT,
            "core",
            reason,
            [
                _command_step(
                    "upgrade-core",
                    "Upgrade dcc-mcp-core",
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--upgrade",
                        "dcc-mcp-core>=%s" % MIN_CORE_VERSION,
                    ],
                    reason,
                )
            ],
        )
    steps.append({"id": "validate-core", "status": "ok"})

    try:
        installs = install_lifecycle.discover_host_installations()
    except Exception as exc:  # discovery touches the filesystem; never fatal
        installs = []
        checks["installed_host"] = {
            "success": False,
            "reason": "SketchUp host discovery failed: %s" % exc,
        }
    else:
        checks["installed_host"] = {
            "success": bool(installs),
            "installations": installs,
            "newest_year": max((item.get("profile_year") or 0 for item in installs), default=None),
        }
    steps.append(
        {
            "id": "discover-installed-host",
            "status": "ok" if checks["installed_host"]["success"] else "failed",
        }
    )

    # The live host is the only source of an observed version, so it is probed
    # even when nothing is installed: a running SketchUp is the ground truth and
    # a stale or moved profile directory must not outrank it.
    runtime: dict[str, Any] = {}
    runtime_error: Optional[str] = None
    try:
        bridge = SketchupBridge.from_env()
        runtime = bridge.status()
    except BridgeError as exc:
        runtime_error = str(exc)
    except (OSError, ValueError) as exc:
        runtime_error = str(exc)
    live = bool(runtime.get("ready"))
    checks["live_host"] = {
        "success": live,
        "sketchup_version": runtime.get("sketchup_version"),
        "ruby_version": runtime.get("ruby_version"),
        "host_pid": runtime.get("host_pid"),
        "adapter_version": runtime.get("adapter_version"),
        "bridge": runtime.get("bridge"),
        "error": runtime_error,
    }

    version = str(runtime.get("sketchup_version") or "")
    if not version:
        # Fall back to the newest installed host. This is an inference, so the
        # report says so: the version is labelled as unresolved and the run is
        # not directly usable regardless of what the matrix says about it.
        for item in installs:
            candidate = item.get("native_version") or (
                str(item["profile_year"]) if item.get("profile_year") else ""
            )
            if candidate:
                version = str(candidate)
                break

    if not version:
        reason = (
            "No SketchUp host was found: no running SketchUp answered the bridge and no "
            "installed SketchUp profile was discovered"
        )
        steps.append({"id": "resolve-host-version", "status": "failed", "message": reason})
        return _report(
            verb,
            checks,
            steps,
            EXIT_PREFLIGHT,
            "host",
            reason,
            [_install_sketchup_step(), _start_sketchup_step()],
            ERROR_HOST_NOT_FOUND,
        )

    verdict = classify_host(version)
    checks["host_matrix"] = {
        "success": verdict["status"] == SUPPORTED,
        "source": "live_host" if runtime.get("sketchup_version") else "installed_host",
        "version": version,
        "product_year": product_year(version),
        "matrix": verdict,
    }

    if verdict["status"] != SUPPORTED:
        reason = unsupported_reason(verdict)
        steps.append({"id": "verify-host-matrix", "status": "failed", "message": reason})
        return _report(
            verb,
            checks,
            steps,
            EXIT_PREFLIGHT,
            "host_version",
            reason,
            [
                _install_sketchup_step(),
                _command_step(
                    "recheck-host-matrix",
                    "Re-run the compatibility preflight against a supported SketchUp",
                    ["dcc-mcp-sketchup", verb, "--json"],
                    reason,
                ),
            ],
            _host_error_code(verdict["status"]),
        )
    steps.append(
        {
            "id": "verify-host-matrix",
            "status": "ok",
            "message": "SketchUp %s is inside the verified compatibility matrix (%s)"
            % (version, ", ".join(verdict["supported_ranges"])),
        }
    )

    if not live:
        reason = (
            "SketchUp %s is installed and supported, but no running SketchUp answered the "
            "bridge, so the host version and Ruby API surface could not be observed" % version
        )
        steps.append({"id": "verify-live-host", "status": "failed", "message": reason})
        return _report(
            verb,
            checks,
            steps,
            EXIT_VERIFY,
            "live_host",
            reason,
            [
                _start_sketchup_step(),
                _command_step(
                    "reinstall-extension",
                    "Reinstall the SketchUp Ruby extension if SketchUp is already running",
                    ["dcc-mcp-sketchup", "install", "--json", "--yes"],
                    reason,
                ),
            ],
            ERROR_HOST_NOT_RUNNING,
        )
    steps.append({"id": "verify-live-host", "status": "ok"})

    probe = _probe_api_surface(bridge)
    missing = missing_required_api(probe, load_matrix())
    checks["api_surface"] = {
        "success": not missing,
        "required_count": len(required_api_surface(load_matrix())),
        "probe": probe,
        "missing": missing,
    }
    if missing:
        reason = "SketchUp %s does not expose %d required Ruby API symbol(s): %s" % (
            version,
            len(missing),
            ", ".join("%s#%s" % (item.get("owner"), item.get("symbol")) for item in missing),
        )
        steps.append({"id": "verify-api-surface", "status": "failed", "message": reason})
        return _report(
            verb,
            checks,
            steps,
            EXIT_VERIFY,
            "api_surface",
            reason,
            [
                _command_step(
                    "review-host-api",
                    "Reinstall the extension for this SketchUp version, then re-run the doctor",
                    ["dcc-mcp-sketchup", "upgrade", "--json", "--yes"],
                    reason,
                )
            ],
        )
    steps.append(
        {
            "id": "verify-api-surface",
            "status": "ok",
            "message": "All %d required Ruby API symbols are present on SketchUp %s"
            % (checks["api_surface"]["required_count"], version),
        }
    )

    checks["contract"] = {
        "mutating_tools": list(MUTATING_TOOLS),
        "read_only_tools": list(READ_ONLY_TOOLS),
        "evidence_level": verdict["evidence_level"],
        "evidence_bound": verdict["evidence_bound"],
    }

    return _report(verb, checks, steps, EXIT_OK)


def _probe_api_surface(bridge: SketchupBridge) -> list[dict[str, Any]]:
    """Ask the live host which required Ruby API symbols it exposes.

    The symbol list comes from ``compat_matrix.json`` and is sent to the host,
    so the Ruby side never owns a second copy that could drift from the matrix.
    A failed probe returns an empty list, which ``missing_required_api`` reads
    as "unverified" rather than "present".
    """
    symbols = [
        "%s#%s" % (entry.get("owner"), entry.get("symbol"))
        for entry in required_api_surface(load_matrix())
    ]
    if not symbols:
        return []
    try:
        result = bridge.call("diagnostics.api_probe", symbols=symbols, timeout=STATUS_TIMEOUT_SECS)
    except (BridgeError, OSError, ValueError):
        return []
    probe = result.get("probe") if isinstance(result, dict) else None
    return [item for item in probe if isinstance(item, dict)] if isinstance(probe, list) else []
