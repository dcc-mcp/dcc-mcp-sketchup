"""Post-write read-back contract for mutating SketchUp tools.

The failure mode this module exists to eliminate is **"reported success, model
unchanged"**. In an agent loop that is the most expensive class of bug there is:
the caller receives an affirmative answer, keeps building on it, and the error
only surfaces steps later as an unrelated symptom.

The contract is one sentence:

    A mutating tool returns only after the target has been read back and this
    call's change is proven to be there.

Anything else is a bug.

Where the read-back happens
---------------------------
The SketchUp adapter is two runtimes, not one. The Python sidecar marshals a
JSON-RPC call over loopback; a Ruby extension inside SketchUp performs it on the
SketchUp main thread. Python cannot read the model, so **the read-back must run
on the Ruby side** -- but a read-back that only Ruby knows about is not a
contract, because nothing on the caller's side proves it happened.

So the contract is enforced at both ends and crosses the boundary once:

1. **Ruby performs the check** (``sketchup_plugin/verification.rb``). After the
   mutation it re-reads the target through the model and builds a list of
   ``{check, expected, actual}`` triples. A disagreement raises a structured
   error that the runtime serialises with ``code = write_verification_failed``
   and the full payload under ``data``.
2. **Ruby reports the evidence.** Every mutating command's result carries a
   ``verification`` block with ``verified: true`` and the checks that passed.
3. **Python proves the evidence arrived.** :func:`require_verification` runs on
   the caller's side of the boundary and rejects any mutating result that does
   not carry a verified block. A Ruby side that silently stopped verifying, or a
   response that never crossed the wire, fails here rather than being read as
   success.

Step 3 is what makes this a boundary contract instead of a Ruby implementation
detail.

Two properties matter more than the exact checks:

* **Expected and actual are always both reported.** A mismatch that only says
  "failed" makes the caller guess; the pair is what makes it actionable.
* **The host version is always attached.** A read-back that disagrees is the
  classic signature of host API drift, and without the product year the report
  is unreproducible.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional

SCHEMA_VERSION = 1

# The error code the Ruby side uses when a read-back disagrees. It has to match
# sketchup_plugin/verification.rb exactly, because bridge.py keys off it to
# rebuild the structured payload on this side of the boundary.
WRITE_VERIFICATION_CODE = "write_verification_failed"

# Floating point read-back tolerance. SketchUp stores lengths internally in
# inches as doubles and the Ruby API reports bounds in inches, so the tolerance
# absorbs serialisation noise only. It is deliberately far tighter than any
# modelling-relevant delta: one inch is 25.4 mm, and 1e-6 relative on a
# metre-scale part is a fraction of a micron.
DEFAULT_REL_TOLERANCE = 1e-6
DEFAULT_ABS_TOLERANCE = 1e-9

MUTATING = "mutating"
READ_ONLY = "read_only"

# Tools that change the model or write a file. Every entry owes a read-back.
MUTATING_TOOLS = (
    "model.save",
    "model.save_copy",
    "model.import",
    "model.export",
    "geometry.add_box",
    "geometry.add_cylinder",
    "geometry.group",
    "entity.transform",
    "entity.rename",
    "entity.erase",
    "entity.select",
    "materials.create",
    "materials.update",
    "materials.assign",
    "materials.remove",
    "scenes.create",
    "scenes.update",
    "scenes.remove",
    "tags.create",
    "tags.assign",
    "tags.remove",
)

# Tools that observe state and change nothing. Kept here so the classification
# test can prove no method is left unclassified.
READ_ONLY_TOOLS = (
    "diagnostics.ping",
    "diagnostics.api_probe",
    # Not in the Ruby command map: runtime.rb routes bridge.health to
    # diagnostics.ping on the SketchUp main thread. It is listed because the
    # get_status skill calls it by name, and an unclassified method would raise
    # at skill import -- which is how this entry was found.
    "bridge.health",
    "model.inspect",
    "model.list_entities",
    "model.validate",
    "materials.list",
    "scenes.list",
    "tags.list",
)

TOOL_CLASSIFICATION_ERROR = (
    "every bridge method must be listed in write_contract.MUTATING_TOOLS or "
    "write_contract.READ_ONLY_TOOLS; an unclassified method has no answer to "
    "'does this owe a post-write read-back?'"
)


class ToolClassificationError(AssertionError):
    """A bridge method was added without deciding whether it owes a read-back."""


def classify(method: str) -> str:
    """Return ``MUTATING`` or ``READ_ONLY`` for a bridge method.

    Raises :class:`ToolClassificationError` for anything unlisted, so adding a
    command to the Ruby map fails the Python test suite until the author has
    decided whether it changes the model.
    """
    if method in MUTATING_TOOLS:
        return MUTATING
    if method in READ_ONLY_TOOLS:
        return READ_ONLY
    raise ToolClassificationError("%s: %s" % (method, TOOL_CLASSIFICATION_ERROR))


def is_mutating(method: str) -> bool:
    return method in MUTATING_TOOLS


def unclassified_tools(methods: Iterable[str]) -> List[str]:
    """Return the methods from ``methods`` that appear in neither tool list."""
    known = set(MUTATING_TOOLS) | set(READ_ONLY_TOOLS)
    return sorted(set(methods) - known)


def jsonable(value: Any) -> Any:
    """Coerce ``value`` into something ``json.dump`` accepts.

    Read-back evidence crosses a process boundary, so anything that cannot be
    represented in JSON is rendered as text rather than dropped: a dropped field
    is how a report ends up saying "expected something, got something".
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # NaN/Infinity are not valid JSON; keep them visible as text.
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    return repr(value)


