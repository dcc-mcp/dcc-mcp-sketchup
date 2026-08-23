"""Install the SketchUp Ruby extension into the per-user Plugins directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import site
import subprocess
import sys
import sysconfig
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from dcc_mcp_core.install_lifecycle import (
    inspect_install_root,
    safe_remove_tree,
    wait_for_sidecar_ready,
)

from .__version__ import __version__

EXTENSION_DIRECTORY = "dcc_mcp_sketchup"
REGISTRATION_FILENAME = "dcc_mcp_sketchup.rb"
MIN_SKETCHUP_VERSION = 2021
MIN_CORE_VERSION = "0.19.91"
SCHEMA_VERSION = "1"
RECEIPT_RELATIVE_PATH = Path(".dcc-mcp") / "receipts" / "sketchup.json"
BOOTSTRAP_ERRORS_RELATIVE_PATH = Path(".dcc-mcp") / "logs" / "sketchup-bootstrap-errors.jsonl"
EXIT_OK, EXIT_PREFLIGHT, EXIT_ACQUIRE = 0, 10, 20
EXIT_INSTALL, EXIT_VERIFY, EXIT_REQUIRES_RESTART = 30, 40, 50
_VERSION_RE = re.compile(r"SketchUp\s+(\d{4})$")
LIFECYCLE_VERBS = ("install", "status", "verify", "uninstall", "upgrade")


class InstallFailure(ValueError):
    """Structured lifecycle failure with a stable process exit code."""

    def __init__(self, exit_code: int, stage: str, reason: str):
        super().__init__(reason)
        self.exit_code = exit_code
        self.stage = stage
        self.reason = reason


def discover_plugin_dirs() -> list[Path]:
    """Return existing per-user SketchUp Plugins directories, newest first."""
    candidates: list[Path] = []
    if sys.platform == "win32":
        app_data = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        product_root = app_data / "SketchUp"
        if product_root.is_dir():
            candidates.extend(path / "Plugins" for path in product_root.glob("SketchUp */SketchUp"))
    elif sys.platform == "darwin":
        app_support = Path.home() / "Library" / "Application Support"
        candidates.extend(path / "Plugins" for path in app_support.glob("SketchUp */SketchUp"))
    return sorted(
        (path.resolve() for path in candidates if path.parent.is_dir()),
        key=_plugin_dir_version,
        reverse=True,
    )


def default_plugin_dir() -> Path:
    """Return the newest installed per-user Plugins directory.

    SketchUp uses versioned plugin roots, so installation without an explicit
    path is rejected when no local SketchUp profile exists.
    """
    discovered = discover_plugin_dirs()
    if discovered:
        return discovered[0]
    raise RuntimeError(
        "No SketchUp user Plugins directory was found; start SketchUp once or pass "
        "--plugins-dir explicitly"
    )


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+(?:\.\d+)*)", value)
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def _resolve_python(value: Optional[Path]) -> Path:
    configured = value
    if configured is None and os.environ.get("DCC_MCP_INSTALL_PYTHON"):
        configured = Path(os.environ["DCC_MCP_INSTALL_PYTHON"])
    python = (configured or Path(sys.executable)).expanduser().resolve()
    if not python.is_file():
        raise InstallFailure(EXIT_PREFLIGHT, "python", f"Python interpreter not found: {python}")
    return python


def _target_environment(python: Path) -> dict[str, str]:
    code = (
        "import importlib.metadata as m, json, sys, sysconfig; "
        "print(json.dumps({'python': sys.executable, "
        "'scripts': sysconfig.get_path('scripts'), "
        "'core_version': m.version('dcc-mcp-core'), "
        "'adapter_version': m.version('dcc-mcp-sketchup')}))"
    )
    try:
        completed = subprocess.run(
            [str(python), "-c", code],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "python",
            f"cannot inspect target interpreter: {exc}",
        ) from exc
    if completed.returncode:
        error_lines = completed.stderr.strip().splitlines()
        reason = error_lines[-1] if error_lines else "package metadata query failed"
        raise InstallFailure(EXIT_PREFLIGHT, "python", reason)
    try:
        environment = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as exc:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "python",
            "target interpreter returned invalid package metadata",
        ) from exc
    if _version_tuple(str(environment["core_version"])) < _version_tuple(MIN_CORE_VERSION):
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "core",
            f"dcc-mcp-core {environment['core_version']} is unsupported; "
            f"version {MIN_CORE_VERSION} or newer is required",
        )
    return {key: str(value) for key, value in environment.items()}


def _target_server_executable(environment: dict[str, str]) -> Path:
    executable_name = "dcc-mcp-sketchup.exe" if sys.platform == "win32" else "dcc-mcp-sketchup"
    executable = Path(environment["scripts"]) / executable_name
    if not executable.is_file():
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "sidecar",
            f"dcc-mcp-sketchup executable was not found for {environment['python']}",
        )
    return executable.resolve()


def _files_manifest(root: Path) -> list[dict[str, Any]]:
    result = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        data = path.read_bytes()
        result.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    return result


def _manifest_digest(files: list[dict[str, Any]]) -> str:
    payload = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _source_digest() -> str:
    source = Path(__file__).resolve().parent / "sketchup_plugin"
    if not (source / "registration.rb").is_file():
        raise InstallFailure(EXIT_ACQUIRE, "package", f"bundled extension is missing: {source}")
    return _manifest_digest(_files_manifest(source))


def _read_receipt(plugins_dir: Path) -> Optional[dict[str, Any]]:
    path = plugins_dir / RECEIPT_RELATIVE_PATH
    if not path.is_file():
        return None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallFailure(
            EXIT_PREFLIGHT, "receipt", f"install receipt is unreadable: {path}"
        ) from exc
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", f"unsupported install receipt: {path}")
    return receipt


def _installation_state(
    plugins_dir: Path,
    source_digest: str,
    expected_server: Optional[Path] = None,
    expected_python: Optional[Path] = None,
) -> str:
    target = plugins_dir / EXTENSION_DIRECTORY
    registration = plugins_dir / REGISTRATION_FILENAME
    receipt = _read_receipt(plugins_dir)
    present = (target.exists(), registration.exists(), receipt is not None)
    if not any(present):
        return "fresh"
    if not all(present):
        return "partial"
    if (
        Path(str(receipt.get("extension_path", ""))).resolve() != target.resolve()
        or Path(str(receipt.get("registration_path", ""))).resolve() != registration.resolve()
    ):
        return "partial"
    if (
        receipt.get("source_digest") != source_digest
        or receipt.get("adapter_version") != __version__
        or (
            expected_server is not None
            and Path(str(receipt.get("server_path", ""))).resolve() != expected_server.resolve()
        )
        or (
            expected_python is not None
            and Path(str(receipt.get("python", ""))).resolve() != expected_python.resolve()
        )
    ):
        return "upgrade"
    server_path_file = target / "server_path.txt"
    try:
        configured_server = server_path_file.read_text(encoding="utf-8").strip()
        files = _files_manifest(target)
        registration_manifest = _registration_manifest(registration)
    except OSError:
        return "repair"
    if (
        not configured_server
        or not Path(configured_server).is_file()
        or Path(configured_server).resolve() != Path(str(receipt.get("server_path", ""))).resolve()
        or _manifest_digest(files) != receipt.get("extension_digest")
        or registration_manifest["sha256"] != receipt.get("registration", {}).get("sha256")
    ):
        return "repair"
    return "current"


def _resolve_plan_plugins_dir(value: Optional[Path]) -> Path:
    try:
        plugins_dir = value.expanduser().resolve() if value is not None else default_plugin_dir()
    except RuntimeError as exc:
        raise InstallFailure(EXIT_PREFLIGHT, "host", str(exc)) from exc
    if not plugins_dir.parent.is_dir():
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "host",
            f"SketchUp profile is unavailable; start SketchUp once: {plugins_dir.parent}",
        )
    version = _plugin_dir_version(plugins_dir)
    if version < MIN_SKETCHUP_VERSION:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "host_version",
            f"SketchUp {version or 'unknown'} is unsupported; "
            f"version {MIN_SKETCHUP_VERSION} or newer is required",
        )
    return plugins_dir


def _resolve_explicit_host_path(value: Path) -> Path:
    host = value.expanduser().resolve()
    if host.is_dir():
        choices = (
            host / "SketchUp.exe",
            host / "Contents" / "MacOS" / "SketchUp",
            host / "SketchUp.app" / "Contents" / "MacOS" / "SketchUp",
        )
        host = next((path for path in choices if path.is_file()), host)
    if not host.is_file():
        raise InstallFailure(EXIT_PREFLIGHT, "host", f"SketchUp host path not found: {host}")
    version = _plugin_dir_version(host)
    if version < MIN_SKETCHUP_VERSION:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "host_version",
            f"SketchUp {version or 'unknown'} is unsupported; "
            f"version {MIN_SKETCHUP_VERSION} or newer is required",
        )
    return host


def _discover_host_for_version(version: int) -> Optional[Path]:
    candidates = []
    if sys.platform == "win32":
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(variable):
                candidates.append(
                    Path(os.environ[variable]) / "SketchUp" / f"SketchUp {version}" / "SketchUp.exe"
                )
    elif sys.platform == "darwin":
        candidates.append(
            Path("/Applications")
            / f"SketchUp {version}"
            / "SketchUp.app"
            / "Contents"
            / "MacOS"
            / "SketchUp"
        )
    return next((path.resolve() for path in candidates if path.is_file()), None)


def _resolve_host_and_plugins(
    plugins_value: Optional[Path],
    dcc_path: Optional[Path],
) -> tuple[Path, Optional[Path]]:
    if plugins_value is not None:
        plugins_dir = _resolve_plan_plugins_dir(plugins_value)
        host = _resolve_explicit_host_path(dcc_path) if dcc_path is not None else None
        if host is not None and _plugin_dir_version(host) != _plugin_dir_version(plugins_dir):
            raise InstallFailure(
                EXIT_PREFLIGHT,
                "host_version",
                "--dcc-path and --plugins-dir select different SketchUp versions",
            )
        return plugins_dir, host

    if dcc_path is not None:
        host = _resolve_explicit_host_path(dcc_path)
        version = _plugin_dir_version(host)
        plugins_dir = next(
            (path for path in discover_plugin_dirs() if _plugin_dir_version(path) == version),
            None,
        )
        if plugins_dir is None:
            raise InstallFailure(
                EXIT_PREFLIGHT,
                "host_profile",
                f"SketchUp {version} profile was not found; start that SketchUp version once",
            )
        return _resolve_plan_plugins_dir(plugins_dir), host

    plugins_dir = _resolve_plan_plugins_dir(None)
    version = _plugin_dir_version(plugins_dir)
    host = _discover_host_for_version(version)
    if host is None:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "host",
            f"SketchUp {version} installation was not found; pass --dcc-path",
        )
    return plugins_dir, host


def plan(
    verb: str,
    plugins_value: Optional[Path],
    python_value: Optional[Path],
    dcc_path: Optional[Path],
) -> dict[str, Any]:
    plugins_dir, host_path = _resolve_host_and_plugins(plugins_value, dcc_path)
    writable_root = plugins_dir if plugins_dir.exists() else plugins_dir.parent
    if verb in {"install", "upgrade", "uninstall"} and not os.access(writable_root, os.W_OK):
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "permissions",
            f"SketchUp profile is not writable: {writable_root}",
        )
    python = _resolve_python(python_value)
    environment = _target_environment(python)
    if environment["adapter_version"] != __version__:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "python",
            f"target interpreter has dcc-mcp-sketchup {environment['adapter_version']}; "
            f"this installer is {__version__}",
        )
    server = _target_server_executable(environment)
    source_digest = _source_digest()
    state = _installation_state(plugins_dir, source_digest, server, python)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "planned",
        "dcc_type": "sketchup",
        "verb": verb,
        "adapter_version": __version__,
        "core_version": environment["core_version"],
        "target_adapter_version": environment["adapter_version"],
        "sketchup_version": str(_plugin_dir_version(plugins_dir)),
        "dcc_path": str(host_path) if host_path else None,
        "plugins_dir": str(plugins_dir),
        "python": str(python),
        "server_path": str(server),
        "installation_state": state,
        "steps": [
            {
                "id": "preflight",
                "status": "ok",
                "sketchup_version": str(_plugin_dir_version(plugins_dir)),
            },
            {"id": "resolve-python", "status": "ok", "path": str(python)},
            {"id": "resolve-sidecar", "status": "ok", "path": str(server)},
            {"id": verb, "status": "planned", "installation_state": state},
        ],
        "next_steps": [],
        "receipt_path": str(plugins_dir / RECEIPT_RELATIVE_PATH),
        "verify": None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the DCC-MCP SketchUp installation.")
    subparsers = parser.add_subparsers(dest="verb", required=True)
    for verb in LIFECYCLE_VERBS:
        command = subparsers.add_parser(verb)
        command.add_argument("--json", action="store_true", dest="as_json")
        command.add_argument("--yes", action="store_true")
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--dcc-path", type=Path)
        command.add_argument("--python", type=Path)
        command.add_argument("--plugins-dir", type=Path, help=argparse.SUPPRESS)
        command.add_argument("--ready-timeout", type=float, default=0.0, help=argparse.SUPPRESS)
    return parser


def _failure_result(verb: str, failure: InstallFailure) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "requires_restart" if failure.exit_code == EXIT_REQUIRES_RESTART else "failed",
        "dcc_type": "sketchup",
        "verb": verb,
        "adapter_version": __version__,
        "core_version": None,
        "steps": [{"id": failure.stage, "status": "failed", "reason": failure.reason}],
        "next_steps": [],
        "receipt_path": None,
        "verify": {
            "directly_usable": False,
            "failure_stage": failure.stage,
            "failure_reason": failure.reason,
        },
    }


def _is_restart_lock_error(error: OSError) -> bool:
    winerror = getattr(error, "winerror", None)
    return winerror in {5, 32, 33} or isinstance(error, PermissionError)


def run(argv: Sequence[str]) -> tuple[dict[str, Any], int, bool]:
    args = _parser().parse_args(list(argv))
    mutating = args.verb in {"install", "upgrade", "uninstall"}
    try:
        report = plan(args.verb, args.plugins_dir, args.python, args.dcc_path)
        if args.dry_run or (mutating and not args.yes):
            return report, EXIT_OK, args.as_json
        if args.verb in {"install", "upgrade"}:
            if args.verb == "upgrade" and report["installation_state"] == "fresh":
                raise InstallFailure(EXIT_PREFLIGHT, "upgrade", "nothing is installed; use install")
            report, code = _execute_install(report, max(0.0, args.ready_timeout))
        elif args.verb == "uninstall":
            report, code = _execute_uninstall(report)
        elif args.verb == "status":
            state = report["installation_state"]
            report["status"] = "ok" if state in {"fresh", "current"} else state
            report["steps"][-1] = {
                "id": "status",
                "status": report["status"],
                "installation_state": state,
            }
            code = EXIT_OK if state in {"fresh", "current"} else EXIT_VERIFY
            if code:
                report["next_steps"] = _next_steps(report)
        else:
            report["verify"] = verify_install(
                Path(report["plugins_dir"]),
                Path(report["python"]),
                max(0.0, args.ready_timeout),
            )
            report["status"] = "ok" if report["verify"]["directly_usable"] else "failed"
            code = EXIT_OK if report["status"] == "ok" else EXIT_VERIFY
            if code:
                report["next_steps"] = _next_steps(report)
        return report, code, args.as_json
    except InstallFailure as exc:
        return _failure_result(args.verb, exc), exc.exit_code, args.as_json
    except OSError as exc:
        if _is_restart_lock_error(exc):
            code = EXIT_REQUIRES_RESTART
            reason = f"SketchUp has locked an installed file; close SketchUp and retry: {exc}"
        else:
            code = EXIT_INSTALL if mutating else EXIT_VERIFY
            reason = f"{exc.__class__.__name__}: {exc}"
        failure = InstallFailure(code, args.verb, reason)
        return _failure_result(args.verb, failure), code, args.as_json


def print_report(report: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return
    print(f"DCC-MCP SketchUp {report.get('verb')}: {report['status']}")
    if report.get("plugins_dir"):
        print(f"Plugins: {report['plugins_dir']}")
    if report.get("installation_state"):
        print(f"Installation: {report['installation_state']}")
    verification = report.get("verify") or {}
    if verification.get("failure_reason"):
        print(f"Verification: {verification['failure_reason']}")
    for step in report.get("next_steps", []):
        print(f"Next: {step['description']}")


def install_extension(
    plugins_dir: Path,
    *,
    overwrite: bool = False,
    server_executable: Optional[Path] = None,
) -> Path:
    """Copy the extension and bind it to this environment's console script."""
    root = plugins_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / EXTENSION_DIRECTORY
    registration = root / REGISTRATION_FILENAME
    existing = [path for path in (target, registration) if path.exists()]
    if existing and not overwrite:
        raise FileExistsError(f"SketchUp extension already exists: {existing[0]}")

    executable = server_executable or _find_server_executable()
    if executable is None:
        raise RuntimeError("dcc-mcp-sketchup executable was not found in this Python environment")

    staging_root, staged_target, staged_registration = _stage_extension(root, executable)
    try:
        _commit_staged_extension(
            staged_target,
            staged_registration,
            target,
            registration,
        )
    finally:
        safe_remove_tree(staging_root)
    return target


