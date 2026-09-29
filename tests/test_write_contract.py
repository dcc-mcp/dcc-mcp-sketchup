"""Contract tests for the post-write read-back contract."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dcc_mcp_sketchup import write_contract

ROOT = Path(__file__).parents[1]
COMMANDS_SOURCE = ROOT / "src" / "dcc_mcp_sketchup" / "sketchup_plugin" / "commands.rb"
VERIFICATION_SOURCE = ROOT / "src" / "dcc_mcp_sketchup" / "sketchup_plugin" / "verification.rb"


def ruby_commands() -> list[str]:
    source = COMMANDS_SOURCE.read_text(encoding="utf-8")
    return sorted(set(re.findall(r"'([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)' => method", source)))


def skill_methods() -> list[str]:
    methods = []
    for script in sorted((ROOT / "src" / "dcc_mcp_sketchup" / "skills").glob("*/scripts/*.py")):
        source = script.read_text(encoding="utf-8")
        methods.extend(re.findall(r'bridge_main\(\s*"([^"]+)"', source))
    return sorted(set(methods))


def test_every_bridge_method_is_classified():
    """An unclassified method has no answer to "does this owe a read-back?".

    This is the guard that makes the contract survive new commands: add one to
    the Ruby map and this test fails until the author decides.
    """
    unclassified = write_contract.unclassified_tools(ruby_commands())

    assert unclassified == [], write_contract.TOOL_CLASSIFICATION_ERROR


def test_classify_rejects_unknown_methods():
    assert write_contract.classify("geometry.add_box") == write_contract.MUTATING
    assert write_contract.classify("model.inspect") == write_contract.READ_ONLY
    with pytest.raises(write_contract.ToolClassificationError):
        write_contract.classify("model.something_new")


def test_every_skill_method_is_a_known_bridge_method():
    assert set(skill_methods()) <= set(write_contract.MUTATING_TOOLS) | set(
        write_contract.READ_ONLY_TOOLS
    )


def test_mutating_tools_are_not_listed_as_read_only():
    overlap = set(write_contract.MUTATING_TOOLS) & set(write_contract.READ_ONLY_TOOLS)

    assert overlap == set()


def test_error_code_matches_the_ruby_side():
    """bridge.py keys off this string to rebuild the structured payload.

    A rename on either side would silently downgrade a structured mismatch to
    prose, which is exactly what the contract exists to prevent.
    """
    ruby = VERIFICATION_SOURCE.read_text(encoding="utf-8")
    declared = re.search(r"ERROR_CODE\s*=\s*'([^']+)'", ruby)

    assert declared is not None
    assert declared.group(1) == write_contract.WRITE_VERIFICATION_CODE


# --- comparison helpers ---------------------------------------------------


def test_numbers_match_uses_a_tight_tolerance():
    assert write_contract.numbers_match(1.0, 1.0)
    assert write_contract.numbers_match(1.0, 1.0 + 1e-12)
    assert not write_contract.numbers_match(1.0, 1.001)
    # Numeric strings coerce: read-back evidence crosses a JSON boundary where
    # a number may arrive as text. Genuinely non-numeric values must not.
    assert write_contract.numbers_match(1.0, "1.0")
    assert not write_contract.numbers_match(1.0, "one")
    assert not write_contract.numbers_match(1.0, None)


def test_sequences_match_treats_length_as_significant():
    assert write_contract.sequences_match([1, 2, 3], [1, 2, 3])
    assert not write_contract.sequences_match([1, 2, 3], [1, 2])
    assert not write_contract.sequences_match([1, 2], [1, 2, 3])


def test_jsonable_keeps_non_finite_floats_visible():
    assert write_contract.jsonable(float("nan")) == repr(float("nan"))
    assert write_contract.jsonable({"a": (1, 2)}) == {"a": [1, 2]}


# --- the boundary enforcer ------------------------------------------------


def verified_result(**extra):
    payload = {
        "verification": {
            "verified": True,
            "tool": "geometry.add_box",
            "host_version": "2026.0",
            "checks": [{"check": "bounds_extents", "expected": [1, 1, 1], "actual": [1, 1, 1]}],
        }
    }
    payload.update(extra)
    return payload


def test_require_verification_accepts_a_verified_block():
    verification = write_contract.require_verification("geometry.add_box", verified_result())

    assert verification["verified"] is True
    assert verification["checks"]


def test_require_verification_rejects_a_mutating_result_without_evidence():
    """A missing block is a failure, not a pass.

    This is the whole point of enforcing on the caller's side: a Ruby side that
    silently stopped verifying must fail here rather than be read as success.
    """
    with pytest.raises(write_contract.WriteVerificationError) as caught:
        write_contract.require_verification("geometry.add_box", {"entity": {"persistent_id": 1}})

    assert caught.value.check == "read_back_present"
    assert "unproven" in str(caught.value)


@pytest.mark.parametrize("result", [None, "ok", 42, [1, 2]])
def test_require_verification_rejects_non_object_results(result):
    with pytest.raises(write_contract.WriteVerificationError):
        write_contract.require_verification("geometry.add_box", result)


def test_require_verification_rejects_an_unverified_block():
    with pytest.raises(write_contract.WriteVerificationError):
        write_contract.require_verification(
            "geometry.add_box", {"verification": {"verified": False, "checks": []}}
        )


def test_require_verification_rejects_a_block_with_no_checks():
    """A verification block with no checks proves nothing."""
    with pytest.raises(write_contract.WriteVerificationError) as caught:
        write_contract.require_verification(
            "geometry.add_box", {"verification": {"verified": True, "checks": []}}
        )

    assert caught.value.check == "read_back_checks"


# --- error shape ----------------------------------------------------------


def test_error_message_states_tool_check_values_and_host():
    error = write_contract.WriteVerificationError(
        tool="geometry.add_box",
        check="bounds_extents",
        expected=[1, 2, 3],
        actual=[1, 2, 4],
        host_version="2026.0",
        host_matrix={"status": "supported"},
    )

    message = str(error)
    assert "geometry.add_box" in message
    assert "bounds_extents" in message
    assert "[1, 2, 3]" in message
    assert "[1, 2, 4]" in message
    assert "2026.0" in message
    assert "supported" in message
    assert error.expected == [1, 2, 3]
    assert error.actual == [1, 2, 4]
    assert error.host_version == "2026.0"


def test_error_survives_a_round_trip_through_a_payload():
    original = write_contract.WriteVerificationError(
        tool="materials.update",
        check="material_alpha",
        expected=0.25,
        actual=1.0,
        host_version="26.0.575",
        params={"name": "Brick"},
        remediation="Read alpha back on SketchUp 2026.",
    )

    rebuilt = write_contract.WriteVerificationError.from_payload(dict(original.payload))

    assert rebuilt.tool == original.tool
    assert rebuilt.check == original.check
    assert rebuilt.expected == original.expected
    assert rebuilt.actual == original.actual
    assert rebuilt.payload["remediation"] == "Read alpha back on SketchUp 2026."
    assert str(rebuilt) == str(original)


def test_payload_is_json_serialisable():
    import json

    error = write_contract.WriteVerificationError(
        tool="geometry.add_box",
        check="bounds_extents",
        expected=[1, 2, 3],
        actual=[1, 2, 4],
        host_version="2026.0",
        host_matrix={"status": "supported", "supported_ranges": ["2026"]},
        params={"name": "Box", "unit": "meters"},
    )

    assert json.loads(json.dumps(error.payload)) == error.payload


def test_every_skill_script_imports_and_classifies():
    """Import-time classification is the guard; prove it actually runs.

    A method missing from both tool lists raises ToolClassificationError when
    the skill module is imported, which would take the whole server down at
    skill registration rather than failing one call. Importing every script is
    the only way to exercise that path.
    """
    import importlib
    import sys

    scripts = sorted((ROOT / "src" / "dcc_mcp_sketchup" / "skills").glob("*/scripts/*.py"))
    assert len(scripts) == 28

    src = str(ROOT / "src")
    added = src not in sys.path
    if added:
        sys.path.insert(0, src)
    try:
        for script in scripts:
            module_name = "dcc_mcp_sketchup.skills._probe_%s" % script.stem
            spec = importlib.util.spec_from_file_location(module_name, script)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            assert callable(module.main), script
    finally:
        if added:
            sys.path.remove(src)
