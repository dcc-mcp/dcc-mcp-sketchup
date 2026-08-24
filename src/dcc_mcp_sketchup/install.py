"""Install the SketchUp Ruby extension into the per-user Plugins directory."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import plistlib
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
from urllib.parse import urlsplit
from urllib.request import url2pathname

from dcc_mcp_core import __version__ as running_core_version
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
SCHEMA_VERSION = 1
RECEIPT_RELATIVE_PATH = Path(".dcc-mcp") / "receipts" / "sketchup.json"
BOOTSTRAP_ERRORS_RELATIVE_PATH = Path(".dcc-mcp") / "logs" / "sketchup-bootstrap-errors.jsonl"
EXIT_OK, EXIT_PREFLIGHT, EXIT_ACQUIRE = 0, 10, 20
EXIT_INSTALL, EXIT_VERIFY, EXIT_REQUIRES_RESTART = 30, 40, 50
_VERSION_RE = re.compile(r"SketchUp\s+(\d{4})$")
_VERSION_COMPONENT_RE = re.compile(r"^(?:0|[1-9][0-9]{0,5})$")
_MAX_VERSION_LENGTH = 32
_MAX_PROBE_OUTPUT_BYTES = 4096
_MAX_SIDECAR_BYTES = 16 * 1024 * 1024
_MAX_WINDOWS_VERSION_RESOURCE_BYTES = 1024 * 1024
LIFECYCLE_VERBS = ("install", "status", "verify", "uninstall", "upgrade")


class InstallFailure(ValueError):
    """Structured lifecycle failure with a stable process exit code."""

    def __init__(self, exit_code: int, stage: str, reason: str):
        super().__init__(reason)
        self.exit_code = exit_code
        self.stage = stage
        self.reason = reason


class _CommittedInstall:
    """Retain the prior installation until post-commit live verification succeeds."""

    def __init__(
        self,
        *,
        target: Path,
        registration: Path,
        receipt_path: Optional[Path],
        backup_target: Path,
        backup_registration: Path,
        backup_receipt: Optional[Path],
        failed_target: Path,
        failed_registration: Path,
        failed_receipt: Optional[Path],
    ) -> None:
        self.target = target
        self.registration = registration
        self.receipt_path = receipt_path
        self.backup_target = backup_target
        self.backup_registration = backup_registration
        self.backup_receipt = backup_receipt
        self.failed_target = failed_target
        self.failed_registration = failed_registration
        self.failed_receipt = failed_receipt
        self.target_preexisted = target.exists()
        self.registration_preexisted = registration.exists()
        self.receipt_preexisted = receipt_path is not None and receipt_path.exists()
        self.target_backed_up = False
        self.registration_backed_up = False
        self.receipt_backed_up = False
        self.target_installed = False
        self.registration_installed = False
        self.receipt_installed = False
        self.rolled_back = False

    @property
    def had_previous_state(self) -> bool:
        return self.target_preexisted or self.registration_preexisted or self.receipt_preexisted

    def rollback(self) -> None:
        if self.rolled_back:
            return
        try:
            if (
                self.receipt_installed
                and self.receipt_path is not None
                and self.receipt_path.exists()
            ):
                if self.failed_receipt is None:
                    raise AssertionError("receipt rollback path was not created")
                os.replace(self.receipt_path, self.failed_receipt)
            if self.registration_installed and self.registration.exists():
                os.replace(self.registration, self.failed_registration)
            if self.target_installed and self.target.exists():
                os.replace(self.target, self.failed_target)
            if self.receipt_backed_up:
                if self.backup_receipt is None or self.receipt_path is None:
                    raise AssertionError("receipt backup path was not created")
                os.replace(self.backup_receipt, self.receipt_path)
            if self.registration_backed_up:
                os.replace(self.backup_registration, self.registration)
            if self.target_backed_up:
                os.replace(self.backup_target, self.target)
            expected = (
                (self.target, self.target_preexisted),
                (self.registration, self.registration_preexisted),
            )
            if self.receipt_path is not None:
                expected += ((self.receipt_path, self.receipt_preexisted),)
            if any(path.exists() != should_exist for path, should_exist in expected):
                raise OSError("restored install paths do not match the prior state")
        except BaseException as exc:
            code = (
                EXIT_REQUIRES_RESTART
                if isinstance(exc, OSError) and _is_restart_lock_error(exc)
                else EXIT_INSTALL
            )
            raise InstallFailure(
                code,
                "install",
                "post-install verification failed and the prior installation could not be restored",
            ) from exc
        self.rolled_back = True

    def finalize(self) -> None:
        for directory in (self.failed_target, self.backup_target):
            if directory.exists():
                result = safe_remove_tree(directory)
                if not result.get("success"):
                    code = EXIT_REQUIRES_RESTART if result.get("requires_restart") else EXIT_INSTALL
                    raise InstallFailure(
                        code,
                        "install",
                        result.get("message", "failed to clean the install transaction"),
                    )
        for file_path in (self.failed_registration, self.backup_registration):
            file_path.unlink(missing_ok=True)
        for file_path in (self.failed_receipt, self.backup_receipt):
            if file_path is not None:
                file_path.unlink(missing_ok=True)


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


def _numeric_version_tuple(
    value: object,
    *,
    minimum_components: int,
    maximum_components: int,
) -> Optional[tuple[int, ...]]:
    if not isinstance(value, str) or not 0 < len(value) <= _MAX_VERSION_LENGTH:
        return None
    parts = value.split(".")
    if not minimum_components <= len(parts) <= maximum_components:
        return None
    if any(_VERSION_COMPONENT_RE.fullmatch(part) is None for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _version_tuple(value: object) -> Optional[tuple[int, int, int]]:
    parsed = _numeric_version_tuple(value, minimum_components=3, maximum_components=3)
    if parsed is None:
        return None
    return parsed[0], parsed[1], parsed[2]


def _run_bounded_command(command: Sequence[str], timeout: float = 10.0) -> dict[str, Any]:
    """Run one read-only probe without retaining unbounded child output."""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    with tempfile.TemporaryFile(mode="w+b") as stdout_file:
        with tempfile.TemporaryFile(mode="w+b") as stderr_file:
            try:
                process = subprocess.Popen(
                    list(command),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    creationflags=creationflags,
                )
            except OSError as exc:
                return {"success": False, "reason": f"launch failed: {exc.__class__.__name__}"}
            try:
                process.wait(timeout=max(0.1, min(float(timeout), 30.0)))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
                return {"success": False, "reason": "probe timed out"}
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read(_MAX_PROBE_OUTPUT_BYTES + 1)
            stderr = stderr_file.read(_MAX_PROBE_OUTPUT_BYTES + 1)
    return {
        "success": process.returncode == 0,
        "returncode": int(process.returncode or 0),
        "stdout": stdout[:_MAX_PROBE_OUTPUT_BYTES].decode("utf-8", errors="replace"),
        "stderr": stderr[:_MAX_PROBE_OUTPUT_BYTES].decode("utf-8", errors="replace"),
        "truncated": len(stdout) > _MAX_PROBE_OUTPUT_BYTES or len(stderr) > _MAX_PROBE_OUTPUT_BYTES,
    }


def _resolve_python(value: Optional[Path]) -> Path:
    configured = value
    if configured is None and os.environ.get("DCC_MCP_INSTALL_PYTHON"):
        configured = Path(os.environ["DCC_MCP_INSTALL_PYTHON"])
    python = (configured or Path(sys.executable)).expanduser().resolve()
    if not python.is_file():
        raise InstallFailure(EXIT_PREFLIGHT, "python", f"Python interpreter not found: {python}")
    return python


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _editable_distribution_root(direct_url: object) -> Optional[Path]:
    if not isinstance(direct_url, dict):
        return None
    url = direct_url.get("url")
    directory_info = direct_url.get("dir_info")
    if (
        not isinstance(url, str)
        or not 0 < len(url) <= 2048
        or not isinstance(directory_info, dict)
        or directory_info.get("editable") is not True
    ):
        return None
    parsed = urlsplit(url)
    if parsed.scheme != "file" or parsed.query or parsed.fragment:
        return None
    url_path = f"//{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path
    try:
        root = Path(url2pathname(url_path)).resolve()
    except (OSError, ValueError):
        return None
    return root if root.is_dir() else None


def _distribution_identity(environment: dict[str, Any]) -> dict[str, Any]:
    module_file = Path(str(environment.get("module_file") or "")).resolve()
    server_file = Path(str(environment.get("server_file") or "")).resolve()
    distribution_root = Path(str(environment.get("distribution_root") or "")).resolve()
    if (
        environment.get("adapter_version") != __version__
        or environment.get("module_version") != __version__
        or not module_file.is_file()
        or not server_file.is_file()
        or module_file.stat().st_size <= 0
        or server_file.stat().st_size <= 0
        or module_file.name != "__init__.py"
        or server_file.name != "server.py"
        or module_file.parent != server_file.parent
        or module_file.parent.name != "dcc_mcp_sketchup"
        or not distribution_root.is_dir()
    ):
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "python",
            "target interpreter did not load this dcc-mcp-sketchup distribution",
        )

    module_record = environment.get("module_record")
    server_record = environment.get("server_record")
    association = "record"
    if isinstance(module_record, str) and isinstance(server_record, str):
        if (
            (distribution_root / module_record).resolve() != module_file
            or (distribution_root / server_record).resolve() != server_file
            or not _is_within(module_file, distribution_root)
            or not _is_within(server_file, distribution_root)
        ):
            raise InstallFailure(
                EXIT_PREFLIGHT,
                "python",
                "imported SketchUp adapter modules are outside distribution ownership",
            )
    else:
        editable_root = _editable_distribution_root(environment.get("direct_url"))
        candidates = ()
        if editable_root is not None:
            candidates = (
                editable_root / "src" / "dcc_mcp_sketchup",
                editable_root / "dcc_mcp_sketchup",
            )
        if not any(
            module_file == (candidate / "__init__.py").resolve()
            and server_file == (candidate / "server.py").resolve()
            for candidate in candidates
        ):
            raise InstallFailure(
                EXIT_PREFLIGHT,
                "python",
                "imported SketchUp adapter modules are not owned by the selected distribution",
            )
        association = "editable-direct-url"

    direct_url = environment.get("direct_url")
    direct_url_identity = None
    if isinstance(direct_url, dict):
        direct_url_identity = {
            "url": direct_url.get("url"),
            "editable": (direct_url.get("dir_info") or {}).get("editable")
            if isinstance(direct_url.get("dir_info"), dict)
            else None,
        }
    return {
        "distribution": "dcc-mcp-sketchup",
        "distribution_version": str(environment["adapter_version"]),
        "distribution_root": str(distribution_root),
        "module_version": str(environment["module_version"]),
        "module_file": str(module_file),
        "server_file": str(server_file),
        "module_record": module_record,
        "server_record": server_record,
        "association": association,
        "direct_url": direct_url_identity,
    }


def _target_environment(python: Path) -> dict[str, Any]:
    code = """
