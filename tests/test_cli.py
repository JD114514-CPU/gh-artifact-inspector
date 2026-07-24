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
    actor_matches_filter,
    artifact_name_matches_filter,
    artifact_size_matches_filter,
    attempt_matches_filter,
    branch_matches_filter,
    build_report_context,
    build_recent_runs_context,
    collect_strict_failures,
    collect_recent_runs_strict_failures,
    conclusion_matches_filter,
    created_at_matches_filter,
    download_strategy_matches_filter,
    filter_recent_run_inspections,
    filter_summaries_by_artifact_name,
    format_recent_runs_json_report,
    format_recent_runs_markdown_table,
    format_recent_runs_markdown_report,
    format_recent_runs_table,
    format_json_report,
    format_markdown_table,
    format_markdown_report,
    head_sha_matches_filter,
    inspect_recent_runs,
    event_matches_filter,
    parse_run_url,
    probe_content_type,
    RecentRunInspection,
    status_matches_filter,
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


def test_cli_emits_json_with_artifact_size_filters_from_fixture():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "gh_artifact_inspector.cli",
            "--from-file",
            str(FIXTURE),
            "--artifact-min-bytes",
            "200",
            "--artifact-max-bytes",
            "300",
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )

    payload = json.loads(completed.stdout)
    assert [item["name"] for item in payload] == ["coverage-summary.json"]


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


