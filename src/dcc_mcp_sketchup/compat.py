"""Machine-readable SketchUp host compatibility matrix.

The matrix itself lives in ``compat_matrix.json`` next to this module, and
``pyproject.toml`` ships it inside the wheel, so the host-side checks (this
module), the ``doctor``/``verify`` reports, and the Ruby contract layer all
read one single source of truth.

SketchUp versioning is the awkward part. SketchUp reports its version either as
a product year (``"2026.0"``) or as a two-digit build line (``"26.0.575"``), and
the same application is referred to as both "SketchUp 2026" and "SketchUp 26".
:func:`normalize_version` folds both spellings onto one axis, the product year,
so that a range declared as ``2026.0`` .. ``2026.9999`` matches both.

A host version that is not covered by the matrix is reported as unsupported with
the covered ranges and a concrete remediation. Nothing here degrades silently:
an unrecognised or out-of-range version never becomes "good enough".

Evidence level
--------------
The matrix is honest about how weak its evidence is. SketchUp is a licensed
desktop application that cannot be installed on a hosted CI runner, so no
host-level end-to-end run exists. Every range carries
``evidence_level: "contract"`` and the matrix records the bound in
``evidence_bound``. Contract-green is not host-green, and this module never
lets a caller infer otherwise.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MATRIX_PATH = Path(__file__).resolve().parent / "compat_matrix.json"

SUPPORTED = "supported"
TOO_OLD = "too_old"
TOO_NEW = "too_new"
UNLISTED = "unlisted"
UNKNOWN = "unknown"

STATUS_MESSAGES = {
    SUPPORTED: "supported",
    TOO_OLD: "below the supported range",
    TOO_NEW: "above the supported range",
    UNLISTED: "inside the covered span but not in any declared range",
    UNKNOWN: "not recognised as a SketchUp version",
}

# SketchUp reports either "2026.0" or "26.0.575". Both are the 2026 product.
# A bare "2026" is accepted too: that is the shape a versioned user profile
# directory yields, and it is a real product year rather than a parse failure.
_RELEASE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_YEAR_CUTOFF = 2000

Version = Tuple[int, int, int]


def parse_version(value: str) -> Optional[Version]:
    """Parse the leading ``major.minor[.patch]`` of a SketchUp version string.

    The returned tuple is the raw parsed value, not the normalised product year;
    use :func:`normalize_version` when the value has to be compared against a
    declared range.
    """
    if not value:
        return None
    match = _RELEASE.match(str(value).strip())
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor or 0), int(patch or 0)


def normalize_version(value: str) -> Optional[Version]:
    """Fold a SketchUp version onto the product-year axis.

    ``"26.0.575"`` and ``"2026.0"`` both become ``(2026, 0, 575)`` /
    ``(2026, 0, 0)``. A two-digit major below the year cutoff is read as a
    SketchUp build line and has 2000 added to it, which is the same rule the
    installer uses when it reconciles a native executable version against a
    versioned user profile.
    """
    parsed = parse_version(value)
    if parsed is None:
        return None
    major, minor, patch = parsed
    if major < _YEAR_CUTOFF:
        major += _YEAR_CUTOFF
    return major, minor, patch


def product_year(value: str) -> Optional[int]:
    """Return the SketchUp product year (``2026``) for a version string."""
    normalized = normalize_version(value)
    return None if normalized is None else normalized[0]


def format_version(version: Version) -> str:
    return "%d.%d.%d" % version


def load_matrix(path: Optional[str] = None) -> Dict[str, Any]:
    with open(path or MATRIX_PATH, "r", encoding="utf-8") as stream:
        return json.load(stream)


def supported_ranges(matrix: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    return list((matrix or load_matrix()).get("supported_ranges") or ())


def supported_range_labels(matrix: Optional[Dict[str, Any]] = None) -> List[str]:
    labels = []
    for entry in supported_ranges(matrix):
        minimum = normalize_version(str(entry.get("min_version", "")))
        if minimum is None:
            continue
        labels.append(str(minimum[0]))
    return labels


def minimum_supported_year(matrix: Optional[Dict[str, Any]] = None) -> Optional[int]:
    years = [
        normalize_version(str(entry.get("min_version", "")))[0]
        for entry in supported_ranges(matrix)
        if normalize_version(str(entry.get("min_version", ""))) is not None
    ]
    return min(years) if years else None


def breaking_changes_for(
    version: str, matrix: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """Return the declared host API breaks that apply to ``version``."""
    matrix = matrix or load_matrix()
    parsed = normalize_version(version)
    if parsed is None:
        return []
    applied = []
    for entry in matrix.get("breaking_changes") or ():
        since = normalize_version(str(entry.get("applies_from", "")))
        if since is None or parsed < since:
            continue
        applied.append(entry)
    return applied


def required_api_surface(matrix: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Return the API symbols the adapter depends on and cannot work without."""
    return [
        entry
        for entry in (matrix or load_matrix()).get("api_surface") or ()
        if entry.get("required") is True
    ]