def _stage_extension(root: Path, executable: Path) -> tuple[Path, Path, Path]:
    source = Path(__file__).resolve().parent / "sketchup_plugin"
    staging_root = Path(tempfile.mkdtemp(prefix=".dcc-mcp-sketchup-stage-", dir=root))
    staged_target = staging_root / EXTENSION_DIRECTORY
    staged_registration = staging_root / REGISTRATION_FILENAME
    try:
        shutil.copytree(
            source,
            staged_target,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        shutil.copy2(staged_target / "registration.rb", staged_registration)
        (staged_target / "registration.rb").unlink()
        (staged_target / "server_path.txt").write_text(str(executable), encoding="utf-8")
    except BaseException:
        safe_remove_tree(staging_root)
        raise
    return staging_root, staged_target, staged_registration


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _registration_manifest(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def _receipt_payload(report: dict[str, Any]) -> dict[str, Any]:
    plugins_dir = Path(report["plugins_dir"])
    target = plugins_dir / EXTENSION_DIRECTORY
    registration = plugins_dir / REGISTRATION_FILENAME
    files = _files_manifest(target)
    return {
        "schema_version": SCHEMA_VERSION,
        "dcc_type": "sketchup",
        "adapter_version": __version__,
        "core_version": report["core_version"],
        "sketchup_version": report["sketchup_version"],
        "dcc_path": report["dcc_path"],
        "plugins_dir": str(plugins_dir),
        "python": report["python"],
        "server_path": report["server_path"],
        "extension_path": str(target),
        "registration_path": str(registration),
        "source_digest": _source_digest(),
        "extension_digest": _manifest_digest(files),
        "files": files,
        "registration": _registration_manifest(registration),
        "host_paths_touched": [str(target), str(registration)],
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }


def _selector_arguments(report: dict[str, Any]) -> list[str]:
    if report.get("dcc_path"):
        return ["--dcc-path", report["dcc_path"]]
    return ["--plugins-dir", report["plugins_dir"]]


def _next_steps(report: dict[str, Any]) -> list[dict[str, Any]]:
    selector = _selector_arguments(report)
    python_args = ["--python", report["python"]]
    verification = report.get("verify") or {}
    steps = []
    if verification.get("failure_stage") in {
        "artifact",
        "server_path",
        "interpreter",
        "import",
        "bootstrap",
    }:
        steps.append(
            {
                "id": "repair-extension",
                "description": "Transactionally repair the extension and its sidecar binding.",
                "command": [
                    "dcc-mcp-sketchup",
                    "upgrade",
                    *selector,
                    *python_args,
                    "--json",
                    "--yes",
                ],
                "why": "Installed files or interpreter binding did not pass verification.",
            }
        )
    if report.get("dcc_path"):
        steps.append(
            {
                "id": "open-sketchup",
                "description": "Open the selected SketchUp installation.",
                "command": [report["dcc_path"]],
                "why": "The Ruby extension and host-bound sidecar start inside SketchUp.",
            }
        )
    steps.append(
        {
            "id": "verify-ready",
            "description": "Verify receipt integrity, interpreter import, and live host readiness.",
            "command": [
                "dcc-mcp-sketchup",
                "verify",
                *selector,
                *python_args,
                "--json",
            ],
            "why": "Copied files are not usable until the live SketchUp probe succeeds.",
        }
    )
    return steps


def _install_from_report(report: dict[str, Any]) -> None:
    plugins_dir = Path(report["plugins_dir"])
    plugins_dir.mkdir(parents=True, exist_ok=True)
    target = plugins_dir / EXTENSION_DIRECTORY
    registration = plugins_dir / REGISTRATION_FILENAME
    receipt_path = plugins_dir / RECEIPT_RELATIVE_PATH
    lock_state = inspect_install_root(target)
    if lock_state.get("requires_restart"):
        raise InstallFailure(
            EXIT_REQUIRES_RESTART,
            "install",
            lock_state.get(
                "recommended_next_action",
                "Close SketchUp and retry the same command.",
            ),
        )
    staging_root, staged_target, staged_registration = _stage_extension(
        plugins_dir,
        Path(report["server_path"]),
    )
    try:
        _commit_staged_extension(
            staged_target,
            staged_registration,
            target,
            registration,
            receipt_path=receipt_path,
            receipt_factory=lambda: _receipt_payload(report),
        )
    finally:
        safe_remove_tree(staging_root)


def verify_install(
    plugins_dir: Path,
    python: Path,
    timeout: float,
) -> dict[str, Any]:
    """Verify receipt integrity, interpreter import, bootstrap, and live host readiness."""
    target = plugins_dir / EXTENSION_DIRECTORY
    registration = plugins_dir / REGISTRATION_FILENAME
    receipt = _read_receipt(plugins_dir)
    result: dict[str, Any] = {
        "directly_usable": False,
        "failure_stage": None,
        "failure_reason": None,
        "artifact": {"success": False},
        "server_path": {"success": False},
        "import": {"success": False},
        "bootstrap": {"success": False},
        "readiness": {"success": False},
    }
    if receipt is None or not target.is_dir() or not registration.is_file():
        result.update(
            failure_stage="artifact",
            failure_reason="extension, registration, or install receipt is missing",
        )
        return result
    if (
        Path(str(receipt.get("extension_path", ""))).resolve() != target.resolve()
        or Path(str(receipt.get("registration_path", ""))).resolve() != registration.resolve()
        or Path(str(receipt.get("plugins_dir", ""))).resolve() != plugins_dir.resolve()
    ):
        result.update(
            failure_stage="artifact",
            failure_reason="receipt paths do not match this SketchUp profile",
        )
        return result
    receipt_python = Path(str(receipt.get("python", ""))).resolve()
    if receipt_python != python.resolve():
        result.update(
            failure_stage="interpreter",
            failure_reason=(
                f"selected interpreter {python.resolve()} differs from receipt {receipt_python}; "
                "run upgrade --yes with the intended --python interpreter"
            ),
        )
        return result

    server_path_file = target / "server_path.txt"
    if not server_path_file.is_file():
        result.update(
            failure_stage="server_path",
            failure_reason="server_path.txt is missing; run upgrade --yes to repair the extension",
        )
        return result
    configured_server = server_path_file.read_text(encoding="utf-8").strip()
    if not configured_server or not Path(configured_server).is_file():
        result["server_path"] = {"success": False, "path": configured_server or None}
        result.update(
            failure_stage="server_path",
            failure_reason=(
                f"server_path.txt points to a missing sidecar: {configured_server or '<empty>'}; "
                "run upgrade --yes with the intended --python interpreter"
            ),
        )
        return result
    expected_server = str(receipt.get("server_path", ""))
    if Path(configured_server).resolve() != Path(expected_server).resolve():
        result["server_path"] = {
            "success": False,
            "path": configured_server,
            "expected_path": expected_server,
        }
        result.update(
            failure_stage="server_path",
            failure_reason=(
                "server_path.txt differs from the install receipt; "
                "run upgrade --yes with the intended --python interpreter"
            ),
        )
        return result
    result["server_path"] = {"success": True, "path": configured_server}

    files = _files_manifest(target)
    actual_extension = _manifest_digest(files)
    expected_extension = receipt.get("extension_digest")
    actual_registration = _registration_manifest(registration)
    expected_registration = receipt.get("registration", {}).get("sha256")
    artifact_ok = (
        actual_extension == expected_extension
        and actual_registration["sha256"] == expected_registration
    )
    result["artifact"] = {
        "success": artifact_ok,
        "extension_expected_sha256": expected_extension,
        "extension_actual_sha256": actual_extension,
        "registration_expected_sha256": expected_registration,
        "registration_actual_sha256": actual_registration["sha256"],
    }
    if not artifact_ok:
        result.update(
            failure_stage="artifact",
            failure_reason="installed extension files differ from the receipt; run upgrade --yes",
        )
        return result

    result["import"] = _python_import_check(python)
    if not result["import"].get("success"):
        result.update(
            failure_stage="import",
            failure_reason=result["import"].get("reason", "adapter import failed"),
        )
        return result

    bootstrap_path = plugins_dir / BOOTSTRAP_ERRORS_RELATIVE_PATH
    bootstrap_error = _last_bootstrap_error(bootstrap_path)
    if bootstrap_error is not None:
        result["bootstrap"] = {"success": False, "path": str(bootstrap_path), **bootstrap_error}
        result.update(
            failure_stage="bootstrap",
            failure_reason=(
                f"SketchUp bootstrap failed with {bootstrap_error.get('error_class', 'error')}: "
                f"{bootstrap_error.get('message', 'see bootstrap log')}"
            ),
        )
        return result
    result["bootstrap"] = {"success": True, "path": str(bootstrap_path)}

    readiness = wait_for_sidecar_ready(
        dcc_type="sketchup",
        timeout_secs=timeout,
        probe_tool="sketchup_session__get_status",
    )
    result["readiness"] = readiness
    if not readiness.get("success"):
        result.update(
            failure_stage="readiness",
            failure_reason=readiness.get("message", "SketchUp sidecar is not ready"),
        )
        return result
    result["directly_usable"] = True
    return result


def _python_import_check(python: Path) -> dict[str, Any]:
    code = (
        "import json, dcc_mcp_sketchup; "
        "print(json.dumps({'importable': True, 'version': dcc_mcp_sketchup.__version__}))"
    )
    try:
        completed = subprocess.run(
            [str(python), "-c", code],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"success": False, "reason": str(exc)}
    if completed.returncode:
        error_lines = completed.stderr.strip().splitlines()
        return {
            "success": False,
            "reason": error_lines[-1] if error_lines else "adapter import failed",
        }
    try:
        payload = json.loads(completed.stdout.strip())
    except json.JSONDecodeError:
        return {"success": False, "reason": "target interpreter returned invalid output"}
    if payload.get("version") != __version__:
        return {
            "success": False,
            "version": payload.get("version"),
            "expected_version": __version__,
            "reason": "target interpreter adapter version does not match this installer",
        }
    return {"success": bool(payload.get("importable")), **payload}


def _last_bootstrap_error(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            return None
        payload = json.loads(lines[-1])
    except (OSError, json.JSONDecodeError) as exc:
        return {"error_class": exc.__class__.__name__, "message": "bootstrap log is unreadable"}
    if not isinstance(payload, dict):
        return {"error_class": "InvalidRecord", "message": "bootstrap log is malformed"}
    return payload


def _execute_install(report: dict[str, Any], timeout: float) -> tuple[dict[str, Any], int]:
    plugins_dir = Path(report["plugins_dir"])
    state = report["installation_state"]
    if state != "current":
        _install_from_report(report)
    report["steps"][-1] = {
        "id": report["verb"],
        "status": "ok",
        "previous_state": state,
    }
    report["verify"] = verify_install(plugins_dir, Path(report["python"]), timeout)
    if report["verify"]["directly_usable"]:
        report["status"] = "ok"
        return report, EXIT_OK
    report["status"] = "failed"
    report["next_steps"] = _next_steps(report)
    return report, EXIT_VERIFY


def _execute_uninstall(report: dict[str, Any]) -> tuple[dict[str, Any], int]:
    plugins_dir = Path(report["plugins_dir"])
    target = plugins_dir / EXTENSION_DIRECTORY
    registration = plugins_dir / REGISTRATION_FILENAME
    receipt_path = plugins_dir / RECEIPT_RELATIVE_PATH
    receipt = _read_receipt(plugins_dir)
    if not target.exists() and not registration.exists() and receipt is None:
        report["status"] = "ok"
        report["steps"][-1] = {"id": "uninstall", "status": "already-absent"}
        return report, EXIT_OK
    if receipt is None:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "receipt",
            "refusing to remove an unreceipted SketchUp extension; run install --yes to repair it",
        )
    if (
        Path(str(receipt.get("extension_path", ""))).resolve() != target.resolve()
        or Path(str(receipt.get("registration_path", ""))).resolve() != registration.resolve()
    ):
        raise InstallFailure(EXIT_PREFLIGHT, "receipt", "receipt paths do not match this profile")
    lock_state = inspect_install_root(target)
    if lock_state.get("requires_restart"):
        raise InstallFailure(
            EXIT_REQUIRES_RESTART,
            "uninstall",
            lock_state.get(
                "recommended_next_action",
                "Close SketchUp and retry the same command.",
            ),
        )
    removed = safe_remove_tree(target)
    if not removed.get("success"):
        code = EXIT_REQUIRES_RESTART if removed.get("requires_restart") else EXIT_INSTALL
        raise InstallFailure(
            code,
            "uninstall",
            removed.get("message", "failed to remove the SketchUp extension"),
        )
    registration.unlink(missing_ok=True)
    receipt_path.unlink(missing_ok=True)
    report["status"] = "ok"
    report["steps"][-1] = {"id": "uninstall", "status": "removed"}
    return report, EXIT_OK


def _commit_staged_extension(
    staged_target: Path,
    staged_registration: Path,
    target: Path,
    registration: Path,
    *,
    receipt_path: Optional[Path] = None,
    receipt_factory: Optional[Callable[[], dict[str, Any]]] = None,
) -> None:
    """Commit a prepared extension and restore the previous pair on failure."""
    transaction_id = uuid.uuid4().hex
    backup_target = target.with_name(f".{target.name}.backup-{transaction_id}")
    backup_registration = registration.with_name(f".{registration.name}.backup-{transaction_id}")
    backup_receipt = (
        receipt_path.with_name(f".{receipt_path.name}.backup-{transaction_id}")
        if receipt_path is not None
        else None
    )
    failed_target = target.with_name(f".{target.name}.failed-{transaction_id}")
    failed_registration = registration.with_name(f".{registration.name}.failed-{transaction_id}")
    failed_receipt = (
        receipt_path.with_name(f".{receipt_path.name}.failed-{transaction_id}")
        if receipt_path is not None
        else None
    )
    target_backed_up = False
    registration_backed_up = False
    receipt_backed_up = False
    target_installed = False
    registration_installed = False
    receipt_installed = False

    try:
        if target.exists():
            os.replace(target, backup_target)
            target_backed_up = True
        if registration.exists():
            os.replace(registration, backup_registration)
            registration_backed_up = True
        if receipt_path is not None and receipt_path.exists():
            if backup_receipt is None:
                raise AssertionError("receipt backup path was not created")
            os.replace(receipt_path, backup_receipt)
            receipt_backed_up = True
        os.replace(staged_target, target)
        target_installed = True
        os.replace(staged_registration, registration)
        registration_installed = True
        if receipt_path is not None:
            if receipt_factory is None:
                raise ValueError("receipt_factory is required with receipt_path")
            _write_json_atomic(receipt_path, receipt_factory())
            receipt_installed = True
    except BaseException:
        if (
            receipt_installed
            and receipt_path is not None
            and receipt_path.exists()
            and failed_receipt is not None
        ):
            os.replace(receipt_path, failed_receipt)
        if registration_installed and registration.exists():
            os.replace(registration, failed_registration)
        if target_installed and target.exists():
            os.replace(target, failed_target)
        if (
            receipt_backed_up
            and backup_receipt is not None
            and backup_receipt.exists()
            and receipt_path is not None
        ):
            os.replace(backup_receipt, receipt_path)
        if registration_backed_up and backup_registration.exists():
            os.replace(backup_registration, registration)
        if target_backed_up and backup_target.exists():
            os.replace(backup_target, target)
        raise
    finally:
        for directory in (failed_target, backup_target):
            if directory.exists():
                safe_remove_tree(directory)
        for file_path in (failed_registration, backup_registration):
            file_path.unlink(missing_ok=True)
        for file_path in (failed_receipt, backup_receipt):
            if file_path is not None:
                file_path.unlink(missing_ok=True)


def uninstall_extension(plugins_dir: Path) -> bool:
    """Remove only the two paths owned by this package."""
    root = plugins_dir.expanduser().resolve()
    target = root / EXTENSION_DIRECTORY
    registration = root / REGISTRATION_FILENAME
    removed = False
    if target.is_dir():
        shutil.rmtree(target)
        removed = True
    if registration.is_file():
        registration.unlink()
        removed = True
    return removed


def _plugin_dir_version(path: Path) -> int:
    for part in path.parts:
        match = _VERSION_RE.fullmatch(part)
        if match:
            return int(match.group(1))
    return 0


def _find_server_executable() -> Optional[Path]:
    executable_name = "dcc-mcp-sketchup.exe" if sys.platform == "win32" else "dcc-mcp-sketchup"
    script_dirs = [Path(sys.executable).resolve().parent]
    configured_scripts = sysconfig.get_path("scripts")
    if configured_scripts:
        script_dirs.append(Path(configured_scripts).expanduser())
    user_bin = Path(site.getuserbase()).expanduser() / (
        "Scripts" if sys.platform == "win32" else "bin"
    )
    script_dirs.append(user_bin)
    for scripts_dir in dict.fromkeys(script_dirs):
        candidate = scripts_dir / executable_name
        if candidate.is_file():
            return candidate.resolve()
    resolved = shutil.which("dcc-mcp-sketchup")
    return Path(resolved).resolve() if resolved else None
