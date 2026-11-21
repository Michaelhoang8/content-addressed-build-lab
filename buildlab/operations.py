"""Owned transformations only. No shell execution, plugins or dynamic imports."""

import csv
import hashlib
import io
import json
from pathlib import Path
import re

OPERATIONS = {
    "csv_to_json",
    "validate_rows",
    "join_aggregate",
    "render_report",
    "generate_constants",
    "bundle",
    "fail_after_write",
}


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


def one(dependencies, name):
    files = dependencies[name]
    if len(files) != 1:
        raise ValueError("Operation requires a single-output dependency")
    return next(iter(files.values()))


def flag(argv, name):
    if len(argv) != 2 or argv[0] != name:
        raise ValueError(f"Expected owned argument pair {name} VALUE")
    return argv[1]


def transform(action, inputs, dependencies, stage: Path):
    """Write the declared file; return only after a complete deterministic result."""
    operation = action["operation"]
    argv = action["argv"]
    config = action["config"]
    if len(action["outputs"]) != 1:
        raise ValueError("Owned fixture operations each declare one output")
    output = stage / action["outputs"][0]
    output.parent.mkdir(parents=True, exist_ok=True)
    if operation == "fail_after_write":
        output.write_bytes(b"UNPUBLISHED PARTIAL OUTPUT\n")
        raise RuntimeError("Injected failure after staged file write")
    if operation == "csv_to_json":
        sort_by = flag(argv, "--sort-by")
        if len(inputs) != 1:
            raise ValueError("CSV operation requires one source")
        columns = config["columns"]
        reader = csv.DictReader(
            io.StringIO(next(iter(inputs.values())).decode("utf-8"))
        )
        if (
            reader.fieldnames is None
            or len(reader.fieldnames) != len(columns)
            or set(reader.fieldnames) != set(columns)
        ):
            raise ValueError("CSV columns differ from the declared schema")
        result = []
        for row in reader:
            if (
                len(result) == 10000
                or None in row
                or any(v is None for v in row.values())
            ):
                raise ValueError("Malformed or oversized CSV")
            parsed = {}
            for name, kind in sorted(columns.items()):
                if kind == "integer":
                    if not re.fullmatch(r"-?(0|[1-9][0-9]*)", row[name]):
                        raise ValueError("Invalid integer field")
                    parsed[name] = int(row[name])
                elif kind == "string":
                    parsed[name] = row[name]
                else:
                    raise ValueError("Unsupported CSV type")
            result.append(parsed)
        result.sort(key=lambda row: row[sort_by])
        if len({row[sort_by] for row in result}) != len(result):
            raise ValueError("Sort key must be unique")
        data = canonical(result)
    elif operation == "validate_rows":
        if argv:
            raise ValueError("Validation takes no argv")
        rows = json.loads(one(dependencies, config["rows_dep"]))
        schema = json.loads(inputs[config["schema_input"]])
        if set(schema) != {"fields"} or not isinstance(rows, list):
            raise ValueError("Unsupported local schema format")
        for row in rows:
            if set(row) != set(schema["fields"]):
                raise ValueError("Required fields mismatch")
            for name, rule in schema["fields"].items():
                value = row[name]
                if rule["type"] == "integer":
                    if type(value) is not int or value < rule.get(
                        "minimum", -(1 << 63)
                    ):
                        raise ValueError("Integer schema check failed")
                elif rule["type"] == "string":
                    if not isinstance(value, str) or len(value) < rule.get(
                        "min_length", 0
                    ):
                        raise ValueError("String schema check failed")
                else:
                    raise ValueError("Unsupported schema type")
        data = canonical(rows)
    elif operation == "join_aggregate":
        currency = flag(argv, "--currency")
        rows = json.loads(one(dependencies, config["orders_dep"]))
        customers = json.loads(one(dependencies, config["customers_dep"]))
        lookup = {row["customer_id"]: row for row in customers}
        if len(lookup) != len(customers):
            raise ValueError("Duplicate customer key")
        regions = {}
        for row in rows:
            region = lookup[row["customer_id"]]["region"]
            total = regions.setdefault(region, {"orders": 0, "amount_cents": 0})
            total["orders"] += 1
            total["amount_cents"] += row["amount_cents"]
        data = canonical(
            {
                "currency": currency,
                "regions": regions,
                "orders": len(rows),
                "total_cents": sum(r["amount_cents"] for r in rows),
            }
        )
    elif operation == "render_report":
        title = flag(argv, "--title")
        revenue = json.loads(one(dependencies, config["revenue_dep"]))
        template = inputs[config["template_input"]].decode("utf-8")
        lines = ["| Region | Orders | Amount (cents) |", "|---|---:|---:|"]
        for region, value in sorted(revenue["regions"].items()):
            lines.append(f'| {region} | {value["orders"]} | {value["amount_cents"]} |')
        text = template.replace("{{title}}", title).replace(
            "{{rows}}", "\n".join(lines)
        )
        text = text.replace("{{total}}", str(revenue["total_cents"]))
        if "{{" in text:
            raise ValueError("Unknown template placeholder")
        data = text.encode("utf-8")
    elif operation == "generate_constants":
        name = flag(argv, "--name")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name) or len(inputs) != 1:
            raise ValueError("Invalid generated constant name/source")
        schema = json.loads(next(iter(inputs.values())))
        # JSON string literal is embedded as data; no generated code is executed.
        literal = repr(canonical(schema).decode("utf-8"))
        data = f"# Generated from the local fixture schema.\nimport json\n{name} = json.loads({literal})\n".encode()
    elif operation == "bundle":
        label = flag(argv, "--label")
        files = {}
        for dependency, outputs in sorted(dependencies.items()):
            for name, content in sorted(outputs.items()):
                files[f"{dependency}/{name}"] = {
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "bytes": len(content),
                    "text": content.decode("utf-8"),
                }
        data = canonical({"label": label, "files": files})
    else:
        raise ValueError("Unknown owned operation")
    if len(data) > 1_000_000:
        raise ValueError("Output exceeds one-megabyte limit")
    output.write_bytes(data)
