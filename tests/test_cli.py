from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gh_artifact_inspector.cli import (
    build_report_context,
    build_recent_runs_context,
    collect_strict_failures,
    collect_recent_runs_strict_failures,
    filter_recent_run_inspections,
    format_recent_runs_json_report,
    format_recent_runs_markdown_report,
    format_json_report,
    format_markdown_table,
    format_markdown_report,
    inspect_recent_runs,
    event_matches_filter,
    parse_run_url,
    probe_content_type,
    RecentRunInspection,
    resolve_recent_runs_target,
    resolve_run_target,
    summarize_artifact,
    summarize_payload,
    validate_argument_combinations,
    workflow_matches_filter,
)
from gh_artifact_inspector.downloader import (
    build_download_actions,
    describe_download_actions,
    render_download_script,
    sanitize_artifact_name,
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


def test_summarize_artifact_detects_direct_file_when_download_url_ends_with_zip_path():
    summary = summarize_artifact(
        {
            "name": "workflow-artifact-demo.txt",
            "size_in_bytes": 47,
            "expired": False,
            "archive_download_url": "https://api.github.com/repos/example/repo/actions/artifacts/1/zip",
        },
        content_type="text/plain",
    )

    assert summary.archive_kind == "direct-file"
    assert summary.download_strategy == "download-as-is"


def test_summarize_artifact_treats_tarball_payloads_as_direct_file_downloads():
    summary = summarize_artifact(
        {
            "name": "logs-bundle.tar.gz",
            "size_in_bytes": 4096,
            "expired": False,
            "archive_download_url": "https://api.github.com/repos/example/repo/actions/artifacts/1/zip",
        },
        content_type="application/gzip",
    )

    assert summary.archive_kind == "direct-file"
    assert summary.download_strategy == "download-as-is"


def test_probe_content_type_falls_back_to_get_when_head_has_no_type(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    class DummyHeaders:
        def __init__(self, content_type: str | None) -> None:
            self._content_type = content_type

        def get_content_type(self) -> str | None:
            return self._content_type

    class DummyResponse:
        def __init__(self, content_type: str | None) -> None:
            self.headers = DummyHeaders(content_type)

        def __enter__(self) -> "DummyResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class DummyOpener:
        def open(self, request, timeout=30):  # type: ignore[no-untyped-def]
            calls.append(request.get_method())
            if request.get_method() == "HEAD":
                return DummyResponse(None)
            return DummyResponse("text/plain")

    def fake_build_opener(*handlers):  # type: ignore[no-untyped-def]
        return DummyOpener()

    monkeypatch.setattr("gh_artifact_inspector.cli.build_opener", fake_build_opener)

    content_type = probe_content_type("https://example.invalid/artifact", headers={})

    assert content_type == "text/plain"
    assert calls == ["HEAD", "GET"]


def test_probe_content_type_follows_redirect_without_auth(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, str]] = []

    class DummyHeaders:
        def __init__(self, content_type: str | None = None, location: str | None = None) -> None:
            self._content_type = content_type
            self._location = location

        def get_content_type(self) -> str | None:
            return self._content_type

        def get(self, key: str, default=None):  # type: ignore[no-untyped-def]
            if key == "Location":
                return self._location
            return default

    class DummyResponse:
        def __init__(self, content_type: str | None) -> None:
            self.headers = DummyHeaders(content_type=content_type)

        def __enter__(self) -> "DummyResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class DummyOpener:
        def open(self, request, timeout=30):  # type: ignore[no-untyped-def]
            calls.append(("opener", request.get_method()))
            raise HTTPError(
                request.full_url,
                302,
                "Found",
                DummyHeaders(location="https://downloads.example.invalid/artifact.txt"),
                None,
            )

    def fake_build_opener(*handlers):  # type: ignore[no-untyped-def]
        return DummyOpener()

    def fake_urlopen(request, timeout=30):  # type: ignore[no-untyped-def]
        calls.append(("urlopen", request.get_method()))
        assert request.full_url == "https://downloads.example.invalid/artifact.txt"
        assert request.headers.get("Authorization") is None
        return DummyResponse("text/plain")

    monkeypatch.setattr("gh_artifact_inspector.cli.build_opener", fake_build_opener)
    monkeypatch.setattr("gh_artifact_inspector.cli.urlopen", fake_urlopen)

    content_type = probe_content_type("https://example.invalid/artifact", headers={"Authorization": "Bearer secret"})

    assert content_type == "text/plain"
    assert calls == [("opener", "HEAD"), ("urlopen", "HEAD")]


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


def test_resolve_recent_runs_target_rejects_run_id_mix():
    args = argparse.Namespace(
        repo="example/project",
        run_id=123,
        run_url=None,
        from_file=None,
        recent_runs=3,
    )

    with pytest.raises(SystemExit, match="cannot be combined with --run-id or --run-url"):
        resolve_recent_runs_target(args)


def test_validate_argument_combinations_rejects_from_file_with_live_run_flags():
    args = argparse.Namespace(
        repo="example/project",
        run_id=123,
        run_url=None,
        from_file=FIXTURE,
        recent_runs=None,
        strict_only=False,
    )

    with pytest.raises(SystemExit, match="--from-file cannot be combined"):
        validate_argument_combinations(args)


def test_validate_argument_combinations_rejects_strict_only_without_recent_runs():
    args = argparse.Namespace(
        repo="example/project",
        run_id=None,
        run_url=None,
        from_file=None,
        recent_runs=None,
        strict_only=True,
    )

    with pytest.raises(SystemExit, match="can only be used together with --recent-runs"):
        validate_argument_combinations(args)


def test_validate_argument_combinations_rejects_workflow_without_recent_runs():
    args = argparse.Namespace(
        repo="example/project",
        run_id=None,
        run_url=None,
        from_file=None,
        recent_runs=None,
        strict_only=False,
        workflow="nightly",
    )

    with pytest.raises(SystemExit, match="can only be used together with --recent-runs"):
        validate_argument_combinations(args)


def test_validate_argument_combinations_rejects_event_without_recent_runs():
    args = argparse.Namespace(
        repo="example/project",
        run_id=None,
        run_url=None,
        from_file=None,
        recent_runs=None,
        strict_only=False,
        workflow=None,
        event="push",
    )

    with pytest.raises(SystemExit, match="can only be used together with --recent-runs"):
        validate_argument_combinations(args)


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


def test_format_json_report_includes_summary_and_artifacts():
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
        json_report=True,
        markdown=False,
        markdown_report=False,
    )

    report = format_json_report(build_report_context(args, payload, summaries), summaries)

    assert report["source"] == "GitHub Actions run `example/project` / `123456789`"
    assert report["summary"] == {
        "total_artifacts": 3,
        "expired_artifacts": 1,
        "zip_artifacts": 1,
        "direct_file_artifacts": 1,
        "unknown_artifacts": 1,
    }
    assert report["strict_failures"] == ["stale-artifact: artifact expired"]
    assert report["artifacts"][1]["name"] == "coverage-summary.json"
    assert report["artifacts"][1]["download_strategy"] == "download-as-is"


