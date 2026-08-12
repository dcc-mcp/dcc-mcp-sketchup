import ast
import re
from pathlib import Path

import yaml
from dcc_mcp_core import validate_skill

SKILLS_ROOT = Path("src/dcc_mcp_sketchup/skills")


def test_all_packaged_skills_pass_core_validation():
    skill_dirs = sorted(path.parent for path in SKILLS_ROOT.glob("*/SKILL.md"))
    assert [path.name for path in skill_dirs] == [
        "sketchup-materials",
        "sketchup-modeling",
        "sketchup-scenes",
        "sketchup-session",
    ]

    reports = [validate_skill(str(path)) for path in skill_dirs]

    assert all(report.is_clean for report in reports), [
        issue for report in reports for issue in report.issues
    ]


def test_tool_manifests_are_typed_and_point_to_scripts():
    tool_names = set()
    script_methods = set()
    for manifest_path in SKILLS_ROOT.glob("*/tools.yaml"):
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        for tool in manifest["tools"]:
            assert tool["name"] not in tool_names
            tool_names.add(tool["name"])
            assert tool["input_schema"]["type"] == "object"
            assert tool["output_schema"]["type"] == "object"
            assert tool["affinity"] == "main"
            assert tool["enforce_thread_affinity"] is True
            assert "annotations" in tool
            script_path = manifest_path.parent / tool["source_file"]
            assert script_path.is_file()
            assert script_path.stem == tool["name"]
            calls = [
                node
                for node in ast.walk(ast.parse(script_path.read_text(encoding="utf-8")))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "bridge_main"
            ]
            assert len(calls) == 1
            assert isinstance(calls[0].args[0], ast.Constant)
            script_methods.add(calls[0].args[0].value)

    assert len(tool_names) == 28
    ruby_source = Path("src/dcc_mcp_sketchup/sketchup_plugin/commands.rb").read_text(
        encoding="utf-8"
    )
    ruby_methods = set(
        re.findall(r"'([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)' => method", ruby_source)
    )
    assert ruby_methods - {"diagnostics.ping"} == script_methods - {"bridge.health"}
    assert "diagnostics.ping" in ruby_methods
    assert "bridge.health" in script_methods