def _describe(value: Any) -> str:
    """Render one side of an expected/actual pair compactly."""
    if isinstance(value, (list, tuple)):
        return "[%s]" % ", ".join(_describe(item) for item in value)
    if isinstance(value, dict):
        return "{%s}" % ", ".join("%s: %s" % (key, _describe(item)) for key, item in value.items())
    if isinstance(value, float):
        return repr(value)
    return str(value)


def numbers_match(
    expected: Any,
    actual: Any,
    rel_tolerance: Optional[float] = None,
    abs_tolerance: Optional[float] = None,
) -> bool:
    """Compare two scalars with the contract's default tolerance."""
    try:
        return math.isclose(
            float(expected),
            float(actual),
            rel_tol=DEFAULT_REL_TOLERANCE if rel_tolerance is None else rel_tolerance,
            abs_tol=DEFAULT_ABS_TOLERANCE if abs_tolerance is None else abs_tolerance,
        )
    except (TypeError, ValueError):
        return False


def sequences_match(
    expected: Any,
    actual: Any,
    rel_tolerance: Optional[float] = None,
    abs_tolerance: Optional[float] = None,
) -> bool:
    """Compare two numeric sequences element by element.

    A length mismatch is a mismatch, not a truncated comparison: reporting three
    coordinates against two would hide the difference.
    """
    try:
        expected = [float(item) for item in expected]
        actual = [float(item) for item in actual]
    except (TypeError, ValueError):
        return False
    if len(expected) != len(actual):
        return False
    return all(
        numbers_match(item, other, rel_tolerance, abs_tolerance)
        for item, other in zip(expected, actual)
    )


def format_message(payload: Dict[str, Any]) -> str:
    """Render the human- and agent-readable sentence for a mismatch.

    Deliberately states the tool, the check, both values, and the host version
    in that order: the reader should never have to re-run the call to find out
    what differed.
    """
    tool = payload.get("tool") or "unknown tool"
    check = payload.get("check") or "unknown check"
    message = (
        "%s did not take effect: the post-write read-back disagreed on %s "
        "(expected %s, read back %s)"
        % (
            tool,
            check,
            _describe(payload.get("expected")),
            _describe(payload.get("actual")),
        )
    )
    version = payload.get("host_version")
    if version:
        message += "; host SketchUp %s" % version
    matrix = payload.get("host_matrix") or {}
    if matrix.get("status"):
        message += " (matrix status: %s)" % matrix["status"]
    remediation = payload.get("remediation")
    if remediation:
        message += ". %s" % remediation
    return message


