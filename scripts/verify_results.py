"""Independent evidence checks for saved builds and selected release contents."""

import argparse
import ast
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return (
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()


def read_rows(path):
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def verify(out):
    experiments = json.loads((out / "results/experiments.json").read_text())
    assert [e["name"] for e in experiments] == [
        "clean",
        "warm",
        "selective_edit",
        "cache_corruption",
        "failed_action",
        "same_output_edit",
    ]
    content_digests = {}
    for experiment in experiments:
        folder = out / "results/experiments" / experiment["name"]
        result = json.loads((folder / "result.json").read_text())
        publication = json.loads((folder / "publication.json").read_text())
        assert result["status"] == experiment["expected_status"]
        built = sorted(
            name
            for name, action in result["actions"].items()
            if action["status"] == "BUILT"
        )
        assert built == sorted(experiment["expected_built"]), experiment["name"]
        events = [
            json.loads(line)
            for line in (folder / "trace.jsonl").read_text().splitlines()
        ]
        assert events == result["events"]
        assert [e["event_id"] for e in events] == list(range(len(events)))
        assert [e["time_ns"] for e in events] == sorted(e["time_ns"] for e in events)
        spec = json.loads((folder / "sources/build.json").read_text())
        actions = {a["id"]: a for a in spec["actions"]}
        for identity, action_result in result["actions"].items():
            action = actions[identity]
            material = {
                "key_format": 1,
                "id": identity,
                "operation": action["operation"],
                "argv": action["argv"],
                "config": action["config"],
                "outputs": sorted(action["outputs"]),
                "sources": {
                    name: {
                        "sha256": digest((folder / "sources" / name).read_bytes()),
                        "bytes": (folder / "sources" / name).stat().st_size,
                    }
                    for name in sorted(action["inputs"])
                },
                "dependency_outputs": {
                    name: result["actions"][name]["files"]
                    for name in sorted(action["deps"])
                },
                "tool": result["tool"],
            }
            assert digest(canonical(material)) == action_result["key"]
            assert action_result["duration_ns"] > 0
            terminal = [
                e
                for e in events
                if e.get("action") == identity
                and e["event"] in ["ACTION_BUILT", "CACHE_HIT"]
            ]
            assert len(terminal) == 1 and terminal[0]["key"] == action_result["key"]
            assert terminal[0]["output_bytes"] == sum(
                f["bytes"] for f in action_result["files"].values()
            )
        starts = {
            e["action"]: e["event_id"] for e in events if e["event"] == "ACTION_STARTED"
        }
        finishes = {
            e["action"]: e["event_id"]
            for e in events
            if e["event"] in ["ACTION_BUILT", "CACHE_HIT"]
        }
        for identity, start in starts.items():
            assert all(finishes[dep] < start for dep in actions[identity]["deps"])
        pointer = json.loads((folder / "current.json").read_text())
        release_bytes = (folder / "selected_release/release.json").read_bytes()
        assert digest(release_bytes) == pointer["manifest_sha256"]
        release = json.loads(release_bytes)
        file_records = {
            name: action["files"] for name, action in release["actions"].items()
        }
        assert digest(canonical(file_records)) == pointer["artifact_digest"]
        content_digests[experiment["name"]] = pointer["artifact_digest"]
        for identity, action in release["actions"].items():
            for name, metadata in action["files"].items():
                data = (
                    folder / "selected_release/actions" / identity / name
                ).read_bytes()
                assert (
                    len(data) == metadata["bytes"]
                    and digest(data) == metadata["sha256"]
                )
        if result["status"] == "SUCCESS":
            assert result["pointer"] == pointer
            assert set(result["actions"]) == set(actions)
        else:
            assert result["pointer"] is None
            assert (
                publication["pointer_before_sha256"]
                == publication["pointer_after_sha256"]
            )
            failed = next(e for e in events if e["event"] == "ACTION_FAILED")
            assert failed["staged_files"] == ["report.md"]
            assert failed["key"] not in publication["cache_keys_after"]
            assert (
                publication["release_count_before"]
                == publication["release_count_after"]
            )
        assert publication["remaining_staging_entries"] == 0
        # Independently recompute revenue from captured CSV sources, rather than trusting actions.
        customers = {
            row["customer_id"]: row["region"]
            for row in read_rows(folder / "sources/customers.csv")
        }
        orders = read_rows(folder / "sources/orders.csv")
        expected = {}
        for order in orders:
            group = expected.setdefault(
                customers[order["customer_id"]], {"orders": 0, "amount_cents": 0}
            )
            group["orders"] += 1
            group["amount_cents"] += int(order["amount_cents"])
        revenue = json.loads(
            (folder / "selected_release/actions/revenue/revenue.json").read_text()
        )
        assert revenue["regions"] == expected and revenue["orders"] == len(orders)
        assert revenue["total_cents"] == sum(int(row["amount_cents"]) for row in orders)
        bundle = json.loads(
            (folder / "selected_release/actions/bundle/bundle.json").read_text()
        )
        for name, item in bundle["files"].items():
            data = (folder / "selected_release/actions" / name).read_bytes()
            assert (
                item["sha256"] == digest(data)
                and item["bytes"] == len(data)
                and item["text"].encode() == data
            )
        ast.parse(
            (
                folder / "selected_release/actions/schema_source/order_schema.py"
            ).read_text()
        )
    assert content_digests["clean"] == content_digests["warm"]
    assert content_digests["selective_edit"] != content_digests["warm"]
    assert (
        len(
            {
                content_digests[name]
                for name in [
                    "selective_edit",
                    "cache_corruption",
                    "failed_action",
                    "same_output_edit",
                ]
            }
        )
        == 1
    )
    corruption = json.loads((out / "results/corruption.json").read_text())
    assert (
        corruption["before_sha256"]
        == corruption["repaired_sha256"]
        != corruption["corrupted_sha256"]
    )
    corrupt_bytes = (out / "results/rejected_object/files/customers.json").read_bytes()
    assert digest(corrupt_bytes) == corruption["corrupted_sha256"]
    for name, expected in json.loads(
        (out / "results/source-sha256.json").read_text()
    ).items():
        assert (
            digest((ROOT / name).read_bytes().replace(b"\r\n", b"\n")) == expected
        ), name
    print(
        "Verified six real build scenarios, action keys, dependencies, selected releases, source totals and corruption evidence."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    verify(parser.parse_args().output_dir.resolve())
