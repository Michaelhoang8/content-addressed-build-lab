# Cache integrity, execution and publication

## Single local writer and concurrent actions

A build creates `cache/build.lock` with exclusive creation. If it exists, another build is refused; it is not silently stolen. A normal exit removes it. Process death can leave a stale lock. Before removing one manually, verify no build still owns the cache. The engine does not coordinate multiple caches writing the same output directory; the caller must use one writer for that directory too.

Within the lock, a thread pool of one to four workers executes ready actions. Ready IDs are selected in sorted order. A dependent is submitted only after all dependency futures have returned complete action results. If one action fails, no new work is scheduled; already-running siblings finish and may publish valid cache objects. The whole-release pointer is not updated by the failed build.

## Object cache

Objects live at `cache/objects/<action-key>/`, with a manifest and `files/` tree. Before accepting a hit, the engine checks key, output names, file lengths and hashes, rejects symlinked objects/outputs, and rejects missing/extra files. Corrupt objects move to `cache/quarantine/` before rebuilding.

A miss executes into an owned temporary directory under `cache/staging/`. The operation must produce exactly its declared files. The engine computes output records and writes the manifest, then renames the complete staged directory into the object location. Staging and destination share the cache filesystem. If execution fails, staged files are recorded in the failure trace and removed; no object for the failed key is published.

Hashes address accidental corruption, not an adversarial cache writer. Someone who can replace both a file and its manifest can poison this local cache. Source-path checks and owned operations are not a sandbox against malicious filesystem races. Cache and output directories must be trusted and not modified externally during a build.

## Complete release selection

After every action succeeds, verified object contents are copied into a private release directory. `release.json` records all action keys and file hashes. The whole directory is renamed under `output/releases/` with a unique name. A temporary pointer file is written and fsynced, then atomically replaces `output/current.json`.

Readers first obtain `current.json`, then read that immutable release directory. They should not choose whichever staging/release directory happens to be newest. Releases are retained; garbage collection is not implemented. An error between release-directory publication and pointer selection can leave a complete unreferenced release, which is safe to inspect but not selected.

The selected pointer changes only after the complete release exists. Independent tests inject an error specifically at pointer replacement and verify the old pointer and referenced bytes remain intact. A later build reuses valid cached actions and can publish successfully. The action-failure experiment separately observes an actual staged partial file while proving the previous selected pointer stays byte-identical.

## Atomicity is not durability

Same-filesystem `os.replace` supplies the publication boundary assumed by this local design. Pointer file data is fsynced; all artifact files and containing directories are not. No power-loss, disk-failure or network-filesystem durability guarantee is made. Source snapshot acquisition is a sequence of file reads, not an atomic filesystem snapshot across files.

On Windows, rename may fail with access/sharing-related error codes. The helper retries only `PermissionError` carrying Windows code 5, 32 or 33, with five bounded delays of 10/20/30/40/50 ms before a final attempt. Each retry records the code and delay. Other errors, including the generic injected pointer error, propagate immediately. No cause such as a particular background program is inferred, and destinations are never deleted to force a rename.