def missing_required_api(
    probe: Optional[List[Dict[str, Any]]], matrix: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """Return the required API symbols a live host reported as absent.

    ``probe`` is the ``api_probe`` list returned by ``diagnostics.ping``. An
    empty or absent probe means the host did not answer the question at all, so
    every required symbol is reported as unverified rather than as present: an
    unanswered probe must never be read as a healthy one.
    """
    required = required_api_surface(matrix)
    if not probe:
        return [
            {
                "owner": entry.get("owner"),
                "symbol": entry.get("symbol"),
                "present": None,
                "unverified": True,
            }
            for entry in required
        ]
    reported = {
        (str(item.get("owner") or ""), str(item.get("symbol") or "")): item.get("present")
        for item in probe
        if isinstance(item, dict)
    }
    return [
        {
            "owner": entry.get("owner"),
            "symbol": entry.get("symbol"),
            "present": reported.get(
                (str(entry.get("owner") or ""), str(entry.get("symbol") or ""))
            ),
            "unverified": (str(entry.get("owner") or ""), str(entry.get("symbol") or ""))
            not in reported,
        }
        for entry in required
        if reported.get((str(entry.get("owner") or ""), str(entry.get("symbol") or ""))) is not True
    ]


def evidence_bound(matrix: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the declared evidence bound so reports can carry it verbatim."""
    return dict((matrix or load_matrix()).get("evidence_bound") or {})


def classify_host(version: str, matrix: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Classify a discovered SketchUp version against the matrix.

    The result is machine readable and is embedded verbatim in the doctor and
    verify reports, so callers can gate on ``status`` instead of parsing prose.
    ``evidence_level`` is always present: a consumer that sees ``supported``
    must also be able to see that the claim rests on contract evidence only.
    """
    matrix = matrix or load_matrix()
    parsed = normalize_version(version)
    verdict: Dict[str, Any] = {
        "version": version,
        "parsed_version": format_version(parsed) if parsed is not None else None,
        "product_year": product_year(version),
        "status": UNKNOWN,
        "matrix_version": matrix.get("matrix_version"),
        "version_axis": matrix.get("version_axis"),
        "supported_ranges": supported_range_labels(matrix),
        "range": None,
        "evidence_level": "contract",
        "evidence_bound": evidence_bound(matrix),
        "breaking_changes": [
            {
                "id": entry.get("id"),
                "title": entry.get("title"),
                "changed_in": entry.get("changed_in"),
                "kind": entry.get("kind"),
                "adapter_usage": entry.get("adapter_usage"),
                "enforcement": entry.get("enforcement"),
                "remediation": entry.get("remediation"),
                "replacement": entry.get("replacement"),
            }
            for entry in breaking_changes_for(version, matrix)
        ],
    }
    if parsed is None:
        return verdict

    for entry in supported_ranges(matrix):
        minimum = normalize_version(str(entry.get("min_version", "")))
        maximum = normalize_version(str(entry.get("max_version", "")))
        if minimum is None or maximum is None:
            continue
        if minimum <= parsed <= maximum:
            verdict["status"] = SUPPORTED
            verdict["evidence_level"] = entry.get("evidence_level") or "contract"
            verdict["range"] = {
                "id": entry.get("id"),
                "min_version": entry.get("min_version"),
                "max_version": entry.get("max_version"),
                "evidence": entry.get("evidence"),
                "evidence_level": entry.get("evidence_level"),
            }
            return verdict

    minimums = [
        normalize_version(str(entry.get("min_version", ""))) for entry in supported_ranges(matrix)
    ]
    maximums = [
        normalize_version(str(entry.get("max_version", ""))) for entry in supported_ranges(matrix)
    ]
    low = min([item for item in minimums if item is not None], default=None)
    high = max([item for item in maximums if item is not None], default=None)
    if low is not None and parsed < low:
        status = TOO_OLD
    elif high is not None and parsed > high:
        status = TOO_NEW
    else:
        # Inside the covered span but in a gap between declared ranges (for
        # example 2022 and 2024 declared with 2023 unverified). That is still
        # outside the matrix and must not be treated as supported.
        status = UNLISTED
    verdict["status"] = status
    return verdict


def is_supported(version: str, matrix: Optional[Dict[str, Any]] = None) -> bool:
    return classify_host(version, matrix)["status"] == SUPPORTED


def unsupported_reason(verdict: Dict[str, Any]) -> str:
    """Build the human- and agent-readable rejection sentence for a verdict."""
    ranges = verdict.get("supported_ranges") or ()
    covered = ", ".join(ranges) if ranges else "no declared range"
    version = verdict.get("version") or "unknown"
    status = verdict.get("status")
    if status == UNKNOWN:
        return "SketchUp reported an unrecognised version %r; supported product years: %s" % (
            version,
            covered,
        )
    if status == TOO_NEW:
        return (
            "SketchUp %s is newer than the verified compatibility matrix (supported: %s); "
            "the host Ruby API may have moved, so the adapter refuses to run unverified"
            % (version, covered)
        )
    if status == UNLISTED:
        return (
            "SketchUp %s is not listed in the verified compatibility matrix (supported: %s); "
            "the adapter refuses to run unverified" % (version, covered)
        )
    return "SketchUp %s is unsupported; supported product years: %s" % (version, covered)
