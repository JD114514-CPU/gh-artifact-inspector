from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


@dataclass(slots=True)
class DownloadAction:
    name: str
    strategy: str
    source_url: str | None
    target_path: Path | None
    extract_dir: Path | None
    reason: str


def load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_download_actions(report: dict[str, Any], output_dir: Path) -> list[DownloadAction]:
    artifacts = report.get("artifacts", [])
    actions: list[DownloadAction] = []
    for artifact in artifacts:
        name = str(artifact.get("name") or "artifact")
        strategy = str(artifact.get("download_strategy") or "manual-check")
        source_url = _string_or_none(artifact.get("archive_download_url"))
        note = str(artifact.get("note") or "")
        safe_name = sanitize_artifact_name(name)

        if strategy == "download-and-unzip":
            archive_name = safe_name if safe_name.lower().endswith(".zip") else f"{safe_name}.zip"
            archive_path = output_dir / archive_name
            extract_dir = output_dir / strip_zip_suffix(safe_name)
            actions.append(
                DownloadAction(
                    name=name,
                    strategy=strategy,
                    source_url=source_url,
                    target_path=archive_path,
                    extract_dir=extract_dir,
                    reason=note,
                )
            )
            continue

        if strategy == "download-as-is":
            actions.append(
                DownloadAction(
                    name=name,
                    strategy=strategy,
                    source_url=source_url,
                    target_path=output_dir / safe_name,
                    extract_dir=None,
                    reason=note,
                )
            )
            continue

        actions.append(
            DownloadAction(
                name=name,
                strategy=strategy,
                source_url=source_url,
                target_path=None,
                extract_dir=None,
                reason=note or "Skipped because the report did not provide a safe automatic download strategy.",
            )
        )

    return actions


def execute_download_actions(actions: list[DownloadAction], github_token: str | None = None) -> list[str]:
    logs: list[str] = []
    for action in actions:
        if action.strategy not in {"download-and-unzip", "download-as-is"}:
            logs.append(f"skip {action.name}: {action.reason}")
            continue
        if not action.source_url or action.target_path is None:
            logs.append(f"skip {action.name}: missing download URL or target path")
            continue

        action.target_path.parent.mkdir(parents=True, exist_ok=True)
        download_file(action.source_url, action.target_path, github_token=github_token)

        if action.strategy == "download-and-unzip":
            assert action.extract_dir is not None
            if action.extract_dir.exists():
                shutil.rmtree(action.extract_dir)
            action.extract_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(action.target_path) as archive:
                archive.extractall(action.extract_dir)
            logs.append(f"downloaded+unzipped {action.name} -> {action.extract_dir}")
        else:
            logs.append(f"downloaded {action.name} -> {action.target_path}")
    return logs


def describe_download_actions(actions: list[DownloadAction]) -> list[str]:
    logs: list[str] = []
    for action in actions:
        if action.strategy == "download-and-unzip" and action.target_path and action.extract_dir:
            logs.append(f"plan unzip {action.name}: {action.target_path} -> {action.extract_dir}")
        elif action.strategy == "download-as-is" and action.target_path:
            logs.append(f"plan download {action.name}: {action.target_path}")
        else:
            logs.append(f"skip {action.name}: {action.reason}")
    return logs


def download_file(url: str, target_path: Path, github_token: str | None = None) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    request_headers = github_headers(github_token, url)
    request = Request(url, headers=request_headers)
    opener = build_opener(NoRedirectHandler())
    try:
        with opener.open(request, timeout=60) as response:
            with target_path.open("wb") as handle:
                shutil.copyfileobj(response, handle)
            return
    except HTTPError as exc:
        redirected = exc.headers.get("Location")
        if redirected:
            redirected_request = Request(redirected, headers=github_headers(None, redirected))
            with urlopen(redirected_request, timeout=60) as response:
                with target_path.open("wb") as handle:
                    shutil.copyfileobj(response, handle)
            return
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Download failed ({exc.code}) for {url}\n{body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Download failed for {url}: {exc.reason}") from exc


def github_headers(github_token: str | None, url: str) -> dict[str, str]:
    headers = {"User-Agent": "gh-artifact-inspector-downloader"}
    parsed = urlparse(url)
    if github_token and parsed.netloc.lower() in {"api.github.com", "github.com", "www.github.com"}:
        headers["Authorization"] = f"Bearer {github_token}"
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    return headers


def sanitize_artifact_name(name: str) -> str:
    cleaned = "".join(char if char not in '<>:"/\\|?*' else "_" for char in name).strip()
    return cleaned or "artifact"


def strip_zip_suffix(name: str) -> str:
    return name[:-4] if name.lower().endswith(".zip") else name


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
