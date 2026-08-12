import pytest

import dcc_mcp_sketchup
import dcc_mcp_sketchup.server as server_module
from dcc_mcp_sketchup.server import SketchupMcpServer, _process_is_alive


def test_package_version_is_public_string():
    assert dcc_mcp_sketchup.__version__ == "0.1.0"


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
