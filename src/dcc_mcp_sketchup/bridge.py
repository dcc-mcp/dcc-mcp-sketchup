from __future__ import annotations

import json
import socket
from typing import Any


class BridgeError(RuntimeError):
    pass


class SketchupBridge:
    def __init__(self, host="127.0.0.1", port=3854, timeout=10.0):
        self.host, self.port, self.timeout = host, port, timeout

    @classmethod
    def from_env(cls):
        import os

        return cls(
            os.environ.get("DCC_MCP_sketchup_BRIDGE_HOST", "127.0.0.1"),
            int(os.environ.get("DCC_MCP_sketchup_BRIDGE_PORT", "3854")),
            float(os.environ.get("DCC_MCP_sketchup_BRIDGE_TIMEOUT", "10")),
        )

    def status(self) -> dict[str, Any]:
        return {
            "ready": False,
            "bridge": f"{self.host}:{self.port}",
            "mode": "Ruby extension",
            "note": "Host bridge live smoke pending",
        }

    def call(self, method: str, **params: Any) -> Any:
        request = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            ) as conn:
                conn.sendall((request + "\n").encode())
                line = conn.makefile("r", encoding="utf-8").readline()
        except OSError as exc:
            raise BridgeError(
                f"SketchUp bridge unavailable at {self.host}:{self.port}"
            ) from exc
        if not line:
            raise BridgeError("Host bridge closed the connection")
        payload = json.loads(line)
        if "error" in payload:
            raise BridgeError(str(payload["error"]))
        return payload.get("result")


def get_bridge():
    return SketchupBridge.from_env()
