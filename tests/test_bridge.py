import json
import socket
import threading

import pytest

from dcc_mcp_sketchup.bridge import BridgeConfig, BridgeError, SketchupBridge


def _serve_once(response_factory):
    received = {}
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve():
        try:
            connection, _ = listener.accept()
            with connection:
                request = json.loads(connection.makefile("r", encoding="utf-8").readline())
                received["request"] = request
                response = response_factory(request)
                connection.sendall(json.dumps(response).encode("utf-8") + b"\n")
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return listener.getsockname()[1], received, thread


def test_bridge_sends_authenticated_request_with_deadline():
    port, received, thread = _serve_once(
        lambda request: {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"ready": True},
        }
    )
    adapter = SketchupBridge(BridgeConfig("127.0.0.1", port, "secret", 2.0))

    assert adapter.call("model.inspect") == {"ready": True}
    thread.join(timeout=2)
    request = received["request"]
    assert request["token"] == "secret"
    assert request["method"] == "model.inspect"
    assert request["params"]["_dcc_mcp_deadline_unix_ms"] > 0
    assert isinstance(request["id"], str) and request["id"]


def test_bridge_rejects_response_id_mismatch():
    port, _, _ = _serve_once(lambda _request: {"jsonrpc": "2.0", "id": "wrong-id", "result": {}})
    adapter = SketchupBridge(BridgeConfig("127.0.0.1", port, "secret", 2.0))

    with pytest.raises(
        BridgeError,
        match=r"response id does not match.*expected [0-9a-f]{32}, received wrong-id",
    ):
        adapter.call("model.inspect")


def test_bridge_surfaces_structured_host_error():
    port, _, _ = _serve_once(
        lambda request: {
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {"code": "invalid_request", "message": "bad input"},
        }
    )
    adapter = SketchupBridge(BridgeConfig("127.0.0.1", port, "secret", 2.0))

    with pytest.raises(BridgeError, match="invalid_request: bad input"):
        adapter.call("model.inspect")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"host": "0.0.0.0", "port": 1234, "token": "x"}, "loopback"),
        ({"host": "localhost", "port": 1234, "token": "x"}, "loopback"),
        ({"host": "127.0.0.1", "port": 0, "token": "x"}, "between 1 and 65535"),
        ({"host": "127.0.0.1", "port": 1234, "token": ""}, "TOKEN is required"),
    ],
)
def test_bridge_config_rejects_unsafe_values(kwargs, message):
    with pytest.raises(BridgeError, match=message):
        BridgeConfig(**kwargs)


def test_status_reports_live_bridge():
    port, _, _ = _serve_once(
        lambda request: {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"status": "ok", "sketchup_version": "2026.0"},
        }
    )
    adapter = SketchupBridge(BridgeConfig("127.0.0.1", port, "secret", 2.0))

    status = adapter.status()

    assert status["ready"] is True
    assert status["sketchup_version"] == "2026.0"
    assert "external DCC-MCP sidecar" in status["mode"]


def test_bridge_rejects_invalid_jsonrpc_response_version():
    port, _, _ = _serve_once(lambda request: {"jsonrpc": "1.0", "id": request["id"], "result": {}})
    adapter = SketchupBridge(BridgeConfig("127.0.0.1", port, "secret", 2.0))

    with pytest.raises(BridgeError, match="JSON-RPC version"):
        adapter.call("model.inspect")
