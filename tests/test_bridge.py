import json
import socket
import threading

from dcc_mcp_sketchup.bridge import SketchupBridge


def test_bridge_request():
    received = {}

    def serve(listener):
        conn, _ = listener.accept()
        with conn:
            received["request"] = json.loads(
                conn.makefile("r", encoding="utf-8").readline()
            )
            conn.sendall(b'{"jsonrpc":"2.0","id":1,"result":{"ready":true}}\n')

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    threading.Thread(target=serve, args=(listener,), daemon=True).start()
    adapter = SketchupBridge(port=listener.getsockname()[1])
    assert adapter.call("sketchup.ping") == {"ready": True}
    assert received["request"]["method"] == "sketchup.ping"