def test_collect_strict_failures_reports_expired_and_unknown():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    summaries = summarize_payload(payload, headers={}, probe_download=False)

    failures = collect_strict_failures(summaries)

    assert failures == ["stale-artifact: artifact expired"]


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


def test_cli_emits_json_report_from_fixture():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "gh_artifact_inspector.cli",
            "--from-file",
            str(FIXTURE),
            "--json-report",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )

    payload = json.loads(completed.stdout)
    assert payload["source"] == f"saved payload `{FIXTURE}`"
    assert payload["summary"]["expired_artifacts"] == 1
    assert payload["strict_failures"] == ["stale-artifact: artifact expired"]
    assert payload["artifacts"][0]["name"] == "bundle.zip"


def test_cli_strict_fails_when_fixture_contains_expired_artifact():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "gh_artifact_inspector.cli",
            "--from-file",
            str(FIXTURE),
            "--strict",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )

    assert completed.returncode == 2
    assert "Strict check failed:" in completed.stderr
    assert "- stale-artifact: artifact expired" in completed.stderr


def test_cli_strict_succeeds_for_known_non_expired_artifacts(tmp_path: Path):
    payload_path = tmp_path / "artifacts.json"
    payload_path.write_text(
        json.dumps(
            {
                "total_count": 2,
                "artifacts": [
                    {
                        "name": "bundle.zip",
                        "size_in_bytes": 100,
                        "expired": False,
                        "archive_download_url": "https://api.github.com/repos/example/repo/actions/artifacts/1/zip",
                        "content_type": "application/zip",
                    },
                    {
                        "name": "summary.json",
                        "size_in_bytes": 200,
                        "expired": False,
                        "archive_download_url": "https://example.invalid/summary.json",
                        "content_type": "application/json",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "gh_artifact_inspector.cli",
            "--from-file",
            str(payload_path),
            "--strict",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )

    assert completed.returncode == 0
    assert "bundle.zip" in completed.stdout
    assert completed.stderr == ""


def test_cli_emit_script_requires_output_dir():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "gh_artifact_inspector.cli",
            "--from-file",
            str(FIXTURE),
            "--emit-script",
            "powershell",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )

    assert completed.returncode != 0
    assert "--output-dir is required when --emit-script is used." in completed.stderr


def test_cli_rejects_from_file_with_repo_and_run_id():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "gh_artifact_inspector.cli",
            "--from-file",
            str(FIXTURE),
            "--repo",
            "example/project",
            "--run-id",
            "123456789",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )

    assert completed.returncode != 0
    assert "--from-file cannot be combined with --repo, --run-id, or --run-url." in completed.stderr


def test_cli_emits_powershell_script_from_fixture(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "gh_artifact_inspector.cli",
            "--from-file",
            str(FIXTURE),
            "--emit-script",
            "powershell",
            "--output-dir",
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )

    assert "$ErrorActionPreference = 'Stop'" in completed.stdout
    assert "Invoke-WebRequest -Headers $headers" in completed.stdout
    assert f"Expand-Archive -LiteralPath '{tmp_path / 'bundle.zip'}'" in completed.stdout
    assert "# skip stale-artifact: Artifact is expired." in completed.stdout


def test_inspect_recent_runs_summarizes_each_run(monkeypatch: pytest.MonkeyPatch):
    responses = {
        "https://api.github.com/repos/example/project/actions/runs?per_page=2&page=1": {
            "workflow_runs": [
                {
                    "id": 101,
                    "run_number": 11,
                    "run_attempt": 1,
                    "display_title": "CI",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://github.com/example/project/actions/runs/101",
                    "created_at": "2026-07-14T08:00:00Z",
                },
                {
                    "id": 102,
                    "run_number": 12,
                    "run_attempt": 1,
                    "display_title": "Nightly",
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "https://github.com/example/project/actions/runs/102",
                    "created_at": "2026-07-14T09:00:00Z",
                },
            ]
        },
        "https://api.github.com/repos/example/project/actions/runs/101/artifacts?per_page=100": {
            "total_count": 1,
            "artifacts": [
                {
                    "name": "bundle.zip",
                    "size_in_bytes": 100,
                    "expired": False,
                    "archive_download_url": "https://api.github.com/repos/example/project/actions/artifacts/1/zip",
                    "content_type": "application/zip",
                }
            ],
        },
        "https://api.github.com/repos/example/project/actions/runs/102/artifacts?per_page=100": {
            "total_count": 1,
            "artifacts": [
                {
                    "name": "stale-artifact",
                    "size_in_bytes": 50,
                    "expired": True,
                    "archive_download_url": "https://api.github.com/repos/example/project/actions/artifacts/2/zip",
                }
            ],
        },
    }

    def fake_request_json(url: str, headers: dict[str, str]):  # type: ignore[no-untyped-def]
        return responses[url]

    monkeypatch.setattr("gh_artifact_inspector.cli.request_json", fake_request_json)

    inspections = inspect_recent_runs("example/project", 2, headers={}, probe_download=False)

    assert len(inspections) == 2
    assert inspections[0].run_id == 101
    assert inspections[0].zip_artifacts == 1
    assert inspections[1].run_id == 102
    assert inspections[1].strict_failures == ["stale-artifact: artifact expired"]


def test_workflow_matches_filter_uses_case_insensitive_substring():
    run = {"display_title": "Nightly Artifact Sweep"}

    assert workflow_matches_filter(run, "nightly")
    assert workflow_matches_filter(run, "artifact")
    assert not workflow_matches_filter(run, "release")


def test_event_matches_filter_uses_case_insensitive_substring():
    run = {"event": "pull_request_target"}

    assert event_matches_filter(run, "pull_request")
    assert event_matches_filter(run, "TARGET")
    assert not event_matches_filter(run, "workflow_dispatch")


def test_inspect_recent_runs_filters_by_workflow_title(monkeypatch: pytest.MonkeyPatch):
    responses = {
        "https://api.github.com/repos/example/project/actions/runs?per_page=30&page=1": {
            "workflow_runs": [
                {
                    "id": 101,
                    "run_number": 11,
                    "run_attempt": 1,
                    "display_title": "CI",
                    "status": "completed",
                    "conclusion": "success",
                    "event": "push",
                    "html_url": "https://github.com/example/project/actions/runs/101",
                    "created_at": "2026-07-14T08:00:00Z",
                },
                {
                    "id": 102,
                    "run_number": 12,
                    "run_attempt": 1,
                    "display_title": "Nightly",
                    "status": "completed",
                    "conclusion": "failure",
                    "event": "schedule",
                    "html_url": "https://github.com/example/project/actions/runs/102",
                    "created_at": "2026-07-14T09:00:00Z",
                },
            ]
        },
        "https://api.github.com/repos/example/project/actions/runs/102/artifacts?per_page=100": {
            "total_count": 1,
            "artifacts": [
                {
                    "name": "stale-artifact",
                    "size_in_bytes": 50,
                    "expired": True,
                    "archive_download_url": "https://api.github.com/repos/example/project/actions/artifacts/2/zip",
                }
            ],
        },
    }

    def fake_request_json(url: str, headers: dict[str, str]):  # type: ignore[no-untyped-def]
        return responses[url]

    monkeypatch.setattr("gh_artifact_inspector.cli.request_json", fake_request_json)

    inspections = inspect_recent_runs(
        "example/project",
        1,
        headers={},
        probe_download=False,
        workflow_filter="nightly",
    )

    assert len(inspections) == 1
    assert inspections[0].run_id == 102


def test_inspect_recent_runs_filters_by_event(monkeypatch: pytest.MonkeyPatch):
    responses = {
        "https://api.github.com/repos/example/project/actions/runs?per_page=30&page=1": {
            "workflow_runs": [
                {
                    "id": 101,
                    "run_number": 11,
                    "run_attempt": 1,
                    "display_title": "CI",
                    "status": "completed",
                    "conclusion": "success",
                    "event": "push",
                    "html_url": "https://github.com/example/project/actions/runs/101",
                    "created_at": "2026-07-14T08:00:00Z",
                },
                {
                    "id": 102,
                    "run_number": 12,
                    "run_attempt": 1,
                    "display_title": "CI Pull Request",
                    "status": "completed",
                    "conclusion": "success",
                    "event": "pull_request",
                    "html_url": "https://github.com/example/project/actions/runs/102",
                    "created_at": "2026-07-14T09:00:00Z",
                },
            ]
        },
        "https://api.github.com/repos/example/project/actions/runs/102/artifacts?per_page=100": {
            "total_count": 1,
            "artifacts": [
                {
                    "name": "bundle.zip",
                    "size_in_bytes": 100,
                    "expired": False,
                    "archive_download_url": "https://api.github.com/repos/example/project/actions/artifacts/1/zip",
                    "content_type": "application/zip",
                }
            ],
        },
    }

    def fake_request_json(url: str, headers: dict[str, str]):  # type: ignore[no-untyped-def]
        return responses[url]

    monkeypatch.setattr("gh_artifact_inspector.cli.request_json", fake_request_json)

    inspections = inspect_recent_runs(
        "example/project",
        1,
        headers={},
        probe_download=False,
        event_filter="pull_request",
    )

    assert len(inspections) == 1
    assert inspections[0].run_id == 102
    assert inspections[0].event == "pull_request"


def test_filter_recent_run_inspections_keeps_only_strict_failures():
    inspections = [
        RecentRunInspection(
            run_id=101,
            run_number=11,
            run_attempt=1,
            title="CI",
            status="completed",
            conclusion="success",
            html_url="https://github.com/example/project/actions/runs/101",
            created_at="2026-07-14T08:00:00Z",
            total_artifacts=1,
            expired_artifacts=0,
            zip_artifacts=1,
            direct_file_artifacts=0,
            unknown_artifacts=0,
            strict_failures=[],
        ),
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=1,
            title="Nightly",
            status="completed",
            conclusion="failure",
            html_url="https://github.com/example/project/actions/runs/102",
            created_at="2026-07-14T09:00:00Z",
            total_artifacts=1,
            expired_artifacts=1,
            zip_artifacts=0,
            direct_file_artifacts=0,
            unknown_artifacts=1,
            strict_failures=["stale-artifact: artifact expired"],
        ),
    ]

    filtered = filter_recent_run_inspections(inspections, strict_only=True)

    assert [inspection.run_id for inspection in filtered] == [102]


def test_recent_runs_markdown_report_includes_summary_and_failures():
    inspections = [
        RecentRunInspection(
            run_id=101,
            run_number=11,
            run_attempt=1,
            title="CI",
            status="completed",
            conclusion="success",
            html_url="https://github.com/example/project/actions/runs/101",
            created_at="2026-07-14T08:00:00Z",
            total_artifacts=1,
            expired_artifacts=0,
            zip_artifacts=1,
            direct_file_artifacts=0,
            unknown_artifacts=0,
            strict_failures=[],
        ),
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=1,
            title="Nightly",
            status="completed",
            conclusion="failure",
            html_url="https://github.com/example/project/actions/runs/102",
            created_at="2026-07-14T09:00:00Z",
            total_artifacts=1,
            expired_artifacts=1,
            zip_artifacts=0,
            direct_file_artifacts=0,
            unknown_artifacts=1,
            strict_failures=["stale-artifact: artifact expired"],
        ),
    ]

    context = build_recent_runs_context("example/project", 2, inspections, scanned_runs=2)
    report = format_recent_runs_markdown_report(context, inspections)

    assert report.startswith("# Recent artifact inspection report")
    assert "- Runs scanned: 2" in report
    assert "| 102 | 12 | completed | failure | unknown | 1 | 0 | 0 | 1 | 1 | 1 | Nightly |" in report
    assert "- run 102 (Nightly): stale-artifact: artifact expired" in report


