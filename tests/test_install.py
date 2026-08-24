import hashlib
import json
import os
import sys
from importlib.resources import files
from pathlib import Path

import pytest
from dcc_mcp_core.deployment import (
    INSTALL_EXIT_ACQUIRE,
    INSTALL_EXIT_INSTALL,
    INSTALL_EXIT_OK,
    INSTALL_EXIT_PREFLIGHT,
    INSTALL_EXIT_REQUIRES_RESTART,
    INSTALL_EXIT_VERIFY,
    INSTALL_SOP_SCHEMA_VERSION,
    load_install_sop_schema,
)
from jsonschema import Draft202012Validator

from dcc_mcp_sketchup import install
from dcc_mcp_sketchup import server as server_module

ROOT = Path(__file__).parents[1]
INSTALL_SOP_SCHEMA = load_install_sop_schema()
INSTALL_SOP_VALIDATOR = Draft202012Validator(INSTALL_SOP_SCHEMA)
Draft202012Validator.check_schema(INSTALL_SOP_SCHEMA)


def test_core_install_contract_floor_is_projected_everywhere():
    floor = install.MIN_CORE_VERSION
    assert floor == "0.20.14"

    for path in (ROOT / "pyproject.toml", ROOT / "README.md", ROOT / "install.md"):
        assert f"dcc-mcp-core>={floor},<1.0.0" in path.read_text(encoding="utf-8")

    for skill_path in sorted((ROOT / "src" / "dcc_mcp_sketchup" / "skills").glob("*/SKILL.md")):
        assert f"dcc-mcp-core {floor}+" in skill_path.read_text(encoding="utf-8")

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f'CORE_INSTALL_SOP_VERSION: "{floor}"' in workflow
    assert "dcc-mcp-core==${CORE_INSTALL_SOP_VERSION}" in workflow


def test_install_reports_use_the_published_core_schema_and_constants():
    schema_resource = files("dcc_mcp_core").joinpath(
        "schemas", "adapter-install-sop-v1.schema.json"
    )
    schema_bytes = schema_resource.read_bytes()

    assert len(schema_bytes) == 4261
    assert hashlib.sha256(schema_bytes).hexdigest() == (
        "3ca25788439917b4d4c0617230a762f9797756b5b54f45c8c4149f975b90f904"
    )
    assert b"\r\n" not in schema_bytes
    assert load_install_sop_schema() == json.loads(schema_bytes)
    assert INSTALL_SOP_SCHEMA == load_install_sop_schema()

    fixture_dir = ROOT / "tests" / "fixtures"
    assert not (fixture_dir / "adapter-install-sop-v1.schema.json").exists()
    assert not (fixture_dir / "README.md").exists()

    assert install.SCHEMA_VERSION == INSTALL_SOP_SCHEMA_VERSION
    assert (
        install.EXIT_OK,
        install.EXIT_PREFLIGHT,
        install.EXIT_ACQUIRE,
        install.EXIT_INSTALL,
        install.EXIT_VERIFY,
        install.EXIT_REQUIRES_RESTART,
    ) == (
        INSTALL_EXIT_OK,
        INSTALL_EXIT_PREFLIGHT,
        INSTALL_EXIT_ACQUIRE,
        INSTALL_EXIT_INSTALL,
        INSTALL_EXIT_VERIFY,
        INSTALL_EXIT_REQUIRES_RESTART,
    )


def readiness_success(
    plugins_dir: Path,
    *,
    year: int = 2026,
    host_pid: int = 4242,
    instance_id: str = "sketchup-2026-4242",
) -> dict:
    version = f"{year}.0.0"
    return {
        "success": True,
        "status": "ready",
        "entry": {
            "instance_id": instance_id,
            "adapter_version": install.__version__,
            "version": version,
            "metadata": {
                "dcc_pid": host_pid,
                "dcc_version": version,
            },
        },
        "probe": {
            "success": True,
            "result": {
                "structuredContent": {
                    "success": True,
                    "context": {
                        "status": "ok",
                        "sketchup_version": version,
                        "host_pid": host_pid,
                        "adapter_version": install.__version__,
                        "plugin_path": str((plugins_dir / install.EXTENSION_DIRECTORY).resolve()),
                    },
                }
            },
        },
    }


def installed_bytes(plugins_dir: Path) -> dict[str, bytes]:
    owned_paths = (
        plugins_dir / install.EXTENSION_DIRECTORY,
        plugins_dir / install.REGISTRATION_FILENAME,
        plugins_dir / install.RECEIPT_RELATIVE_PATH,
    )
    snapshot: dict[str, bytes] = {}
    for owned_path in owned_paths:
        if owned_path.is_dir():
            for path in sorted(item for item in owned_path.rglob("*") if item.is_file()):
                snapshot[path.relative_to(plugins_dir).as_posix()] = path.read_bytes()
        elif owned_path.is_file():
            snapshot[owned_path.relative_to(plugins_dir).as_posix()] = owned_path.read_bytes()
    return snapshot


def fake_server_executable(tmp_path: Path, monkeypatch) -> Path:
    executable = tmp_path / "dcc-mcp-sketchup.exe"
    executable.write_bytes(b"bounded test launcher")
    monkeypatch.setattr(install, "_find_server_executable", lambda: executable)
    monkeypatch.setattr(
        install,
        "_run_bounded_command",
        lambda _command, **_kwargs: {
            "success": True,
            "stdout": install.__version__,
            "stderr": "",
            "truncated": False,
        },
    )
    return executable


