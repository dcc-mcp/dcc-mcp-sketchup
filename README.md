# dcc-mcp-sketchup

SketchUp adapter foundation for the DCC-MCP organization.

This is an experimental, read-only first slice. It is **not** in the released
`dcc-mcp-cli dcc-types` catalog yet.

## Scope

- Discover the Ruby extension boundary.
- Expose one typed, read-only model inspection tool.
- Keep host API calls outside the MCP HTTP worker.
- Do not expose arbitrary source evaluation.

## Install

```bash
python -m pip install -e ".[test]"
dcc-mcp-sketchup
```

Configure the bridge environment variables in `src/dcc_mcp_sketchup/bridge.py`.
A real SketchUp live smoke is required before catalog onboarding.

Official API reference: https://developer.sketchup.com/

