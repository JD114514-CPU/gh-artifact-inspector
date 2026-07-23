from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from gh_artifact_inspector.downloader import build_download_actions, render_download_script


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


@dataclass(slots=True)
class ArtifactSummary:
    name: str
    size_in_bytes: int
    expired: bool
    archive_kind: str
    content_type: str | None
    download_strategy: str
    note: str
    archive_download_url: str | None


@dataclass(slots=True)
class ReportContext:
    source_label: str
    total_artifacts: int
    expired_artifacts: int
    zip_artifacts: int
    direct_file_artifacts: int
    unknown_artifacts: int


@dataclass(slots=True)
class RecentRunsContext:
    source_label: str
    scanned_runs: int
    total_runs: int
    runs_with_artifacts: int
    total_artifacts: int
    runs_with_failures: int


@dataclass(slots=True)
class RecentRunInspection:
    run_id: int
    run_number: int | None
    run_attempt: int | None
    title: str
    status: str
    conclusion: str | None
    html_url: str | None
    created_at: str | None
    total_artifacts: int
    expired_artifacts: int
    zip_artifacts: int
    direct_file_artifacts: int
    unknown_artifacts: int
    strict_failures: list[str]
    actor: str = "unknown"
    event: str = "unknown"
    head_sha: str | None = None


@dataclass(slots=True)
class WorkflowSummary:
    title: str
    runs: int
    total_artifacts: int
    expired_artifacts: int
    zip_artifacts: int
    direct_file_artifacts: int
    unknown_artifacts: int
    runs_with_failures: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gh-artifact-inspector",
        description="Inspect GitHub Actions artifacts from a run or a saved JSON payload.",
    )
    parser.add_argument("--repo", help="Repository in owner/name form.")
    parser.add_argument("--run-id", type=int, help="GitHub Actions run id.")
    parser.add_argument(
        "--run-url",
        help="GitHub Actions run URL, for example https://github.com/owner/name/actions/runs/123456789.",
    )
    parser.add_argument(
        "--from-file",
        type=Path,
        help="Read a saved artifacts JSON payload instead of calling the GitHub API.",
    )
    parser.add_argument(
        "--recent-runs",
        type=int,
        help="Inspect the most recent N workflow runs for a repository. Requires --repo and cannot be combined with --run-id, --run-url, or --from-file.",
    )
    parser.add_argument(
        "--strict-only",
        action="store_true",
        help="When used with --recent-runs, keep only runs whose artifact inspection has strict failures.",
    )
    parser.add_argument(
        "--artifacts-only",
        action="store_true",
        help="When used with --recent-runs, keep only runs that still have at least one matching artifact after filters.",
    )
    parser.add_argument(
        "--workflow",
        help="When used with --recent-runs, only inspect workflow runs whose title contains this case-insensitive text.",
    )
    parser.add_argument(
        "--branch",
        help="When used with --recent-runs, only inspect workflow runs whose head branch matches this case-insensitive text.",
    )
    parser.add_argument(
        "--head-sha",
        help="When used with --recent-runs, only inspect workflow runs whose head SHA matches this case-insensitive text.",
    )
    parser.add_argument(
        "--event",
        help="When used with --recent-runs, only inspect workflow runs whose event matches this case-insensitive text.",
    )
    parser.add_argument(
        "--conclusion",
        help="When used with --recent-runs, only inspect workflow runs whose conclusion matches this case-insensitive text.",
    )
    parser.add_argument(
        "--status",
        help="When used with --recent-runs, only inspect workflow runs whose status matches this case-insensitive text.",
    )
    parser.add_argument(
        "--actor",
        help="When used with --recent-runs, only inspect workflow runs whose actor login matches this case-insensitive text.",
    )
    parser.add_argument(
        "--attempt",
        type=int,
        help="When used with --recent-runs, only inspect workflow runs whose run attempt exactly matches this integer.",
    )
    parser.add_argument(
        "--created-after",
        help="When used with --recent-runs, only inspect workflow runs whose created_at is on or after this date or timestamp, for example 2026-07-17 or 2026-07-17T08:00:00Z.",
    )
    parser.add_argument(
        "--created-before",
        help="When used with --recent-runs, only inspect workflow runs whose created_at is on or before this date or timestamp, for example 2026-07-20 or 2026-07-20T18:30:00Z.",
    )
    parser.add_argument(
        "--artifact-name",
        help="Only keep artifacts whose name contains this case-insensitive text. Applies to single-run inspection and --recent-runs summaries.",
    )
    parser.add_argument(
        "--artifact-kind",
        choices=("zip", "direct-file", "unknown"),
        help="Only keep artifacts whose inferred archive kind matches this value. Applies to single-run inspection and --recent-runs summaries.",
    )
    parser.add_argument(
        "--download-strategy",
        choices=("download-and-unzip", "download-as-is", "manual-check", "unavailable"),
        help="Only keep artifacts whose recommended consumption action matches this value. Applies to single-run inspection and --recent-runs summaries.",
    )
    parser.add_argument(
        "--github-token",
        default=os.getenv("GITHUB_TOKEN"),
        help="GitHub token for higher rate limits and private repositories. Defaults to GITHUB_TOKEN.",
    )
    parser.add_argument(
        "--probe-download",
        action="store_true",
        help="Issue HEAD requests to artifact download URLs to capture content-type when possible.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON instead of a table.",
    )
    parser.add_argument(
        "--json-report",
        action="store_true",
        help="Emit a structured report with source, summary counts, strict failures, and artifact details.",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Emit a Markdown table for issue comments, PRs, or reports.",
    )
    parser.add_argument(
        "--markdown-report",
        action="store_true",
        help="Emit a Markdown report with summary bullets plus the artifact table.",
    )
    parser.add_argument(
        "--emit-script",
        choices=("powershell", "bash"),
        help="Emit a download script for the inspected artifacts instead of a table/report. Requires --output-dir and cannot be combined with --recent-runs.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Target directory to bake into --emit-script output.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with code 2 when any artifact is expired or needs manual packaging checks.",
    )
    return parser


