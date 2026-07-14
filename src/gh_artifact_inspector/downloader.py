from __future__ import annotations

import json
import shlex
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


def render_download_script(actions: list[DownloadAction], shell: str) -> str:
    normalized_shell = shell.lower()
    if normalized_shell == "powershell":
        return _render_powershell_script(actions)
    if normalized_shell == "bash":
        return _render_bash_script(actions)
    raise ValueError(f"Unsupported shell '{shell}'. Expected 'powershell' or 'bash'.")


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


def _render_powershell_script(actions: list[DownloadAction]) -> str:
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "",
        "$headers = @{}",
        "if ($env:GITHUB_TOKEN) {",
        "  $headers['Authorization'] = \"Bearer $env:GITHUB_TOKEN\"",
        "  $headers['Accept'] = 'application/vnd.github+json'",
        "  $headers['X-GitHub-Api-Version'] = '2022-11-28'",
        "}",
    ]
    for action in actions:
        lines.extend(["", *list(_render_powershell_action(action))])
    return "\n".join(lines) + "\n"


def _render_powershell_action(action: DownloadAction) -> list[str]:
    if action.strategy not in {"download-and-unzip", "download-as-is"}:
        return [f"# skip {action.name}: {action.reason}"]
    if not action.source_url or action.target_path is None:
        return [f"# skip {action.name}: missing download URL or target path"]

    target_parent = _ps_quote(str(action.target_path.parent))
    source_url = _ps_quote(action.source_url)
    target_path = _ps_quote(str(action.target_path))
    lines = [
        f"# {action.name}",
        f"New-Item -ItemType Directory -Force -Path {target_parent} | Out-Null",
        f"Invoke-WebRequest -Headers $headers -Uri {source_url} -OutFile {target_path}",
    ]
    if action.strategy == "download-and-unzip":
        assert action.extract_dir is not None
        extract_parent = _ps_quote(str(action.extract_dir.parent))
        extract_dir = _ps_quote(str(action.extract_dir))
        lines.extend(
            [
                f"New-Item -ItemType Directory -Force -Path {extract_parent} | Out-Null",
                f"if (Test-Path -LiteralPath {extract_dir}) {{ Remove-Item -Recurse -Force -LiteralPath {extract_dir} }}",
                f"Expand-Archive -LiteralPath {target_path} -DestinationPath {extract_dir} -Force",
            ]
        )
    return lines


def _render_bash_script(actions: list[DownloadAction]) -> str:
    lines = [
        "set -euo pipefail",
        "",
        'auth_args=()',
        'if [[ -n "${GITHUB_TOKEN:-}" ]]; then',
        '  auth_args=(-H "Authorization: Bearer ${GITHUB_TOKEN}" -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28")',
        "fi",
    ]
    for action in actions:
        lines.extend(["", *list(_render_bash_action(action))])
    return "\n".join(lines) + "\n"


def _render_bash_action(action: DownloadAction) -> list[str]:
    if action.strategy not in {"download-and-unzip", "download-as-is"}:
        return [f"# skip {action.name}: {action.reason}"]
    if not action.source_url or action.target_path is None:
        return [f"# skip {action.name}: missing download URL or target path"]

    target_parent = _sh_quote(str(action.target_path.parent))
    source_url = _sh_quote(action.source_url)
    target_path = _sh_quote(str(action.target_path))
    lines = [
        f"# {action.name}",
        f"mkdir -p {target_parent}",
        f"curl -L \"${{auth_args[@]}}\" {source_url} -o {target_path}",
    ]
    if action.strategy == "download-and-unzip":
        assert action.extract_dir is not None
        extract_parent = _sh_quote(str(action.extract_dir.parent))
        extract_dir = _sh_quote(str(action.extract_dir))
        lines.extend(
            [
                f"mkdir -p {extract_parent}",
                f"rm -rf {extract_dir}",
                f"unzip -o {target_path} -d {extract_dir}",
            ]
        )
    return lines


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sh_quote(value: str) -> str:
    return shlex.quote(value)
