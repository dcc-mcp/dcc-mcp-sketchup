"""Contract tests for the machine-readable SketchUp compatibility matrix."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from dcc_mcp_sketchup import compat

ROOT = Path(__file__).parents[1]
MATRIX_PATH = ROOT / "src" / "dcc_mcp_sketchup" / "compat_matrix.json"
COMMANDS_SOURCE = ROOT / "src" / "dcc_mcp_sketchup" / "sketchup_plugin" / "commands.rb"


def test_matrix_is_packaged_next_to_the_module():
    assert Path(compat.MATRIX_PATH).is_file()
    assert compat.load_matrix()["host"] == "sketchup"
    assert compat.load_matrix()["schema_version"] == 1


def test_matrix_records_its_evidence_bound():
    """Contract-green must never be readable as host-green.

    SketchUp cannot be installed on a hosted runner, so every range in the
    matrix is contract-level. That bound travels with the verdict so a consumer
    that reads ``supported`` also reads what it rests on.
    """
    bound = compat.evidence_bound()

    assert bound["level"] == "contract"
    assert bound["host_level"] == "none"
    assert bound["reason"]
    assert bound["what_contract_evidence_proves"]
    # The negative half is the one that matters: without it a reader can stop at
    # "contract-level" and never learn what that excludes.
    assert "real SketchUp" in bound["what_contract_evidence_does_not_prove"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026.0", (2026, 0, 0)),
        ("26.0.575", (2026, 0, 575)),
        ("2026", (2026, 0, 0)),
        ("2021.1.2172", (2021, 1, 2172)),
        ("26", (2026, 0, 0)),
        ("", None),
        ("not a version", None),
        (None, None),
    ],
)
def test_normalize_version_folds_both_spellings_onto_the_product_year(value, expected):
    """SketchUp is called both "SketchUp 2026" and "SketchUp 26".

    ``Sketchup.version`` reports the build line ("26.0.575") while profiles and
    marketing use the year ("2026"). Both must land on one axis or the matrix
    would classify the same application two different ways.
    """
    assert compat.normalize_version(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2026.0", 2026), ("26.0.575", 2026), ("2021.1", 2021), ("bogus", None)],
)
def test_product_year(value, expected):
    assert compat.product_year(value) == expected


def test_supported_versions_classify_as_supported():
    for year in compat.supported_range_labels():
        verdict = compat.classify_host(year)
        assert verdict["status"] == compat.SUPPORTED, year
        assert verdict["product_year"] == int(year)
        assert verdict["range"]["id"] == year
        # The verdict must carry its own evidence level, never bare "supported".
        assert verdict["evidence_level"] == "contract"


def test_unsupported_versions_are_rejected_explicitly():
    oldest = min(int(year) for year in compat.supported_range_labels())

    too_old = compat.classify_host(str(oldest - 1))
    assert too_old["status"] == compat.TOO_OLD

    too_new = compat.classify_host(str(oldest + len(compat.supported_range_labels()) + 5))
    assert too_new["status"] == compat.TOO_NEW

    unknown = compat.classify_host("not-a-version")
    assert unknown["status"] == compat.UNKNOWN
    assert unknown["parsed_version"] is None

    assert not compat.is_supported(str(oldest - 1))
    assert not compat.is_supported("not-a-version")


def test_unlisted_gap_is_not_supported():
    """A year inside the covered span but in no declared range is still outside."""
    matrix = {
        "matrix_version": "test",
        "version_axis": "product_year",
        "supported_ranges": [
            {"id": "2022", "min_version": "2022.0", "max_version": "2022.9999"},
            {"id": "2024", "min_version": "2024.0", "max_version": "2024.9999"},
        ],
    }

    verdict = compat.classify_host("2023", matrix)

    assert verdict["status"] == compat.UNLISTED
    assert not compat.is_supported("2023", matrix)


def test_unsupported_reason_names_the_version_and_the_covered_range():
    verdict = compat.classify_host("2019.0")
    reason = compat.unsupported_reason(verdict)

    assert "2019.0" in reason
    assert "2021" in reason


def test_minimum_supported_year_matches_the_lowest_range():
    assert compat.minimum_supported_year() == min(
        int(year) for year in compat.supported_range_labels()
    )


def test_breaking_changes_apply_from_their_declared_version():
    applied = compat.breaking_changes_for("2026.0")

    assert applied, "the matrix declares host API breaks the adapter guards"
    for entry in applied:
        assert entry["id"]
        assert entry["enforcement"]
        assert entry["remediation"]


def test_required_api_surface_is_non_empty_and_declared():
    surface = compat.required_api_surface()

    assert surface
    for entry in surface:
        assert entry["owner"]
        assert entry["symbol"]
        assert entry["used_by"]


def test_missing_required_api_treats_an_empty_probe_as_unverified():
    """An unanswered probe must never be read as a healthy one."""
    missing = compat.missing_required_api(None)

    assert len(missing) == len(compat.required_api_surface())
    assert all(item["unverified"] is True for item in missing)
    assert all(item["present"] is None for item in missing)


def test_missing_required_api_reports_only_absent_or_unanswered_symbols():
    required = compat.required_api_surface()
    target = required[0]
    symbols = "%s#%s" % (target["owner"], target["symbol"])

    complete = [
        {"owner": entry["owner"], "symbol": entry["symbol"], "present": True} for entry in required
    ]
    assert compat.missing_required_api(complete) == []

    partial = [
        {"owner": entry["owner"], "symbol": entry["symbol"], "present": entry is not target}
        for entry in required
    ]
    missing = compat.missing_required_api(partial)
    assert len(missing) == 1
    assert missing[0]["owner"] == target["owner"]
    assert missing[0]["symbol"] == target["symbol"]
    assert missing[0]["unverified"] is False
    assert symbols


def test_every_required_api_symbol_is_resolvable_by_the_ruby_probe():
    """The probe contract: Python sends "Owner#symbol", Ruby answers presence.

    The Ruby side validates every entry against PROBE_ENTRY_PATTERN, so a
    symbol written in a shape that pattern rejects would be reported absent on
    every host and the doctor would fail everywhere. This mirrors that pattern
    rather than parsing the Ruby literal.
    """
    pattern = re.compile(
        r"\A[A-Z][A-Za-z0-9]*(?:::[A-Z][A-Za-z0-9]*)*[#.][A-Za-z_][A-Za-z0-9_]*[?!=]?\Z"
    )

    for entry in compat.required_api_surface():
        candidate = "%s#%s" % (entry["owner"], entry["symbol"])
        assert pattern.match(candidate), candidate


def test_matrix_is_valid_json_with_a_stable_axis():
    raw = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))

    assert raw["version_axis"] == "product_year"
    assert raw["executable_names"]
    assert raw["ruby_contract"]["ci_ruby_versions"]
    assert raw["matrix_version"]
