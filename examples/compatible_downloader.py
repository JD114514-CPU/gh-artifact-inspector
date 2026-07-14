from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gh_artifact_inspector.downloader import (
    build_download_actions,
    describe_download_actions,
    execute_download_actions,
    load_report,
    render_download_script,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Consume a gh-artifact-inspector JSON report and download each artifact safely."
    )
    parser.add_argument(
        "--report",
        type=Path,
        required=True,
        help="Path to a gh-artifact-inspector --json-report output file.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory where downloaded files or extracted zips should be written.",
    )
    parser.add_argument(
        "--github-token",
        default=os.getenv("GITHUB_TOKEN"),
        help="Optional GitHub token for private repos or higher rate limits. Defaults to GITHUB_TOKEN.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned download actions without making network requests.",
    )
    parser.add_argument(
        "--emit-script",
        choices=("powershell", "bash"),
        help="Print a PowerShell or bash download script instead of executing the plan.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    report = load_report(args.report)
    actions = build_download_actions(report, args.output_dir)
    if args.emit_script:
        if args.dry_run:
            parser.error("--emit-script cannot be combined with --dry-run.")
        print(render_download_script(actions, shell=args.emit_script), end="")
    elif args.dry_run:
        logs = describe_download_actions(actions)
        for line in logs:
            print(line)
    else:
        logs = execute_download_actions(actions, github_token=args.github_token)
        for line in logs:
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