def test_recent_runs_json_report_includes_summary_and_run_rows():
    inspections = [
        RecentRunInspection(
            run_id=101,
            run_number=11,
            run_attempt=1,
            title="CI",
            status="completed",
            conclusion="success",
            html_url="https://github.com/example/project/actions/runs/101",
            created_at="2026-07-14T08:00:00Z",
            total_artifacts=1,
            expired_artifacts=0,
            zip_artifacts=1,
            direct_file_artifacts=0,
            unknown_artifacts=0,
            strict_failures=[],
        ),
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=1,
            title="Nightly",
            status="completed",
            conclusion="failure",
            html_url="https://github.com/example/project/actions/runs/102",
            created_at="2026-07-14T09:00:00Z",
            total_artifacts=1,
            expired_artifacts=1,
            zip_artifacts=0,
            direct_file_artifacts=0,
            unknown_artifacts=1,
            strict_failures=["stale-artifact: artifact expired"],
        ),
    ]

    context = build_recent_runs_context("example/project", 2, inspections, scanned_runs=2)
    report = format_recent_runs_json_report(context, inspections)

    assert report["summary"] == {
        "scanned_runs": 2,
        "total_runs": 2,
        "runs_with_artifacts": 2,
        "total_artifacts": 2,
        "runs_with_failures": 1,
    }
    assert report["strict_failures"] == ["run 102 (Nightly): stale-artifact: artifact expired"]
    assert report["runs"][0]["event"] == "unknown"
    assert report["runs"][0]["title"] == "CI"
    assert collect_recent_runs_strict_failures(inspections) == [
        "run 102 (Nightly): stale-artifact: artifact expired"
    ]


