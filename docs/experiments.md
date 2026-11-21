# Experiments, validation and timing

## Controlled sequence

The default fixture contains 1,200 synthetic orders, 40 synthetic customers, a local schema and a Markdown template. Customer labels are generated, not personal records. Amounts are integer cents. A seeded generator produces the baseline source files; quick mode uses 100 orders.

1. **Clean:** a new empty cache executes all seven actions.
2. **Warm:** unchanged sources and tool identity reuse all seven verified objects.
3. **Selective edit:** increase the first order by 137 cents. An independently traversed dependency closure predicts orders, validated, revenue, report and bundle rebuilds.
4. **Cache corruption:** append controlled bytes to the cached customer JSON. It is rejected and quarantined. Rebuilding produces the original bytes, so customers alone executes and downstream cache keys remain valid.
5. **Failed action:** change report to the owned failure operation. It writes `report.md` in staging and raises. Five dependencies/independent actions are hits; report fails; bundle is not run. The selected release pointer remains unchanged.
6. **Same-output edit:** restore the report definition and append a blank line to orders.csv. The source hash and orders key change, but normalization emits the same rows. Only orders rebuilds; the artifact content digest remains unchanged.

Each folder under `results/experiments/` preserves the effective sources/graph, returned build result, JSONL event trace, actual current pointer, publication counters and a copy of the complete selected release. The failure folder copies the previously selected release, not the failed staged marker. `results/rejected_object/` preserves the actual corrupted cache file with its old manifest.

## What is measured

The engine's trace uses `perf_counter_ns` with a per-build origin after graph validation, source snapshot and tool-identity preparation. The recorded interval continues through cache locking, action execution/validation, release copying, pointer selection and `BUILD_FINISHED`. Action durations include key computation and cache validation; a miss also includes transformation, staging and object publication. Retry delays, if any, are included and logged.

The timeline uses actual start/terminal event timestamps. It includes no simulated scheduler durations and introduces no sleeps to make concurrent actions look larger. The only deliberate waits in production are bounded Windows rename retries after observed access/sharing errors. Barrier waits belong only to a concurrency test.

There is one local observation per scenario in fixed sequence. Warm cache still hashes bytes and copies a complete release, and a selective edit can take longer than a clean build due to filesystem/scheduling noise. These records illustrate behavior rather than establish statistical speedups or compare production build systems.

## Tests and evidence checks

Thirteen tests cover key changes for source bytes, dependency content, argv, config and tool identity; column-map ordering equivalence; expected dependency closure; same-output invalidation stopping; corruption rejection; cycle/unknown dependency/path validation; exclusive cache locking; barrier-established concurrency; staged-action failure; pointer-replacement failure and recovery; bounded Windows retries; and concise CLI exit behavior.

The independent artifact checker reconstructs action-key material from saved source bytes and dependency metadata without calling the engine's key function. It checks dependency-before-start event ordering, every selected file's length/hash, pointer/manifest consistency, artifact digests, cache-rejection evidence and unchanged pointer bytes after failure. It separately recomputes all region revenue totals from captured CSV rows, validates bundle content and parses the generated Python source without executing it.

Text produced by the generator, transformations and evidence writer uses LF. Tool/source provenance normalizes CRLF to LF; copied artifact bytes retain their actual hashes. There are no hashed SVG artifacts. Reproduction creates fresh release IDs and timings but must preserve semantic outcomes and deterministic file content for the same inputs/tool identity.

## Commands

```sh
python -m unittest discover -s tests -v
python -m pip install -r requirements.txt
python scripts/run_study.py --output-dir build/my-run
python scripts/verify_results.py --output-dir build/my-run
```

The driver creates a fresh ignored experiment workspace and bounds the test subprocess. The engine's work is bounded by graph/source/output/row limits; it is not a general timeout-enforced arbitrary command runner. Cache objects, quarantines and releases are retained for inspection with no automatic eviction or cleanup policy.
