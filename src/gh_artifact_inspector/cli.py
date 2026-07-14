from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


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
        "--markdown",
        action="store_true",
        help="Emit a Markdown table for issue comments, PRs, or reports.",
    )
    parser.add_argument(
        "--markdown-report",
        action="store_true",
        help="Emit a Markdown report with summary bullets plus the artifact table.",
    )
    return parser


def read_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.from_file:
        return json.loads(args.from_file.read_text(encoding="utf-8"))

    repo, run_id = resolve_run_target(args)

    owner, repo_name = split_repo(repo)
    url = f"https://api.github.com/repos/{owner}/{repo_name}/actions/runs/{run_id}/artifacts?per_page=100"
    headers = github_headers(args.github_token)
    return request_json(url, headers=headers)


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

    return ReportContext(
        source_label=source_label,
        total_artifacts=int(payload.get("total_count") or len(summaries)),
        expired_artifacts=sum(1 for summary in summaries if summary.expired),
        zip_artifacts=sum(1 for summary in summaries if summary.archive_kind == "zip"),
        direct_file_artifacts=sum(1 for summary in summaries if summary.archive_kind == "direct-file"),
        unknown_artifacts=sum(1 for summary in summaries if summary.archive_kind == "unknown"),
    )


def infer_archive_kind(name: str, content_type: str | None, archive_download_url: str | None) -> str:
    normalized_name = name.lower()
    normalized_type = (content_type or "").lower()
    normalized_url = (archive_download_url or "").lower()

    if "zip" in normalized_type or normalized_name.endswith(".zip"):
        return "zip"
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    selected_formats = sum(bool(flag) for flag in (args.json, args.markdown, args.markdown_report))
    if selected_formats > 1:
        raise SystemExit("--json, --markdown, and --markdown-report cannot be used together.")
    payload = read_payload(args)
    headers = github_headers(args.github_token)
    summaries = summarize_payload(payload, headers=headers, probe_download=args.probe_download)

    if args.json:
        json.dump([asdict(summary) for summary in summaries], sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    elif args.markdown_report:
        context = build_report_context(args, payload, summaries)
        print(format_markdown_report(context, summaries))
    elif args.markdown:
        print(format_markdown_table(summaries))
    else:
        print(format_table(summaries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