def test_recent_runs_json_report_groups_runs_by_workflow_title():
    inspections = [
        RecentRunInspection(
            run_id=101,
            run_number=11,
            run_attempt=1,
            title="CI",
            status="completed",
            conclusion="success",
            html_url="https://github.com/example/project/actions/runs/101",
            created_at="2026-07-14T08:00:00Z",
            total_artifacts=1,
            expired_artifacts=0,
            zip_artifacts=1,
            direct_file_artifacts=0,
            unknown_artifacts=0,
            strict_failures=[],
        ),
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=1,
            title="CI",
            status="completed",
            conclusion="failure",
            html_url="https://github.com/example/project/actions/runs/102",
            created_at="2026-07-14T09:00:00Z",
            total_artifacts=2,
            expired_artifacts=1,
            zip_artifacts=0,
            direct_file_artifacts=1,
            unknown_artifacts=1,
            strict_failures=["stale-artifact: artifact expired"],
        ),
        RecentRunInspection(
            run_id=103,
            run_number=13,
            run_attempt=1,
            title="Nightly",
            status="completed",
            conclusion="success",
            html_url="https://github.com/example/project/actions/runs/103",
            created_at="2026-07-14T10:00:00Z",
            total_artifacts=3,
            expired_artifacts=0,
            zip_artifacts=1,
            direct_file_artifacts=2,
            unknown_artifacts=0,
            strict_failures=[],
        ),
    ]

    context = build_recent_runs_context("example/project", 3, inspections, scanned_runs=3)
    report = format_recent_runs_json_report(context, inspections)

    assert report["workflow_summary"] == [
        {
            "title": "CI",
            "runs": 2,
            "total_artifacts": 3,
            "expired_artifacts": 1,
            "zip_artifacts": 1,
            "direct_file_artifacts": 1,
            "unknown_artifacts": 1,
            "runs_with_failures": 1,
        },
        {
            "title": "Nightly",
            "runs": 1,
            "total_artifacts": 3,
            "expired_artifacts": 0,
            "zip_artifacts": 1,
            "direct_file_artifacts": 2,
            "unknown_artifacts": 0,
            "runs_with_failures": 0,
        },
    ]


