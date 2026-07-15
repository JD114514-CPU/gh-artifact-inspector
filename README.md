# gh-artifact-inspector

[![CI](https://github.com/JD114514-CPU/gh-artifact-inspector/actions/workflows/ci.yml/badge.svg)](https://github.com/JD114514-CPU/gh-artifact-inspector/actions/workflows/ci.yml)

一个面向 GitHub Actions artifact 的小型诊断 CLI，目标是尽快回答一个常见问题：

“这个 artifact 到底该直接消费，还是应该先当 zip 解压？”

## 目标用户

- 维护 GitHub Actions workflow 的开发者
- 需要排查 artifact 下载兼容性问题的工程师
- 想把 run / artifact 元数据结构化输出给脚本或 agent 的用户

## 当前已实现的 MVP

- 读取 GitHub Actions `run_id` 的 artifact 列表
- 支持直接读取离线 JSON payload，方便复盘或写测试
- 输出 `name / size / expired / archive_kind / content_type / download_strategy / note`
- 支持终端表格输出、`--json`、`--json-report`、`--markdown` 和 `--markdown-report`
- 支持直接从主 CLI 导出 `powershell` / `bash` 下载脚本，不必先手动中转 JSON
- 支持 `--strict`，可在 CI / agent 流程里把“人工确认”升级成非零退出码
- 支持 `--recent-runs N`，批量扫描最近 N 次 workflow run 的 artifact 风险概况
- 支持 `--recent-runs N --workflow nightly`，只看某一类 workflow 的最近 runs，避免多流水线仓库噪音
- 支持 `--recent-runs N --event pull_request`，只看某一类触发事件的 runs，便于区分 `push` / `pull_request` / `schedule`
- 支持 `--recent-runs N --strict-only`，只保留真正有 artifact 风险的 runs，适合日报和 issue 跟进
- `--recent-runs` 的 JSON / Markdown 报告会额外带上 run `event`，并按 workflow 名称聚合，方便看哪条流水线、哪类触发方式最常出问题
- 能识别 `.tar.gz` / `.tgz` 一类“本身就是单文件归档”的 artifact，避免误导成自动 unzip
- 对疑似 `direct-file` artifact 明确提示“不要自动 unzip”

## 为什么值得做

- 方向贴近 GitHub workflow / 开发者工具链，适合简历上的“工程效率工具”
- 范围小，能快速演示“从真实痛点到可运行 CLI”的落地能力
- 今天在 `cli/cli#13012` 对应的问题背景下，这个需求是明确存在的

## 安装和运行

```bash
git clone https://github.com/JD114514-CPU/gh-artifact-inspector.git
cd gh-artifact-inspector
uv sync --group dev
uv run gh-artifact-inspector --from-file tests/fixtures/artifacts.json
```

如果只想快速试跑，不想先同步开发依赖：

```bash
cd gh-artifact-inspector
set PYTHONPATH=src
python -m gh_artifact_inspector.cli --from-file tests/fixtures/artifacts.json
```

如果要直接读取 GitHub API：

```bash
gh-artifact-inspector --repo owner/name --run-id 123456789
```

也可以直接贴 workflow run URL：

```bash
gh-artifact-inspector --run-url https://github.com/owner/name/actions/runs/123456789
```

如果同时传 `--run-url` 和 `--repo` / `--run-id`，工具会校验两者是否一致，避免静默读错 run。
如果使用 `--from-file`，就不要再混传 `--repo` / `--run-id` / `--run-url`；CLI 现在会直接报错，避免你以为自己在读线上 run，实际却在消费本地 payload。

私有仓库或更高 rate limit 建议设置：

```bash
set GITHUB_TOKEN=your_token_here
gh-artifact-inspector --repo owner/name --run-id 123456789 --probe-download --json
```

如果要把结果直接贴进 issue、PR 或日报：

```bash
gh-artifact-inspector --from-file tests/fixtures/artifacts.json --markdown
```

如果 agent、脚本或 CI 需要同时拿到汇总结论和明细列表：

```bash
gh-artifact-inspector --from-file tests/fixtures/artifacts.json --json-report
```

如果想直接生成一段更完整的 Markdown 报告，包含来源和汇总要点：

```bash
gh-artifact-inspector --from-file tests/fixtures/artifacts.json --markdown-report
```

如果你已经确认诊断逻辑没问题，想直接把下载计划导出成可编辑脚本：

```bash
gh-artifact-inspector --from-file tests/fixtures/artifacts.json --emit-script powershell --output-dir downloaded-artifacts
gh-artifact-inspector --from-file tests/fixtures/artifacts.json --emit-script bash --output-dir downloaded-artifacts
```

这样可以少一次 `--json-report > report.json` 的中转，更适合临时排障或 agent 直接拼装后续步骤。

如果要把它接进 CI 或 agent 流程，遇到过期 artifact 或无法自动判断包装形式时直接失败：

```bash
gh-artifact-inspector --repo owner/name --run-id 123456789 --probe-download --strict
```

`--strict` 会保持正常输出，但在以下场景返回退出码 `2`：

- artifact 已过期
- artifact 仍需要人工确认包装形式

如果要先做仓库级巡检，看最近几次 workflow run 里有没有 artifact 过期、直出文件或包装不明：

```bash
gh-artifact-inspector --repo owner/name --recent-runs 5 --json-report
```

它会逐个 run 拉取 artifact 列表，并输出每个 run 的 artifact 数量、zip/direct-file/unknown 分布、strict 失败项，以及按 workflow 名称聚合后的风险汇总。

如果仓库 workflow 很多，但你只想盯住某一类流水线：

```bash
gh-artifact-inspector --repo owner/name --recent-runs 10 --workflow nightly --markdown-report
```

这里的 `--workflow` 会按 workflow 标题做大小写不敏感的包含匹配，适合只看 `Nightly`、`Release`、`Artifacts` 这类固定名称。

如果同一个仓库同时有 `push`、`pull_request`、`schedule` 等多种触发方式，但你只想看其中一类：

```bash
gh-artifact-inspector --repo owner/name --recent-runs 20 --event pull_request --json-report
```

这里的 `--event` 会按 workflow run 的 GitHub event 名称做大小写不敏感的包含匹配，适合单独排查 PR 校验、定时巡检或手动触发的 artifact 行为。

如果你只想盯住“真的需要处理”的 runs，而不是把正常 runs 也混进日报：

```bash
gh-artifact-inspector --repo owner/name --recent-runs 20 --strict-only --markdown-report
```

这会保留带有 strict failure 的 runs，并在报告里同时写出“总共扫描了多少 run”和“最终保留了多少 run”。

## 输出示例

```text
name                  | size | expired | archive_kind | content_type     | download_strategy | note
----------------------+------|---------|--------------|------------------|-------------------|---------------------------------------------------------------
bundle.zip            | 1024 | no      | zip          | application/zip  | download-and-unzip| Treat the artifact as a zip archive before reading files.
coverage-summary.json | 256  | no      | direct-file  | application/json | download-as-is    | Do not unzip automatically; consume the downloaded file directly.
stale-artifact        | 512  | yes     | unknown      | -                | unavailable       | Artifact is expired. Re-run the workflow or extend retention.
```

README 可直接渲染的终端截图素材：

![gh-artifact-inspector demo](examples/demo-output.svg)

真实跑出来的表格输出已保存到 [examples/demo-output.txt](examples/demo-output.txt)，可直接作为后续 README 截图或发布素材。
Markdown 版本示例输出已保存到 [examples/demo-output.md](examples/demo-output.md)，方便直接复用到 GitHub 文本场景。
Markdown 报告版本示例已保存到 [examples/demo-report.md](examples/demo-report.md)，方便直接贴进 issue、PR 或日报。
JSON 报告版本示例已保存到 [examples/demo-report.json](examples/demo-report.json)，方便直接给 agent、脚本或 CI 消费。
真实联网 `--probe-download` 示例已保存到 [examples/live-probe-report.md](examples/live-probe-report.md)，用于展示 direct-file artifact 的真实诊断。

## 本地验证

```bash
uv run python -m pytest
```

## 仓库素材

- 示例输入：`tests/fixtures/artifacts.json`
- 真实 demo 输出：`examples/demo-output.txt`
- README 截图素材：`examples/demo-output.svg`
- JSON 报告示例：`examples/demo-report.json`
- 真实联网 probe 示例：`examples/live-probe-report.md`
- 许可证：`LICENSE`
- 建议仓库 topics：`github-actions`、`artifacts`、`cli`、`devtools`、`workflow-debugging`
- 当前公开 release：`v0.1.0`

## 当前公开状态

- 仓库：`https://github.com/JD114514-CPU/gh-artifact-inspector`
- release：`https://github.com/JD114514-CPU/gh-artifact-inspector/releases/tag/v0.1.0`
- CI：`https://github.com/JD114514-CPU/gh-artifact-inspector/actions/workflows/ci.yml`
- 真实 probe run：`https://github.com/JD114514-CPU/gh-artifact-inspector/actions/runs/29322701009`
- README 已包含可直接渲染的 CLI demo 截图素材

## 发布后建议

- 如果后续要打到 PyPI，再补 `project.urls` 里的文档或 changelog 链接

## 下一步

- 为导出的下载脚本补更多平台级 smoke 示例，比如 `tar.gz` 或多文件 artifact 情况

## 兼容下载器示例

如果你已经用 `--json-report` 拿到了结构化报告，可以直接用仓库里的示例脚本按推荐策略下载：

```bash
gh-artifact-inspector --repo owner/name --run-id 123456789 --json-report > report.json
set GITHUB_TOKEN=your_token_here
python examples/compatible_downloader.py --report report.json --output-dir downloaded-artifacts
```

注意：GitHub Actions artifact 下载 URL 通常需要认证，即使仓库本身是公开的；因此真实下载场景建议显式提供 `GITHUB_TOKEN`。

这个脚本会按 `download_strategy` 自动区分三类动作：

- `download-and-unzip`：下载 zip，并解压到同名目录
- `download-as-is`：直接按原文件名保存
- `manual-check` / `unavailable`：跳过，并打印原因

如果只想先确认会做哪些动作，不想立刻访问 GitHub API：

```bash
python examples/compatible_downloader.py --report examples/demo-report.json --output-dir downloaded-artifacts --dry-run
```

这样可以把诊断结果继续接到后续脚本、agent 或一次性的排障流程里，而不用手工判断每个 artifact 是否该 unzip。

如果你已经确认报告没问题，只是想在单独脚本里真正执行下载计划：

```bash
python examples/compatible_downloader.py --report examples/demo-report.json --output-dir downloaded-artifacts --emit-script powershell
python examples/compatible_downloader.py --report examples/demo-report.json --output-dir downloaded-artifacts --emit-script bash
```

导出的脚本会保留 `download-and-unzip` / `download-as-is` / `skip` 三类决策，并约定从环境变量 `GITHUB_TOKEN` 读取认证信息。
