from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gh_artifact_inspector.cli import (
    build_report_context,
    format_markdown_table,
    format_markdown_report,
    parse_run_url,
    resolve_run_target,
    summarize_artifact,
    summarize_payload,
)


FIXTURE = Path(__file__).parent / "fixtures" / "artifacts.json"


def test_summarize_artifact_detects_zip_and_direct_file():
    zip_summary = summarize_artifact(
        {
            "name": "bundle.zip",
            "size_in_bytes": 100,
            "expired": False,
            "archive_download_url": "https://api.github.com/repos/example/repo/actions/artifacts/1/zip",
        },
        content_type="application/zip",
    )
    direct_summary = summarize_artifact(
        {
            "name": "summary.json",
            "size_in_bytes": 200,
            "expired": False,
            "archive_download_url": "https://example.invalid/summary.json",
        },
        content_type="application/json",
    )

    assert zip_summary.archive_kind == "zip"
    assert zip_summary.download_strategy == "download-and-unzip"
    assert direct_summary.archive_kind == "direct-file"
    assert direct_summary.download_strategy == "download-as-is"


def test_cli_emits_json_from_fixture():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "gh_artifact_inspector.cli",
            "--from-file",
            str(FIXTURE),
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )

    payload = json.loads(completed.stdout)
    assert [item["archive_kind"] for item in payload] == ["zip", "direct-file", "unknown"]
    assert payload[2]["download_strategy"] == "unavailable"


def test_parse_run_url_extracts_repo_and_run_id():
    repo, run_id = parse_run_url("https://github.com/example/project/actions/runs/123456789?check_suite_focus=true")

    assert repo == "example/project"
    assert run_id == 123456789


def test_resolve_run_target_rejects_mismatched_repo_and_url():
    args = argparse.Namespace(
        repo="example/other-project",
        run_id=None,
        run_url="https://github.com/example/project/actions/runs/123456789",
    )

    with pytest.raises(SystemExit, match="does not match --run-url repository"):
        resolve_run_target(args)


def test_format_markdown_table_escapes_pipe_characters():
    summary = summarize_artifact(
        {
            "name": "coverage|summary.json",
            "size_in_bytes": 200,
            "expired": False,
            "archive_download_url": "https://example.invalid/summary.json",
        },
        content_type="application/json",
    )

    table = format_markdown_table([summary])

    assert table.splitlines()[0] == (
        "| name | size | expired | archive_kind | content_type | download_strategy | note |"
    )
    assert "coverage\\|summary.json" in table


def test_cli_emits_markdown_from_fixture():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "gh_artifact_inspector.cli",
            "--from-file",
            str(FIXTURE),
            "--markdown",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )

    lines = completed.stdout.strip().splitlines()
    assert lines[0] == "| name | size | expired | archive_kind | content_type | download_strategy | note |"
    assert lines[1] == "| --- | --- | --- | --- | --- | --- | --- |"
    assert any("coverage-summary.json" in line for line in lines[2:])


def test_format_markdown_report_includes_summary_and_table():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    summaries = summarize_payload(payload, headers={}, probe_download=False)
    args = argparse.Namespace(
        repo="example/project",
        run_id=123456789,
        run_url=None,
        from_file=None,
        github_token=None,
        probe_download=False,
        json=False,
        markdown=False,
        markdown_report=True,
    )

    report = format_markdown_report(build_report_context(args, payload, summaries), summaries)

    assert report.startswith("# Artifact inspection report")
    assert "- Source: GitHub Actions run `example/project` / `123456789`" in report
    assert "- Artifact count: 3" in report
    assert "zip=1, direct-file=1, unknown=1" in report
    assert "| coverage-summary.json | 256 | no | direct-file | application/json | download-as-is |" in report


def test_cli_emits_markdown_report_from_fixture():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "gh_artifact_inspector.cli",
            "--from-file",
            str(FIXTURE),
            "--markdown-report",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )

    output = completed.stdout
    assert "# Artifact inspection report" in output
    assert f"- Source: saved payload `{FIXTURE}`" in output
    assert "| stale-artifact | 512 | yes | unknown | - | unavailable |" in output