def test_recent_runs_markdown_report_includes_workflow_summary_section():
    inspections = [
        RecentRunInspection(
            run_id=101,
            run_number=11,
            run_attempt=1,
            title="CI",
            status="completed",
            conclusion="success",
            html_url="https://github.com/example/project/actions/runs/101",
            created_at="2026-07-14T08:00:00Z",
            total_artifacts=1,
            expired_artifacts=0,
            zip_artifacts=1,
            direct_file_artifacts=0,
            unknown_artifacts=0,
            strict_failures=[],
        ),
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=1,
            title="CI",
            status="completed",
            conclusion="failure",
            html_url="https://github.com/example/project/actions/runs/102",
            created_at="2026-07-14T09:00:00Z",
            total_artifacts=2,
            expired_artifacts=1,
            zip_artifacts=0,
            direct_file_artifacts=1,
            unknown_artifacts=1,
            strict_failures=["stale-artifact: artifact expired"],
        ),
    ]

    context = build_recent_runs_context("example/project", 2, inspections, scanned_runs=2)
    report = format_recent_runs_markdown_report(context, inspections)

    assert "## Workflow summary" in report
    assert "| CI | 2 | 3 | 1 | 1 | 1 | 1 | 1 |" in report


def test_recent_runs_markdown_report_shows_filtered_count_when_strict_only():
    inspections = [
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=1,
            title="Nightly",
            status="completed",
            conclusion="failure",
            html_url="https://github.com/example/project/actions/runs/102",
            created_at="2026-07-14T09:00:00Z",
            total_artifacts=1,
            expired_artifacts=1,
            zip_artifacts=0,
            direct_file_artifacts=0,
            unknown_artifacts=1,
            strict_failures=["stale-artifact: artifact expired"],
        ),
    ]

    context = build_recent_runs_context(
        "example/project",
        2,
        inspections,
        scanned_runs=2,
        strict_only=True,
    )
    report = format_recent_runs_markdown_report(context, inspections)

    assert "strict failures only" in report
    assert "- Runs scanned: 2" in report
    assert "- Runs included: 1" in report


