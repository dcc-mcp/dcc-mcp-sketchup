---
name: sketchup-materials
description: >-
  List, create, edit, assign, and remove SketchUp materials through typed host
  operations. Use for color and texture look development after entity discovery;
  use sketchup-modeling for geometry changes.
license: MIT
compatibility: "SketchUp 2021+; external sidecar Python 3.9+; dcc-mcp-core 0.20.14+"
allowed-tools: Python
metadata:
  dcc-mcp:
    dcc: sketchup
    layer: domain
    version: "0.2.0"  # x-release-please-version
    stage: lookdev
    search-hint: "SketchUp materials colors textures assign front back face remove"
    tags: "sketchup,materials,lookdev,textures"
    tools: tools.yaml
---

# SketchUp Materials

Texture paths must be absolute existing image files. Removing a material is
refused while any model or component-definition entity still uses it.