def test_validate_argument_combinations_rejects_artifacts_only_without_recent_runs():
    args = argparse.Namespace(
        repo="example/project",
        run_id=None,
        run_url=None,
        from_file=None,
        recent_runs=None,
        strict_only=False,
        artifacts_only=True,
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


def test_validate_argument_combinations_rejects_branch_without_recent_runs():
    args = argparse.Namespace(
        repo="example/project",
        run_id=None,
        run_url=None,
        from_file=None,
        recent_runs=None,
        strict_only=False,
        workflow=None,
        branch="main",
        event=None,
        conclusion=None,
    )

    with pytest.raises(SystemExit, match="can only be used together with --recent-runs"):
        validate_argument_combinations(args)


def test_validate_argument_combinations_rejects_head_sha_without_recent_runs():
    args = argparse.Namespace(
        repo="example/project",
        run_id=None,
        run_url=None,
        from_file=None,
        recent_runs=None,
        strict_only=False,
        workflow=None,
        branch=None,
        head_sha="abc123",
        event=None,
        conclusion=None,
    )

    with pytest.raises(SystemExit, match="can only be used together with --recent-runs"):
        validate_argument_combinations(args)


def test_validate_argument_combinations_rejects_conclusion_without_recent_runs():
    args = argparse.Namespace(
        repo="example/project",
        run_id=None,
        run_url=None,
        from_file=None,
        recent_runs=None,
        strict_only=False,
        workflow=None,
        event=None,
        conclusion="failure",
    )

    with pytest.raises(SystemExit, match="can only be used together with --recent-runs"):
        validate_argument_combinations(args)


def test_validate_argument_combinations_rejects_status_without_recent_runs():
    args = argparse.Namespace(
        repo="example/project",
        run_id=None,
        run_url=None,
        from_file=None,
        recent_runs=None,
        strict_only=False,
        workflow=None,
        event=None,
        conclusion=None,
        status="completed",
    )

    with pytest.raises(SystemExit, match="can only be used together with --recent-runs"):
        validate_argument_combinations(args)


def test_validate_argument_combinations_rejects_actor_without_recent_runs():
    args = argparse.Namespace(
        repo="example/project",
        run_id=None,
        run_url=None,
        from_file=None,
        recent_runs=None,
        strict_only=False,
        workflow=None,
        event=None,
        conclusion=None,
        status=None,
        actor="dependabot",
    )

    with pytest.raises(SystemExit, match="can only be used together with --recent-runs"):
        validate_argument_combinations(args)


def test_validate_argument_combinations_rejects_attempt_without_recent_runs():
    args = argparse.Namespace(
        repo="example/project",
        run_id=None,
        run_url=None,
        from_file=None,
        recent_runs=None,
        strict_only=False,
        workflow=None,
        event=None,
        conclusion=None,
        status=None,
        actor=None,
        attempt=2,
    )

    with pytest.raises(SystemExit, match="can only be used together with --recent-runs"):
        validate_argument_combinations(args)


def test_validate_argument_combinations_rejects_created_after_without_recent_runs():
    args = argparse.Namespace(
        repo="example/project",
        run_id=None,
        run_url=None,
        from_file=None,
        recent_runs=None,
        strict_only=False,
        workflow=None,
        event=None,
        conclusion=None,
        status=None,
        actor=None,
        attempt=None,
        created_after="2026-07-17",
    )

    with pytest.raises(SystemExit, match="can only be used together with --recent-runs"):
        validate_argument_combinations(args)


def test_validate_argument_combinations_rejects_created_before_without_recent_runs():
    args = argparse.Namespace(
        repo="example/project",
        run_id=None,
        run_url=None,
        from_file=None,
        recent_runs=None,
        strict_only=False,
        workflow=None,
        event=None,
        conclusion=None,
        status=None,
        actor=None,
        attempt=None,
        created_after=None,
        created_before="2026-07-20",
    )

    with pytest.raises(SystemExit, match="can only be used together with --recent-runs"):
        validate_argument_combinations(args)


def test_validate_argument_combinations_rejects_inverted_created_at_window():
    args = argparse.Namespace(
        repo="example/project",
        run_id=None,
        run_url=None,
        from_file=None,
        recent_runs=10,
        strict_only=False,
        workflow=None,
        branch=None,
        head_sha=None,
        event=None,
        conclusion=None,
        status=None,
        actor=None,
        attempt=None,
        created_after="2026-07-21",
        created_before="2026-07-20",
        artifacts_only=False,
    )

    with pytest.raises(SystemExit, match="--created-after cannot be later than --created-before."):
        validate_argument_combinations(args)


def test_validate_argument_combinations_rejects_negative_artifact_min_bytes():
    args = argparse.Namespace(
        repo="example/project",
        run_id=None,
        run_url=None,
        from_file=None,
        recent_runs=None,
        strict_only=False,
        workflow=None,
        branch=None,
        head_sha=None,
        event=None,
        conclusion=None,
        status=None,
        actor=None,
        attempt=None,
        created_after=None,
        created_before=None,
        artifacts_only=False,
        artifact_min_bytes=-1,
        artifact_max_bytes=None,
    )

    with pytest.raises(SystemExit, match="--artifact-min-bytes cannot be negative."):
        validate_argument_combinations(args)


def test_validate_argument_combinations_rejects_negative_artifact_max_bytes():
    args = argparse.Namespace(
        repo="example/project",
        run_id=None,
        run_url=None,
        from_file=None,
        recent_runs=None,
        strict_only=False,
        workflow=None,
        branch=None,
        head_sha=None,
        event=None,
        conclusion=None,
        status=None,
        actor=None,
        attempt=None,
        created_after=None,
        created_before=None,
        artifacts_only=False,
        artifact_min_bytes=None,
        artifact_max_bytes=-1,
    )

    with pytest.raises(SystemExit, match="--artifact-max-bytes cannot be negative."):
        validate_argument_combinations(args)


def test_validate_argument_combinations_rejects_inverted_artifact_size_window():
    args = argparse.Namespace(
        repo="example/project",
        run_id=None,
        run_url=None,
        from_file=None,
        recent_runs=None,
        strict_only=False,
        workflow=None,
        branch=None,
        head_sha=None,
        event=None,
        conclusion=None,
        status=None,
        actor=None,
        attempt=None,
        created_after=None,
        created_before=None,
        artifacts_only=False,
        artifact_min_bytes=1024,
        artifact_max_bytes=512,
    )

    with pytest.raises(SystemExit, match="--artifact-min-bytes cannot be greater than --artifact-max-bytes."):
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


def test_format_json_report_source_mentions_artifact_size_filters():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    summaries = summarize_payload(payload, headers={}, probe_download=False)
    filtered = [summary for summary in summaries if artifact_size_matches_filter(summary.size_in_bytes, 200, 300)]
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
        artifact_name=None,
        artifact_kind=None,
        download_strategy=None,
        artifact_min_bytes=200,
        artifact_max_bytes=300,
    )

    report = format_json_report(build_report_context(args, payload, filtered), filtered)

    assert "artifact size >= 200 bytes" in report["source"]
    assert "artifact size <= 300 bytes" in report["source"]
    assert [artifact["name"] for artifact in report["artifacts"]] == ["coverage-summary.json"]


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