def test_recent_runs_markdown_report_mentions_workflow_filter():
    inspections = [
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=1,
            title="Nightly",
            status="completed",
            conclusion="failure",
            html_url="https://github.com/example/project/actions/runs/102",
            created_at="2026-07-14T09:00:00Z",
            total_artifacts=1,
            expired_artifacts=1,
            zip_artifacts=0,
            direct_file_artifacts=0,
            unknown_artifacts=1,
            strict_failures=["stale-artifact: artifact expired"],
        ),
    ]

    context = build_recent_runs_context(
        "example/project",
        5,
        inspections,
        scanned_runs=1,
        workflow_filter="nightly",
    )
    report = format_recent_runs_markdown_report(context, inspections)

    assert "workflow contains 'nightly'" in report


def test_recent_runs_markdown_report_mentions_event_filter():
    inspections = [
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=1,
            title="Nightly",
            status="completed",
            conclusion="failure",
            html_url="https://github.com/example/project/actions/runs/102",
            created_at="2026-07-14T09:00:00Z",
            total_artifacts=1,
            expired_artifacts=1,
            zip_artifacts=0,
            direct_file_artifacts=0,
            unknown_artifacts=1,
            strict_failures=["stale-artifact: artifact expired"],
            event="schedule",
        ),
    ]

    context = build_recent_runs_context(
        "example/project",
        5,
        inspections,
        scanned_runs=1,
        event_filter="schedule",
    )
    report = format_recent_runs_markdown_report(context, inspections)

    assert "event contains 'schedule'" in report


