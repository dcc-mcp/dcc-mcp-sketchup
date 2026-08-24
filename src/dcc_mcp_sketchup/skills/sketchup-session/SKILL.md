---
name: sketchup-session
description: >-
  Inspect a live SketchUp model and manage save, validation, import, and export
  through typed host operations. Use for session discovery and interchange;
  use sketchup-modeling for geometry changes. Never executes arbitrary Ruby.
license: MIT
compatibility: "SketchUp 2021+; external sidecar Python 3.9+; dcc-mcp-core 0.20.14+"
allowed-tools: Python
metadata:
  dcc-mcp:
    dcc: sketchup
    layer: domain
    version: "0.2.0"  # x-release-please-version
    stage: session
    search-hint: "SketchUp session inspect entities save copy validate import export SKP DAE STL GLB"
    tags: "sketchup,session,model,interchange"
    tools: tools.yaml
---

# SketchUp Session

Inspect before mutating. File paths must be absolute. Import/export support is
determined by the active SketchUp edition and installed importers/exporters.
Existing output files are rejected unless `overwrite` is explicit.