def test_branch_matches_filter_uses_head_branch_case_insensitively():
    run = {"head_branch": "Release/Main"}

    assert branch_matches_filter(run, "main")
    assert not branch_matches_filter(run, "develop")


def test_head_sha_matches_filter_uses_case_insensitive_substring():
    run = {"head_sha": "44E5D386DA9C78D59FAB018B04FD433B7CFEABC4"}

    assert head_sha_matches_filter(run, "44e5d386")
    assert head_sha_matches_filter(run, "B7CFEABC4")
    assert not head_sha_matches_filter(run, "9140ded8")


def test_actor_matches_filter_uses_actor_login_case_insensitively():
    run = {"actor": {"login": "Dependabot[bot]"}}

    assert actor_matches_filter(run, "dependabot")
    assert actor_matches_filter(run, "BOT")
    assert not actor_matches_filter(run, "renovate")


def test_attempt_matches_filter_uses_exact_integer_match():
    run = {"run_attempt": 2}

    assert attempt_matches_filter(run, 2)
    assert not attempt_matches_filter(run, 1)
    assert not attempt_matches_filter({}, 2)


def test_created_at_matches_filter_accepts_date_and_timestamp():
    run = {"created_at": "2026-07-17T19:00:38Z"}

    assert created_at_matches_filter(run, "2026-07-17")
    assert created_at_matches_filter(run, "2026-07-17T19:00:38Z")
    assert not created_at_matches_filter(run, "2026-07-18")


def test_created_at_matches_filter_supports_created_before_date_and_timestamp():
    run = {"created_at": "2026-07-18T12:30:00Z"}

    assert created_at_matches_filter(run, None, "2026-07-18")
    assert created_at_matches_filter(run, None, "2026-07-18T12:30:00Z")
    assert not created_at_matches_filter(run, None, "2026-07-18T12:29:59Z")
    assert not created_at_matches_filter(run, None, "2026-07-17")


def test_created_at_matches_filter_rejects_invalid_filter_value():
    with pytest.raises(
        SystemExit,
        match="--created-after must be a date in YYYY-MM-DD form or an ISO-8601 timestamp.",
    ):
        created_at_matches_filter({"created_at": "2026-07-17T19:00:38Z"}, "07/17/2026")

    with pytest.raises(
        SystemExit,
        match="--created-before must be a date in YYYY-MM-DD form or an ISO-8601 timestamp.",
    ):
        created_at_matches_filter({"created_at": "2026-07-17T19:00:38Z"}, None, "07/17/2026")


def test_artifact_name_matches_filter_uses_case_insensitive_substring():
    assert artifact_name_matches_filter("Coverage-Summary.JSON", "summary")
    assert artifact_name_matches_filter("Coverage-Summary.JSON", "COVERAGE")
    assert not artifact_name_matches_filter("Coverage-Summary.JSON", "bundle")


def test_filter_summaries_by_artifact_name_keeps_only_matching_rows():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    summaries = summarize_payload(payload, headers={}, probe_download=False)

    filtered = filter_summaries_by_artifact_name(summaries, "summary")

    assert [summary.name for summary in filtered] == ["coverage-summary.json"]


def test_download_strategy_matches_filter_uses_case_insensitive_exact_match():
    assert download_strategy_matches_filter("download-as-is", "DOWNLOAD-AS-IS")
    assert not download_strategy_matches_filter("download-and-unzip", "download-as-is")


def test_artifact_size_matches_filter_respects_min_and_max_bounds():
    assert artifact_size_matches_filter(256, 200, 300)
    assert artifact_size_matches_filter(256, 256, 256)
    assert not artifact_size_matches_filter(128, 200, None)
    assert not artifact_size_matches_filter(512, None, 300)


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


def test_cli_emits_filtered_json_from_fixture():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "gh_artifact_inspector.cli",
            "--from-file",
            str(FIXTURE),
            "--artifact-name",
            "summary",
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )

    payload = json.loads(completed.stdout)
    assert [item["name"] for item in payload] == ["coverage-summary.json"]


