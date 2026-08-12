from pathlib import Path

import pytest

from dcc_mcp_sketchup import install


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
