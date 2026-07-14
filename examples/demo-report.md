# Artifact inspection report

- Source: saved payload `tests\fixtures\artifacts.json`
- Artifact count: 3
- Expired artifacts: 1
- Packaging split: zip=1, direct-file=1, unknown=1

| name | size | expired | archive_kind | content_type | download_strategy | note |
| --- | --- | --- | --- | --- | --- | --- |
| bundle.zip | 1024 | no | zip | application/zip | download-and-unzip | Treat the artifact as a zip archive before reading files. |
| coverage-summary.json | 256 | no | direct-file | application/json | download-as-is | Do not unzip automatically; consume the downloaded file directly. |
| stale-artifact | 512 | yes | unknown | - | unavailable | Artifact is expired. Re-run the workflow or extend retention. |
