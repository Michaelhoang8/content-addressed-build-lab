import argparse
import json
import sys
from pathlib import Path

from .engine import Engine


def main():
    parser = argparse.ArgumentParser(
        description="Run the local owned-operation build graph"
    )
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--trace", type=Path)
    args = parser.parse_args()
    try:
        spec_path = args.workspace / "build.json"
        if spec_path.stat().st_size > 65536:
            raise ValueError("build.json exceeds 64 KiB")
        result = Engine(
            args.workspace, args.cache, args.output, workers=args.workers
        ).build(json.loads(spec_path.read_text(encoding="utf-8")))
        if args.trace:
            args.trace.parent.mkdir(parents=True, exist_ok=True)
            args.trace.write_text(
                json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
    except (OSError, ValueError, RuntimeError) as error:
        print(f"buildlab: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": result["status"],
                "failure": result["failure"],
                "actions": {
                    name: value["status"] for name, value in result["actions"].items()
                },
                "pointer": result["pointer"],
            },
            indent=2,
        )
    )
    return 0 if result["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