@pytest.mark.parametrize("verb", install.LIFECYCLE_VERBS)
def test_every_public_lifecycle_plan_matches_the_core_draft_schema(tmp_path, verb):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)

    report, code, as_json = install.run(
        [verb, "--plugins-dir", str(plugins_dir), "--json", "--dry-run"]
    )

    assert code == install.EXIT_OK
    assert as_json is True
    INSTALL_SOP_VALIDATOR.validate(report)


def test_public_preflight_failure_matches_the_core_draft_schema(tmp_path):
    unavailable = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"

    report, code, as_json = install.run(
        ["install", "--plugins-dir", str(unavailable), "--json", "--dry-run"]
    )

    assert code == install.EXIT_PREFLIGHT
    assert as_json is True
    INSTALL_SOP_VALIDATOR.validate(report)


def test_install_and_uninstall_copy_only_owned_extension(tmp_path, monkeypatch):
    executable = fake_server_executable(tmp_path, monkeypatch)
    plugins_dir = tmp_path / "Plugins"

    target = install.install_extension(plugins_dir)

    assert target == plugins_dir / "dcc_mcp_sketchup"
    assert (plugins_dir / "dcc_mcp_sketchup.rb").is_file()
    assert (target / "main.rb").is_file()
    assert (target / "runtime.rb").is_file()
    assert (target / "commands.rb").is_file()
    assert (target / "server_path.txt").read_text(encoding="utf-8") == str(executable)
    unrelated = plugins_dir / "user_plugin.rb"
    unrelated.write_text("keep", encoding="utf-8")

    assert install.uninstall_extension(plugins_dir) is True
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert install.uninstall_extension(plugins_dir) is False


def test_install_refuses_existing_extension_without_overwrite(tmp_path, monkeypatch):
    fake_server_executable(tmp_path, monkeypatch)
    plugins_dir = tmp_path / "Plugins"
    install.install_extension(plugins_dir)

    with pytest.raises(FileExistsError):
        install.install_extension(plugins_dir)


def test_overwrite_failure_preserves_the_previous_extension(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "Plugins"
    target = plugins_dir / "dcc_mcp_sketchup"
    target.mkdir(parents=True)
    previous_payload = target / "previous.rb"
    previous_payload.write_text("previous extension", encoding="utf-8")
    registration = plugins_dir / "dcc_mcp_sketchup.rb"
    registration.write_text("previous registration", encoding="utf-8")

    fake_server_executable(tmp_path, monkeypatch)
    monkeypatch.setattr(
        install.shutil,
        "copytree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("staged copy failed")),
    )

    with pytest.raises(OSError, match="staged copy failed"):
        install.install_extension(plugins_dir, overwrite=True)

    assert previous_payload.read_text(encoding="utf-8") == "previous extension"
    assert registration.read_text(encoding="utf-8") == "previous registration"


def test_standard_install_dry_run_is_machine_readable_and_does_not_write(tmp_path):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)

    report, code, as_json = install.run(
        ["install", "--plugins-dir", str(plugins_dir), "--json", "--dry-run"]
    )

    assert code == install.EXIT_OK
    assert as_json is True
    assert report["schema_version"] == 1
    assert report["status"] == "planned"
    assert report["dcc_type"] == "sketchup"
    assert report["sketchup_version"] == "2026"
    assert report["python"]
    assert report["installation_state"] == "fresh"
    assert report["steps"][-1] == {
        "id": "install",
        "status": "planned",
        "installation_state": "fresh",
    }
    assert not (plugins_dir / install.EXTENSION_DIRECTORY).exists()
    assert not (plugins_dir / install.RECEIPT_RELATIVE_PATH).exists()


