"""Command line: `python -m wingreg validate` and `python -m wingreg build`."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from . import ROOT, RegistryError, announcement, build_index, report, validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wingreg")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("validate", help="check every registry entry and its adapter")
    check.add_argument("--previous", type=Path, help="last published index.json, to check version bumps")

    build = commands.add_parser("build", help="write archives, icons and index.json")
    build.add_argument("--out", type=Path, required=True)
    build.add_argument("--sequence", type=int, required=True)
    build.add_argument("--base-url", required=True, help="URL the release assets are served from, ending in /")

    review = commands.add_parser("report", help="write the review comment for a pull request")
    review.add_argument("--previous", type=Path, help="last published index.json")
    review.add_argument("--out", type=Path, required=True)

    announce = commands.add_parser("announce", help="print the Discord message for a new index, if any")
    announce.add_argument("--previous", type=Path)
    announce.add_argument("--current", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            previous = json.loads(args.previous.read_text()) if args.previous and args.previous.is_file() else None
            problems = validate(ROOT, previous)
            for problem in problems:
                print(f"error: {problem}")
            print(f"{len(problems)} problem(s)")
            return 1 if problems else 0
        if args.command == "announce":
            previous = json.loads(args.previous.read_text()) if args.previous and args.previous.is_file() else None
            message = announcement(previous, json.loads(args.current.read_text()))
            if message:
                print(json.dumps(message))
            return 0
        if args.command == "report":
            previous = json.loads(args.previous.read_text()) if args.previous and args.previous.is_file() else None
            with tempfile.TemporaryDirectory() as tmp:
                args.out.write_text(report(ROOT, previous, Path(tmp)))
            return 0
        with tempfile.TemporaryDirectory() as tmp:
            index = build_index(ROOT, args.out, args.sequence, args.base_url, Path(tmp))
        print(f"index {index['sequence']}: {len(index['adapters'])} adapter(s), {len(index['revoked'])} revocation(s)")
        return 0
    except RegistryError as error:
        print(f"error: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
