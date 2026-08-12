# Architecture and host contract

The adapter has two process boundaries and one mandatory thread-affinity hop.

1. The Ruby extension loads on SketchUp's UI thread, binds an ephemeral
   `127.0.0.1` TCP listener, generates a random token, and starts the installed
   `dcc-mcp-sketchup` console script.
2. The external Python sidecar hosts `dcc-mcp-core`, binds its lifecycle to the
   SketchUp PID, and sends only allowlisted command names over authenticated
   newline-delimited JSON-RPC.
3. A repeating `UI.start_timer` callback is the only owner of accepted sockets.
   It multiplexes at most 16 connections with zero-timeout `IO.select` and
   nonblocking accept, read, and write operations. Complete one-line frames are
   validated before entering a one-request queue.
4. The same callback executes at most one typed command per tick on SketchUp's
   UI thread, then pumps the correlated response without blocking. There is no
   Ruby network worker thread.

## Protocol invariants

- The listener is always loopback and uses an operating-system-assigned port.
- Every request carries JSON-RPC version `2.0`, a unique 32-character hex ID, a random
  token, a typed method name, and an object-valued parameter map.
- Non-health requests include an absolute millisecond deadline.
- Requests, responses, connections, per-socket I/O, and queues are bounded.
- Read, request, write, and peer-close phases all have deadlines; a connection
  carries exactly one newline-delimited request.
- Responses echo the request ID; the sidecar rejects mismatches.
- The extension launches one sidecar and terminates it on SketchUp shutdown.

## Mutation invariants

- No command evaluates source or dispatches arbitrary methods.
- Every model mutation is wrapped by `start_operation`, `commit_operation`, and
  `abort_operation` on failure.
- Persistent IDs are the public entity reference.
- Destructive operations require explicit targets and are marked with MCP
  `destructive_hint` annotations.
- File output rejects an existing target unless `overwrite=true`.

## Verification tiers

1. Python unit tests cover protocol validation, installer ownership, sidecar PID
   binding, Skill validation, and manifest-to-script integrity.
2. Ruby tests cover syntax, the bounded command allowlist, deadlines, unit
   conversion, persistent IDs, and undo transaction boundaries with host mocks.
3. Real-host acceptance uses a supported SketchUp Desktop build to create,
   inspect, validate, save, and export geometry through typed DCC-MCP tools;
   the exported artifact is parsed independently of SketchUp.
4. Cross-DCC acceptance additionally imports a supported export into another
   live DCC when that host is available. It is reported separately and is never
   inferred from a successful SketchUp export alone.
5. Release acceptance installs the published wheel from public PyPI into a new
   environment and repeats artifact/entry-point checks.
