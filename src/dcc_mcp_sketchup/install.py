"""Install the SketchUp Ruby extension into the per-user Plugins directory."""

from __future__ import annotations

import os
import re
import shutil
import site
import sys
import sysconfig
from pathlib import Path
from typing import Optional

EXTENSION_DIRECTORY = "dcc_mcp_sketchup"
REGISTRATION_FILENAME = "dcc_mcp_sketchup.rb"
_VERSION_RE = re.compile(r"SketchUp\s+(\d{4})$")


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


def install_extension(plugins_dir: Path, *, overwrite: bool = False) -> Path:
    """Copy the extension and bind it to this environment's console script."""
    root = plugins_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / EXTENSION_DIRECTORY
    registration = root / REGISTRATION_FILENAME
    existing = [path for path in (target, registration) if path.exists()]
    if existing and not overwrite:
        raise FileExistsError(f"SketchUp extension already exists: {existing[0]}")
    if target.exists():
        shutil.rmtree(target)
    if registration.exists():
        registration.unlink()

    package_root = Path(__file__).resolve().parent
    source = package_root / "sketchup_plugin"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(source / "registration.rb", registration)
    (target / "registration.rb").unlink()

    executable = _find_server_executable()
    if executable is None:
        shutil.rmtree(target)
        registration.unlink(missing_ok=True)
        raise RuntimeError("dcc-mcp-sketchup executable was not found in this Python environment")
    (target / "server_path.txt").write_text(str(executable), encoding="utf-8")
    return target


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