def test_build_download_actions_uses_report_strategies(tmp_path: Path):
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
        json_report=True,
        markdown=False,
        markdown_report=False,
    )
    report = format_json_report(build_report_context(args, payload, summaries), summaries)

    actions = build_download_actions(report, tmp_path)

    assert [(action.name, action.strategy) for action in actions] == [
        ("bundle.zip", "download-and-unzip"),
        ("coverage-summary.json", "download-as-is"),
        ("stale-artifact", "unavailable"),
    ]
    assert actions[0].target_path == tmp_path / "bundle.zip"
    assert actions[0].extract_dir == tmp_path / "bundle"
    assert actions[1].target_path == tmp_path / "coverage-summary.json"
    assert actions[2].target_path is None


def test_sanitize_artifact_name_replaces_windows_unsafe_characters():
    assert sanitize_artifact_name('bad:name/with\\chars?.zip') == "bad_name_with_chars_.zip"


def test_describe_download_actions_marks_downloads_and_skips(tmp_path: Path):
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
        json_report=True,
        markdown=False,
        markdown_report=False,
    )
    report = format_json_report(build_report_context(args, payload, summaries), summaries)

    logs = describe_download_actions(build_download_actions(report, tmp_path))

    assert logs == [
        f"plan unzip bundle.zip: {tmp_path / 'bundle.zip'} -> {tmp_path / 'bundle'}",
        f"plan download coverage-summary.json: {tmp_path / 'coverage-summary.json'}",
        "skip stale-artifact: Artifact is expired. Re-run the workflow or extend retention.",
    ]


def test_render_download_script_supports_powershell_and_bash(tmp_path: Path):
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
        json_report=True,
        markdown=False,
        markdown_report=False,
    )
    report = format_json_report(build_report_context(args, payload, summaries), summaries)
    actions = build_download_actions(report, tmp_path)

    powershell_script = render_download_script(actions, shell="powershell")
    bash_script = render_download_script(actions, shell="bash")

    assert "Invoke-WebRequest -Headers $headers" in powershell_script
    assert "Expand-Archive -LiteralPath" in powershell_script
    assert "# skip stale-artifact: Artifact is expired." in powershell_script
    assert 'curl -L "${auth_args[@]}"' in bash_script
    assert "unzip -o" in bash_script
    assert "# skip stale-artifact: Artifact is expired." in bash_script


def test_compatible_downloader_can_emit_powershell_script(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "examples/compatible_downloader.py",
            "--report",
            str(ROOT / "examples" / "demo-report.json"),
            "--output-dir",
            str(tmp_path),
            "--emit-script",
            "powershell",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )

    assert "$ErrorActionPreference = 'Stop'" in completed.stdout
    assert "Invoke-WebRequest -Headers $headers" in completed.stdout


def test_compatible_downloader_can_emit_bash_script(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "examples/compatible_downloader.py",
            "--report",
            str(ROOT / "examples" / "demo-report.json"),
            "--output-dir",
            str(tmp_path),
            "--emit-script",
            "bash",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )

    assert "set -euo pipefail" in completed.stdout
    assert 'curl -L "${auth_args[@]}"' in completed.stdout
