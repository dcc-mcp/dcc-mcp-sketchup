from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_install_runbook_covers_the_agent_contract_and_raw_url():
    runbook = (ROOT / "install.md").read_text(encoding="utf-8")
    for heading in (
        "## Requirements",
        "## Supported versions",
        "## Agent quick path",
        "## Manual path",
        "## Verify",
        "## Upgrade",
        "## Uninstall",
        "## Troubleshooting",
    ):
        assert heading in runbook
    for verb in ("install", "status", "verify", "uninstall", "upgrade"):
        assert f"dcc-mcp-sketchup {verb}" in runbook
    for flag in ("--json", "--yes", "--dry-run", "--dcc-path", "--python"):
        assert flag in runbook
    assert "Windows" in runbook
    assert "macOS" in runbook
    assert "Linux" in runbook
    assert "server_path.txt" in runbook
    assert "sketchup-bootstrap-errors.jsonl" in runbook

    raw_url = "https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-sketchup/main/install.md"
    assert raw_url in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[Install and lifecycle runbook](install.md)" in (ROOT / "README.md").read_text(
        encoding="utf-8"
    )


def test_ci_runs_the_built_wheel_install_lifecycle_round_trip():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "Install lifecycle round trip" in workflow
    assert "dcc-mcp-sketchup install" in workflow
    assert "dcc-mcp-sketchup status" in workflow
    assert "dcc-mcp-sketchup uninstall" in workflow
