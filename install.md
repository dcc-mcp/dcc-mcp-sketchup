# Install DCC-MCP SketchUp

This runbook is the contract for agent-driven installation. The lifecycle command plans by
default and uses stable exit codes: `0` success, `10` preflight, `20` acquire, `30` install,
`40` verify, and `50` restart required because an installed file is locked.

## Requirements

- SketchUp Desktop 2021 or newer on Windows or macOS.
- Python 3.9 or newer containing `dcc-mcp-sketchup` and
  `dcc-mcp-core>=0.19.91,<1.0.0`.
- A versioned per-user SketchUp profile created by starting the selected SketchUp version once.

SketchUp Desktop is supported on Windows and macOS. Linux is not a supported SketchUp host
platform; Linux CI validates the installer plan, receipt, and package contracts only.
Interpreter selection is `--python`, then `DCC_MCP_INSTALL_PYTHON`, then the interpreter running
the command.

## Supported versions

SketchUp 2021 and newer versioned profiles are supported. The installer selects the newest
profile by default. `--dcc-path` selects an exact SketchUp executable or application bundle and
requires a matching versioned profile; it never silently installs into another SketchUp version.

## Agent quick path

Use the pinned ecosystem installer, audit the adapter plan, then execute it non-interactively:

```bash
dcc-mcp-cli install --dcc-type sketchup --execute
dcc-mcp-sketchup install --dcc-path /path/to/SketchUp --json --dry-run
dcc-mcp-sketchup install --dcc-path /path/to/SketchUp --python /path/to/python --json --yes
```

Every verb supports `--json`, `--yes`, `--dry-run`, `--dcc-path`, and `--python`. JSON follows
schema version `1` and includes the selected host version, profile, interpreter, sidecar path,
partial/current/repair/upgrade state, steps, machine-executable next steps, receipt path, and the
verify-to-usable verdict. A copied extension may correctly exit `40` until a live SketchUp probe
succeeds; only a real locked-file deferral exits `50`.

## Manual path

```bash
python -m pip install dcc-mcp-sketchup
dcc-mcp-sketchup install --dcc-path /path/to/SketchUp --yes
```

The Ruby payload and registration file are fully prepared in the selected Plugins directory,
then swapped as one rollback-protected transaction. The previous extension, registration, and
receipt remain recoverable until the new receipt is durable. Re-running `install --yes` converges
an already-current install and repairs partial state.

The receipt is stored at `.dcc-mcp/receipts/sketchup.json` under the selected Plugins directory.
It records file hashes, adapter/Core/host versions, the target interpreter, `server_path.txt`, and
the exact host paths touched.

## Verify

```bash
dcc-mcp-sketchup status --dcc-path /path/to/SketchUp --json
dcc-mcp-sketchup verify --dcc-path /path/to/SketchUp --python /path/to/python --json
```

`verify` checks the receipt and hashes, diagnoses a missing or stale `server_path.txt`, imports the
adapter in the selected interpreter, checks Ruby bootstrap diagnostics, and calls the read-only
`sketchup_session__get_status` tool through live sidecar readiness. Only all-green evidence sets
`directly_usable: true`.

## Upgrade

```bash
python -m pip install --upgrade dcc-mcp-sketchup
dcc-mcp-sketchup upgrade --dcc-path /path/to/SketchUp --json --dry-run
dcc-mcp-sketchup upgrade --dcc-path /path/to/SketchUp --python /path/to/python --json --yes
```

Upgrade uses the same staged transaction. Any staging, swap, or receipt failure restores the
previous extension and receipt. Close SketchUp and retry the exact command only when exit `50`
reports an actual file lock.

## Uninstall

```bash
dcc-mcp-sketchup uninstall --dcc-path /path/to/SketchUp --json --dry-run
dcc-mcp-sketchup uninstall --dcc-path /path/to/SketchUp --json --yes
python -m pip uninstall dcc-mcp-sketchup
```

Uninstall consumes the receipt and removes only the recorded extension directory and registration
file. It refuses to delete an unreceipted payload and is idempotent when all owned paths are absent.
It never closes SketchUp or the user's model.

## Troubleshooting

- **Exit `10`, host/profile:** start the intended SketchUp version once, or pass its executable
  with `--dcc-path`. The host and profile versions must match.
- **Exit `10`, interpreter/Core:** pass the Python environment containing this adapter and Core
  with `--python`; inspect the recorded interpreter and versions in the JSON plan.
- **Exit `40`, stale `server_path.txt`:** the recorded environment moved or was recreated. Run
  `dcc-mcp-sketchup upgrade --dcc-path /path/to/SketchUp --python /path/to/python --json --yes`.
- **Exit `40`, artifact/import:** run `status --json`, then use `upgrade --yes` to repair the exact
  selected profile.
- **Exit `40`, readiness:** open SketchUp, let the extension start, inspect `dcc-mcp-cli list`, and
  rerun `verify`. Transport or copied files alone are not readiness evidence.
- **Exit `50`, locked install:** close every SketchUp process using that profile and repeat the
  command. No existing extension was deleted before this result.
- **Ruby bootstrap failure:** inspect
  `.dcc-mcp/logs/sketchup-bootstrap-errors.jsonl` under the Plugins directory. The startup hook
  records timestamp, stage, error class, and message; a logging failure is also surfaced in the
  SketchUp warning instead of being swallowed.
- **Manual profile override:** during migration, `--plugins-dir` selects an exact versioned Plugins
  directory. Prefer the uniform `--dcc-path` flag for new automation.
