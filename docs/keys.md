# Declared action semantics and cache keys

## Graph contract

`build.json` has format 1 and 1..32 actions. An action declares its unique ID, owned operation, dependencies, source names, output names, argv and JSON config. Dependencies must exist and the graph must be acyclic. Validation happens before cache mutation. IDs, normalized relative file names, list lengths, configuration depth and action-definition byte size are bounded.

The engine snapshots each distinct source into immutable bytes once per build. A source must resolve to a regular file inside the workspace, at most one megabyte. Total source bytes are capped at four megabytes. Those bytes are reused by all actions that declare the source. This prevents different actions from rereading the same source at different times within the build, but acquisition across multiple files is not atomic under external edits.

## Key material

```text
SHA256(canonical JSON + newline):
  key format version
  action ID + operation
  declared output names
  owned argv + JSON configuration
  source names → SHA256 + byte length
  dependency IDs/output names → SHA256 + byte length
  engine API version + Python implementation/version
  LF-normalized engine.py and operations.py source hashes
```

Canonical JSON uses sorted object keys, compact separators, ASCII escaping and no NaN/Infinity. Configuration requires JSON types and string object keys; tuples and nonstring keys are rejected. File content hashes cover the exact snapshotted bytes. Tool source hashes normalize CRLF to LF so Git checkout line endings do not change tool identity. Runtime version is exact: a Python patch upgrade conservatively invalidates action keys.

Dependency action keys are deliberately absent. If a source byte edit changes an upstream key but its operation produces byte-identical output, a dependent action's observable inputs are unchanged. This is demonstrated by the extra-newline experiment. Including the action ID prevents cross-action cache deduplication even for otherwise identical work; the design favors transparent ownership over maximal reuse.

## Owned operations

`csv_to_json` requires the exact set/count of configured columns, parses integer/string fields, sorts by a unique key and emits canonical JSON. Column mapping order does not matter. `validate_rows` implements a small local schema format for integer/string requirements; it is not a full JSON Schema implementation. `join_aggregate` joins customers and sums integer cents by region. `render_report` substitutes three owned Markdown placeholders. `generate_constants` produces a Python file containing a JSON-backed constant but never executes generated code. `bundle` embeds text artifacts with hashes and sizes.

Each current operation produces one declared output, at most one megabyte. CSV input is capped at 10,000 rows. Arguments are parsed by these functions, not passed to a shell. `fail_after_write` is an explicit test operation that writes a private staged marker and raises.

Any future operation must declare every input that affects its output and preserve key/behavior equivalence. The engine does not sandbox arbitrary code or automatically discover undeclared environment dependencies. The fixed owned transformations are the implementation's trust boundary.