def test_cli_emits_kind_filtered_json_from_fixture():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "gh_artifact_inspector.cli",
            "--from-file",
            str(FIXTURE),
            "--artifact-kind",
            "direct-file",
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )

    payload = json.loads(completed.stdout)
    assert [item["name"] for item in payload] == ["coverage-summary.json"]


def test_cli_emits_download_strategy_filtered_json_from_fixture():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "gh_artifact_inspector.cli",
            "--from-file",
            str(FIXTURE),
            "--download-strategy",
            "download-as-is",
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )

    payload = json.loads(completed.stdout)
    assert [item["name"] for item in payload] == ["coverage-summary.json"]


def test_cli_emits_artifact_size_filtered_json_from_fixture():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "gh_artifact_inspector.cli",
            "--from-file",
            str(FIXTURE),
            "--artifact-min-bytes",
            "300",
            "--artifact-max-bytes",
            "1024",
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )

    payload = json.loads(completed.stdout)
    assert [item["name"] for item in payload] == ["bundle.zip", "stale-artifact"]


def test_build_report_context_includes_artifact_size_suffixes():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    summaries = summarize_payload(payload, headers={}, probe_download=False)
    filtered = [summary for summary in summaries if 300 <= summary.size_in_bytes <= 1024]
    args = argparse.Namespace(
        from_file=FIXTURE,
        repo=None,
        run_id=None,
        run_url=None,
        artifact_name=None,
        artifact_kind=None,
        download_strategy=None,
        artifact_min_bytes=300,
        artifact_max_bytes=1024,
    )

    context = build_report_context(args, payload, filtered)

    assert "artifact size >= 300 bytes" in context.source_label
    assert "artifact size <= 1024 bytes" in context.source_label


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
                    "actor": {"login": "octocat"},
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
                    "actor": {"login": "dependabot[bot]"},
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
    assert inspections[0].actor == "octocat"
    assert inspections[0].zip_artifacts == 1
    assert inspections[1].run_id == 102
    assert inspections[1].actor == "dependabot[bot]"
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


def test_status_matches_filter_uses_case_insensitive_substring():
    run = {"status": "in_progress"}

    assert status_matches_filter(run, "progress")
    assert status_matches_filter(run, "IN_")
    assert not status_matches_filter(run, "completed")


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


