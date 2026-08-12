---
name: sketchup-scenes
description: >-
  Manage SketchUp scenes and Tags through typed, undoable host operations.
  Use for saved views, animation inclusion, organization, and entity tagging;
  use sketchup-session to discover persistent entity IDs first.
license: MIT
compatibility: "SketchUp 2021+; external sidecar Python 3.9+; dcc-mcp-core 0.19.91+"
allowed-tools: Python
metadata:
  dcc-mcp:
    dcc: sketchup
    layer: domain
    version: "0.1.0"  # x-release-please-version
    stage: scene
    search-hint: "SketchUp scenes pages saved views animation tags layers assign organize"
    tags: "sketchup,scenes,tags,organization"
    tools: tools.yaml
---

# SketchUp Scenes and Tags

Scene capture stores the active SketchUp view. The default Untagged tag cannot
be removed, and any tag still used by model or definition entities is protected.
