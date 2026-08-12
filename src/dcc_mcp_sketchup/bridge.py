"""Authenticated loopback client for the SketchUp Ruby extension."""

from __future__ import annotations

import hmac
import json
import os
import socket
import time
from dataclasses import dataclass
from typing import Any, Optional
from uuid import uuid4

MAX_MESSAGE_BYTES = 1024 * 1024
_LOOPBACK_HOSTS = {"127.0.0.1"}


class BridgeError(RuntimeError):
    """Raised when the SketchUp host bridge rejects or cannot complete a call."""


@dataclass(frozen=True)
class BridgeConfig:
    """Validated connection settings shared by all SketchUp skill wrappers."""

    host: str
    port: int
    token: str
    timeout: float = 620.0

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        host = os.environ.get("DCC_MCP_SKETCHUP_BRIDGE_HOST", "127.0.0.1").strip()
        port_text = os.environ.get("DCC_MCP_SKETCHUP_BRIDGE_PORT", "0").strip()
        token = os.environ.get("DCC_MCP_SKETCHUP_BRIDGE_TOKEN", "")
        timeout_text = os.environ.get("DCC_MCP_SKETCHUP_BRIDGE_TIMEOUT", "620").strip()
        try:
            port = int(port_text)
        except ValueError as exc:
            raise BridgeError("DCC_MCP_SKETCHUP_BRIDGE_PORT must be an integer") from exc
        try:
            timeout = float(timeout_text)
        except ValueError as exc:
            raise BridgeError("DCC_MCP_SKETCHUP_BRIDGE_TIMEOUT must be numeric") from exc
        return cls(host=host, port=port, token=token, timeout=timeout)

    def __post_init__(self) -> None:
        if self.host.casefold() not in _LOOPBACK_HOSTS:
            raise BridgeError("SketchUp bridge host must be loopback")
        if not 1 <= self.port <= 65535:
            raise BridgeError("DCC_MCP_SKETCHUP_BRIDGE_PORT must be between 1 and 65535")
        if not self.token:
            raise BridgeError("DCC_MCP_SKETCHUP_BRIDGE_TOKEN is required")
        if not 0 < self.timeout <= 3600:
            raise BridgeError("DCC_MCP_SKETCHUP_BRIDGE_TIMEOUT must be between 0 and 3600 seconds")


class SketchupBridge:
    """Call the bounded command map owned by one SketchUp process."""

    def __init__(self, config: BridgeConfig):
        self._config = config

    @classmethod
    def from_env(cls) -> "SketchupBridge":
        return cls(BridgeConfig.from_env())

    @property
    def endpoint(self) -> str:
        return f"{self._config.host}:{self._config.port}"

    def status(self) -> dict[str, Any]:
        try:
            result = self.call("bridge.health", timeout=min(self._config.timeout, 8.0))
        except (BridgeError, OSError, ValueError) as exc:
            return {
                "ready": False,
                "bridge": self.endpoint,
                "mode": "SketchUp Ruby extension + external DCC-MCP sidecar",
                "error": str(exc),
            }
        if not isinstance(result, dict):
            raise BridgeError("SketchUp bridge health response must be an object")
        return {
            "ready": True,
            "bridge": self.endpoint,
            "mode": "SketchUp Ruby extension + external DCC-MCP sidecar",
            **result,
        }

    def call(self, method: str, *, timeout: Optional[float] = None, **params: Any) -> Any:
        if not isinstance(method, str) or not method.strip():
            raise ValueError("method must be a non-empty string")
        request_timeout = self._config.timeout if timeout is None else float(timeout)
        if not 0 < request_timeout <= 3600:
            raise ValueError("timeout must be between 0 and 3600 seconds")

        request_id = uuid4().hex
        request_params = dict(params)
        if method != "bridge.health":
            request_params["_dcc_mcp_deadline_unix_ms"] = int(
                (time.time() + request_timeout) * 1000
            )
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "token": self._config.token,
                "method": method,
                "params": request_params,
            },
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(payload) > MAX_MESSAGE_BYTES:
            raise ValueError("SketchUp bridge request exceeds 1 MiB")

        try:
            with socket.create_connection(
                (self._config.host, self._config.port),
                timeout=min(request_timeout, 10.0),
            ) as connection:
                connection.settimeout(request_timeout)
                connection.sendall(payload + b"\n")
                response = _read_line(connection)
        except OSError as exc:
            raise BridgeError(f"SketchUp bridge unavailable at {self.endpoint}") from exc

        try:
            envelope = json.loads(response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError("SketchUp bridge returned invalid JSON") from exc
        if not isinstance(envelope, dict):
            raise BridgeError("SketchUp bridge returned a non-object response")
        if envelope.get("jsonrpc") != "2.0":
            raise BridgeError("SketchUp bridge returned an invalid JSON-RPC version")
        response_id = str(envelope.get("id", ""))
        if not hmac.compare_digest(response_id, request_id):
            raise BridgeError(
                "SketchUp bridge response id does not match the request "
                f"(expected {request_id}, received {response_id or '<missing>'})"
            )
        error = envelope.get("error")
        if error:
            if isinstance(error, dict):
                code = str(error.get("code") or "host_error")
                message = str(error.get("message") or "SketchUp command failed")
                raise BridgeError(f"{code}: {message}")
            raise BridgeError(str(error))
        if "result" not in envelope:
            raise BridgeError("SketchUp bridge response is missing result")
        return envelope["result"]


def _read_line(connection: socket.socket) -> bytes:
    chunks = bytearray()
    while len(chunks) <= MAX_MESSAGE_BYTES:
        chunk = connection.recv(min(65536, MAX_MESSAGE_BYTES + 1 - len(chunks)))
        if not chunk:
            break
        newline = chunk.find(b"\n")
        chunks.extend(chunk if newline < 0 else chunk[:newline])
        if newline >= 0:
            break
    if len(chunks) > MAX_MESSAGE_BYTES:
        raise BridgeError("SketchUp bridge response exceeds 1 MiB")
    if not chunks:
        raise BridgeError("SketchUp bridge closed without a response")
    return bytes(chunks)


def get_bridge() -> SketchupBridge:
    return SketchupBridge.from_env()


def is_connected() -> bool:
    """Return whether the configured SketchUp bridge completed a health call."""
    try:
        return bool(get_bridge().status().get("ready"))
    except (BridgeError, OSError, ValueError):
        return False