def test_inspect_recent_runs_filters_by_head_sha(monkeypatch: pytest.MonkeyPatch):
    responses = {
        "https://api.github.com/repos/example/project/actions/runs?per_page=30&page=1": {
            "workflow_runs": [
                {
                    "id": 101,
                    "run_number": 11,
                    "run_attempt": 1,
                    "head_sha": "44e5d386da9c78d59fab018b04fd433b7cfeabc4",
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
                    "head_sha": "9140ded8eefce7ab6e64944337b6374ecd8739e5",
                    "display_title": "Nightly",
                    "status": "completed",
                    "conclusion": "failure",
                    "event": "schedule",
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
    }

    def fake_request_json(url: str, headers: dict[str, str]):  # type: ignore[no-untyped-def]
        return responses[url]

    monkeypatch.setattr("gh_artifact_inspector.cli.request_json", fake_request_json)

    inspections = inspect_recent_runs(
        "example/project",
        1,
        headers={},
        probe_download=False,
        head_sha_filter="44e5d386",
    )

    assert len(inspections) == 1
    assert inspections[0].run_id == 101
    assert inspections[0].head_sha == "44e5d386da9c78d59fab018b04fd433b7cfeabc4"


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


def test_inspect_recent_runs_filters_by_actor(monkeypatch: pytest.MonkeyPatch):
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
                    "actor": {"login": "octocat"},
                    "html_url": "https://github.com/example/project/actions/runs/101",
                    "created_at": "2026-07-14T08:00:00Z",
                },
                {
                    "id": 102,
                    "run_number": 12,
                    "run_attempt": 1,
                    "display_title": "CI Bot",
                    "status": "completed",
                    "conclusion": "success",
                    "event": "pull_request",
                    "actor": {"login": "dependabot[bot]"},
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
        actor_filter="dependabot",
    )

    assert len(inspections) == 1
    assert inspections[0].run_id == 102
    assert inspections[0].actor == "dependabot[bot]"


def test_inspect_recent_runs_filters_by_created_after(monkeypatch: pytest.MonkeyPatch):
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
                    "created_at": "2026-07-17T23:59:59Z",
                },
                {
                    "id": 102,
                    "run_number": 12,
                    "run_attempt": 1,
                    "display_title": "Nightly",
                    "status": "completed",
                    "conclusion": "success",
                    "event": "schedule",
                    "html_url": "https://github.com/example/project/actions/runs/102",
                    "created_at": "2026-07-18T00:00:00Z",
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

    def fake_request_json(url: str, headers: dict[str, str]):
        return responses[url]

    monkeypatch.setattr("gh_artifact_inspector.cli.request_json", fake_request_json)

    inspections = inspect_recent_runs(
        "example/project",
        1,
        headers={},
        probe_download=False,
        created_after_filter="2026-07-18",
    )

    assert len(inspections) == 1
    assert inspections[0].run_id == 102


def test_inspect_recent_runs_filters_by_created_before(monkeypatch: pytest.MonkeyPatch):
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
                    "created_at": "2026-07-18T23:59:59Z",
                },
                {
                    "id": 102,
                    "run_number": 12,
                    "run_attempt": 1,
                    "display_title": "Nightly",
                    "status": "completed",
                    "conclusion": "success",
                    "event": "schedule",
                    "html_url": "https://github.com/example/project/actions/runs/102",
                    "created_at": "2026-07-19T00:00:00Z",
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
    }

    def fake_request_json(url: str, headers: dict[str, str]):
        return responses[url]

    monkeypatch.setattr("gh_artifact_inspector.cli.request_json", fake_request_json)

    inspections = inspect_recent_runs(
        "example/project",
        1,
        headers={},
        probe_download=False,
        created_before_filter="2026-07-18",
    )

    assert len(inspections) == 1
    assert inspections[0].run_id == 101


def test_inspect_recent_runs_filters_by_attempt(monkeypatch: pytest.MonkeyPatch):
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
                    "actor": {"login": "octocat"},
                    "html_url": "https://github.com/example/project/actions/runs/101",
                    "created_at": "2026-07-14T08:00:00Z",
                },
                {
                    "id": 102,
                    "run_number": 11,
                    "run_attempt": 2,
                    "display_title": "CI rerun",
                    "status": "completed",
                    "conclusion": "failure",
                    "event": "workflow_dispatch",
                    "actor": {"login": "octocat"},
                    "html_url": "https://github.com/example/project/actions/runs/102",
                    "created_at": "2026-07-14T09:00:00Z",
                },
            ]
        },
        "https://api.github.com/repos/example/project/actions/runs/102/artifacts?per_page=100": {
            "total_count": 0,
            "artifacts": [],
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
        attempt_filter=2,
    )

    assert len(inspections) == 1
    assert inspections[0].run_id == 102
    assert inspections[0].run_attempt == 2


def test_inspect_recent_runs_filters_artifacts_by_name(monkeypatch: pytest.MonkeyPatch):
    responses = {
        "https://api.github.com/repos/example/project/actions/runs?per_page=1&page=1": {
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
                }
            ]
        },
        "https://api.github.com/repos/example/project/actions/runs/101/artifacts?per_page=100": {
            "artifacts": [
                {
                    "name": "bundle.zip",
                    "size_in_bytes": 100,
                    "expired": False,
                    "archive_download_url": "https://api.github.com/repos/example/repo/actions/artifacts/1/zip",
                    "content_type": "application/zip",
                },
                {
                    "name": "coverage-summary.json",
                    "size_in_bytes": 200,
                    "expired": False,
                    "archive_download_url": "https://example.invalid/summary.json",
                    "content_type": "application/json",
                },
            ]
        },
    }

    def fake_request_json(url: str, headers: dict[str, str]):
        return responses[url]

    monkeypatch.setattr("gh_artifact_inspector.cli.request_json", fake_request_json)

    inspections = inspect_recent_runs(
        "example/project",
        1,
        headers={},
        probe_download=False,
        artifact_name_filter="summary",
    )

    assert len(inspections) == 1
    assert inspections[0].total_artifacts == 1
    assert inspections[0].direct_file_artifacts == 1
    assert inspections[0].zip_artifacts == 0


