from pathlib import Path

import pytest

from dcc_mcp_sketchup import install
from dcc_mcp_sketchup import server as server_module


def test_install_and_uninstall_copy_only_owned_extension(tmp_path, monkeypatch):
    executable = tmp_path / "dcc-mcp-sketchup.exe"
    executable.touch()
    monkeypatch.setattr(install, "_find_server_executable", lambda: executable)
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
    executable = tmp_path / "dcc-mcp-sketchup.exe"
    executable.touch()
    monkeypatch.setattr(install, "_find_server_executable", lambda: executable)
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

    executable = tmp_path / "dcc-mcp-sketchup.exe"
    executable.touch()
    monkeypatch.setattr(install, "_find_server_executable", lambda: executable)
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
    assert report["schema_version"] == "1"
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
        lambda *_args: {
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


def test_receipt_failure_rolls_back_extension_registration_and_receipt(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    monkeypatch.setattr(
        install,
        "verify_install",
        lambda *_args: {
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
    assert marker.read_text(encoding="utf-8") == "previous extension"
    assert registration.read_text(encoding="utf-8") == "previous registration"
    assert receipt_path.read_bytes() == old_receipt


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


def test_dcc_path_selects_the_matching_versioned_profile(tmp_path, monkeypatch):
    profiles = []
    for version in (2024, 2026):
        profile = tmp_path / "profiles" / f"SketchUp {version}" / "SketchUp" / "Plugins"
        profile.parent.mkdir(parents=True)
        profiles.append(profile)
    host = tmp_path / "Program Files" / "SketchUp" / "SketchUp 2024" / "SketchUp.exe"
    host.parent.mkdir(parents=True)
    host.touch()
    monkeypatch.setattr(install, "discover_plugin_dirs", lambda: list(reversed(profiles)))

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
    assert report["status"] == "repair"
    assert report["installation_state"] == "repair"


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


def test_standard_entry_point_dispatches_status_json(tmp_path, capsys):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)

    server_module.main(["status", "--plugins-dir", str(plugins_dir), "--json"])

    payload = install.json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "1"
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
        lambda _python: {"success": True, "importable": True, "version": install.__version__},
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


def test_verify_reports_usable_only_after_the_live_probe_succeeds(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "SketchUp" / "SketchUp 2026" / "SketchUp" / "Plugins"
    plugins_dir.parent.mkdir(parents=True)
    report = install.plan("install", plugins_dir, None, None)
    install._install_from_report(report)
    monkeypatch.setattr(
        install,
        "_python_import_check",
        lambda _python: {"success": True, "importable": True, "version": install.__version__},
    )
    monkeypatch.setattr(
        install,
        "wait_for_sidecar_ready",
        lambda **_kwargs: {"success": True, "status": "ready", "probe": {"success": True}},
    )

    verification = install.verify_install(plugins_dir, Path(report["python"]), 0.0)

    assert verification["directly_usable"] is True
    assert verification["failure_stage"] is None
    assert verification["artifact"]["success"] is True
    assert verification["import"]["success"] is True
    assert verification["readiness"]["success"] is True


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
        lambda _python: {"success": True, "importable": True, "version": install.__version__},
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
