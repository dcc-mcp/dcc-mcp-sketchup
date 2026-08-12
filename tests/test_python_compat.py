"""Declared Python-version syntax contract."""

from __future__ import annotations

import ast
from pathlib import Path


def test_package_sources_parse_as_python_39() -> None:
    package_root = Path(__file__).parents[1] / "src" / "dcc_mcp_sketchup"
    for source_path in package_root.rglob("*.py"):
        ast.parse(
            source_path.read_text(encoding="utf-8"),
            filename=str(source_path),
            feature_version=(3, 9),
        )
