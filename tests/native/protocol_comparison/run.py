# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.28.1,<0.29"]
# ///

"""Validate a pair of disposable, committed embedded builds over real HTTP."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from protocol_comparison.compare import compare
from protocol_comparison.evidence import build_receipt, write_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    receipt = commands.add_parser("receipt")
    for field in ("source", "component", "wheel", "output"):
        receipt.add_argument("--" + field, type=Path, required=True)
    for field in ("revision", "core-image-id", "run-nonce"):
        receipt.add_argument("--" + field, required=True)
    run = commands.add_parser("compare")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--rounds", type=int, default=12)
    run.add_argument("--warmups", type=int, default=2)
    args = parser.parse_args()
    if args.command == "receipt":
        write_json(
            args.output,
            build_receipt(
                args.source,
                args.revision,
                args.component,
                args.wheel,
                args.core_image_id,
                args.run_nonce,
            ),
        )
        return 0
    success = asyncio.run(
        compare(
            json.loads(args.config.read_bytes()),
            args.output,
            rounds=args.rounds,
            warmups=args.warmups,
        )
    )
    print(
        "Protocol comparison completed"
        if success
        else "Protocol comparison failed; inspect sanitized JSON"
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