def test_inspect_recent_runs_filters_artifacts_by_kind(monkeypatch: pytest.MonkeyPatch):
    responses = {
        "https://api.github.com/repos/example/project/actions/runs?per_page=1&page=1": {
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
                }
            ]
        },
        "https://api.github.com/repos/example/project/actions/runs/101/artifacts?per_page=100": {
            "artifacts": [
                {
                    "name": "bundle.zip",
                    "size_in_bytes": 100,
                    "expired": False,
                    "archive_download_url": "https://api.github.com/repos/example/repo/actions/artifacts/1/zip",
                    "content_type": "application/zip",
                },
                {
                    "name": "coverage-summary.json",
                    "size_in_bytes": 200,
                    "expired": False,
                    "archive_download_url": "https://example.invalid/summary.json",
                    "content_type": "application/json",
                },
            ]
        },
    }

    def fake_request_json(url: str, headers: dict[str, str]):
        return responses[url]

    monkeypatch.setattr("gh_artifact_inspector.cli.request_json", fake_request_json)

    inspections = inspect_recent_runs(
        "example/project",
        1,
        headers={},
        probe_download=False,
        artifact_kind_filter="direct-file",
    )

    assert len(inspections) == 1
    assert inspections[0].total_artifacts == 1
    assert inspections[0].direct_file_artifacts == 1
    assert inspections[0].zip_artifacts == 0


def test_inspect_recent_runs_filters_by_status(monkeypatch: pytest.MonkeyPatch):
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
                    "display_title": "CI",
                    "status": "in_progress",
                    "conclusion": None,
                    "event": "workflow_dispatch",
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

    def fake_request_json(url: str, headers: dict[str, str]):
        return responses[url]

    monkeypatch.setattr("gh_artifact_inspector.cli.request_json", fake_request_json)

    inspections = inspect_recent_runs(
        "example/project",
        1,
        headers={},
        probe_download=False,
        status_filter="progress",
    )

    assert len(inspections) == 1
    assert inspections[0].run_id == 102
    assert inspections[0].status == "in_progress"
    assert inspections[0].event == "workflow_dispatch"


def test_conclusion_matches_filter_is_case_insensitive():
    assert conclusion_matches_filter({"conclusion": "failure"}, "FAIL")
    assert not conclusion_matches_filter({"conclusion": "success"}, "failure")


def test_inspect_recent_runs_filters_by_branch(monkeypatch: pytest.MonkeyPatch):
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
                    "head_branch": "main",
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
                    "head_branch": "release/v1",
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
        branch_filter="release",
    )

    assert len(inspections) == 1
    assert inspections[0].run_id == 102
    assert inspections[0].conclusion == "failure"


def test_inspect_recent_runs_filters_by_conclusion(monkeypatch: pytest.MonkeyPatch):
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
        conclusion_filter="fail",
    )

    assert len(inspections) == 1
    assert inspections[0].run_id == 102
    assert inspections[0].conclusion == "failure"


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


def test_filter_recent_run_inspections_can_keep_only_runs_with_matching_artifacts():
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
            total_artifacts=0,
            expired_artifacts=0,
            zip_artifacts=0,
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
            conclusion="success",
            html_url="https://github.com/example/project/actions/runs/102",
            created_at="2026-07-14T09:00:00Z",
            total_artifacts=1,
            expired_artifacts=0,
            zip_artifacts=0,
            direct_file_artifacts=1,
            unknown_artifacts=0,
            strict_failures=[],
        ),
    ]

    filtered = filter_recent_run_inspections(inspections, strict_only=False, artifacts_only=True)

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
    assert "| 102 | 12 | 1 | - | completed | failure | unknown | unknown | 1 | 0 | 0 | 1 | 1 | 1 | Nightly |" in report
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
    assert report["runs"][0]["actor"] == "unknown"
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


def test_recent_runs_table_includes_run_attempt_column():
    inspections = [
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=2,
            head_sha="44e5d386da9c78d59fab018b04fd433b7cfeabc4",
            title="Nightly rerun",
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
            actor="octocat",
            event="workflow_dispatch",
        ),
    ]

    table = format_recent_runs_table(inspections)

    assert "run_attempt" in table.splitlines()[0]
    assert "head_sha" in table.splitlines()[0]
    assert "102" in table
    assert "2" in table
    assert "44e5d386da9c" in table