def validate_argument_combinations(args: argparse.Namespace) -> None:
    if args.from_file and any(value is not None for value in (args.repo, args.run_id, args.run_url)):
        raise SystemExit("--from-file cannot be combined with --repo, --run-id, or --run-url.")
    if args.strict_only and args.recent_runs is None:
        raise SystemExit("--strict-only can only be used together with --recent-runs.")
    if getattr(args, "artifacts_only", False) and args.recent_runs is None:
        raise SystemExit("--artifacts-only can only be used together with --recent-runs.")
    if args.workflow and args.recent_runs is None:
        raise SystemExit("--workflow can only be used together with --recent-runs.")
    if getattr(args, "branch", None) and args.recent_runs is None:
        raise SystemExit("--branch can only be used together with --recent-runs.")
    if getattr(args, "head_sha", None) and args.recent_runs is None:
        raise SystemExit("--head-sha can only be used together with --recent-runs.")
    if getattr(args, "event", None) and args.recent_runs is None:
        raise SystemExit("--event can only be used together with --recent-runs.")
    if getattr(args, "conclusion", None) and args.recent_runs is None:
        raise SystemExit("--conclusion can only be used together with --recent-runs.")
    if getattr(args, "status", None) and args.recent_runs is None:
        raise SystemExit("--status can only be used together with --recent-runs.")
    if getattr(args, "actor", None) and args.recent_runs is None:
        raise SystemExit("--actor can only be used together with --recent-runs.")
    if getattr(args, "attempt", None) is not None and args.recent_runs is None:
        raise SystemExit("--attempt can only be used together with --recent-runs.")
    if getattr(args, "created_after", None) and args.recent_runs is None:
        raise SystemExit("--created-after can only be used together with --recent-runs.")
    if getattr(args, "created_before", None) and args.recent_runs is None:
        raise SystemExit("--created-before can only be used together with --recent-runs.")
    created_after = getattr(args, "created_after", None)
    created_before = getattr(args, "created_before", None)
    if created_after and created_before:
        created_after_dt = parse_datetime_filter(created_after, "--created-after")
        created_before_dt = created_before_filter_upper_bound(created_before)
        if created_after_dt > created_before_dt:
            raise SystemExit("--created-after cannot be later than --created-before.")


def read_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.from_file:
        return json.loads(args.from_file.read_text(encoding="utf-8"))

    repo, run_id = resolve_run_target(args)

    owner, repo_name = split_repo(repo)
    url = f"https://api.github.com/repos/{owner}/{repo_name}/actions/runs/{run_id}/artifacts?per_page=100"
    headers = github_headers(args.github_token)
    return request_json(url, headers=headers)


def resolve_recent_runs_target(args: argparse.Namespace) -> tuple[str, int]:
    if args.recent_runs is None:
        raise SystemExit("--recent-runs requires a positive integer.")
    if args.recent_runs < 1:
        raise SystemExit("--recent-runs must be at least 1.")
    if args.from_file:
        raise SystemExit("--recent-runs cannot be combined with --from-file.")
    if args.run_id or args.run_url:
        raise SystemExit("--recent-runs cannot be combined with --run-id or --run-url.")
    if not args.repo:
        raise SystemExit("--repo is required when --recent-runs is used.")
    return args.repo, args.recent_runs


def resolve_run_target(args: argparse.Namespace) -> tuple[str, int]:
    parsed_repo: str | None = None
    parsed_run_id: int | None = None
    if args.run_url:
        parsed_repo, parsed_run_id = parse_run_url(args.run_url)

    repo = args.repo or parsed_repo
    run_id = args.run_id or parsed_run_id
    if not repo or not run_id:
        raise SystemExit(
            "--repo and --run-id are required unless --from-file is used. "
            "You can also provide --run-url instead."
        )
    if args.repo and parsed_repo and args.repo != parsed_repo:
        raise SystemExit(f"--repo '{args.repo}' does not match --run-url repository '{parsed_repo}'.")
    if args.run_id and parsed_run_id and args.run_id != parsed_run_id:
        raise SystemExit(f"--run-id '{args.run_id}' does not match --run-url run id '{parsed_run_id}'.")
    return repo, run_id


def parse_run_url(run_url: str) -> tuple[str, int]:
    parsed = urlparse(run_url)
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        raise SystemExit(f"Invalid run URL '{run_url}'. Expected a github.com actions run URL.")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 5 or parts[2] != "actions" or parts[3] != "runs":
        raise SystemExit(f"Invalid run URL '{run_url}'. Expected /owner/repo/actions/runs/<run_id>.")
    if not parts[4].isdigit():
        raise SystemExit(f"Invalid run URL '{run_url}'. Run id must be numeric.")

    return f"{parts[0]}/{parts[1]}", int(parts[4])


def split_repo(repo: str) -> tuple[str, str]:
    if "/" not in repo:
        raise SystemExit(f"Invalid repo '{repo}'. Expected owner/name.")
    owner, name = repo.split("/", 1)
    if not owner or not name:
        raise SystemExit(f"Invalid repo '{repo}'. Expected owner/name.")
    return owner, name


