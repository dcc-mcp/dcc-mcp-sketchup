"""Contract tests for the SketchUp doctor report."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from dcc_mcp_sketchup import compat, doctor
from dcc_mcp_sketchup import install as install_lifecycle

ROOT = Path(__file__).parents[1]
LIVE_VERSION = "2026.0"


class _FakeBridge:
    """Stand-in for SketchupBridge with a configurable live host."""

    def __init__(self, runtime=None, probe=None, error=None):
        self._runtime = runtime
        self._probe = probe
        self._error = error

    @classmethod
    def from_env(cls):
        return cls(**_BRIDGE_STATE)

    def status(self):
        if self._error:
            raise self._error
        return dict(self._runtime) if self._runtime else {"ready": False}

    def call(self, method, **params):
        assert method == "diagnostics.api_probe"
        return {"probe": self._probe if self._probe is not None else []}


_BRIDGE_STATE: dict = {}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Pin every input the doctor has: bridge, installations, environment."""
    _BRIDGE_STATE.clear()
    monkeypatch.setattr(doctor, "SketchupBridge", _FakeBridge)
    monkeypatch.setattr(install_lifecycle, "discover_host_installations", lambda: list(_INSTALLS))
    for name in (
        "DCC_MCP_SKETCHUP_BRIDGE_HOST",
        "DCC_MCP_SKETCHUP_BRIDGE_PORT",
        "DCC_MCP_SKETCHUP_BRIDGE_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    _INSTALLS.clear()
    yield
    _BRIDGE_STATE.clear()
    _INSTALLS.clear()


_INSTALLS: list = []


def _installed(version: str, year: int | None = None) -> None:
    _INSTALLS.append(
        {
            "plugins_dir": "C:/profile/SketchUp %s/SketchUp/Plugins" % (year or version),
            "profile_year": year if year is not None else compat.product_year(version),
            "host_path": "C:/Program Files/SketchUp/SketchUp.exe",
            "native_version": version,
        }
    )


def _live(version: str = LIVE_VERSION):
    _BRIDGE_STATE.update(
        runtime={
            "ready": True,
            "bridge": "127.0.0.1:5555",
            "sketchup_version": version,
            "ruby_version": "3.2.2",
            "host_pid": 4242,
            "adapter_version": "0.2.0",
        },
        probe=[
            {"owner": entry["owner"], "symbol": entry["symbol"], "present": True}
            for entry in compat.required_api_surface()
        ],
    )


def test_report_shape_is_machine_readable():
    _live()
    report = doctor.doctor_report("doctor")

    assert report.pop("_exit_code") == doctor.EXIT_OK
    assert json.loads(json.dumps(report)) == report
    assert report["dcc_type"] == "sketchup"
    assert report["adapter_version"]
    assert report["core_version"]
    assert report["min_core_version"] == doctor.MIN_CORE_VERSION
    assert report["verify"]["directly_usable"] is True
    assert report["error_code"] is None


def test_core_floor_is_the_same_constant_the_installer_enforces():
    assert doctor.MIN_CORE_VERSION == install_lifecycle.MIN_CORE_VERSION == "0.20.36"


def test_no_host_at_all_is_reported_not_assumed():
    report = doctor.doctor_report("doctor")

    assert report["_exit_code"] == doctor.EXIT_PREFLIGHT
    assert report["error_code"] == doctor.ERROR_HOST_NOT_FOUND
    assert report["verify"]["failure_stage"] == "host"
    assert report["verify"]["directly_usable"] is False
    assert report["next_steps"]


def test_unsupported_installed_version_is_rejected_explicitly():
    _installed("2019.0", 2019)

    report = doctor.doctor_report("doctor")

    assert report["error_code"] == doctor.ERROR_HOST_TOO_OLD
    assert report["verify"]["failure_stage"] == "host_version"
    assert report["checks"]["host_matrix"]["source"] == "installed_host"
    assert report["checks"]["host_matrix"]["matrix"]["status"] == compat.TOO_OLD
    assert "2019" in report["verify"]["failure_reason"]


def test_installed_but_not_running_is_not_directly_usable():
    """An executable on disk is not evidence the adapter works.

    The host version and Ruby API surface are only observed once SketchUp
    answers the bridge, so an installed-but-silent host fails at the live_host
    stage with a concrete next step rather than being reported as supported.
    """
    _installed(LIVE_VERSION)

    report = doctor.doctor_report("doctor")

    assert report["error_code"] == doctor.ERROR_HOST_NOT_RUNNING
    assert report["verify"]["failure_stage"] == "live_host"
    assert report["checks"]["host_matrix"]["success"] is True
    assert report["checks"]["live_host"]["success"] is False
    assert report["_exit_code"] == doctor.EXIT_VERIFY


def test_live_host_is_preferred_over_the_installed_version():
    """A running SketchUp is ground truth; a stale profile must not outrank it."""
    _installed("2021.0", 2021)
    _live("2026.0")

    report = doctor.doctor_report("doctor")

    assert report["checks"]["host_matrix"]["source"] == "live_host"
    assert report["checks"]["host_matrix"]["version"] == "2026.0"
    assert report["checks"]["live_host"]["ruby_version"] == "3.2.2"


def test_two_digit_build_line_is_folded_onto_the_product_year():
    _live("26.0.575")

    report = doctor.doctor_report("doctor")

    assert report["checks"]["host_matrix"]["product_year"] == 2026
    assert report["checks"]["host_matrix"]["success"] is True


def test_missing_required_api_symbol_fails_the_doctor():
    _live()
    probe = _BRIDGE_STATE["probe"]
    probe[0]["present"] = False

    report = doctor.doctor_report("doctor")

    assert report["_exit_code"] == doctor.EXIT_VERIFY
    assert report["verify"]["failure_stage"] == "api_surface"
    assert report["checks"]["api_surface"]["missing"]
    assert report["checks"]["api_surface"]["missing"][0]["present"] is False


def test_empty_api_probe_is_unverified_not_healthy():
    """An unanswered probe must never be read as a healthy one."""
    _live()
    _BRIDGE_STATE["probe"] = []

    report = doctor.doctor_report("doctor")

    assert report["verify"]["failure_stage"] == "api_surface"
    assert all(item["unverified"] for item in report["checks"]["api_surface"]["missing"])


def test_bridge_error_is_recorded_not_swallowed():
    from dcc_mcp_sketchup.bridge import BridgeError

    _installed("2019.0", 2019)
    _BRIDGE_STATE.update(runtime=None, probe=[], error=BridgeError("connection refused"))

    report = doctor.doctor_report("doctor")

    assert report["checks"]["live_host"]["success"] is False
    assert "connection refused" in report["checks"]["live_host"]["error"]
    # The installed 2019 host still governs the verdict.
    assert report["error_code"] == doctor.ERROR_HOST_TOO_OLD


def test_invalid_bridge_port_fails_at_configuration():
    import os

    from dcc_mcp_sketchup import bridge as bridge_module

    os.environ["DCC_MCP_SKETCHUP_BRIDGE_PORT"] = "not-a-port"
    try:
        report = doctor.doctor_report("doctor")
    finally:
        del os.environ["DCC_MCP_SKETCHUP_BRIDGE_PORT"]

    assert report["_exit_code"] == doctor.EXIT_PREFLIGHT
    assert report["verify"]["failure_stage"] == "configuration"
    assert report["checks"]["configuration"]["success"] is False
    assert bridge_module  # imported for the exception type used above


def test_core_below_the_floor_fails_before_host_discovery(monkeypatch):
    monkeypatch.setattr(doctor, "running_core_version", "0.20.35", raising=False)
    _live()

    report = doctor.doctor_report("doctor")

    assert report["_exit_code"] == doctor.EXIT_PREFLIGHT
    assert report["verify"]["failure_stage"] == "core"
    assert report["checks"]["core"]["success"] is False
    assert "0.20.36" in report["verify"]["failure_reason"]


def test_doctor_verb_is_echoed_into_the_report():
    _live()
    report = doctor.doctor_report("verify")

    assert report["verb"] == "verify"


def test_contract_block_exposes_the_evidence_level():
    _live()
    report = doctor.doctor_report("doctor")

    contract = report["checks"]["contract"]
    assert contract["evidence_level"] == "contract"
    assert contract["evidence_bound"]["host_level"] == "none"
    assert contract["mutating_tools"]
    assert contract["read_only_tools"]


def test_cli_prints_json_and_exits_with_the_report_code(monkeypatch, capsys):
    from dcc_mcp_sketchup import server as server_module

    _live()
    monkeypatch.setattr(doctor, "SketchupBridge", _FakeBridge)
    monkeypatch.setattr(install_lifecycle, "discover_host_installations", lambda: list(_INSTALLS))

    code = 0
    with pytest.raises(SystemExit) as caught:
        server_module.main(["doctor", "--json"])
    code = caught.value.code
    assert code == doctor.EXIT_OK
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_cli_without_json_prints_a_readable_summary(monkeypatch, capsys):
    from dcc_mcp_sketchup import server as server_module

    _live()
    monkeypatch.setattr(doctor, "SketchupBridge", _FakeBridge)
    monkeypatch.setattr(install_lifecycle, "discover_host_installations", lambda: list(_INSTALLS))

    with pytest.raises(SystemExit) as caught:
        server_module.main(["doctor"])
    assert caught.value.code == doctor.EXIT_OK
    assert "doctor: ok" in capsys.readouterr().out


def test_module_import_is_not_required_for_the_matrix_to_ship():
    """The matrix must be readable without importing the adapter's runtime deps."""
    matrix = json.loads(
        (ROOT / "src" / "dcc_mcp_sketchup" / "compat_matrix.json").read_text(encoding="utf-8")
    )

    assert matrix["host"] == "sketchup"
    assert importlib.util.find_spec("dcc_mcp_sketchup.compat") is not None
