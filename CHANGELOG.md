# Changelog

## 1.8.0

- Default output now shows only the action table, with no progress, summary, notes or registry counters.
- Added `--verbose` / `-v` for the detailed report; `--debug` also enables it.
- Changed `--ssh` to a repeatable option; scans hosts sequentially and labels each report.
- SSH errors on one host no longer prevent checks on subsequent hosts; the process exits nonzero if any host failed.
- Kept the Compose v1 and Buildx-free OCI fallback behavior of v1.7.5.

## 1.7.5

- Added direct OCI registry fallback for hosts without Docker Buildx.
- Kept legacy Docker Compose v1 discovery and SSH streaming.
