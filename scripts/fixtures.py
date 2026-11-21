"""Deterministic synthetic source files and an owned transformation graph."""

import csv
import json
from pathlib import Path
import random


def generate(workspace: Path, count=1200):
    workspace.mkdir(parents=True, exist_ok=True)
    rng = random.Random(8152026)
    with (workspace / "customers.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["customer_id", "name", "region"])
        for i in range(40):
            writer.writerow(
                [
                    f"C{i:03}",
                    f"Synthetic customer {i:03}",
                    ["North", "South", "East", "West"][i % 4],
                ]
            )
    with (workspace / "orders.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["order_id", "customer_id", "amount_cents"])
        for i in range(count):
            writer.writerow(
                [i + 1, f"C{rng.randrange(40):03}", rng.randrange(100, 50000)]
            )
    schema = {
        "fields": {
            "order_id": {"type": "integer", "minimum": 1},
            "customer_id": {"type": "string", "min_length": 1},
            "amount_cents": {"type": "integer", "minimum": 0},
        }
    }
    (workspace / "schema.json").write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (workspace / "report_template.txt").write_text(
        "# {{title}}\n\nSynthetic fixture output. Amounts are integer cents.\n\n{{rows}}\n\nTotal cents: {{total}}\n",
        encoding="utf-8",
        newline="\n",
    )

    def action(
        identity, operation, *, deps=(), inputs=(), output, argv=(), config=None
    ):
        return {
            "id": identity,
            "operation": operation,
            "deps": list(deps),
            "inputs": list(inputs),
            "outputs": [output],
            "argv": list(argv),
            "config": config or {},
        }

    spec = {
        "format": 1,
        "actions": [
            action(
                "orders",
                "csv_to_json",
                inputs=["orders.csv"],
                output="orders.json",
                argv=["--sort-by", "order_id"],
                config={
                    "columns": {
                        "order_id": "integer",
                        "customer_id": "string",
                        "amount_cents": "integer",
                    }
                },
            ),
            action(
                "customers",
                "csv_to_json",
                inputs=["customers.csv"],
                output="customers.json",
                argv=["--sort-by", "customer_id"],
                config={
                    "columns": {
                        "customer_id": "string",
                        "name": "string",
                        "region": "string",
                    }
                },
            ),
            action(
                "schema_source",
                "generate_constants",
                inputs=["schema.json"],
                output="order_schema.py",
                argv=["--name", "ORDER_SCHEMA"],
            ),
            action(
                "validated",
                "validate_rows",
                deps=["orders"],
                inputs=["schema.json"],
                output="orders.valid.json",
                config={"rows_dep": "orders", "schema_input": "schema.json"},
            ),
            action(
                "revenue",
                "join_aggregate",
                deps=["validated", "customers"],
                output="revenue.json",
                argv=["--currency", "USD"],
                config={"orders_dep": "validated", "customers_dep": "customers"},
            ),
            action(
                "report",
                "render_report",
                deps=["revenue"],
                inputs=["report_template.txt"],
                output="report.md",
                argv=["--title", "Revenue by region"],
                config={
                    "revenue_dep": "revenue",
                    "template_input": "report_template.txt",
                },
            ),
            action(
                "bundle",
                "bundle",
                deps=["report", "revenue", "schema_source"],
                output="bundle.json",
                argv=["--label", "synthetic-build-demo"],
            ),
        ],
    }
    (workspace / "build.json").write_text(
        json.dumps(spec, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return spec


def edit_first_amount(workspace, delta=137):
    path = workspace / "orders.csv"
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        fields = reader.fieldnames
    rows[0]["amount_cents"] = str(int(rows[0]["amount_cents"]) + delta)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    generate(Path(__file__).resolve().parents[1] / "fixture")