def test_recent_runs_markdown_table_includes_run_attempt_column():
    inspections = [
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=2,
            head_sha="44e5d386da9c78d59fab018b04fd433b7cfeabc4",
            title="Nightly rerun",
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
            actor="octocat",
            event="workflow_dispatch",
        ),
    ]

    table = format_recent_runs_markdown_table(inspections)

    assert "| run_id | run_number | run_attempt | head_sha | status |" in table
    assert "| 102 | 12 | 2 | 44e5d386da9c | completed |" in table


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


def test_recent_runs_markdown_report_mentions_artifacts_only_filter():
    inspections = [
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=1,
            title="Nightly",
            status="completed",
            conclusion="success",
            html_url="https://github.com/example/project/actions/runs/102",
            created_at="2026-07-14T09:00:00Z",
            total_artifacts=1,
            expired_artifacts=0,
            zip_artifacts=0,
            direct_file_artifacts=1,
            unknown_artifacts=0,
            strict_failures=[],
        ),
    ]

    context = build_recent_runs_context(
        "example/project",
        5,
        inspections,
        scanned_runs=3,
        artifacts_only=True,
    )
    report = format_recent_runs_markdown_report(context, inspections)

    assert "runs with matching artifacts only" in report
    assert "- Runs scanned: 3" in report
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


def test_recent_runs_markdown_report_mentions_branch_filter():
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
        branch_filter="main",
    )
    report = format_recent_runs_markdown_report(context, inspections)

    assert "branch contains 'main'" in report


def test_recent_runs_markdown_report_mentions_head_sha_filter():
    inspections = [
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=1,
            head_sha="44e5d386da9c78d59fab018b04fd433b7cfeabc4",
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
        head_sha_filter="44e5d386",
    )
    report = format_recent_runs_markdown_report(context, inspections)

    assert "head_sha contains '44e5d386'" in report


def test_recent_runs_markdown_report_mentions_conclusion_filter():
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
        conclusion_filter="failure",
    )
    report = format_recent_runs_markdown_report(context, inspections)

    assert "conclusion contains 'failure'" in report


def test_recent_runs_markdown_report_mentions_status_filter():
    inspections = [
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=1,
            title="Nightly",
            status="in_progress",
            conclusion=None,
            html_url="https://github.com/example/project/actions/runs/102",
            created_at="2026-07-14T09:00:00Z",
            total_artifacts=1,
            expired_artifacts=0,
            zip_artifacts=1,
            direct_file_artifacts=0,
            unknown_artifacts=0,
            strict_failures=[],
            event="workflow_dispatch",
        ),
    ]

    context = build_recent_runs_context(
        "example/project",
        5,
        inspections,
        scanned_runs=1,
        status_filter="progress",
    )
    report = format_recent_runs_markdown_report(context, inspections)

    assert "status contains 'progress'" in report


def test_recent_runs_markdown_report_mentions_actor_filter():
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
            actor="dependabot[bot]",
            event="schedule",
        ),
    ]

    context = build_recent_runs_context(
        "example/project",
        5,
        inspections,
        scanned_runs=1,
        actor_filter="dependabot",
    )
    report = format_recent_runs_markdown_report(context, inspections)

    assert "actor contains 'dependabot'" in report


def test_recent_runs_markdown_report_mentions_attempt_filter():
    inspections = [
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=2,
            title="Nightly rerun",
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
            actor="octocat",
            event="workflow_dispatch",
        ),
    ]

    context = build_recent_runs_context(
        "example/project",
        5,
        inspections,
        scanned_runs=1,
        attempt_filter=2,
    )
    report = format_recent_runs_markdown_report(context, inspections)

    assert "attempt = 2" in report


def test_recent_runs_markdown_report_mentions_created_after_filter():
    inspections = [
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=1,
            title="Nightly",
            status="completed",
            conclusion="success",
            html_url="https://github.com/example/project/actions/runs/102",
            created_at="2026-07-18T00:00:00Z",
            total_artifacts=1,
            expired_artifacts=0,
            zip_artifacts=1,
            direct_file_artifacts=0,
            unknown_artifacts=0,
            strict_failures=[],
            actor="octocat",
            event="push",
        ),
    ]

    context = build_recent_runs_context(
        "example/project",
        5,
        inspections,
        scanned_runs=1,
        created_after_filter="2026-07-18",
    )
    report = format_recent_runs_markdown_report(context, inspections)

    assert "created_at >= '2026-07-18'" in report