def test_install_status_and_uninstall_round_trip(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    monkeypatch.setattr(
        install,
        "verify_install",
        lambda *_args, **_kwargs: {
            "directly_usable": True,
            "failure_stage": None,
            "failure_reason": None,
        },
    )

    installed, install_code, _ = install.run(
        ["install", "--plugins-dir", str(plugins_dir), "--yes", "--json"]
    )
    status, status_code, _ = install.run(["status", "--plugins-dir", str(plugins_dir), "--json"])
    removed, remove_code, _ = install.run(
        ["uninstall", "--plugins-dir", str(plugins_dir), "--yes", "--json"]
    )

    assert install_code == status_code == remove_code == install.EXIT_OK
    assert installed["verify"]["directly_usable"] is True
    assert status["installation_state"] == "current"
    assert removed["status"] == "ok"
    for result in (installed, status, removed):
        INSTALL_SOP_VALIDATOR.validate(result)
    assert not (plugins_dir / install.EXTENSION_DIRECTORY).exists()
    assert not (plugins_dir / install.REGISTRATION_FILENAME).exists()
    assert not (plugins_dir / install.RECEIPT_RELATIVE_PATH).exists()


def test_partial_install_is_planned_as_repairable_without_writes(tmp_path):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    target = plugins_dir / install.EXTENSION_DIRECTORY
    target.mkdir(parents=True)
    (target / "partial.rb").write_text("partial", encoding="utf-8")

    report, code, _ = install.run(
        ["install", "--plugins-dir", str(plugins_dir), "--json", "--dry-run"]
    )

    assert code == install.EXIT_OK
    assert report["installation_state"] == "partial"
    assert (target / "partial.rb").read_text(encoding="utf-8") == "partial"
    assert not (plugins_dir / install.RECEIPT_RELATIVE_PATH).exists()


def test_uninstall_refuses_an_unreceipted_extension(tmp_path):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    target = plugins_dir / install.EXTENSION_DIRECTORY
    target.mkdir(parents=True)
    registration = plugins_dir / install.REGISTRATION_FILENAME
    registration.write_text("user-owned", encoding="utf-8")

    report, code, _ = install.run(
        ["uninstall", "--plugins-dir", str(plugins_dir), "--json", "--yes"]
    )

    assert code == install.EXIT_PREFLIGHT
    assert report["verify"]["failure_stage"] == "receipt"
    assert target.is_dir()
    assert registration.read_text(encoding="utf-8") == "user-owned"


@pytest.mark.parametrize("tamper_target", ["unexpected-file", "registration"])
def test_uninstall_refuses_tampered_or_unowned_content(tmp_path, tamper_target):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    install._install_from_report(install.plan("install", plugins_dir, None, None))
    if tamper_target == "unexpected-file":
        unexpected = plugins_dir / install.EXTENSION_DIRECTORY / "operator-data.txt"
        unexpected.write_text("must survive", encoding="utf-8")
    else:
        registration = plugins_dir / install.REGISTRATION_FILENAME
        registration.write_text("operator replacement", encoding="utf-8")
    before = installed_bytes(plugins_dir)

    report, code, _ = install.run(
        ["uninstall", "--plugins-dir", str(plugins_dir), "--json", "--yes"]
    )

    assert code == install.EXIT_PREFLIGHT
    assert report["verify"]["failure_stage"] == "ownership"
    assert installed_bytes(plugins_dir) == before


@pytest.mark.parametrize(
    ("failure_target", "error_type", "expected_code"),
    [
        ("registration", PermissionError, install.EXIT_REQUIRES_RESTART),
        ("receipt", OSError, install.EXIT_INSTALL),
    ],
)
def test_uninstall_delete_failure_restores_the_complete_prior_state(
    tmp_path,
    monkeypatch,
    failure_target,
    error_type,
    expected_code,
):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    install._install_from_report(install.plan("install", plugins_dir, None, None))
    before = installed_bytes(plugins_dir)
    registration = (plugins_dir / install.REGISTRATION_FILENAME).resolve()
    receipt_path = (plugins_dir / install.RECEIPT_RELATIVE_PATH).resolve()
    failing_path = registration if failure_target == "registration" else receipt_path
    real_unlink = Path.unlink

    def fail_selected_unlink(path, *args, **kwargs):
        if path.resolve() == failing_path:
            raise error_type(f"injected {failure_target} delete failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_selected_unlink)

    report, code, _ = install.run(
        ["uninstall", "--plugins-dir", str(plugins_dir), "--json", "--yes"]
    )

    assert code == expected_code
    assert report["verify"]["failure_stage"] == "uninstall"
    assert installed_bytes(plugins_dir) == before
    INSTALL_SOP_VALIDATOR.validate(report)


def test_uninstall_partial_directory_delete_failure_restores_every_owned_byte(
    tmp_path,
    monkeypatch,
):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    install._install_from_report(install.plan("install", plugins_dir, None, None))
    target = (plugins_dir / install.EXTENSION_DIRECTORY).resolve()
    before = installed_bytes(plugins_dir)
    real_safe_remove_tree = install.safe_remove_tree

    def partially_remove_selected_tree(path):
        if Path(path).resolve() == target:
            next(item for item in target.rglob("*") if item.is_file()).unlink()
            return {
                "success": False,
                "requires_restart": False,
                "message": "injected partial directory deletion",
            }
        return real_safe_remove_tree(path)

    monkeypatch.setattr(install, "safe_remove_tree", partially_remove_selected_tree)

    report, code, _ = install.run(
        ["uninstall", "--plugins-dir", str(plugins_dir), "--json", "--yes"]
    )

    assert code == install.EXIT_INSTALL
    assert report["verify"]["failure_stage"] == "uninstall"
    assert installed_bytes(plugins_dir) == before
    INSTALL_SOP_VALIDATOR.validate(report)


def test_receipt_failure_rolls_back_extension_registration_and_receipt(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    monkeypatch.setattr(
        install,
        "verify_install",
        lambda *_args, **_kwargs: {
            "directly_usable": True,
            "failure_stage": None,
            "failure_reason": None,
        },
    )
    _, code, _ = install.run(["install", "--plugins-dir", str(plugins_dir), "--yes", "--json"])
    assert code == install.EXIT_OK
    target = plugins_dir / install.EXTENSION_DIRECTORY
    marker = target / "previous.rb"
    marker.write_text("previous extension", encoding="utf-8")
    registration = plugins_dir / install.REGISTRATION_FILENAME
    registration.write_text("previous registration", encoding="utf-8")
    receipt_path = plugins_dir / install.RECEIPT_RELATIVE_PATH
    receipt = install.json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["source_digest"] = "previous-release"
    receipt_path.write_text(install.json.dumps(receipt), encoding="utf-8")
    old_receipt = receipt_path.read_bytes()

    monkeypatch.setattr(
        install,
        "_write_json_atomic",
        lambda *_args: (_ for _ in ()).throw(OSError("receipt write failed")),
    )

    report, upgrade_code, _ = install.run(
        ["upgrade", "--plugins-dir", str(plugins_dir), "--yes", "--json"]
    )

    assert upgrade_code == install.EXIT_INSTALL
    assert "receipt write failed" in report["verify"]["failure_reason"]
    INSTALL_SOP_VALIDATOR.validate(report)
    assert marker.read_text(encoding="utf-8") == "previous extension"
    assert registration.read_text(encoding="utf-8") == "previous registration"
    assert receipt_path.read_bytes() == old_receipt


def test_live_verify_failure_restores_the_exact_previous_install(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    monkeypatch.setattr(
        install,
        "verify_install",
        lambda *_args, **_kwargs: {
            "directly_usable": True,
            "failure_stage": None,
            "failure_reason": None,
        },
    )
    _, install_code, _ = install.run(
        ["install", "--plugins-dir", str(plugins_dir), "--yes", "--json"]
    )
    assert install_code == install.EXIT_OK

    target = plugins_dir / install.EXTENSION_DIRECTORY
    (target / "previous.rb").write_text("previous extension", encoding="utf-8")
    (target / "operator-empty").mkdir()
    registration = plugins_dir / install.REGISTRATION_FILENAME
    registration.write_text("previous registration", encoding="utf-8")
    receipt_path = plugins_dir / install.RECEIPT_RELATIVE_PATH
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["source_digest"] = "previous-release"
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    before = installed_bytes(plugins_dir)

    def fail_live_verify(*_args, **_kwargs):
        assert list(plugins_dir.glob(f".{install.EXTENSION_DIRECTORY}.backup-*"))
        assert list(plugins_dir.glob(f".{install.REGISTRATION_FILENAME}.backup-*"))
        assert list(receipt_path.parent.glob(f".{receipt_path.name}.backup-*"))
        return {
            "directly_usable": False,
            "failure_stage": "readiness",
            "failure_reason": "no live SketchUp sidecar",
        }

    monkeypatch.setattr(install, "verify_install", fail_live_verify)

    report, upgrade_code, _ = install.run(
        ["upgrade", "--plugins-dir", str(plugins_dir), "--yes", "--json"]
    )

    assert upgrade_code == install.EXIT_VERIFY
    assert report["verify"]["failure_stage"] == "readiness"
    assert report["previous_state_restored"] is True
    assert installed_bytes(plugins_dir) == before
    assert (target / "operator-empty").is_dir()
    assert not list(plugins_dir.glob(".*.backup-*"))
    assert not list(plugins_dir.glob(".*.failed-*"))
    assert not list(receipt_path.parent.glob(".*.backup-*"))
    assert not list(receipt_path.parent.glob(".*.failed-*"))
    INSTALL_SOP_VALIDATOR.validate(report)


def test_target_environment_rejects_pythonpath_shadow_modules(tmp_path, monkeypatch):
    shadow_root = tmp_path / "shadow"
    package = shadow_root / "dcc_mcp_sketchup"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        f"__version__ = {install.__version__!r}\n",
        encoding="utf-8",
    )
    (package / "server.py").write_text("SHADOW = True\n", encoding="utf-8")
    existing = os.environ.get("PYTHONPATH")
    pythonpath = str(shadow_root) if not existing else str(shadow_root) + os.pathsep + existing
    monkeypatch.setenv("PYTHONPATH", pythonpath)

    with pytest.raises(install.InstallFailure, match="distribution") as raised:
        install._target_environment(Path(sys.executable).resolve())

    assert raised.value.exit_code == install.EXIT_PREFLIGHT
    assert raised.value.stage == "python"


def test_verify_rejects_pythonpath_shadow_modules_after_install(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    report = install.plan("install", plugins_dir, None, None)
    install._install_from_report(report)

    shadow_root = tmp_path / "verify-shadow"
    package = shadow_root / "dcc_mcp_sketchup"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        f"__version__ = {install.__version__!r}\n",
        encoding="utf-8",
    )
    (package / "server.py").write_text(
        "from dcc_mcp_sketchup import __version__\n"
        "def main(*_args):\n"
        "    print(__version__)\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    existing = os.environ.get("PYTHONPATH")
    pythonpath = str(shadow_root) if not existing else str(shadow_root) + os.pathsep + existing
    monkeypatch.setenv("PYTHONPATH", pythonpath)
    monkeypatch.setattr(
        install,
        "wait_for_sidecar_ready",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("shadowed imports must fail before live readiness")
        ),
    )

    verification = install.verify_install(plugins_dir, Path(report["python"]), 0.0)

    assert verification["directly_usable"] is False
    assert verification["failure_stage"] == "import"
    assert "distribution" in verification["failure_reason"]


def test_verify_diagnoses_the_exact_stale_server_path(tmp_path):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    report = install.plan("install", plugins_dir, None, None)
    install._install_from_report(report)
    stale_server = tmp_path / "removed-environment" / "dcc-mcp-sketchup.exe"
    (plugins_dir / install.EXTENSION_DIRECTORY / "server_path.txt").write_text(
        str(stale_server),
        encoding="utf-8",
    )

    verification = install.verify_install(plugins_dir, Path(report["python"]), 0.0)

    assert verification["directly_usable"] is False
    assert verification["failure_stage"] == "server_path"
    assert str(stale_server) in verification["failure_reason"]
    assert "upgrade --yes" in verification["failure_reason"]


def test_verify_rejects_a_receipted_but_unloadable_sidecar(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    fake_server = tmp_path / "dcc-mcp-sketchup.exe"
    fake_server.write_bytes(b"arbitrary executable bytes")
    report = install.plan("install", plugins_dir, None, None)
    report["server_path"] = str(fake_server.resolve())
    report["server"] = install._sidecar_manifest(fake_server)
    install._install_from_report(report)
    monkeypatch.setattr(
        install,
        "_python_import_check",
        lambda _python, _identity: (_ for _ in ()).throw(
            AssertionError("unloadable sidecar must fail before import/readiness")
        ),
    )

    verification = install.verify_install(plugins_dir, Path(report["python"]), 0.0)

    assert verification["directly_usable"] is False
    assert verification["failure_stage"] == "server_path"
    assert "load/version" in verification["failure_reason"]


def test_verify_rejects_sidecar_bytes_tampered_after_receipt(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    report = install.plan("install", plugins_dir, None, None)
    copied_server = tmp_path / Path(report["server_path"]).name
    copied_server.write_bytes(Path(report["server_path"]).read_bytes())
    report["server_path"] = str(copied_server.resolve())
    report["server"] = install._sidecar_manifest(copied_server)
    install._install_from_report(report)
    copied_server.write_bytes(copied_server.read_bytes() + b"tampered")
    monkeypatch.setattr(
        install,
        "_python_import_check",
        lambda _python, _identity: (_ for _ in ()).throw(
            AssertionError("tampered sidecar must fail before import/readiness")
        ),
    )

    verification = install.verify_install(plugins_dir, Path(report["python"]), 0.0)

    assert verification["directly_usable"] is False
    assert verification["failure_stage"] == "server_path"
    assert "differ" in verification["failure_reason"]


def test_dcc_path_selects_the_matching_versioned_profile(tmp_path, monkeypatch):
    profiles = []
    for version in (2024, 2026):
        profile = tmp_path / "profiles" / f"SketchUp {version}" / "SketchUp" / "Plugins"
        profile.parent.mkdir(parents=True)
        profiles.append(profile)
    host = tmp_path / "Program Files" / "SketchUp" / "SketchUp 2024" / "SketchUp.exe"
    host.parent.mkdir(parents=True)
    host.write_bytes(b"MZ" + b"\0" * 512)
    monkeypatch.setattr(install, "discover_plugin_dirs", lambda: list(reversed(profiles)))
    monkeypatch.setattr(install, "_native_host_version", lambda _path: "2024.0.0")

    report, code, _ = install.run(["install", "--dcc-path", str(host), "--json", "--dry-run"])

    assert code == install.EXIT_OK
    assert report["sketchup_version"] == "2024"
    assert report["plugins_dir"] == str(profiles[0].resolve())
    assert report["dcc_path"] == str(host.resolve())


def test_status_marks_a_stale_server_path_as_repair(tmp_path):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    install._install_from_report(install.plan("install", plugins_dir, None, None))
    (plugins_dir / install.EXTENSION_DIRECTORY / "server_path.txt").write_text(
        str(tmp_path / "missing-sidecar.exe"),
        encoding="utf-8",
    )

    report, code, _ = install.run(["status", "--plugins-dir", str(plugins_dir), "--json"])

    assert code == install.EXIT_VERIFY
    assert report["status"] == "partial"
    assert report["installation_state"] == "repair"
    INSTALL_SOP_VALIDATOR.validate(report)


def test_loaded_install_root_uses_the_restart_exit_contract(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    monkeypatch.setattr(
        install,
        "inspect_install_root",
        lambda _path: {
            "requires_restart": True,
            "recommended_next_action": "Close SketchUp and retry the same command.",
        },
    )

    report, code, _ = install.run(["install", "--plugins-dir", str(plugins_dir), "--yes", "--json"])

    assert code == install.EXIT_REQUIRES_RESTART
    assert report["status"] == "requires_restart"
    assert report["verify"]["failure_reason"] == "Close SketchUp and retry the same command."
    INSTALL_SOP_VALIDATOR.validate(report)


def test_standard_entry_point_dispatches_status_json(tmp_path, capsys):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)

    server_module.main(["status", "--plugins-dir", str(plugins_dir), "--json"])

    payload = install.json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["verb"] == "status"
    assert payload["status"] == "ok"
    assert payload["installation_state"] == "fresh"


def test_standard_entry_point_help_lists_every_lifecycle_verb(capsys):
    with pytest.raises(SystemExit) as raised:
        server_module.main(["--help"])

    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    for verb in ("install", "status", "verify", "uninstall", "upgrade"):
        assert verb in help_text


def test_verify_requires_a_real_host_probe_and_emits_executable_recovery(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    install._install_from_report(install.plan("install", plugins_dir, None, None))
    monkeypatch.setattr(
        install,
        "_python_import_check",
        lambda _python, _identity: {
            "success": True,
            "importable": True,
            "version": install.__version__,
        },
    )
    probe = {}

    def unavailable(**kwargs):
        probe.update(kwargs)
        return {"success": False, "message": "no live SketchUp sidecar"}

    monkeypatch.setattr(install, "wait_for_sidecar_ready", unavailable)

    report, code, _ = install.run(["verify", "--plugins-dir", str(plugins_dir), "--json"])

    assert code == install.EXIT_VERIFY
    assert report["status"] == "failed"
    assert report["verify"]["directly_usable"] is False
    assert report["verify"]["failure_stage"] == "readiness"
    assert probe["dcc_type"] == "sketchup"
    assert probe["probe_tool"] == "sketchup_session__get_status"
    assert report["next_steps"]
    assert all("command" in step or "file_edit" in step for step in report["next_steps"])
    INSTALL_SOP_VALIDATOR.validate(report)


def test_verify_reports_usable_only_after_the_live_probe_succeeds(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    report = install.plan("install", plugins_dir, None, None)
    install._install_from_report(report)
    monkeypatch.setattr(
        install,
        "_python_import_check",
        lambda _python, _identity: {
            "success": True,
            "importable": True,
            "version": install.__version__,
        },
    )
    monkeypatch.setattr(
        install,
        "wait_for_sidecar_ready",
        lambda **_kwargs: readiness_success(plugins_dir),
    )

    verification = install.verify_install(plugins_dir, Path(report["python"]), 0.0)

    assert verification["directly_usable"] is True
    assert verification["failure_stage"] is None
    assert verification["artifact"]["success"] is True
    assert verification["import"]["success"] is True
    assert verification["readiness"]["success"] is True


def test_verify_rejects_a_foreign_2024_instance_for_the_2026_profile(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    report = install.plan("install", plugins_dir, None, None)
    install._install_from_report(report)
    monkeypatch.setattr(
        install,
        "_python_import_check",
        lambda _python, _identity: {
            "success": True,
            "importable": True,
            "version": install.__version__,
        },
    )
    monkeypatch.setattr(
        install,
        "wait_for_sidecar_ready",
        lambda **_kwargs: readiness_success(
            plugins_dir,
            year=2024,
            host_pid=2424,
            instance_id="foreign-2024",
        ),
    )

    verification = install.verify_install(
        plugins_dir,
        Path(report["python"]),
        0.0,
        instance_id="foreign-2024",
        host_pid=2424,
    )

    assert verification["directly_usable"] is False
    assert verification["failure_stage"] == "readiness_identity"
    assert "2026" in verification["failure_reason"]


@pytest.mark.parametrize("version", ["2026.0rc1", "9" * 10_000 + ".0"])
def test_verify_rejects_noncanonical_or_unbounded_readiness_versions(
    tmp_path,
    monkeypatch,
    version,
):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    report = install.plan("install", plugins_dir, None, None)
    install._install_from_report(report)
    monkeypatch.setattr(
        install,
        "_python_import_check",
        lambda _python, _identity: {
            "success": True,
            "importable": True,
            "version": install.__version__,
        },
    )
    readiness = readiness_success(plugins_dir)
    readiness["entry"]["version"] = version
    readiness["entry"]["metadata"]["dcc_version"] = version
    readiness["probe"]["result"]["structuredContent"]["context"]["sketchup_version"] = version
    monkeypatch.setattr(install, "wait_for_sidecar_ready", lambda **_kwargs: readiness)

    verification = install.verify_install(plugins_dir, Path(report["python"]), 0.0)

    assert verification["directly_usable"] is False
    assert verification["failure_stage"] == "readiness_identity"


@pytest.mark.parametrize(
    ("entry_instance", "entry_pid", "expected_fragment"),
    [
        ("other-instance", 4242, "instance"),
        ("sketchup-2026-4242", 9999, "PID"),
    ],
)
def test_verify_rejects_readiness_that_does_not_match_selected_identity(
    tmp_path,
    monkeypatch,
    entry_instance,
    entry_pid,
    expected_fragment,
):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    report = install.plan("install", plugins_dir, None, None)
    install._install_from_report(report)
    monkeypatch.setattr(
        install,
        "_python_import_check",
        lambda _python, _identity: {
            "success": True,
            "importable": True,
            "version": install.__version__,
        },
    )
    readiness = readiness_success(
        plugins_dir,
        host_pid=entry_pid,
        instance_id=entry_instance,
    )
    monkeypatch.setattr(install, "wait_for_sidecar_ready", lambda **_kwargs: readiness)

    verification = install.verify_install(
        plugins_dir,
        Path(report["python"]),
        0.0,
        instance_id="sketchup-2026-4242",
        host_pid=4242,
    )

    assert verification["directly_usable"] is False
    assert verification["failure_stage"] == "readiness_identity"
    assert expected_fragment.lower() in verification["failure_reason"].lower()


def test_verify_rejects_a_host_process_path_outside_the_receipt(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    report = install.plan("install", plugins_dir, None, None)
    install._install_from_report(report)
    receipt_path = plugins_dir / install.RECEIPT_RELATIVE_PATH
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    expected_host = tmp_path / "SketchUp 2026" / "SketchUp.exe"
    receipt["dcc_path"] = str(expected_host.resolve())
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    monkeypatch.setattr(
        install,
        "_python_import_check",
        lambda _python, _identity: {
            "success": True,
            "importable": True,
            "version": install.__version__,
        },
    )
    monkeypatch.setattr(
        install,
        "wait_for_sidecar_ready",
        lambda **_kwargs: readiness_success(plugins_dir),
    )
    monkeypatch.setattr(
        install,
        "_process_executable_path",
        lambda _pid: tmp_path / "SketchUp 2024" / "SketchUp.exe",
    )

    verification = install.verify_install(plugins_dir, Path(report["python"]), 0.0)

    assert verification["directly_usable"] is False
    assert verification["failure_stage"] == "readiness_identity"
    assert "host path" in verification["failure_reason"]


def test_verify_rejects_success_without_the_real_ruby_payload(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    report = install.plan("install", plugins_dir, None, None)
    install._install_from_report(report)
    monkeypatch.setattr(
        install,
        "_python_import_check",
        lambda _python, _identity: {
            "success": True,
            "importable": True,
            "version": install.__version__,
        },
    )
    readiness = readiness_success(plugins_dir)
    readiness["probe"]["result"].pop("structuredContent")
    monkeypatch.setattr(install, "wait_for_sidecar_ready", lambda **_kwargs: readiness)

    verification = install.verify_install(plugins_dir, Path(report["python"]), 0.0)

    assert verification["directly_usable"] is False
    assert verification["failure_stage"] == "readiness_identity"
    assert "Ruby" in verification["failure_reason"]


def test_locked_backup_move_preserves_install_and_returns_restart(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    initial = install.plan("install", plugins_dir, None, None)
    install._install_from_report(initial)
    target = plugins_dir / install.EXTENSION_DIRECTORY
    marker = target / "previous.rb"
    marker.write_text("keep", encoding="utf-8")
    receipt_path = plugins_dir / install.RECEIPT_RELATIVE_PATH
    receipt = install.json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["source_digest"] = "previous-release"
    receipt_path.write_text(install.json.dumps(receipt), encoding="utf-8")
    real_replace = install.os.replace

    def reject_locked_backup(source, destination):
        if Path(source) == target and ".backup-" in Path(destination).name:
            raise PermissionError("extension is locked")
        return real_replace(source, destination)

    monkeypatch.setattr(install.os, "replace", reject_locked_backup)

    report, code, _ = install.run(["upgrade", "--plugins-dir", str(plugins_dir), "--yes", "--json"])

    assert code == install.EXIT_REQUIRES_RESTART
    assert report["status"] == "requires_restart"
    assert marker.read_text(encoding="utf-8") == "keep"


def test_preflight_rejects_a_different_adapter_in_target_python(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    scripts = Path(install.sysconfig.get_path("scripts"))
    monkeypatch.setattr(
        install,
        "_target_environment",
        lambda python: {
            "python": str(python),
            "scripts": str(scripts),
            "core_version": install.MIN_CORE_VERSION,
            "adapter_version": "0.0.1",
        },
    )

    report, code, _ = install.run(
        ["install", "--plugins-dir", str(plugins_dir), "--json", "--dry-run"]
    )

    assert code == install.EXIT_PREFLIGHT
    assert report["verify"]["failure_stage"] == "python"
    assert "0.0.1" in report["verify"]["failure_reason"]
    assert install.__version__ in report["verify"]["failure_reason"]


@pytest.mark.parametrize(
    "adapter_version",
    [
        pytest.param("0.1.0rc1", id="prerelease"),
        pytest.param("dcc-mcp-sketchup 0.1.0", id="garbage-prefix"),
        pytest.param("0.01.0", id="zero-padded"),
        pytest.param("9" * 10_000 + ".1.0", id="bounded-before-int"),
    ],
)
def test_preflight_rejects_noncanonical_adapter_versions(
    tmp_path,
    monkeypatch,
    adapter_version,
):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    monkeypatch.setattr(
        install,
        "_target_environment",
        lambda python: {
            "python": str(python),
            "scripts": str(tmp_path / "Scripts"),
            "core_version": install.MIN_CORE_VERSION,
            "adapter_version": adapter_version,
        },
    )

    report, code, _ = install.run(
        ["install", "--plugins-dir", str(plugins_dir), "--json", "--dry-run"]
    )

    assert code == install.EXIT_PREFLIGHT
    assert report["verify"]["failure_stage"] == "python"


@pytest.mark.parametrize(
    "core_version",
    [
        pytest.param("0.20.14rc1", id="prerelease"),
        pytest.param("dcc-mcp-core 0.20.14", id="prefix"),
        pytest.param("0.019.91", id="zero-padded"),
        pytest.param("9" * 10_000 + ".19.91", id="bounded-before-int"),
    ],
)
def test_preflight_rejects_noncanonical_core_versions(tmp_path, monkeypatch, core_version):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    executable_name = (
        "dcc-mcp-sketchup.exe" if install.sys.platform == "win32" else "dcc-mcp-sketchup"
    )
    (scripts / executable_name).write_bytes(b"placeholder")
    monkeypatch.setattr(
        install,
        "_target_environment",
        lambda python: {
            "python": str(python),
            "scripts": str(scripts),
            "core_version": core_version,
            "adapter_version": install.__version__,
        },
    )

    report, code, _ = install.run(
        ["install", "--plugins-dir", str(plugins_dir), "--json", "--dry-run"]
    )

    assert code == install.EXIT_PREFLIGHT
    assert report["verify"]["failure_stage"] == "core"


@pytest.mark.parametrize("content", [b"", b"not a loadable sidecar"])
def test_preflight_rejects_zero_or_arbitrary_sidecar_files(tmp_path, monkeypatch, content):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    executable_name = (
        "dcc-mcp-sketchup.exe" if install.sys.platform == "win32" else "dcc-mcp-sketchup"
    )
    (scripts / executable_name).write_bytes(content)
    monkeypatch.setattr(
        install,
        "_target_environment",
        lambda python: {
            "python": str(python),
            "scripts": str(scripts),
            "core_version": install.MIN_CORE_VERSION,
            "adapter_version": install.__version__,
        },
    )

    report, code, _ = install.run(
        ["install", "--plugins-dir", str(plugins_dir), "--json", "--dry-run"]
    )

    assert code == install.EXIT_PREFLIGHT
    assert report["verify"]["failure_stage"] == "sidecar"
    assert report["verify"]["directly_usable"] is False


def test_preflight_rejects_a_fake_version_launcher_outside_distribution_ownership(
    tmp_path,
    monkeypatch,
):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    executable_name = (
        "dcc-mcp-sketchup.exe" if install.sys.platform == "win32" else "dcc-mcp-sketchup"
    )
    executable = scripts / executable_name
    executable.write_bytes(b"fake launcher that claims the current version")
    monkeypatch.setattr(
        install,
        "_target_environment",
        lambda python: {
            "python": str(python),
            "scripts": str(scripts),
            "core_version": install.MIN_CORE_VERSION,
            "adapter_version": install.__version__,
            "launcher_path": str(executable),
            "launcher_hash_mode": "sha256",
            "launcher_hash": "forged-record-digest",
        },
    )
    monkeypatch.setattr(
        install,
        "_run_bounded_command",
        lambda *_args, **_kwargs: {
            "success": True,
            "stdout": install.__version__,
            "stderr": "",
            "truncated": False,
        },
    )

    report, code, _ = install.run(
        ["install", "--plugins-dir", str(plugins_dir), "--json", "--dry-run"]
    )

    assert code == install.EXIT_PREFLIGHT
    assert report["verify"]["failure_stage"] == "sidecar"
    assert "metadata" in report["verify"]["failure_reason"]


@pytest.mark.parametrize("content", [b"", b"not a native SketchUp executable"])
def test_dcc_path_rejects_zero_or_arbitrary_host_files(tmp_path, monkeypatch, content):
    profile = tmp_path / "profiles" / "SketchUp 2026" / "SketchUp" / "Plugins"
    profile.parent.mkdir(parents=True)
    host = tmp_path / "Program Files" / "SketchUp" / "SketchUp 2026" / "SketchUp.exe"
    host.parent.mkdir(parents=True)
    host.write_bytes(content)
    monkeypatch.setattr(install, "discover_plugin_dirs", lambda: [profile])

    report, code, _ = install.run(["install", "--dcc-path", str(host), "--json", "--dry-run"])

    assert code == install.EXIT_PREFLIGHT
    assert report["verify"]["failure_stage"] == "host"


def test_dcc_path_rejects_noncanonical_native_host_version(tmp_path, monkeypatch):
    profile = tmp_path / "profiles" / "SketchUp 2026" / "SketchUp" / "Plugins"
    profile.parent.mkdir(parents=True)
    host = tmp_path / "Program Files" / "SketchUp" / "SketchUp 2026" / "SketchUp.exe"
    host.parent.mkdir(parents=True)
    host.write_bytes(b"MZ" + b"\0" * 512)
    monkeypatch.setattr(install, "discover_plugin_dirs", lambda: [profile])
    monkeypatch.setattr(install, "_native_host_version", lambda _path: "2026.0-rc1")

    report, code, _ = install.run(["install", "--dcc-path", str(host), "--json", "--dry-run"])

    assert code == install.EXIT_PREFLIGHT
    assert report["verify"]["failure_stage"] == "host_version"


def test_preflight_reports_an_unwritable_profile_with_exit_10(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    monkeypatch.setattr(install.os, "access", lambda *_args: False)

    report, code, _ = install.run(
        ["install", "--plugins-dir", str(plugins_dir), "--json", "--dry-run"]
    )

    assert code == install.EXIT_PREFLIGHT
    assert report["verify"]["failure_stage"] == "permissions"
    assert "not writable" in report["verify"]["failure_reason"]


def test_verify_rejects_an_interpreter_that_differs_from_the_receipt(tmp_path):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    install._install_from_report(install.plan("install", plugins_dir, None, None))
    other_python = tmp_path / "other-environment" / "python.exe"
    other_python.parent.mkdir()
    other_python.touch()

    verification = install.verify_install(plugins_dir, other_python, 0.0)

    assert verification["directly_usable"] is False
    assert verification["failure_stage"] == "interpreter"
    assert str(other_python) in verification["failure_reason"]
    assert "upgrade --yes" in verification["failure_reason"]


def test_verify_surfaces_the_latest_ruby_bootstrap_error(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    report = install.plan("install", plugins_dir, None, None)
    install._install_from_report(report)
    bootstrap_log = plugins_dir / install.BOOTSTRAP_ERRORS_RELATIVE_PATH
    bootstrap_log.parent.mkdir(parents=True)
    bootstrap_log.write_text(
        install.json.dumps(
            {
                "timestamp": "2026-08-24T00:00:00Z",
                "stage": "startup",
                "error_class": "RuntimeError",
                "message": "sidecar launch failed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        install,
        "_python_import_check",
        lambda _python, _identity: {
            "success": True,
            "importable": True,
            "version": install.__version__,
        },
    )
    monkeypatch.setattr(
        install,
        "wait_for_sidecar_ready",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("readiness must not hide bootstrap")
        ),
    )

    verification = install.verify_install(plugins_dir, Path(report["python"]), 0.0)

    assert verification["directly_usable"] is False
    assert verification["failure_stage"] == "bootstrap"
    assert "RuntimeError" in verification["failure_reason"]
    assert "sidecar launch failed" in verification["failure_reason"]


def test_default_plugin_dir_requires_a_discovered_profile(monkeypatch):
    monkeypatch.setattr(install, "discover_plugin_dirs", lambda: [])

    with pytest.raises(RuntimeError, match="start SketchUp once"):
        install.default_plugin_dir()


def test_plugin_directory_version_sorting():
    older = Path("C:/Users/example/AppData/Roaming/SketchUp/SketchUp 2024/SketchUp/Plugins")
    newer = Path("C:/Users/example/AppData/Roaming/SketchUp/SketchUp 2026/SketchUp/Plugins")

    assert install._plugin_dir_version(newer) > install._plugin_dir_version(older)


def test_find_server_executable_uses_interpreter_scripts_directory(tmp_path, monkeypatch):
    scripts_dir = tmp_path / "Scripts"
    scripts_dir.mkdir()
    executable = scripts_dir / "dcc-mcp-sketchup.exe"
    executable.write_bytes(b"launcher")
    monkeypatch.setattr(install.sys, "platform", "win32")
    monkeypatch.setattr(install.sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(install.sysconfig, "get_path", lambda name: str(scripts_dir))
    monkeypatch.setattr(install.site, "getuserbase", lambda: str(tmp_path / "user"))
    monkeypatch.setattr(install.shutil, "which", lambda name: None)

    assert install._find_server_executable() == executable.resolve()
