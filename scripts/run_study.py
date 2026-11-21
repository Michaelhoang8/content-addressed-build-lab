"""Execute real local build scenarios, retain release evidence and generate a report."""

import argparse
import copy
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from buildlab.engine import Engine
from fixtures import edit_first_amount, generate
from verify_results import verify
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap
from matplotlib.patches import FancyBboxPatch
import numpy as np


def digest(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="\n")


def closure(spec, changed_source):
    affected = {a["id"] for a in spec["actions"] if changed_source in a["inputs"]}
    while True:
        expanded = affected | {
            a["id"] for a in spec["actions"] if set(a["deps"]) & affected
        }
        if expanded == affected:
            return sorted(affected)
        affected = expanded


def capture(out, quick):
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    transcript = result.stdout + result.stderr
    print(transcript, end="", flush=True)
    (out / "results/test-results.txt").write_text(
        transcript, encoding="utf-8", newline="\n"
    )
    result.check_returncode()
    spec = generate(out / "fixture", count=100 if quick else 1200)
    work = Path(tempfile.mkdtemp(prefix="study-", dir=out / "build"))
    workspace, cache, output = (
        work / name for name in ["workspace", "cache", "output"]
    )
    shutil.copytree(out / "fixture", workspace)
    engine = Engine(workspace, cache, output, workers=3)
    experiments = []

    def run(name, effective_spec, expected_built, status="SUCCESS"):
        write_json(workspace / "build.json", effective_spec)
        folder = out / "results/experiments" / name
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copytree(workspace, folder / "sources", dirs_exist_ok=True)
        before = (
            (output / "current.json").read_bytes()
            if (output / "current.json").exists()
            else None
        )
        releases_before = (
            len(list((output / "releases").iterdir()))
            if (output / "releases").exists()
            else 0
        )
        result = engine.build(effective_spec)
        write_json(folder / "result.json", result)
        (folder / "trace.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in result["events"]),
            encoding="utf-8",
            newline="\n",
        )
        actual_built = sorted(
            action
            for action, value in result["actions"].items()
            if value["status"] == "BUILT"
        )
        if result["status"] != status or actual_built != sorted(expected_built):
            raise AssertionError(
                f"Unexpected experiment outcome: {name}: {result['status']} / {actual_built}; {result['failure']}"
            )
        current = (output / "current.json").read_bytes()
        (folder / "current.json").write_bytes(current)
        pointer = json.loads(current)
        shutil.copytree(
            output / "releases" / pointer["release"],
            folder / "selected_release",
            dirs_exist_ok=True,
        )
        publication = {
            "pointer_before_sha256": (
                hashlib.sha256(before).hexdigest() if before else None
            ),
            "pointer_after_sha256": hashlib.sha256(current).hexdigest(),
            "release_count_before": releases_before,
            "release_count_after": len(list((output / "releases").iterdir())),
            "remaining_staging_entries": len(list((cache / "staging").iterdir())),
            "cache_keys_after": sorted(p.name for p in (cache / "objects").iterdir()),
        }
        write_json(folder / "publication.json", publication)
        actual_built = sorted(
            action
            for action, value in result["actions"].items()
            if value["status"] == "BUILT"
        )
        if result["status"] != status or actual_built != sorted(expected_built):
            raise AssertionError(
                f"Unexpected experiment outcome: {name}: {result['status']} / {actual_built}"
            )
        experiments.append(
            {
                "name": name,
                "expected_status": status,
                "expected_built": sorted(expected_built),
            }
        )
        print(
            f'{name}: {result["status"]}, built={len(actual_built)}, hits={sum(a["status"] == "HIT" for a in result["actions"].values())}'
        )
        return result

    run("clean", spec, [a["id"] for a in spec["actions"]])
    run("warm", spec, [])
    edit_first_amount(workspace)
    edited = run("selective_edit", spec, closure(spec, "orders.csv"))
    key = edited["actions"]["customers"]["key"]
    target = cache / "objects" / key / "files/customers.json"
    before = target.read_bytes()
    target.write_bytes(before + b"\nCONTROLLED_CORRUPTION\n")
    corrupt = target.read_bytes()
    repaired = run("cache_corruption", spec, ["customers"])
    quarantine = next((cache / "quarantine").iterdir())
    shutil.copytree(quarantine, out / "results/rejected_object", dirs_exist_ok=True)
    write_json(
        out / "results/corruption.json",
        {
            "action": "customers",
            "key": key,
            "before_sha256": hashlib.sha256(before).hexdigest(),
            "corrupted_sha256": hashlib.sha256(corrupt).hexdigest(),
            "repaired_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "corrupted_bytes": len(corrupt),
            "cache_rejections": sum(
                e["event"] == "CACHE_REJECTED" for e in repaired["events"]
            ),
        },
    )
    broken = copy.deepcopy(spec)
    report = next(a for a in broken["actions"] if a["id"] == "report")
    report.update(operation="fail_after_write", argv=[], config={})
    run("failed_action", broken, [], status="FAILED")
    with (workspace / "orders.csv").open("ab") as stream:
        stream.write(b"\n")
    run("same_output_edit", spec, ["orders"])
    write_json(out / "results/experiments.json", experiments)
    write_json(
        out / "results/environment.json",
        {
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "workers": 3,
            "orders": 100 if quick else 1200,
            "fixture_seed": 8152026,
            "matplotlib": matplotlib.__version__,
            "numpy": np.__version__,
            "timing_scope": "Engine trace origin after source snapshot/tool identity through BUILD_FINISHED; actual local durations",
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument(
        "--reuse",
        action="store_true",
        help="Replot preserved experiments with matching execution sources",
    )
    args = parser.parse_args()
    out = args.output_dir.resolve()
    for folder in ["build", "results", "docs/charts"]:
        (out / folder).mkdir(parents=True, exist_ok=True)
    measured = sorted((ROOT / "buildlab").glob("*.py")) + [ROOT / "scripts/fixtures.py"]
    hashes = {p.relative_to(ROOT).as_posix(): digest(p) for p in measured}
    if args.reuse:
        old = json.loads((out / "results/source-sha256.json").read_text())
        if any(old.get(k) != v for k, v in hashes.items()):
            parser.error("Execution sources changed; rerun scenarios")
    else:
        capture(out, args.quick)
    for path in [ROOT / "scripts/run_study.py", ROOT / "scripts/verify_results.py"]:
        hashes[path.relative_to(ROOT).as_posix()] = digest(path)
    write_json(out / "results/source-sha256.json", hashes)
    verify(out)
    render(out)


def render(out):
    experiments = json.loads((out / "results/experiments.json").read_text())
    results = {
        e["name"]: json.loads(
            (out / "results/experiments" / e["name"] / "result.json").read_text()
        )
        for e in experiments
    }
    spec = json.loads((out / "fixture/build.json").read_text())
    summaries = []
    for name, result in results.items():
        summaries.append(
            {
                "scenario": name,
                "status": result["status"],
                "built": sum(
                    a["status"] == "BUILT" for a in result["actions"].values()
                ),
                "hits": sum(a["status"] == "HIT" for a in result["actions"].values()),
                "not_completed": len(result["not_completed"]),
                "engine_interval_ms": result["duration_ns"] / 1e6,
                "cache_rejections": sum(
                    e["event"] == "CACHE_REJECTED" for e in result["events"]
                ),
                "successful_action_output_bytes": sum(
                    f["bytes"]
                    for a in result["actions"].values()
                    for f in a["files"].values()
                ),
            }
        )
    with (out / "results/summary.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 11,
            "figure.facecolor": "#f5f7f4",
            "axes.facecolor": "#f5f7f4",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.titlelocation": "left",
            "axes.titleweight": "bold",
            "savefig.dpi": 150,
        }
    )
    green, dark, red = "#4d8563", "#253d37", "#b15d52"
    positions = {
        "customers": (0, 2.1),
        "orders": (0, 1),
        "schema_source": (0, -0.2),
        "validated": (1.5, 1),
        "revenue": (3, 1.6),
        "report": (4.5, 1.6),
        "bundle": (6, 0.65),
    }
    fig, ax = plt.subplots(figsize=(13, 4.6))
    for action in spec["actions"]:
        x, y = positions[action["id"]]
        for dep in action["deps"]:
            sx, sy = positions[dep]
            ax.annotate(
                "",
                xy=(x - 0.5, y),
                xytext=(sx + 0.5, sy),
                arrowprops={
                    "arrowstyle": "->",
                    "color": "#9baea1",
                    "lw": 1.4,
                    "connectionstyle": (
                        "arc3,rad=0.17"
                        if dep == "revenue" and action["id"] == "bundle"
                        else "arc3,rad=0"
                    ),
                },
            )
    for action in spec["actions"]:
        x, y = positions[action["id"]]
        ax.add_patch(
            FancyBboxPatch(
                (x - 0.52, y - 0.28),
                1.04,
                0.56,
                boxstyle="round,pad=0.02,rounding_size=0.025",
                facecolor=green,
                edgecolor=green,
                zorder=2,
            )
        )
        ax.text(
            x,
            y,
            action["id"],
            ha="center",
            va="center",
            color="white",
            fontsize=11,
            zorder=3,
        )
        ax.text(
            x,
            y - 0.45,
            action["outputs"][0],
            ha="center",
            va="top",
            fontsize=9,
            color=dark,
        )
    ax.set_xlim(-0.75, 6.8)
    ax.set_ylim(-1, 2.8)
    ax.axis("off")
    ax.set_title(
        "Declared dependencies and actual produced artifacts", fontsize=21, pad=16
    )
    fig.tight_layout()
    fig.savefig(out / "docs/charts/graph.png")
    plt.close(fig)
    order = [
        "orders",
        "customers",
        "schema_source",
        "validated",
        "revenue",
        "report",
        "bundle",
    ]
    names = list(results)
    codes = {"HIT": 0, "BUILT": 1, "FAILED": 2, "NOT_RUN": 3}
    matrix = []
    for action in order:
        values = []
        for result in results.values():
            status = result["actions"].get(action, {}).get("status", "NOT_RUN")
            if any(
                e["event"] == "ACTION_FAILED" and e.get("action") == action
                for e in result["events"]
            ):
                status = "FAILED"
            values.append(codes[status])
        matrix.append(values)
    fig, ax = plt.subplots(figsize=(12, 5.3))
    ax.imshow(
        matrix,
        cmap=ListedColormap(["#d8e2d8", green, red, "#f0eee6"]),
        vmin=0,
        vmax=3,
        aspect="auto",
    )
    ax.set_xticks(range(6), [n.replace("_", "\n") for n in names])
    ax.xaxis.tick_top()
    ax.set_yticks(range(7), order)
    labels = ["HIT", "BUILT", "FAILED", "NOT RUN"]
    for y in range(7):
        for x in range(6):
            ax.text(
                x,
                y,
                labels[matrix[y][x]],
                ha="center",
                va="center",
                fontsize=10,
                color="white" if matrix[y][x] in [1, 2] else dark,
            )
    ax.set_title("Recorded action outcomes", fontsize=21, pad=24)
    fig.tight_layout()
    fig.savefig(out / "docs/charts/invalidation.png")
    plt.close(fig)
    fig, axes = plt.subplots(
        1, 2, figsize=(13, 5.2), gridspec_kw={"width_ratios": [1.7, 1]}
    )
    clean = results["clean"]
    starts = {
        e["action"]: e["time_ns"]
        for e in clean["events"]
        if e["event"] == "ACTION_STARTED"
    }
    ends = {
        e["action"]: e["time_ns"]
        for e in clean["events"]
        if e["event"] == "ACTION_BUILT"
    }
    for y, action in enumerate(order):
        axes[0].barh(
            y,
            (ends[action] - starts[action]) / 1e6,
            left=starts[action] / 1e6,
            height=0.55,
            color=green,
        )
    axes[0].set_yticks(range(7), order)
    axes[0].invert_yaxis()
    axes[0].set_xlabel("Actual trace clock, milliseconds")
    axes[0].set_title("Clean-build action intervals", fontsize=19, pad=20)
    axes[1].barh(
        range(6),
        [s["engine_interval_ms"] for s in summaries],
        color=[red if s["status"] == "FAILED" else "#819b8d" for s in summaries],
    )
    axes[1].set_yticks(range(6), [s["scenario"].replace("_", " ") for s in summaries])
    axes[1].invert_yaxis()
    axes[1].set_xlabel("Actual engine interval, ms")
    axes[1].set_title("Observed engine intervals", fontsize=16, pad=20)
    fig.tight_layout()
    fig.savefig(out / "docs/charts/timing.png")
    plt.close(fig)
    report(out, results, summaries)


def report(out, results, summaries):
    env = json.loads((out / "results/environment.json").read_text())
    corruption = json.loads((out / "results/corruption.json").read_text())
    failure = json.loads(
        (out / "results/experiments/failed_action/publication.json").read_text()
    )
    failed_event = next(
        e for e in results["failed_action"]["events"] if e["event"] == "ACTION_FAILED"
    )
    table = "| Scenario | Built | Cache hits | Not completed | Observed engine ms |\n|---|---:|---:|---:|---:|\n"
    for s in summaries:
        table += f'| {s["scenario"]} | {s["built"]} | {s["hits"]} | {s["not_completed"]} | {s["engine_interval_ms"]:.2f} |\n'
    findings = f"""# Build experiment findings

Six actual local builds over seven owned actions and {env['orders']} synthetic order rows. Statuses, file bytes and durations come from execution, not a simulated scheduler.

{table}

A single source-value edit rebuilt the five-action orders→validated→revenue→report→bundle closure. Customers and generated schema source remained cache hits. Corrupting customers.json caused a checksum/length rejection and rebuild of customers only; its deterministic regenerated bytes preserved downstream action keys.

The failed report action wrote a staged file and then raised. The previous current.json remained byte-identical, and the failed key did not enter the object cache. A later whitespace-only CSV edit rebuilt orders but produced identical normalized bytes, so all six downstream/independent actions were cache hits and the selected artifact content digest stayed unchanged.

Action keys include source bytes, declared output names, dependency output hashes, owned argv/config and Python/tool source identity. Dependency keys themselves are intentionally excluded: unchanged dependency output bytes stop invalidation. Timestamps do not determine cache hits.

Engine durations start after source snapshot and tool-identity preparation and end at BUILD_FINISHED. They include cache verification and release copying/pointer publication. Each scenario is one local observation in sequence, without isolation or statistical performance claims. Concurrent ready actions are demonstrated, not claimed to deliver a general speedup.

Atomic visibility is not power-loss durability. Only pointer file data is fsynced; source snapshots are per-file reads, not an atomic multi-file snapshot under external edits. This is a trusted local cache, not an authenticated or distributed build system.
"""
    (out / "results/findings.md").write_text(findings, encoding="utf-8", newline="\n")
    page = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Content-addressed build lab</title><style>*{{box-sizing:border-box}}body{{margin:0;background:#f5f7f4;color:#253d37;font:16px/1.65 system-ui,sans-serif}}main{{max-width:1340px;margin:auto;padding:0 45px}}header{{padding:45px 0 30px;border-bottom:1px solid #c4d1c7}}h1{{font-size:48px;letter-spacing:-1.5px;line-height:1.12;margin:16px 0}}h2{{font-size:29px;letter-spacing:-.5px;margin:10px 0 22px}}.index{{font:12px ui-monospace,monospace;letter-spacing:1.5px;color:#4d8563}}nav{{display:flex;gap:24px;flex-wrap:wrap;font:12px ui-monospace,monospace;padding-top:20px}}a{{color:#4d8563;text-decoration:none}}section{{padding:38px 0 42px;border-bottom:1px solid #c4d1c7;scroll-margin-top:10px}}img{{width:100%;display:block}}.columns{{display:grid;grid-template-columns:1fr 1fr;gap:50px}}.note{{font-size:13px;color:#60746a}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.7 ui-monospace,monospace;background:#e8eee7;padding:22px}}.record{{display:grid;grid-template-columns:160px 1fr;border-bottom:1px solid #d5ded5;padding:12px 0;gap:16px;font:13px/1.6 ui-monospace,monospace}}.record b{{color:#4d8563}}footer{{padding:28px 0;font-size:12px;color:#60746a}}@media(max-width:800px){{main{{padding:0 18px}}h1{{font-size:36px}}.columns{{grid-template-columns:1fr;gap:20px}}.record{{grid-template-columns:1fr}}}}</style><main>
<header id="overview"><div class="index">BUILD SYSTEMS / LOCAL ARTIFACT CACHE / PYTHON</div><h1>Content-addressed actions<br>and atomic release selection</h1><p>Seven actual file-producing transformations. Six recorded build scenarios: clean, warm, selective edit, cache corruption, action failure and unchanged-output invalidation.</p><nav><a href="#graph">01 Action graph</a><a href="#invalidation">02 Invalidation</a><a href="#cache">03 Cache repair</a><a href="#publication">04 Publication</a><a href="#measurements">05 Timing and checks</a></nav></header>
<section id="graph"><div class="index">01 / DECLARED DEPENDENCIES</div><h2>CSV and schema sources to a verified artifact bundle</h2><img src="charts/graph.png" alt="Actual seven-action dependency graph and produced filenames"><div class="columns"><div><h3>Owned transformations</h3><p>Normalize CSV rows, validate a local schema subset, join customer regions, aggregate integer cents, generate a schema constant, render Markdown and bundle artifacts. No arbitrary shell commands or plugins execute.</p></div><div><h3>Ready-action concurrency</h3><p>Three workers can execute independent actions. A dependent starts only after every dependency has a complete verified cache object. Barrier-based tests establish concurrent readiness without sleep assumptions.</p></div></div></section>
<section id="invalidation"><div class="index">02 / CONTENT-BASED REUSE</div><h2>Observed rebuild boundaries</h2><img src="charts/invalidation.png" alt="Measured built hit failed and blocked statuses for all six scenarios"><p class="note">The value edit changes the five-action dependency closure. A whitespace-only source edit changes the orders key but not its normalized output; downstream keys remain reusable. A failed action prevents the bundle and final release selection.</p></section>
<section id="cache"><div class="index">03 / CORRUPTED CACHE OBJECT</div><h2>Verify output bytes before accepting a hit</h2><div class="record"><b>Action</b><span>customers / customers.json</span></div><div class="record"><b>Original SHA-256</b><span>{corruption['before_sha256']}</span></div><div class="record"><b>Corrupted SHA-256</b><span>{corruption['corrupted_sha256']}</span></div><div class="record"><b>Rebuilt SHA-256</b><span>{corruption['repaired_sha256']}</span></div><div class="columns"><div><h3>Recorded rejection</h3><p>The experiment appends controlled bytes to a cached output. Verification rejects it, moves the object to quarantine and rebuilds customers. The preserved rejected object contains the actual corrupt file and its old manifest.</p></div><div><h3>Trust boundary</h3><p>Hashes detect accidental corruption; they do not authenticate a cache whose files and manifests an attacker can rewrite. The cache is local and trusted. A lock excludes concurrent builds sharing that cache.</p></div></div></section>
<section id="publication"><div class="index">04 / FAILURE AND VISIBILITY</div><h2>A staged file does not become a selected release</h2><div class="record"><b>Injected failure</b><span>{failed_event['error']}</span></div><div class="record"><b>Staged file observed</b><span>{', '.join(failed_event['staged_files'])}</span></div><div class="record"><b>Pointer before</b><span>{failure['pointer_before_sha256']}</span></div><div class="record"><b>Pointer after</b><span>{failure['pointer_after_sha256']}</span></div><pre>action outputs → private staging → verified cache object rename
all actions complete → new release directory → atomic current.json replacement</pre><p class="note">The pointer hashes are identical after failure. Valid sibling cache objects may remain reusable. Atomic visibility is not crash durability: pointer data is fsynced, but directories and every artifact are not. Independent tests also inject a failure at current.json replacement.</p></section>
<section id="measurements"><div class="index">05 / ACTUAL LOCAL INTERVALS AND VERIFICATION</div><h2>Action timing and reproduction evidence</h2><img src="charts/timing.png" alt="Actual action intervals and observed scenario engine durations"><div class="columns"><div><h3>Timing scope</h3><p>Actual perf_counter_ns intervals, including cache checks and final release copying. Source snapshot and tool-identity preparation precede the trace clock. One sequential local observation per scenario is not a speedup benchmark.</p><p>Source bytes are frozen per build after per-file reads; external edits are not captured as an atomic multi-file snapshot.</p></div><div><h3>Reproduce</h3><pre>python -m unittest discover -s tests -v
python scripts/run_study.py --output-dir build/my-run
python scripts/verify_results.py --output-dir build/my-run</pre><p class="note">Saved evidence includes source snapshots, complete selected releases, action traces, cache corruption and independent recomputation of revenue totals.</p></div></div><p class="note">Python {env['python']} · {env['platform']} · {env['orders']} synthetic orders · recorded {env['recorded_at_utc']}</p></section><footer>AI-assisted portfolio exercise. Actual local execution and file artifacts. No remote cache, distributed scheduler, arbitrary command runner or production durability claim.</footer></main></html>"""
    (out / "docs/report.html").write_text(page, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