def test_recent_runs_markdown_report_mentions_created_before_filter():
    inspections = [
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=1,
            title="Nightly",
            status="completed",
            conclusion="success",
            html_url="https://github.com/example/project/actions/runs/102",
            created_at="2026-07-18T12:30:00Z",
            total_artifacts=1,
            expired_artifacts=0,
            zip_artifacts=1,
            direct_file_artifacts=0,
            unknown_artifacts=0,
            strict_failures=[],
            actor="octocat",
            event="schedule",
        ),
    ]

    context = build_recent_runs_context(
        "example/project",
        5,
        inspections,
        scanned_runs=1,
        created_before_filter="2026-07-18",
    )
    report = format_recent_runs_markdown_report(context, inspections)

    assert "created_at <= end of '2026-07-18'" in report


def test_recent_runs_markdown_report_mentions_artifact_name_filter():
    inspections = [
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=1,
            title="Nightly",
            status="completed",
            conclusion="success",
            html_url="https://github.com/example/project/actions/runs/102",
            created_at="2026-07-14T09:00:00Z",
            total_artifacts=1,
            expired_artifacts=0,
            zip_artifacts=0,
            direct_file_artifacts=1,
            unknown_artifacts=0,
            strict_failures=[],
            actor="octocat",
            event="push",
        ),
    ]

    context = build_recent_runs_context(
        "example/project",
        5,
        inspections,
        scanned_runs=1,
        artifact_name_filter="summary",
    )

    report = format_recent_runs_markdown_report(context, inspections)

    assert "artifact name contains 'summary'" in report


def test_recent_runs_markdown_report_mentions_artifact_kind_filter():
    inspections = [
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=1,
            title="Nightly",
            status="completed",
            conclusion="success",
            html_url="https://github.com/example/project/actions/runs/102",
            created_at="2026-07-14T09:00:00Z",
            total_artifacts=1,
            expired_artifacts=0,
            zip_artifacts=0,
            direct_file_artifacts=1,
            unknown_artifacts=0,
            strict_failures=[],
            actor="octocat",
            event="push",
        ),
    ]

    context = build_recent_runs_context(
        "example/project",
        5,
        inspections,
        scanned_runs=1,
        artifact_kind_filter="direct-file",
    )

    report = format_recent_runs_markdown_report(context, inspections)

    assert "artifact kind = 'direct-file'" in report


def test_recent_runs_markdown_report_mentions_download_strategy_filter():
    inspections = [
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=1,
            title="Nightly",
            status="completed",
            conclusion="success",
            html_url="https://github.com/example/project/actions/runs/102",
            created_at="2026-07-14T09:00:00Z",
            total_artifacts=1,
            expired_artifacts=0,
            zip_artifacts=0,
            direct_file_artifacts=1,
            unknown_artifacts=0,
            strict_failures=[],
            actor="octocat",
            event="push",
        ),
    ]

    context = build_recent_runs_context(
        "example/project",
        5,
        inspections,
        scanned_runs=1,
        download_strategy_filter="download-as-is",
    )

    report = format_recent_runs_markdown_report(context, inspections)

    assert "download strategy = 'download-as-is'" in report


def test_recent_runs_markdown_report_mentions_artifact_size_filters():
    inspections = [
        RecentRunInspection(
            run_id=102,
            run_number=12,
            run_attempt=1,
            title="Nightly",
            status="completed",
            conclusion="success",
            html_url="https://github.com/example/project/actions/runs/102",
            created_at="2026-07-14T09:00:00Z",
            total_artifacts=1,
            expired_artifacts=0,
            zip_artifacts=0,
            direct_file_artifacts=1,
            unknown_artifacts=0,
            strict_failures=[],
            actor="octocat",
            event="push",
        ),
    ]

    context = build_recent_runs_context(
        "example/project",
        5,
        inspections,
        scanned_runs=1,
        artifact_min_bytes=200,
        artifact_max_bytes=300,
    )

    report = format_recent_runs_markdown_report(context, inspections)

    assert "artifact size >= 200 bytes" in report
    assert "artifact size <= 300 bytes" in report


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