import importlib.metadata as m
import json
import pathlib
import sys
import sysconfig
import dcc_mcp_sketchup as p
import dcc_mcp_sketchup.server as s

d = m.distribution("dcc-mcp-sketchup")
n = "dcc-mcp-sketchup.exe" if sys.platform == "win32" else "dcc-mcp-sketchup"
x = (pathlib.Path(sysconfig.get_path("scripts")) / n).resolve()
files = tuple(d.files or ())
owned = {str(pathlib.Path(d.locate_file(item)).resolve()): item for item in files}
launcher_record = owned.get(str(x))
launcher_hash = launcher_record.hash if launcher_record is not None else None
module_file = pathlib.Path(p.__file__).resolve()
server_file = pathlib.Path(s.__file__).resolve()
direct_url_text = d.read_text("direct_url.json")
direct_url = json.loads(direct_url_text) if direct_url_text else None
print(json.dumps({
    "python": sys.executable,
    "scripts": sysconfig.get_path("scripts"),
    "core_version": m.version("dcc-mcp-core"),
    "adapter_version": d.version,
    "module_version": p.__version__,
    "module_file": str(module_file),
    "server_file": str(server_file),
    "distribution_root": str(pathlib.Path(d.locate_file("")).resolve()),
    "module_record": str(owned[str(module_file)]) if str(module_file) in owned else None,
    "server_record": str(owned[str(server_file)]) if str(server_file) in owned else None,
    "direct_url": direct_url,
    "launcher_path": str(x),
    "launcher_hash_mode": getattr(launcher_hash, "mode", None),
    "launcher_hash": getattr(launcher_hash, "value", None),
}))
""".strip()
    completed = _run_bounded_command([str(python), "-c", code], timeout=15)
    if not completed.get("success") or completed.get("truncated"):
        error_lines = str(completed.get("stderr") or "").strip().splitlines()
        reason = error_lines[-1] if error_lines else "package metadata query failed"
        raise InstallFailure(EXIT_PREFLIGHT, "python", reason)
    try:
        environment = json.loads(str(completed.get("stdout") or "").strip())
    except json.JSONDecodeError as exc:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "python",
            "target interpreter returned invalid package metadata",
        ) from exc
    if not isinstance(environment, dict):
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "python",
            "target interpreter returned invalid package metadata",
        )
    core = _version_tuple(environment.get("core_version"))
    floor = _version_tuple(MIN_CORE_VERSION)
    if core is None:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "core",
            "target interpreter returned a noncanonical dcc-mcp-core version",
        )
    if floor is None or core < floor:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "core",
            f"dcc-mcp-core {environment['core_version']} is unsupported; "
            f"version {MIN_CORE_VERSION} or newer is required",
        )
    if Path(str(environment.get("python") or "")).resolve() != python.resolve():
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "python",
            "target interpreter identity changed during the package probe",
        )
    environment["distribution_identity"] = _distribution_identity(environment)
    return environment


def _sidecar_manifest(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        if not 0 < size <= _MAX_SIDECAR_BYTES:
            raise InstallFailure(
                EXIT_PREFLIGHT,
                "sidecar",
                "dcc-mcp-sketchup executable is missing, empty, or unbounded",
            )
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "sidecar",
            "dcc-mcp-sketchup executable could not be read",
        ) from exc
    return {"path": str(path.resolve()), "sha256": digest.hexdigest(), "size": size}


def _target_server_executable(environment: dict[str, Any]) -> Path:
    executable_name = "dcc-mcp-sketchup.exe" if sys.platform == "win32" else "dcc-mcp-sketchup"
    executable = Path(environment["scripts"]) / executable_name
    if (
        not executable.is_file()
        or Path(str(environment.get("launcher_path") or "")).resolve() != executable.resolve()
        or environment.get("launcher_hash_mode") != "sha256"
    ):
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "sidecar",
            f"dcc-mcp-sketchup executable was not found for {environment['python']}",
        )
    executable = executable.resolve()
    manifest = _sidecar_manifest(executable)
    actual_hash = base64.urlsafe_b64encode(bytes.fromhex(manifest["sha256"]))
    if actual_hash.rstrip(b"=").decode("ascii") != environment.get("launcher_hash"):
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "sidecar",
            "dcc-mcp-sketchup executable bytes do not match installed package metadata",
        )
    probe = _run_bounded_command([str(executable), "--version"])
    module_probe = _run_bounded_command(
        [environment["python"], "-m", "dcc_mcp_sketchup.server", "--version"]
    )
    actual_version = str(probe.get("stdout") or "").strip()
    module_version = str(module_probe.get("stdout") or "").strip()
    if (
        not probe.get("success")
        or probe.get("truncated")
        or not module_probe.get("success")
        or module_probe.get("truncated")
        or _version_tuple(actual_version) is None
        or actual_version != __version__
        or module_version != __version__
    ):
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "sidecar",
            "dcc-mcp-sketchup executable failed the bounded load/version probe",
        )
    return executable


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
        server_manifest = _sidecar_manifest(Path(configured_server))
    except OSError:
        return "repair"
    except InstallFailure:
        return "repair"
    if (
        not configured_server
        or not Path(configured_server).is_file()
        or Path(configured_server).resolve() != Path(str(receipt.get("server_path", ""))).resolve()
        or _manifest_digest(files) != receipt.get("extension_digest")
        or registration_manifest["sha256"] != receipt.get("registration", {}).get("sha256")
        or server_manifest != receipt.get("server")
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
    _probe_sketchup_host(host, version)
    return host


def _probe_sketchup_host(host: Path, expected_year: int) -> str:
    try:
        size = host.stat().st_size
    except OSError as exc:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "host",
            "SketchUp host metadata could not be read",
        ) from exc
    if size <= 0:
        raise InstallFailure(EXIT_PREFLIGHT, "host", "SketchUp host executable is empty")
    version = _native_host_version(host)
    parsed = _numeric_version_tuple(version, minimum_components=1, maximum_components=4)
    if parsed is None:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "host_version",
            "SketchUp host returned a noncanonical native version",
        )
    product_year = parsed[0] if parsed[0] >= 2000 else 2000 + parsed[0]
    if product_year != expected_year:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "host_version",
            f"SketchUp native version {version} does not match the {expected_year} profile",
        )
    return version


def _native_host_version(host: Path) -> str:
    if sys.platform == "win32":
        return _windows_file_version(host)
    if sys.platform == "darwin":
        return _macos_bundle_version(host)
    raise InstallFailure(
        EXIT_PREFLIGHT,
        "host",
        "SketchUp native host verification is supported only on Windows and macOS",
    )


def _windows_file_version(host: Path) -> str:
    try:
        with host.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise InstallFailure(
                    EXIT_PREFLIGHT,
                    "host",
                    "SketchUp host is not a loadable Windows executable",
                )
    except OSError as exc:
        raise InstallFailure(EXIT_PREFLIGHT, "host", "SketchUp host could not be read") from exc

    import ctypes
    from ctypes import wintypes

    version_dll = ctypes.WinDLL("version", use_last_error=True)
    version_dll.GetFileVersionInfoSizeW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    version_dll.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version_dll.GetFileVersionInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    version_dll.GetFileVersionInfoW.restype = wintypes.BOOL
    version_dll.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    ]
    version_dll.VerQueryValueW.restype = wintypes.BOOL

    ignored = wintypes.DWORD(0)
    resource_size = int(version_dll.GetFileVersionInfoSizeW(str(host), ctypes.byref(ignored)))
    if not 0 < resource_size <= _MAX_WINDOWS_VERSION_RESOURCE_BYTES:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "host",
            "SketchUp host has no bounded native version resource",
        )
    buffer = ctypes.create_string_buffer(resource_size)
    if not version_dll.GetFileVersionInfoW(str(host), 0, resource_size, buffer):
        raise InstallFailure(
            EXIT_PREFLIGHT, "host", "SketchUp native version resource is unreadable"
        )
    value = ctypes.c_void_p()
    length = wintypes.UINT(0)
    if not version_dll.VerQueryValueW(buffer, "\\", ctypes.byref(value), ctypes.byref(length)):
        raise InstallFailure(EXIT_PREFLIGHT, "host", "SketchUp native version resource is missing")

    class FixedFileInfo(ctypes.Structure):
        _fields_ = [
            ("signature", wintypes.DWORD),
            ("structure_version", wintypes.DWORD),
            ("file_version_ms", wintypes.DWORD),
            ("file_version_ls", wintypes.DWORD),
            ("product_version_ms", wintypes.DWORD),
            ("product_version_ls", wintypes.DWORD),
        ]

    if length.value < ctypes.sizeof(FixedFileInfo):
        raise InstallFailure(
            EXIT_PREFLIGHT, "host", "SketchUp native version resource is truncated"
        )
    info = ctypes.cast(value, ctypes.POINTER(FixedFileInfo)).contents
    if info.signature != 0xFEEF04BD:
        raise InstallFailure(EXIT_PREFLIGHT, "host", "SketchUp native version signature is invalid")
    translation = ctypes.c_void_p()
    translation_length = wintypes.UINT(0)
    if (
        not version_dll.VerQueryValueW(
            buffer,
            "\\VarFileInfo\\Translation",
            ctypes.byref(translation),
            ctypes.byref(translation_length),
        )
        or translation_length.value < 4
    ):
        raise InstallFailure(EXIT_PREFLIGHT, "host", "SketchUp product identity is missing")
    language, code_page = ctypes.cast(
        translation,
        ctypes.POINTER(wintypes.WORD * 2),
    ).contents
    product_name = ctypes.c_void_p()
    product_name_length = wintypes.UINT(0)
    product_name_key = f"\\StringFileInfo\\{language:04x}{code_page:04x}\\ProductName"
    if (
        not version_dll.VerQueryValueW(
            buffer,
            product_name_key,
            ctypes.byref(product_name),
            ctypes.byref(product_name_length),
        )
        or product_name_length.value <= 1
        or not ctypes.wstring_at(product_name).strip().lower().startswith("sketchup")
    ):
        raise InstallFailure(EXIT_PREFLIGHT, "host", "native executable is not SketchUp")
    components = (
        info.product_version_ms >> 16,
        info.product_version_ms & 0xFFFF,
        info.product_version_ls >> 16,
        info.product_version_ls & 0xFFFF,
    )
    return ".".join(str(component) for component in components)


def _macos_bundle_version(host: Path) -> str:
    info_path = host.parents[2] / "Contents" / "Info.plist"
    try:
        with info_path.open("rb") as stream:
            payload = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "host",
            "SketchUp application bundle metadata is unreadable",
        ) from exc
    product_name = str(payload.get("CFBundleName") or payload.get("CFBundleDisplayName") or "")
    if product_name.lower() != "sketchup":
        raise InstallFailure(EXIT_PREFLIGHT, "host", "application bundle is not SketchUp")
    return str(payload.get("CFBundleShortVersionString") or payload.get("CFBundleVersion") or "")


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
    for candidate in candidates:
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        try:
            _probe_sketchup_host(resolved, version)
        except InstallFailure:
            continue
        return resolved
    return None


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
    instance_id: Optional[str] = None,
    host_pid: Optional[int] = None,
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
    core = _version_tuple(environment.get("core_version"))
    floor = _version_tuple(MIN_CORE_VERSION)
    if core is None:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "core",
            "target interpreter returned a noncanonical dcc-mcp-core version",
        )
    if floor is None or core < floor:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "core",
            f"dcc-mcp-core {environment.get('core_version')} is unsupported; "
            f"version {MIN_CORE_VERSION} or newer is required",
        )
    adapter_version = environment.get("adapter_version")
    if _version_tuple(adapter_version) is None or adapter_version != __version__:
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "python",
            f"target interpreter has dcc-mcp-sketchup {adapter_version}; "
            f"this installer is {__version__}",
        )
    server = _target_server_executable(environment)
    server_manifest = _sidecar_manifest(server)
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
        "python_distribution": environment["distribution_identity"],
        "server_path": str(server),
        "server": server_manifest,
        "instance_id": instance_id,
        "host_pid": host_pid,
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
        "verify": {
            "directly_usable": False,
            "failure_stage": None,
            "failure_reason": None,
        },
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
        command.add_argument("--instance-id", help=argparse.SUPPRESS)
        command.add_argument("--host-pid", type=int, help=argparse.SUPPRESS)
    return parser


def _failure_result(verb: str, failure: InstallFailure) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "requires_restart" if failure.exit_code == EXIT_REQUIRES_RESTART else "failed",
        "dcc_type": "sketchup",
        "verb": verb,
        "adapter_version": __version__,
        "core_version": str(running_core_version or "unknown"),
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
        report = plan(
            args.verb,
            args.plugins_dir,
            args.python,
            args.dcc_path,
            args.instance_id,
            args.host_pid,
        )
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
            report["status"] = "ok" if state in {"fresh", "current"} else "partial"
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
                instance_id=report.get("instance_id"),
                host_pid=report.get("host_pid"),
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
    executable = executable.expanduser().resolve()
    if not executable.is_file() or executable.stat().st_size <= 0:
        raise RuntimeError("dcc-mcp-sketchup executable is missing or empty")
    probe = _run_bounded_command([str(executable), "--version"])
    if (
        not probe.get("success")
        or probe.get("truncated")
        or str(probe.get("stdout") or "").strip() != __version__
    ):
        raise RuntimeError("dcc-mcp-sketchup executable failed the bounded load/version probe")

    staging_root, staged_target, staged_registration = _stage_extension(root, executable)
    receipt_path = root / RECEIPT_RELATIVE_PATH
    receipt_report = {
        "core_version": str(running_core_version),
        "sketchup_version": str(_plugin_dir_version(root)),
        "dcc_path": None,
        "plugins_dir": str(root),
        "python": str(Path(sys.executable).resolve()),
        "server_path": str(executable),
        "server": _sidecar_manifest(executable),
    }
    try:
        _commit_staged_extension(
            staged_target,
            staged_registration,
            target,
            registration,
            receipt_path=receipt_path,
            receipt_factory=lambda: _receipt_payload(receipt_report),
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


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _owned_directories(files: list[dict[str, Any]]) -> set[str]:
    directories: set[str] = set()
    for record in files:
        relative = Path(str(record["path"]))
        directories.update(parent.as_posix() for parent in relative.parents if parent != Path("."))
    return directories


def _validate_owned_install(
    plugins_dir: Path,
    receipt: dict[str, Any],
) -> tuple[Path, Path, Path]:
    target = plugins_dir / EXTENSION_DIRECTORY
    registration = plugins_dir / REGISTRATION_FILENAME
    receipt_path = plugins_dir / RECEIPT_RELATIVE_PATH
    required_scalars = {
        "dcc_type": "sketchup",
        "plugins_dir": str(plugins_dir),
        "extension_path": str(target),
        "registration_path": str(registration),
    }
    if any(str(receipt.get(key, "")) != value for key, value in required_scalars.items()):
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "ownership",
            "receipt ownership does not match the selected SketchUp profile",
        )
    if (
        not target.is_dir()
        or not registration.is_file()
        or not receipt_path.is_file()
        or any(_is_link_or_junction(path) for path in (target, registration, receipt_path))
    ):
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "ownership",
            "managed SketchUp paths are missing or use unsupported links",
        )

    expected_files = receipt.get("files")
    if not isinstance(expected_files, list) or not expected_files:
        raise InstallFailure(EXIT_PREFLIGHT, "ownership", "receipt file ownership is missing")
    for record in expected_files:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "size"}:
            raise InstallFailure(EXIT_PREFLIGHT, "ownership", "receipt file ownership is invalid")
        relative = Path(str(record.get("path", "")))
        if (
            not relative.parts
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != str(record.get("path"))
            or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", "")))
            or not isinstance(record.get("size"), int)
            or record["size"] < 0
        ):
            raise InstallFailure(EXIT_PREFLIGHT, "ownership", "receipt file ownership is invalid")

    walked = list(target.rglob("*"))
    if any(_is_link_or_junction(path) for path in walked):
        raise InstallFailure(EXIT_PREFLIGHT, "ownership", "managed extension contains a link")
    actual_files = _files_manifest(target)
    actual_directories = {path.relative_to(target).as_posix() for path in walked if path.is_dir()}
    expected_digest = receipt.get("extension_digest")
    if (
        actual_files != expected_files
        or actual_directories != _owned_directories(expected_files)
        or not isinstance(expected_digest, str)
        or _manifest_digest(actual_files) != expected_digest
    ):
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "ownership",
            "managed extension bytes or directory ownership do not match the receipt",
        )

    expected_registration = receipt.get("registration")
    actual_registration = _registration_manifest(registration)
    if (
        not isinstance(expected_registration, dict)
        or set(expected_registration) != {"path", "sha256", "size"}
        or actual_registration != expected_registration
    ):
        raise InstallFailure(
            EXIT_PREFLIGHT,
            "ownership",
            "managed registration bytes do not match the receipt",
        )
    return target, registration, receipt_path


def _restore_uninstall_snapshot(
    snapshot: Path,
    target: Path,
    registration: Path,
    receipt_path: Path,
    receipt: dict[str, Any],
) -> None:
    snapshot_target = snapshot / EXTENSION_DIRECTORY
    snapshot_registration = snapshot / REGISTRATION_FILENAME
    snapshot_receipt = snapshot / "receipt.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(snapshot_target, target, dirs_exist_ok=True)
    registration.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(snapshot_registration, registration)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(snapshot_receipt, receipt_path)
    _validate_owned_install(target.parent, receipt)


def _transactional_uninstall(plugins_dir: Path, receipt: dict[str, Any]) -> None:
    target, registration, receipt_path = _validate_owned_install(plugins_dir, receipt)
    transaction = Path(tempfile.mkdtemp(prefix=".dcc-mcp-sketchup-uninstall-", dir=plugins_dir))
    snapshot = transaction / "snapshot"
    snapshot.mkdir()
    try:
        shutil.copytree(target, snapshot / EXTENSION_DIRECTORY)
        shutil.copy2(registration, snapshot / REGISTRATION_FILENAME)
        shutil.copy2(receipt_path, snapshot / "receipt.json")
        if _files_manifest(snapshot / EXTENSION_DIRECTORY) != receipt["files"]:
            raise InstallFailure(
                EXIT_INSTALL, "uninstall", "uninstall snapshot verification failed"
            )
        if (
            _registration_manifest(snapshot / REGISTRATION_FILENAME)["sha256"]
            != receipt["registration"]["sha256"]
        ):
            raise InstallFailure(
                EXIT_INSTALL, "uninstall", "uninstall snapshot verification failed"
            )

        try:
            removed = safe_remove_tree(target)
            if not removed.get("success"):
                code = EXIT_REQUIRES_RESTART if removed.get("requires_restart") else EXIT_INSTALL
                raise InstallFailure(
                    code,
                    "uninstall",
                    removed.get("message", "failed to remove the SketchUp extension"),
                )
            registration.unlink()
            receipt_path.unlink()
        except BaseException as exc:
            try:
                _restore_uninstall_snapshot(
                    snapshot,
                    target,
                    registration,
                    receipt_path,
                    receipt,
                )
            except BaseException as restore_error:
                code = (
                    EXIT_REQUIRES_RESTART
                    if isinstance(restore_error, OSError) and _is_restart_lock_error(restore_error)
                    else EXIT_INSTALL
                )
                raise InstallFailure(
                    code,
                    "uninstall",
                    "uninstall failed and the prior managed installation could not be restored",
                ) from restore_error
            raise exc
    finally:
        safe_remove_tree(transaction)


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
        "python_distribution": report.get("python_distribution"),
        "server_path": report["server_path"],
        "server": report["server"],
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
        selector = ["--dcc-path", report["dcc_path"]]
    else:
        selector = ["--plugins-dir", report["plugins_dir"]]
    if report.get("instance_id"):
        selector.extend(["--instance-id", str(report["instance_id"])])
    if report.get("host_pid"):
        selector.extend(["--host-pid", str(report["host_pid"])])
    return selector


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


def _install_from_report(
    report: dict[str, Any],
    *,
    retain_backups: bool = False,
) -> Optional[_CommittedInstall]:
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
        return _commit_staged_extension(
            staged_target,
            staged_registration,
            target,
            registration,
            receipt_path=receipt_path,
            receipt_factory=lambda: _receipt_payload(report),
            retain_backups=retain_backups,
        )
    finally:
        safe_remove_tree(staging_root)


def verify_install(
    plugins_dir: Path,
    python: Path,
    timeout: float,
    *,
    instance_id: Optional[str] = None,
    host_pid: Optional[int] = None,
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
    try:
        server_manifest = _sidecar_manifest(Path(configured_server))
    except InstallFailure as exc:
        result.update(failure_stage="server_path", failure_reason=exc.reason)
        return result
    if server_manifest != receipt.get("server"):
        result["server_path"] = {
            "success": False,
            "path": configured_server,
            "expected_sha256": receipt.get("server", {}).get("sha256"),
            "actual_sha256": server_manifest["sha256"],
        }
        result.update(
            failure_stage="server_path",
            failure_reason="installed sidecar bytes differ from the receipt; reinstall the package",
        )
        return result
    executable_probe = _run_bounded_command([configured_server, "--version"])
    module_probe = _run_bounded_command([str(python), "-m", "dcc_mcp_sketchup.server", "--version"])
    if (
        not executable_probe.get("success")
        or executable_probe.get("truncated")
        or str(executable_probe.get("stdout") or "").strip() != __version__
        or not module_probe.get("success")
        or module_probe.get("truncated")
        or str(module_probe.get("stdout") or "").strip() != __version__
    ):
        result.update(
            failure_stage="server_path",
            failure_reason="installed sidecar failed the bounded load/version probe",
        )
        return result
    result["server_path"] = {
        "success": True,
        "path": configured_server,
        "sha256": server_manifest["sha256"],
    }

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

    result["import"] = _python_import_check(python, receipt.get("python_distribution"))
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
        instance_id=instance_id,
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
    identity_failure = _readiness_identity_failure(
        readiness,
        receipt,
        plugins_dir,
        instance_id=instance_id,
        host_pid=host_pid,
    )
    if identity_failure is not None:
        result.update(
            failure_stage="readiness_identity",
            failure_reason=identity_failure,
        )
        return result
    result["directly_usable"] = True
    return result


def _readiness_identity_failure(
    readiness: dict[str, Any],
    receipt: dict[str, Any],
    plugins_dir: Path,
    *,
    instance_id: Optional[str],
    host_pid: Optional[int],
) -> Optional[str]:
    entry = readiness.get("entry")
    if not isinstance(entry, dict):
        return "ready response did not identify one SketchUp sidecar instance"
    actual_instance = entry.get("instance_id")
    if not isinstance(actual_instance, str) or not actual_instance.strip():
        return "ready response omitted the SketchUp instance id"
    if instance_id is not None and actual_instance != instance_id:
        return "ready response belongs to a different SketchUp instance"

    metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
    try:
        entry_host_pid = int(metadata.get("dcc_pid") or metadata.get("host_pid"))
    except (TypeError, ValueError):
        return "ready response omitted the SketchUp host PID"
    if entry_host_pid <= 0:
        return "ready response reported an invalid SketchUp host PID"
    if host_pid is not None and entry_host_pid != host_pid:
        return "ready response belongs to a different SketchUp host PID"

    expected_year = _plugin_dir_version(plugins_dir)
    entry_version = metadata.get("dcc_version") or entry.get("version")
    if _sketchup_product_year(entry_version) != expected_year:
        return f"ready response does not match the selected SketchUp {expected_year} profile"
    entry_adapter = entry.get("adapter_version") or metadata.get("adapter_version")
    if _version_tuple(entry_adapter) is None or entry_adapter != __version__:
        return "ready response adapter version does not match this installation"

    probe = readiness.get("probe")
    probe_result = probe.get("result") if isinstance(probe, dict) else None
    if not isinstance(probe_result, dict):
        return "ready response omitted the real Ruby probe result"
    structured = probe_result.get("structuredContent")
    if structured is None:
        structured = probe_result.get("structured_content")
    if not isinstance(structured, dict) or structured.get("success") is not True:
        return "ready response omitted the real Ruby structured payload"
    context = structured.get("context")
    if not isinstance(context, dict) or context.get("status") != "ok":
        return "real Ruby payload did not report status=ok"
    try:
        ruby_host_pid = int(context.get("host_pid"))
    except (TypeError, ValueError):
        return "real Ruby payload omitted the SketchUp host PID"
    if ruby_host_pid != entry_host_pid:
        return "real Ruby payload host PID differs from the selected sidecar instance"
    if _sketchup_product_year(context.get("sketchup_version")) != expected_year:
        return f"real Ruby payload does not match the selected SketchUp {expected_year} profile"
    ruby_adapter = context.get("adapter_version")
    if _version_tuple(ruby_adapter) is None or ruby_adapter != __version__:
        return "real Ruby payload adapter version does not match this installation"
    expected_plugin = (plugins_dir / EXTENSION_DIRECTORY).resolve()
    try:
        actual_plugin = Path(str(context.get("plugin_path") or "")).resolve()
    except OSError:
        return "real Ruby payload plugin path is invalid"
    if actual_plugin != expected_plugin:
        return "real Ruby payload came from a different SketchUp profile"

    expected_host = receipt.get("dcc_path")
    if expected_host:
        process_path = _process_executable_path(entry_host_pid)
        if process_path is None or process_path.resolve() != Path(str(expected_host)).resolve():
            return "ready SketchUp host path differs from the install receipt"
    return None


def _sketchup_product_year(value: object) -> Optional[int]:
    parsed = _numeric_version_tuple(value, minimum_components=1, maximum_components=4)
    if parsed is None:
        return None
    return parsed[0] if parsed[0] >= 2000 else 2000 + parsed[0]


def _process_executable_path(pid: int) -> Optional[Path]:
    if pid <= 0:
        return None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(32_768)
            length = wintypes.DWORD(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                return None
            return Path(buffer.value).resolve()
        finally:
            kernel32.CloseHandle(handle)
    if sys.platform == "darwin":
        import ctypes

        buffer = ctypes.create_string_buffer(4096)
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
            length = int(libproc.proc_pidpath(int(pid), buffer, len(buffer)))
        except (OSError, AttributeError):
            return None
        if length <= 0:
            return None
        return Path(buffer.value.decode("utf-8", errors="strict")).resolve()
    try:
        return Path(f"/proc/{pid}/exe").resolve(strict=True)
    except OSError:
        return None


def _python_import_check(python: Path, expected_identity: object) -> dict[str, Any]:
    try:
        environment = _target_environment(python)
    except InstallFailure as exc:
        return {"success": False, "reason": exc.reason}
    identity = environment.get("distribution_identity")
    if not isinstance(expected_identity, dict) or identity != expected_identity:
        return {
            "success": False,
            "version": environment.get("module_version"),
            "expected_version": __version__,
            "distribution": identity,
            "reason": "target interpreter distribution identity differs from the install receipt",
        }
    return {
        "success": True,
        "importable": True,
        "version": environment["module_version"],
        "distribution": identity,
    }


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
    transaction: Optional[_CommittedInstall] = None
    if state != "current":
        transaction = _install_from_report(report, retain_backups=True)
    report["steps"][-1] = {
        "id": report["verb"],
        "status": "ok",
        "previous_state": state,
    }
    try:
        report["verify"] = verify_install(
            plugins_dir,
            Path(report["python"]),
            timeout,
            instance_id=report.get("instance_id"),
            host_pid=report.get("host_pid"),
        )
    except BaseException:
        if transaction is not None:
            transaction.rollback()
            transaction.finalize()
        raise
    if report["verify"]["directly_usable"]:
        if transaction is not None:
            transaction.finalize()
        report["status"] = "ok"
        return report, EXIT_OK
    if transaction is not None:
        transaction.rollback()
        transaction.finalize()
        report["previous_state_restored"] = True
        report["steps"][-1] = {
            "id": report["verb"],
            "status": "rolled-back",
            "previous_state": state,
        }
    report["status"] = "failed"
    report["next_steps"] = _next_steps(report)
    return report, EXIT_VERIFY


def _execute_uninstall(report: dict[str, Any]) -> tuple[dict[str, Any], int]:
    plugins_dir = Path(report["plugins_dir"])
    target = plugins_dir / EXTENSION_DIRECTORY
    registration = plugins_dir / REGISTRATION_FILENAME
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
    _validate_owned_install(plugins_dir, receipt)
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
    _transactional_uninstall(plugins_dir, receipt)
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
    retain_backups: bool = False,
) -> Optional[_CommittedInstall]:
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
    transaction = _CommittedInstall(
        target=target,
        registration=registration,
        receipt_path=receipt_path,
        backup_target=backup_target,
        backup_registration=backup_registration,
        backup_receipt=backup_receipt,
        failed_target=failed_target,
        failed_registration=failed_registration,
        failed_receipt=failed_receipt,
    )

    try:
        if target.exists():
            os.replace(target, backup_target)
            transaction.target_backed_up = True
        if registration.exists():
            os.replace(registration, backup_registration)
            transaction.registration_backed_up = True
        if receipt_path is not None and receipt_path.exists():
            if backup_receipt is None:
                raise AssertionError("receipt backup path was not created")
            os.replace(receipt_path, backup_receipt)
            transaction.receipt_backed_up = True
        os.replace(staged_target, target)
        transaction.target_installed = True
        os.replace(staged_registration, registration)
        transaction.registration_installed = True
        if receipt_path is not None:
            if receipt_factory is None:
                raise ValueError("receipt_factory is required with receipt_path")
            _write_json_atomic(receipt_path, receipt_factory())
            transaction.receipt_installed = True
    except BaseException:
        transaction.rollback()
        transaction.finalize()
        raise
    if retain_backups:
        return transaction
    transaction.finalize()
    return None


def uninstall_extension(plugins_dir: Path) -> bool:
    """Remove a receipt-owned extension without deleting unowned content."""
    root = plugins_dir.expanduser().resolve()
    target = root / EXTENSION_DIRECTORY
    registration = root / REGISTRATION_FILENAME
    receipt = _read_receipt(root)
    if not target.exists() and not registration.exists() and receipt is None:
        return False
    if receipt is None:
        raise RuntimeError("refusing to remove an unreceipted SketchUp extension")
    _transactional_uninstall(root, receipt)
    return True


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