def missing_verification_message(tool: str, host_version: Optional[str] = None) -> str:
    """Explain why a mutating result without evidence is a failure, not a pass."""
    message = (
        "%s returned without post-write read-back evidence, so the change is unproven. "
        "A mutating SketchUp tool must return a verified read-back block; treating a "
        "missing block as success is exactly the silent-failure mode this contract exists "
        "to remove." % tool
    )
    if host_version:
        message += " Host SketchUp %s." % host_version
    return message


class WriteVerificationError(RuntimeError):
    """A mutating tool reported success but the read-back disagreed.

    The structured :attr:`payload` travels with the exception so the boundary
    that serialises the error (the Ruby runtime, then ``bridge.py``) can forward
    it verbatim and the caller can branch on ``check``/``expected``/``actual``
    instead of parsing prose.
    """

    def __init__(
        self,
        tool: str,
        check: str,
        expected: Any = None,
        actual: Any = None,
        host_version: Optional[str] = None,
        host_matrix: Any = None,
        params: Any = None,
        remediation: Optional[str] = None,
        message: Optional[str] = None,
    ):
        self.payload: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "tool": tool,
            "check": check,
            "expected": jsonable(expected),
            "actual": jsonable(actual),
            "host_version": host_version,
            "host_matrix": jsonable(host_matrix),
            "params": jsonable(params),
            "remediation": remediation,
        }
        super().__init__(message or format_message(self.payload))

    @property
    def tool(self) -> Any:
        return self.payload.get("tool")

    @property
    def check(self) -> Any:
        return self.payload.get("check")

    @property
    def expected(self) -> Any:
        return self.payload.get("expected")

    @property
    def actual(self) -> Any:
        return self.payload.get("actual")

    @property
    def host_version(self) -> Any:
        return self.payload.get("host_version")

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "WriteVerificationError":
        """Rebuild the error on the caller's side of a process boundary."""
        return cls(
            tool=payload.get("tool") or "unknown",
            check=payload.get("check") or "unknown",
            expected=payload.get("expected"),
            actual=payload.get("actual"),
            host_version=payload.get("host_version"),
            host_matrix=payload.get("host_matrix"),
            params=payload.get("params"),
            remediation=payload.get("remediation"),
            message=format_message(payload),
        )


def require_verification(
    method: str,
    result: Any,
    host_version: Optional[str] = None,
    host_matrix: Any = None,
) -> Dict[str, Any]:
    """Enforce the read-back contract on the caller's side of the bridge.

    ``result`` is the decoded JSON-RPC result for a mutating method. It must be
    an object carrying ``verification.verified == True``. Anything else -- a
    scalar, a missing block, ``verified: false``, or a block whose checks are
    empty -- raises :class:`WriteVerificationError`.

    Returning the verification block lets callers echo the evidence, and
    returning it unchanged keeps the happy path free of extra allocation.
    """
    verification = result.get("verification") if isinstance(result, dict) else None
    if not isinstance(verification, dict) or verification.get("verified") is not True:
        raise WriteVerificationError(
            tool=method,
            check="read_back_present",
            expected={"verification": {"verified": True}},
            actual=verification if verification is not None else result,
            host_version=host_version,
            host_matrix=host_matrix,
            remediation=(
                "The Ruby extension must re-read the mutated target and return a "
                "verification block; check the SketchUp Ruby Console for a read-back "
                "error recorded before the operation was committed."
            ),
            message=missing_verification_message(method, host_version),
        )
    checks = verification.get("checks")
    if not isinstance(checks, list) or not checks:
        raise WriteVerificationError(
            tool=method,
            check="read_back_checks",
            expected="a non-empty list of read-back checks",
            actual=checks,
            host_version=host_version,
            host_matrix=host_matrix,
            remediation=(
                "A verification block with no checks proves nothing; the Ruby side must "
                "record at least one expected/actual comparison."
            ),
        )
    return verification