def github_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "gh-artifact-inspector",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def request_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"GitHub API request failed ({exc.code}) for {url}\n{body}") from exc
    except URLError as exc:
        raise SystemExit(f"Network request failed for {url}: {exc.reason}") from exc


def workflow_matches_filter(run: dict[str, Any], workflow_filter: str | None) -> bool:
    if not workflow_filter:
        return True
    title = str(run.get("display_title") or run.get("name") or "")
    return workflow_filter.lower() in title.lower()


def branch_matches_filter(run: dict[str, Any], branch_filter: str | None) -> bool:
    if not branch_filter:
        return True
    branch = str(run.get("head_branch") or "")
    return branch_filter.lower() in branch.lower()


def head_sha_matches_filter(run: dict[str, Any], head_sha_filter: str | None) -> bool:
    if not head_sha_filter:
        return True
    head_sha = str(run.get("head_sha") or "")
    return head_sha_filter.lower() in head_sha.lower()


def event_matches_filter(run: dict[str, Any], event_filter: str | None) -> bool:
    if not event_filter:
        return True
    event = str(run.get("event") or "")
    return event_filter.lower() in event.lower()


def conclusion_matches_filter(run: dict[str, Any], conclusion_filter: str | None) -> bool:
    if not conclusion_filter:
        return True
    conclusion = str(run.get("conclusion") or "")
    return conclusion_filter.lower() in conclusion.lower()


def status_matches_filter(run: dict[str, Any], status_filter: str | None) -> bool:
    if not status_filter:
        return True
    status = str(run.get("status") or "")
    return status_filter.lower() in status.lower()


def run_actor_login(run: dict[str, Any]) -> str:
    actor = run.get("actor")
    if isinstance(actor, dict):
        login = actor.get("login")
        if login:
            return str(login)
    triggering_actor = run.get("triggering_actor")
    if isinstance(triggering_actor, dict):
        login = triggering_actor.get("login")
        if login:
            return str(login)
    return "unknown"


def actor_matches_filter(run: dict[str, Any], actor_filter: str | None) -> bool:
    if not actor_filter:
        return True
    return actor_filter.lower() in run_actor_login(run).lower()


def attempt_matches_filter(run: dict[str, Any], attempt_filter: int | None) -> bool:
    if attempt_filter is None:
        return True
    run_attempt = run.get("run_attempt")
    if run_attempt is None:
        return False
    try:
        return int(run_attempt) == attempt_filter
    except (TypeError, ValueError):
        return False


