# Install DCC-MCP SketchUp

This runbook is the contract for agent-driven installation. The lifecycle command plans by
default and uses stable exit codes: `0` success, `10` preflight, `20` acquire, `30` install,
`40` verify, and `50` restart required because an installed file is locked.

## Requirements

- SketchUp Desktop 2021 or newer on Windows or macOS.
- Python 3.9 or newer containing `dcc-mcp-sketchup` and
  `dcc-mcp-core>=0.20.36,<1.0.0`.
- A versioned per-user SketchUp profile created by starting the selected SketchUp version once.

SketchUp Desktop is supported on Windows and macOS. Linux is not a supported SketchUp host
platform; Linux CI validates the installer plan, receipt, and package contracts only.
Interpreter selection is `--python`, then `DCC_MCP_INSTALL_PYTHON`, then the interpreter running
the command.

## Supported versions

SketchUp 2021 and newer versioned profiles are supported. The installer selects the newest
profile by default. `--dcc-path` selects an exact SketchUp executable or application bundle and
requires a matching versioned profile; it never silently installs into another SketchUp version.

## Two runtimes: host-side Python and in-SketchUp Ruby

The adapter is two programs separated by an authenticated loopback socket, and the boundary
determines what each side can prove.

| | Host-side Python sidecar | In-SketchUp Ruby extension |
| --- | --- | --- |
| Runs in | A separate Python process, one per SketchUp PID | The SketchUp process, on the UI thread |
| Owns | MCP surface, skill schemas, install lifecycle, compatibility matrix | The model: every read and every mutation |
| Started by | The Ruby extension, which passes it the port and token | SketchUp, when it loads the Plugins directory |
| Can see | Nothing about the model until the host answers | The whole model, and only on the UI thread |

**Discovery order.** SketchUp launches the sidecar, never the reverse. The extension binds an
ephemeral loopback port, generates a random per-session token, exports
`DCC_MCP_SKETCHUP_BRIDGE_PORT` and `DCC_MCP_SKETCHUP_BRIDGE_TOKEN` to the child, and spawns the
sidecar. The sidecar polls `bridge.health` until SketchUp answers and exits when the host PID
disappears. Readiness is therefore only ever established by the host answering across the
socket — a running Python process, or files copied into place, are not readiness evidence.

**Bridge protocol.** One JSON-RPC request per connection over `127.0.0.1`, at most 16
connections and one request in flight on the UI thread. Requests carry a 32-hex request id and a
deadline; responses are correlated by id and compared in constant time. `bridge.health` is a
virtual method the Ruby runtime routes to `diagnostics.ping`.

**Mutations are proven on the Ruby side.** Python cannot read the model, so every mutating tool
requires the Ruby extension to re-read the target and report `expected` / `actual` evidence
before the tool returns success. A read-back that disagrees aborts the undo operation and
reports the tool, the check, both values, and the host product year. Python additionally
rejects any mutating result that arrives without a verified read-back block.

## Host support

Supported versions come from one machine-readable file, `compat_matrix.json`, shipped inside the
wheel. It is the single source of truth: the `doctor`, this installer's `verify`, and the Ruby
API probe all read it. A product year outside the declared ranges is rejected with an explicit
error code; it is never silently treated as compatible.

SketchUp reports its version either as a product year (`2026.0`) or as a build line
(`26.0.575`). Both name the same application, so both are folded onto the product year before
classification.

```bash
dcc-mcp-sketchup doctor --json
```

`doctor` separates what it observes from what it infers:

- `checks.installed_host` — a SketchUp executable and matching profile exist on this machine.
- `checks.live_host` — a SketchUp process answered `bridge.health`. Only here are
  `sketchup_version` and `ruby_version` observed rather than inferred.
- `checks.host_matrix` — the version classified against the matrix, with `source` naming whether
  the version came from the live host or from an installed profile.
- `checks.api_surface` — which required Ruby API symbols the live host actually exposes. The
  symbol list is sent from the matrix, so the Ruby side never owns a second copy that could drift.
- `verify.directly_usable` — true only when every check passed, which requires a live host.

An installed-but-not-running SketchUp exits `40` with error code
`sketchup_host_not_running`: an executable on disk is not evidence the adapter works.

`verify --json` reports the same matrix verdict for the profile being verified, under
`verify.host_matrix`. It only fails on the matrix when a host version was actually resolved and
found unsupported — on Linux, where no SketchUp release exists, nothing is discovered and the
readiness check remains the gate.

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
