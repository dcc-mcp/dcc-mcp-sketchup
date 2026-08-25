import json
from importlib.metadata import version
from pathlib import Path

import pytest

import dcc_mcp_sketchup
import dcc_mcp_sketchup.server as server_module
from dcc_mcp_sketchup.server import SketchupMcpServer, _process_is_alive


def test_package_version_is_public_string():
    assert dcc_mcp_sketchup.__version__ == version("dcc-mcp-sketchup")


def test_release_please_tracks_every_generic_version_marker():
    repository_root = Path(__file__).parents[1]
    config = json.loads((repository_root / "release-please-config.json").read_text())
    tracked_paths = {
        entry["path"]
        for entry in config["packages"]["."]["extra-files"]
        if entry["type"] == "generic"
    }
    marked_paths = {
        path.relative_to(repository_root).as_posix()
        for path in (repository_root / "src").rglob("*")
        if path.suffix in {".md", ".py", ".rb"}
        and "x-release-please-version" in path.read_text(encoding="utf-8")
    }

    assert tracked_paths == marked_paths


def test_server_requires_a_live_host_pid():
    with pytest.raises(ValueError, match="live SketchUp host PID"):
        SketchupMcpServer(host_pid=0)


def test_current_process_is_alive():
    import os

    assert _process_is_alive(os.getpid()) is True


def test_readiness_monitor_stops_after_first_success(monkeypatch):
    class ProbeStop:
        def __init__(self):
            self.waits = 0

        def wait(self, _timeout):
            self.waits += 1
            return False

    connected = iter((False, True))
    readiness = []
    instance = SketchupMcpServer.__new__(SketchupMcpServer)
    instance._readiness_stop = ProbeStop()
    instance._set_bridge_readiness = readiness.append
    monkeypatch.setattr(server_module.bridge, "is_connected", lambda: next(connected))

    instance._monitor_bridge_readiness()

    assert instance._readiness_stop.waits == 2
    assert readiness == [True]
