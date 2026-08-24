---
name: sketchup-modeling
description: >-
  Create and edit SketchUp geometry through bounded, undoable typed operations.
  Use for primitives, grouping, transforms, naming, selection, and explicit
  erasure. Use sketchup-session first to discover persistent entity IDs.
license: MIT
compatibility: "SketchUp 2021+; external sidecar Python 3.9+; dcc-mcp-core 0.20.14+"
allowed-tools: Python
metadata:
  dcc-mcp:
    dcc: sketchup
    layer: domain
    version: "0.2.0"  # x-release-please-version
    stage: modeling
    search-hint: "SketchUp create box cylinder group transform rename erase select persistent entity id"
    tags: "sketchup,modeling,geometry,transform"
    tools: tools.yaml
---

# SketchUp Modeling

All geometry mutations execute on SketchUp's UI thread inside one named undo
operation. Length inputs declare a unit; returned native bounds are identified
as inches. Entity editing uses persistent IDs from `list_entities`.