def parse_datetime_filter(value: str, flag_name: str) -> datetime:
    normalized = value.strip()
    if not normalized:
        raise SystemExit(f"{flag_name} cannot be empty.")
    if len(normalized) == 10:
        try:
            return datetime.fromisoformat(normalized).replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise SystemExit(
                f"{flag_name} must be a date in YYYY-MM-DD form or an ISO-8601 timestamp."
            ) from exc
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SystemExit(
            f"{flag_name} must be a date in YYYY-MM-DD form or an ISO-8601 timestamp."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_run_created_at(value: Any) -> datetime | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def created_before_filter_upper_bound(value: str) -> datetime:
    created_before = parse_datetime_filter(value, "--created-before")
    if len(value.strip()) == 10:
        return created_before + timedelta(days=1)
    return created_before


def created_at_matches_filter(
    run: dict[str, Any],
    created_after_filter: str | None,
    created_before_filter: str | None = None,
) -> bool:
    if not created_after_filter and not created_before_filter:
        return True
    created_at = parse_run_created_at(run.get("created_at"))
    if created_at is None:
        return False
    if created_after_filter and created_at < parse_datetime_filter(created_after_filter, "--created-after"):
        return False
    if created_before_filter:
        created_before = created_before_filter_upper_bound(created_before_filter)
        if len(created_before_filter.strip()) == 10:
            return created_at < created_before
        return created_at <= created_before
    return True


def artifact_name_matches_filter(name: str, artifact_name_filter: str | None) -> bool:
    if not artifact_name_filter:
        return True
    return artifact_name_filter.lower() in name.lower()


def artifact_kind_matches_filter(archive_kind: str, artifact_kind_filter: str | None) -> bool:
    if not artifact_kind_filter:
        return True
    return archive_kind.lower() == artifact_kind_filter.lower()


def download_strategy_matches_filter(download_strategy: str, download_strategy_filter: str | None) -> bool:
    if not download_strategy_filter:
        return True
    return download_strategy.lower() == download_strategy_filter.lower()


def filter_summaries_by_artifact_name(
    summaries: list[ArtifactSummary], artifact_name_filter: str | None
) -> list[ArtifactSummary]:
    return [
        summary
        for summary in summaries
        if artifact_name_matches_filter(summary.name, artifact_name_filter)
    ]


def filter_summaries(
    summaries: list[ArtifactSummary],
    *,
    artifact_name_filter: str | None = None,
    artifact_kind_filter: str | None = None,
    download_strategy_filter: str | None = None,
) -> list[ArtifactSummary]:
    return [
        summary
        for summary in summaries
        if artifact_name_matches_filter(summary.name, artifact_name_filter)
        and artifact_kind_matches_filter(summary.archive_kind, artifact_kind_filter)
        and download_strategy_matches_filter(summary.download_strategy, download_strategy_filter)
    ]


def fetch_recent_runs(
    repo: str,
    limit: int,
    headers: dict[str, str],
    workflow_filter: str | None = None,
    branch_filter: str | None = None,
    head_sha_filter: str | None = None,
    event_filter: str | None = None,
    conclusion_filter: str | None = None,
    status_filter: str | None = None,
    actor_filter: str | None = None,
    attempt_filter: int | None = None,
    created_after_filter: str | None = None,
    created_before_filter: str | None = None,
) -> list[dict[str, Any]]:
    owner, repo_name = split_repo(repo)
    runs: list[dict[str, Any]] = []
    page = 1
    while len(runs) < limit:
        needs_extra_pages = (
            workflow_filter
            or branch_filter
            or head_sha_filter
            or event_filter
            or conclusion_filter
            or status_filter
            or actor_filter
            or attempt_filter is not None
            or created_after_filter
            or created_before_filter
        )
        per_page = min(max(limit, 30), 100) if needs_extra_pages else min(limit - len(runs), 100)
        url = f"https://api.github.com/repos/{owner}/{repo_name}/actions/runs?per_page={per_page}&page={page}"
        payload = request_json(url, headers=headers)
        page_runs = payload.get("workflow_runs", [])
        if not isinstance(page_runs, list):
            raise SystemExit(f"Unexpected workflow run payload for {repo}.")
        runs.extend(
            run
            for run in page_runs
            if workflow_matches_filter(run, workflow_filter)
            and branch_matches_filter(run, branch_filter)
            and head_sha_matches_filter(run, head_sha_filter)
            and event_matches_filter(run, event_filter)
            and conclusion_matches_filter(run, conclusion_filter)
            and status_matches_filter(run, status_filter)
            and actor_matches_filter(run, actor_filter)
            and attempt_matches_filter(run, attempt_filter)
            and created_at_matches_filter(run, created_after_filter, created_before_filter)
        )
        if len(page_runs) < per_page:
            break
        page += 1
    return runs[:limit]


def probe_content_type(url: str | None, headers: dict[str, str]) -> str | None:
    if not url:
        return None
    methods: tuple[tuple[str, dict[str, str]], ...] = (
        ("HEAD", headers),
        ("GET", {**headers, "Range": "bytes=0-0"}),
    )
    for method, request_headers in methods:
        request = Request(url, headers=request_headers, method=method)
        try:
            opener = build_opener(NoRedirectHandler())
            with opener.open(request, timeout=30) as response:
                content_type = response.headers.get_content_type()
                if content_type:
                    return content_type
        except HTTPError as exc:
            location = exc.headers.get("Location")
            if location:
                redirected_headers = {
                    "User-Agent": headers.get("User-Agent", "gh-artifact-inspector"),
                }
                if method == "GET":
                    redirected_headers["Range"] = "bytes=0-0"
                redirected_request = Request(location, headers=redirected_headers, method=method)
                try:
                    with urlopen(redirected_request, timeout=30) as response:
                        content_type = response.headers.get_content_type()
                        if content_type:
                            return content_type
                except Exception:
                    continue
        except Exception:
            continue
    return None


def summarize_payload(payload: dict[str, Any], headers: dict[str, str], probe_download: bool) -> list[ArtifactSummary]:
    artifacts = payload.get("artifacts", [])
    summaries: list[ArtifactSummary] = []
    for artifact in artifacts:
        content_type = artifact.get("content_type")
        if probe_download and not content_type:
            content_type = probe_content_type(artifact.get("archive_download_url"), headers)
        summaries.append(summarize_artifact(artifact, content_type=content_type))
    return summaries


def summarize_artifact(artifact: dict[str, Any], content_type: str | None = None) -> ArtifactSummary:
    name = str(artifact.get("name") or "")
    size_in_bytes = int(artifact.get("size_in_bytes") or 0)
    expired = bool(artifact.get("expired"))
    archive_download_url = artifact.get("archive_download_url")

    archive_kind = infer_archive_kind(name=name, content_type=content_type, archive_download_url=archive_download_url)
    download_strategy, note = recommend_strategy(archive_kind=archive_kind, expired=expired)

    return ArtifactSummary(
        name=name,
        size_in_bytes=size_in_bytes,
        expired=expired,
        archive_kind=archive_kind,
        content_type=content_type,
        download_strategy=download_strategy,
        note=note,
        archive_download_url=archive_download_url,
    )


def build_report_context(args: argparse.Namespace, payload: dict[str, Any], summaries: list[ArtifactSummary]) -> ReportContext:
    if args.from_file:
        source_label = f"saved payload `{args.from_file}`"
    else:
        repo, run_id = resolve_run_target(args)
        source_label = f"GitHub Actions run `{repo}` / `{run_id}`"
    artifact_name_filter = getattr(args, "artifact_name", None)
    artifact_kind_filter = getattr(args, "artifact_kind", None)
    download_strategy_filter = getattr(args, "download_strategy", None)
    artifact_name_suffix = f"; artifact name contains '{artifact_name_filter}'" if artifact_name_filter else ""
    artifact_kind_suffix = f"; artifact kind = '{artifact_kind_filter}'" if artifact_kind_filter else ""
    download_strategy_suffix = (
        f"; download strategy = '{download_strategy_filter}'" if download_strategy_filter else ""
    )

    return ReportContext(
        source_label=f"{source_label}{artifact_name_suffix}{artifact_kind_suffix}{download_strategy_suffix}",
        total_artifacts=len(summaries),
        expired_artifacts=sum(1 for summary in summaries if summary.expired),
        zip_artifacts=sum(1 for summary in summaries if summary.archive_kind == "zip"),
        direct_file_artifacts=sum(1 for summary in summaries if summary.archive_kind == "direct-file"),
        unknown_artifacts=sum(1 for summary in summaries if summary.archive_kind == "unknown"),
    )


def inspect_recent_runs(
    repo: str,
    recent_runs: int,
    headers: dict[str, str],
    probe_download: bool,
    workflow_filter: str | None = None,
    branch_filter: str | None = None,
    head_sha_filter: str | None = None,
    event_filter: str | None = None,
    conclusion_filter: str | None = None,
    status_filter: str | None = None,
    actor_filter: str | None = None,
    attempt_filter: int | None = None,
    created_after_filter: str | None = None,
    created_before_filter: str | None = None,
    artifact_name_filter: str | None = None,
    artifact_kind_filter: str | None = None,
    download_strategy_filter: str | None = None,
) -> list[RecentRunInspection]:
    inspections: list[RecentRunInspection] = []
    for run in fetch_recent_runs(
        repo,
        recent_runs,
        headers,
        workflow_filter=workflow_filter,
        branch_filter=branch_filter,
        head_sha_filter=head_sha_filter,
        event_filter=event_filter,
        conclusion_filter=conclusion_filter,
        status_filter=status_filter,
        actor_filter=actor_filter,
        attempt_filter=attempt_filter,
        created_after_filter=created_after_filter,
        created_before_filter=created_before_filter,
    ):
        run_id = int(run.get("id") or 0)
        owner, repo_name = split_repo(repo)
        artifacts_url = f"https://api.github.com/repos/{owner}/{repo_name}/actions/runs/{run_id}/artifacts?per_page=100"
        payload = request_json(artifacts_url, headers=headers)
        summaries = summarize_payload(payload, headers=headers, probe_download=probe_download)
        summaries = filter_summaries(
            summaries,
            artifact_name_filter=artifact_name_filter,
            artifact_kind_filter=artifact_kind_filter,
            download_strategy_filter=download_strategy_filter,
        )
        strict_failures = collect_strict_failures(summaries)
        inspections.append(
            RecentRunInspection(
                run_id=run_id,
                run_number=int(run["run_number"]) if run.get("run_number") is not None else None,
                run_attempt=int(run["run_attempt"]) if run.get("run_attempt") is not None else None,
                title=str(run.get("display_title") or run.get("name") or f"run {run_id}"),
                status=str(run.get("status") or "unknown"),
                conclusion=str(run.get("conclusion")) if run.get("conclusion") is not None else None,
                html_url=str(run.get("html_url")) if run.get("html_url") is not None else None,
                created_at=str(run.get("created_at")) if run.get("created_at") is not None else None,
                total_artifacts=len(summaries),
                expired_artifacts=sum(1 for summary in summaries if summary.expired),
                zip_artifacts=sum(1 for summary in summaries if summary.archive_kind == "zip"),
                direct_file_artifacts=sum(1 for summary in summaries if summary.archive_kind == "direct-file"),
                unknown_artifacts=sum(1 for summary in summaries if summary.archive_kind == "unknown"),
                strict_failures=strict_failures,
                actor=run_actor_login(run),
                event=str(run.get("event") or "unknown"),
                head_sha=str(run.get("head_sha")) if run.get("head_sha") is not None else None,
            )
        )
    return inspections


def filter_recent_run_inspections(
    inspections: list[RecentRunInspection], *, strict_only: bool, artifacts_only: bool = False
) -> list[RecentRunInspection]:
    filtered = inspections
    if strict_only:
        filtered = [inspection for inspection in filtered if inspection.strict_failures]
    if artifacts_only:
        filtered = [inspection for inspection in filtered if inspection.total_artifacts > 0]
    return filtered


def build_recent_runs_context(
    repo: str,
    recent_runs: int,
    inspections: list[RecentRunInspection],
    *,
    scanned_runs: int | None = None,
    strict_only: bool = False,
    artifacts_only: bool = False,
    workflow_filter: str | None = None,
    branch_filter: str | None = None,
    head_sha_filter: str | None = None,
    event_filter: str | None = None,
    conclusion_filter: str | None = None,
    status_filter: str | None = None,
    actor_filter: str | None = None,
    attempt_filter: int | None = None,
    created_after_filter: str | None = None,
    created_before_filter: str | None = None,
    artifact_name_filter: str | None = None,
    artifact_kind_filter: str | None = None,
    download_strategy_filter: str | None = None,
) -> RecentRunsContext:
    suffix = "; strict failures only" if strict_only else ""
    artifacts_only_suffix = "; runs with matching artifacts only" if artifacts_only else ""
    workflow_suffix = f"; workflow contains '{workflow_filter}'" if workflow_filter else ""
    branch_suffix = f"; branch contains '{branch_filter}'" if branch_filter else ""
    head_sha_suffix = f"; head_sha contains '{head_sha_filter}'" if head_sha_filter else ""
    event_suffix = f"; event contains '{event_filter}'" if event_filter else ""
    conclusion_suffix = f"; conclusion contains '{conclusion_filter}'" if conclusion_filter else ""
    status_suffix = f"; status contains '{status_filter}'" if status_filter else ""
    actor_suffix = f"; actor contains '{actor_filter}'" if actor_filter else ""
    attempt_suffix = f"; attempt = {attempt_filter}" if attempt_filter is not None else ""
    created_after_suffix = f"; created_at >= '{created_after_filter}'" if created_after_filter else ""
    created_before_suffix = (
        f"; created_at <= end of '{created_before_filter}'"
        if created_before_filter and len(created_before_filter.strip()) == 10
        else f"; created_at <= '{created_before_filter}'"
        if created_before_filter
        else ""
    )
    artifact_name_suffix = f"; artifact name contains '{artifact_name_filter}'" if artifact_name_filter else ""
    artifact_kind_suffix = f"; artifact kind = '{artifact_kind_filter}'" if artifact_kind_filter else ""
    download_strategy_suffix = (
        f"; download strategy = '{download_strategy_filter}'" if download_strategy_filter else ""
    )
    return RecentRunsContext(
        source_label=(
            f"recent GitHub Actions runs `{repo}` "
            f"(limit {recent_runs}{workflow_suffix}{branch_suffix}{head_sha_suffix}{event_suffix}{conclusion_suffix}{status_suffix}{actor_suffix}{attempt_suffix}{created_after_suffix}{created_before_suffix}{artifact_name_suffix}{artifact_kind_suffix}{download_strategy_suffix}{artifacts_only_suffix}{suffix})"
        ),
        scanned_runs=scanned_runs if scanned_runs is not None else len(inspections),
        total_runs=len(inspections),
        runs_with_artifacts=sum(1 for inspection in inspections if inspection.total_artifacts > 0),
        total_artifacts=sum(inspection.total_artifacts for inspection in inspections),
        runs_with_failures=sum(1 for inspection in inspections if inspection.strict_failures),
    )


def infer_archive_kind(name: str, content_type: str | None, archive_download_url: str | None) -> str:
    normalized_name = name.lower()
    normalized_type = (content_type or "").lower()
    normalized_url = (archive_download_url or "").lower()
    zip_content_types = {"application/zip", "application/x-zip-compressed"}
    packaged_file_suffixes = (".tar.gz", ".tgz", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar")
    packaged_file_types = {
        "application/gzip",
        "application/x-gzip",
        "application/x-tar",
        "application/x-bzip2",
        "application/x-xz",
        "application/x-7z-compressed",
        "application/vnd.rar",
    }

    if normalized_name.endswith(".zip") or normalized_type in zip_content_types or normalized_type.endswith("+zip"):
        return "zip"
    if normalized_name.endswith(packaged_file_suffixes) or normalized_type in packaged_file_types:
        return "direct-file"
    if normalized_type in {"application/octet-stream", "text/plain"} and ".zip" not in normalized_url:
        return "direct-file"
    if normalized_type.startswith("text/") or normalized_type in {"application/json", "application/xml"}:
        return "direct-file"
    if ".zip" in normalized_url:
        return "zip"
    return "unknown"


def recommend_strategy(archive_kind: str, expired: bool) -> tuple[str, str]:
    if expired:
        return "unavailable", "Artifact is expired. Re-run the workflow or extend retention."
    if archive_kind == "zip":
        return "download-and-unzip", "Treat the artifact as a zip archive before reading files."
    if archive_kind == "direct-file":
        return "download-as-is", "Do not unzip automatically; consume the downloaded file directly."
    return "manual-check", "Could not infer packaging confidently. Inspect headers or download one sample first."


def format_table(summaries: list[ArtifactSummary]) -> str:
    rows = [
        [
            "name",
            "size",
            "expired",
            "archive_kind",
            "content_type",
            "download_strategy",
            "note",
        ]
    ]
    for summary in summaries:
        rows.append(
            [
                summary.name,
                str(summary.size_in_bytes),
                "yes" if summary.expired else "no",
                summary.archive_kind,
                summary.content_type or "-",
                summary.download_strategy,
                summary.note,
            ]
        )

    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    lines: list[str] = []
    for row_index, row in enumerate(rows):
        padded = " | ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        lines.append(padded)
        if row_index == 0:
            lines.append("-+-".join("-" * width for width in widths))
    return "\n".join(lines)


def format_markdown_table(summaries: list[ArtifactSummary]) -> str:
    rows = [
        [
            "name",
            "size",
            "expired",
            "archive_kind",
            "content_type",
            "download_strategy",
            "note",
        ]
    ]
    for summary in summaries:
        rows.append(
            [
                summary.name,
                str(summary.size_in_bytes),
                "yes" if summary.expired else "no",
                summary.archive_kind,
                summary.content_type or "-",
                summary.download_strategy,
                summary.note,
            ]
        )

    def escape(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ")

    header = "| " + " | ".join(rows[0]) + " |"
    divider = "| " + " | ".join("---" for _ in rows[0]) + " |"
    body = ["| " + " | ".join(escape(value) for value in row) + " |" for row in rows[1:]]
    return "\n".join([header, divider, *body])


def format_markdown_report(context: ReportContext, summaries: list[ArtifactSummary]) -> str:
    lines = [
        "# Artifact inspection report",
        "",
        f"- Source: {context.source_label}",
        f"- Artifact count: {context.total_artifacts}",
        f"- Expired artifacts: {context.expired_artifacts}",
        f"- Packaging split: zip={context.zip_artifacts}, direct-file={context.direct_file_artifacts}, unknown={context.unknown_artifacts}",
        "",
        format_markdown_table(summaries),
    ]
    return "\n".join(lines)


def format_recent_runs_table(inspections: list[RecentRunInspection]) -> str:
    rows = [
        [
            "run_id",
            "run_number",
            "run_attempt",
            "head_sha",
            "status",
            "conclusion",
            "event",
            "actor",
            "artifacts",
            "zip",
            "direct",
            "expired",
            "unknown",
            "strict",
            "title",
        ]
    ]
    for inspection in inspections:
        display_head_sha = (inspection.head_sha or "-")[:12]
        rows.append(
            [
                str(inspection.run_id),
                str(inspection.run_number or "-"),
                str(inspection.run_attempt or "-"),
                display_head_sha,
                inspection.status,
                inspection.conclusion or "-",
                inspection.event,
                inspection.actor,
                str(inspection.total_artifacts),
                str(inspection.zip_artifacts),
                str(inspection.direct_file_artifacts),
                str(inspection.expired_artifacts),
                str(inspection.unknown_artifacts),
                str(len(inspection.strict_failures)),
                inspection.title,
            ]
        )

    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    lines: list[str] = []
    for row_index, row in enumerate(rows):
        padded = " | ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        lines.append(padded)
        if row_index == 0:
            lines.append("-+-".join("-" * width for width in widths))
    return "\n".join(lines)


def format_recent_runs_markdown_table(inspections: list[RecentRunInspection]) -> str:
    rows = [
        [
            "run_id",
            "run_number",
            "run_attempt",
            "head_sha",
            "status",
            "conclusion",
            "event",
            "actor",
            "artifacts",
            "zip",
            "direct",
            "expired",
            "unknown",
            "strict",
            "title",
        ]
    ]
    for inspection in inspections:
        display_head_sha = (inspection.head_sha or "-")[:12]
        rows.append(
            [
                str(inspection.run_id),
                str(inspection.run_number or "-"),
                str(inspection.run_attempt or "-"),
                display_head_sha,
                inspection.status,
                inspection.conclusion or "-",
                inspection.event,
                inspection.actor,
                str(inspection.total_artifacts),
                str(inspection.zip_artifacts),
                str(inspection.direct_file_artifacts),
                str(inspection.expired_artifacts),
                str(inspection.unknown_artifacts),
                str(len(inspection.strict_failures)),
                inspection.title,
            ]
        )

    def escape(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ")

    header = "| " + " | ".join(rows[0]) + " |"
    divider = "| " + " | ".join("---" for _ in rows[0]) + " |"
    body = ["| " + " | ".join(escape(value) for value in row) + " |" for row in rows[1:]]
    return "\n".join([header, divider, *body])


def collect_recent_runs_strict_failures(inspections: list[RecentRunInspection]) -> list[str]:
    failures: list[str] = []
    for inspection in inspections:
        for failure in inspection.strict_failures:
            failures.append(f"run {inspection.run_id} ({inspection.title}): {failure}")
    return failures


def summarize_recent_runs_by_workflow(inspections: list[RecentRunInspection]) -> list[WorkflowSummary]:
    grouped: dict[str, WorkflowSummary] = {}
    for inspection in inspections:
        summary = grouped.get(inspection.title)
        if summary is None:
            summary = WorkflowSummary(
                title=inspection.title,
                runs=0,
                total_artifacts=0,
                expired_artifacts=0,
                zip_artifacts=0,
                direct_file_artifacts=0,
                unknown_artifacts=0,
                runs_with_failures=0,
            )
            grouped[inspection.title] = summary

        summary.runs += 1
        summary.total_artifacts += inspection.total_artifacts
        summary.expired_artifacts += inspection.expired_artifacts
        summary.zip_artifacts += inspection.zip_artifacts
        summary.direct_file_artifacts += inspection.direct_file_artifacts
        summary.unknown_artifacts += inspection.unknown_artifacts
        if inspection.strict_failures:
            summary.runs_with_failures += 1

    return list(grouped.values())


def format_workflow_summary_markdown_table(summaries: list[WorkflowSummary]) -> str:
    header = [
        "title",
        "runs",
        "artifacts",
        "zip",
        "direct",
        "expired",
        "unknown",
        "strict",
    ]

    def escape(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ")

    rows = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for summary in summaries:
        rows.append(
            "| "
            + " | ".join(
                [
                    escape(summary.title),
                    str(summary.runs),
                    str(summary.total_artifacts),
                    str(summary.zip_artifacts),
                    str(summary.direct_file_artifacts),
                    str(summary.expired_artifacts),
                    str(summary.unknown_artifacts),
                    str(summary.runs_with_failures),
                ]
            )
            + " |"
        )
    return "\n".join(rows)


def format_recent_runs_markdown_report(context: RecentRunsContext, inspections: list[RecentRunInspection]) -> str:
    workflow_summaries = summarize_recent_runs_by_workflow(inspections)
    lines = [
        "# Recent artifact inspection report",
        "",
        f"- Source: {context.source_label}",
        f"- Runs scanned: {context.scanned_runs}",
    ]
    if context.scanned_runs != context.total_runs:
        lines.append(f"- Runs included: {context.total_runs}")
    lines.extend(
        [
        f"- Runs with artifacts: {context.runs_with_artifacts}",
        f"- Total artifacts seen: {context.total_artifacts}",
        f"- Runs with strict failures: {context.runs_with_failures}",
        "",
        format_recent_runs_markdown_table(inspections),
        ]
    )
    if workflow_summaries:
        lines.extend(
            [
                "",
                "## Workflow summary",
                "",
                format_workflow_summary_markdown_table(workflow_summaries),
            ]
        )
    failures = collect_recent_runs_strict_failures(inspections)
    if failures:
        lines.extend(["", "## Strict failures", ""])
        lines.extend(f"- {failure}" for failure in failures)
    return "\n".join(lines)


def format_json_report(context: ReportContext, summaries: list[ArtifactSummary]) -> dict[str, Any]:
    return {
        "source": context.source_label,
        "summary": {
            "total_artifacts": context.total_artifacts,
            "expired_artifacts": context.expired_artifacts,
            "zip_artifacts": context.zip_artifacts,
            "direct_file_artifacts": context.direct_file_artifacts,
            "unknown_artifacts": context.unknown_artifacts,
        },
        "strict_failures": collect_strict_failures(summaries),
        "artifacts": [asdict(summary) for summary in summaries],
    }


def format_recent_runs_json_report(
    context: RecentRunsContext, inspections: list[RecentRunInspection]
) -> dict[str, Any]:
    workflow_summaries = summarize_recent_runs_by_workflow(inspections)
    return {
        "source": context.source_label,
        "summary": {
            "scanned_runs": context.scanned_runs,
            "total_runs": context.total_runs,
            "runs_with_artifacts": context.runs_with_artifacts,
            "total_artifacts": context.total_artifacts,
            "runs_with_failures": context.runs_with_failures,
        },
        "strict_failures": collect_recent_runs_strict_failures(inspections),
        "workflow_summary": [asdict(summary) for summary in workflow_summaries],
        "runs": [asdict(inspection) for inspection in inspections],
    }


def collect_strict_failures(summaries: list[ArtifactSummary]) -> list[str]:
    failures: list[str] = []
    for summary in summaries:
        if summary.expired:
            failures.append(f"{summary.name or '<unnamed>'}: artifact expired")
        elif summary.download_strategy == "manual-check":
            failures.append(f"{summary.name or '<unnamed>'}: packaging requires manual check")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_argument_combinations(args)
    selected_formats = sum(
        bool(flag) for flag in (args.json, args.json_report, args.markdown, args.markdown_report, args.emit_script)
    )
    if selected_formats > 1:
        raise SystemExit(
            "--json, --json-report, --markdown, --markdown-report, and --emit-script cannot be used together."
        )
    if args.emit_script and not args.output_dir:
        raise SystemExit("--output-dir is required when --emit-script is used.")
    if args.output_dir and not args.emit_script:
        raise SystemExit("--output-dir can only be used together with --emit-script.")
    headers = github_headers(args.github_token)

    if args.recent_runs is not None:
        if args.emit_script:
            raise SystemExit("--emit-script cannot be combined with --recent-runs.")
        repo, recent_runs = resolve_recent_runs_target(args)
        all_inspections = inspect_recent_runs(
            repo,
            recent_runs,
            headers=headers,
            probe_download=args.probe_download,
            workflow_filter=args.workflow,
            branch_filter=args.branch,
            head_sha_filter=args.head_sha,
            event_filter=args.event,
            conclusion_filter=args.conclusion,
            status_filter=args.status,
            actor_filter=args.actor,
            attempt_filter=args.attempt,
            created_after_filter=args.created_after,
            created_before_filter=args.created_before,
            artifact_name_filter=args.artifact_name,
            artifact_kind_filter=args.artifact_kind,
            download_strategy_filter=args.download_strategy,
        )
        inspections = filter_recent_run_inspections(
            all_inspections,
            strict_only=args.strict_only,
            artifacts_only=args.artifacts_only,
        )
        context = build_recent_runs_context(
            repo,
            recent_runs,
            inspections,
            scanned_runs=len(all_inspections),
            strict_only=args.strict_only,
            artifacts_only=args.artifacts_only,
            workflow_filter=args.workflow,
            branch_filter=args.branch,
            head_sha_filter=args.head_sha,
            event_filter=args.event,
            conclusion_filter=args.conclusion,
            status_filter=args.status,
            actor_filter=args.actor,
            attempt_filter=args.attempt,
            created_after_filter=args.created_after,
            created_before_filter=args.created_before,
            artifact_name_filter=args.artifact_name,
            artifact_kind_filter=args.artifact_kind,
            download_strategy_filter=args.download_strategy,
        )

        if args.json:
            json.dump([asdict(inspection) for inspection in inspections], sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        elif args.json_report:
            json.dump(format_recent_runs_json_report(context, inspections), sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        elif args.markdown_report:
            print(format_recent_runs_markdown_report(context, inspections))
        elif args.markdown:
            print(format_recent_runs_markdown_table(inspections))
        else:
            print(format_recent_runs_table(inspections))

        if args.strict:
            failures = collect_recent_runs_strict_failures(inspections)
            if failures:
                print("Strict check failed:", file=sys.stderr)
                for failure in failures:
                    print(f"- {failure}", file=sys.stderr)
                return 2
        return 0

    payload = read_payload(args)
    summaries = summarize_payload(payload, headers=headers, probe_download=args.probe_download)
    summaries = filter_summaries(
        summaries,
        artifact_name_filter=args.artifact_name,
        artifact_kind_filter=args.artifact_kind,
        download_strategy_filter=args.download_strategy,
    )

    if args.emit_script:
        assert args.output_dir is not None
        context = build_report_context(args, payload, summaries)
        report = format_json_report(context, summaries)
        actions = build_download_actions(report, args.output_dir)
        print(render_download_script(actions, shell=args.emit_script), end="")
    elif args.json:
        json.dump([asdict(summary) for summary in summaries], sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    elif args.json_report:
        context = build_report_context(args, payload, summaries)
        json.dump(format_json_report(context, summaries), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    elif args.markdown_report:
        context = build_report_context(args, payload, summaries)
        print(format_markdown_report(context, summaries))
    elif args.markdown:
        print(format_markdown_table(summaries))
    else:
        print(format_table(summaries))

    if args.strict:
        failures = collect_strict_failures(summaries)
        if failures:
            print("Strict check failed:", file=sys.stderr)
            for failure in failures:
                print(f"- {failure}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
