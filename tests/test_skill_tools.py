"""Host-context tests for the skill tool wrapper.

These cover the failure paths of the post-write read-back contract, which is
where the contract is either honoured or quietly dropped:

* a Ruby read-back that disagreed, raised by ``bridge.call`` before any result
  reaches :func:`require_verification`;
* a result that crossed the boundary with no evidence at all.

Both owe the caller the host version and the matrix verdict -- a read-back
disagreement is the classic signature of host API drift, and without the
product year the report cannot be reproduced. The failure to attach them was
found by review rather than by a test, which is why this file exists.
"""

from __future__ import annotations

import json
import socket
import threading

import pytest

from dcc_mcp_sketchup import skill_tools
from dcc_mcp_sketchup.bridge import BridgeConfig, SketchupBridge
from dcc_mcp_sketchup.write_contract import WriteVerificationError

# A product year inside the shipped matrix, so classify_host resolves it.
HOST_VERSION = "2026.0"


def _serve(handler):
    """Serve JSON-RPC responses until the handler stops returning them."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)

    def serve():
        try:
            while True:
                connection, _ = listener.accept()
                with connection:
                    request = json.loads(connection.makefile("r", encoding="utf-8").readline())
                    response = handler(request)
                    if response is None:
                        return
                    connection.sendall(json.dumps(response).encode("utf-8") + b"\n")
        except OSError:
            return
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return listener.getsockname()[1], thread


def _envelope(request, result=None, error=None):
    envelope = {"jsonrpc": "2.0", "id": request["id"]}
    if error is not None:
        envelope["error"] = error
    else:
        envelope["result"] = result
    return envelope


def _bridge(port):
    return SketchupBridge(BridgeConfig("127.0.0.1", port, "secret", 2.0))


def _install_bridge(monkeypatch, bridge):
    monkeypatch.setattr(skill_tools, "get_bridge", lambda: bridge)


def _error_payload(with_host=True):
    payload = {
        "schema_version": 1,
        "tool": "geometry.add_box",
        "check": "extents",
        "expected": [1.0, 2.0, 3.0],
        "actual": [1.0, 2.0, 9.0],
    }
    if with_host:
        payload["host_version"] = HOST_VERSION
    return payload


def _failure_result(monkeypatch, handler):
    """Run a mutating skill tool and return the error dict skill_entry produced."""
    port, thread = _serve(handler)
    _install_bridge(monkeypatch, _bridge(port))
    try:
        tool = skill_tools.bridge_main("geometry.add_box", "box added")
        return tool(width=1.0, depth=2.0, height=3.0)
    finally:
        thread.join(timeout=2)


def _error_meta(result):
    meta = result.get("_meta") or {}
    return meta.get("dcc.error") or {}


# --- a read-back disagreement raised by bridge.call -----------------------


def test_ruby_read_back_failure_carries_host_context(monkeypatch):
    """A Ruby read-back failure is a verified failure, so it owes host context.

    bridge.call raises this before require_verification ever runs, so wrapping
    the result would leave the most important failure class unannotated: the one
    where the host itself disagreed with the change.
    """

    def handler(request):
        if request["method"] == "geometry.add_box":
            return _envelope(
                request,
                error={
                    "code": "write_verification_failed",
                    "message": "read-back disagreed",
                    "data": _error_payload(),
                },
            )
        return _envelope(request, result={"sketchup_version": HOST_VERSION, "ready": True})

    result = _failure_result(monkeypatch, handler)

    assert result["success"] is False
    message = _error_meta(result).get("message") or ""
    assert "host SketchUp %s" % HOST_VERSION in message
    assert "matrix status: supported" in message


def test_ruby_read_back_failure_resolves_host_from_live_status(monkeypatch):
    """A bare error envelope is completed from the live host handshake.

    The Ruby side stamps its own version into payloads it builds, but the
    envelope can arrive without one; that is the case the extra round trip
    exists for, and it must not be reported as an unattributed mismatch.
    """
    probed = []

    def handler(request):
        probed.append(request["method"])
        if request["method"] == "geometry.add_box":
            return _envelope(
                request,
                error={
                    "code": "write_verification_failed",
                    "message": "read-back disagreed",
                    "data": _error_payload(with_host=False),
                },
            )
        return _envelope(request, result={"sketchup_version": HOST_VERSION, "ready": True})

    result = _failure_result(monkeypatch, handler)

    assert result["success"] is False
    message = _error_meta(result).get("message") or ""
    assert "host SketchUp %s" % HOST_VERSION in message
    # The handshake is a second call, and only because the payload lacked a version.
    assert probed.count("bridge.health") == 1


def test_read_back_failure_keeps_matrix_verdict_when_handshake_fails(monkeypatch):
    """A dead host must not erase a version the Ruby side already reported.

    The payload carries the version the failure was produced under. Losing the
    verdict because a follow-up status call failed would drop the one fact that
    makes the mismatch reproducible.
    """

    def handler(request):
        if request["method"] == "geometry.add_box":
            return _envelope(
                request,
                error={
                    "code": "write_verification_failed",
                    "message": "read-back disagreed",
                    "data": _error_payload(),
                },
            )
        raise AssertionError("no round trip is needed when the payload has a version")

    port, thread = _serve(handler)
    _install_bridge(monkeypatch, _bridge(port))
    try:
        tool = skill_tools.bridge_main("geometry.add_box", "box added")
        result = tool(width=1.0, depth=2.0, height=3.0)
    finally:
        thread.join(timeout=2)

    assert result["success"] is False
    message = _error_meta(result).get("message") or ""
    assert "host SketchUp %s" % HOST_VERSION in message
    assert "matrix status: supported" in message


# --- a result that arrived with no evidence -------------------------------


def test_missing_evidence_failure_carries_host_context(monkeypatch):
    """The require_verification path keeps the annotations it already had."""

    def handler(request):
        if request["method"] == "geometry.add_box":
            return _envelope(request, result={"id": 42})
        return _envelope(request, result={"sketchup_version": HOST_VERSION, "ready": True})

    result = _failure_result(monkeypatch, handler)

    assert result["success"] is False
    message = _error_meta(result).get("message") or ""
    assert "host SketchUp %s" % HOST_VERSION in message
    assert "matrix status: supported" in message
    # The remediation that explains what the Ruby side owes is still there.
    assert "The Ruby extension must re-read the mutated target" in message


def test_missing_evidence_message_survives_host_context(monkeypatch):
    """Attaching host context must not downgrade the explanation.

    require_verification raises with a message that explains *why* a missing
    block is a failure rather than a pass. format_message renders the
    expected/actual pair instead, so re-rendering on the way through host
    context would trade the explanation for a diff. The structured payload is
    the contract; the prose has to stay the one that tells the caller what to
    do.
    """

    def handler(request):
        if request["method"] == "geometry.add_box":
            return _envelope(request, result={"id": 42})
        return _envelope(request, result={"sketchup_version": HOST_VERSION, "ready": True})

    result = _failure_result(monkeypatch, handler)

    message = _error_meta(result).get("message") or ""
    assert "did not take effect: the post-write read-back disagreed" in message
    assert "returned without post-write read-back evidence" not in message
    assert "host SketchUp %s" % HOST_VERSION in message


def test_unresolvable_host_degrades_to_the_original_error(monkeypatch):
    """A missing version must not mask the read-back disagreement.

    The disagreement is the finding; an unattributable version only makes it
    less reproducible, never less true.
    """

    def handler(request):
        return _envelope(
            request,
            error={
                "code": "write_verification_failed",
                "message": "read-back disagreed",
                "data": _error_payload(with_host=False),
            },
        )

    result = _failure_result(monkeypatch, handler)

    assert result["success"] is False
    message = _error_meta(result).get("message") or ""
    assert "expected [1.0, 2.0, 3.0]" in message
    assert "read back [1.0, 2.0, 9.0]" in message


# --- the read-only path is untouched --------------------------------------


def test_read_only_results_are_not_verified(monkeypatch):
    """Read-only methods owe no read-back and must not pay a status round trip."""

    def handler(request):
        assert request["method"] == "model.inspect"
        return _envelope(request, result={"entities": 3})

    port, thread = _serve(handler)
    _install_bridge(monkeypatch, _bridge(port))
    try:
        tool = skill_tools.bridge_main("model.inspect", "model inspected")
        result = tool()
    finally:
        thread.join(timeout=2)

    assert result["success"] is True


def test_with_host_context_rejects_an_unusable_bridge():
    class _BrokenBridge:
        def status(self):
            raise OSError("host went away")

    error = WriteVerificationError("geometry.add_box", "extents", 1.0, 9.0)

    assert skill_tools._with_host_context(_BrokenBridge(), error) is error


def test_every_mutating_tool_wraps_its_bridge_call():
    """The guard for the gap this file was written for.

    If bridge.call ever stops being wrapped, a Ruby read-back failure escapes
    without host context again -- and nothing else in the suite would notice.
    """
    import inspect

    source = inspect.getsource(skill_tools)
    assert "except WriteVerificationError" in source
    assert source.count("_with_host_context") >= 2


@pytest.mark.parametrize("method", ["geometry.add_box", "materials.create"])
def test_mutating_tools_are_classified_as_mutating(method):
    assert skill_tools.classify(method) == skill_tools.MUTATING
