# Compose Image Check

Read-only CLI to identify Docker Compose services that may need **PULL**, **BUILD?**, or **RECREATE**, locally or across SSH hosts. Works with Compose v2 and includes a best-effort legacy Compose v1 mode.

> **Version 1.8.0.** This tool reports differences; it does **not** update containers. `BUILD?` is a heuristic based on OCI layer ancestry, **not** a guaranteed rebuild requirement. Review results before acting.

## Requirements

- Python 3 on the machine running the tool and on every remote SSH host.
- Docker CLI and access to Docker Engine on each monitored host.
- Docker Compose v2 (`docker compose`) **or** legacy `docker-compose` v1. For old Compose v1 instances where JSON config output is unavailable, **PyYAML** is required on that host.
- Optional Docker Buildx. Without Buildx, the tool tries to read public OCI manifests and configuration data directly from the registry. This fallback may not support all private registries and authentication schemes.
- SSH client, remote SSH login and an accessible `python3` for remote mode. Authentication is handled by OpenSSH (`~/.ssh/config`, SSH agent, keys or passwords as usual).

No Python packages are required for Compose v2 with normal Docker/Buildx support.

## Installation

Download `compose-image-check.py` and run it directly, or clone the repository:

```sh
python3 compose-image-check.py
```

The remote executable is **not** installed on remote hosts. The local script is sent over SSH stdin and executed remotely using Python 3. Local cache on remote hosts is stored in their respective home directories.

## Examples

Check one host:

```sh
python3 compose-image-check.py --ssh root@uongo
```

Check several hosts in sequence:

```sh
python3 compose-image-check.py \
  --ssh root@ccmg4 --ssh root@uongo --ssh root@sws4 --ssh root@xmaselli
```

When more than one SSH host is specified, each report has a host heading. If a host fails, the tool prints an error and **continues** with the remaining hosts; exit status is nonzero if at least one failed. Without `--ssh`, the scan runs locally.

### Output modes

By default, output contains **only the results table**, omitting rows classified as `OK` or `PINNED`. It does not include progress, summary, notes, or registry counters.

```text
STATO        PROGETTO/SERVIZIO   IMMAGINE / BASE   SPIEGAZIONE
...
PULL         immich/server       example:tag      layer remoti diversi...
```

- `--verbose` / `-v`: append summary, explanatory notes and registry counters.
- `--debug`: show diagnostics and the verbose report (including SSH diagnostics); inspect before sharing because diagnostic output may expose private information.
- `--progress`: show per-service progress on stderr; including when using SSH.
- `--no-progress`: explicitly disable progress (even when `--debug` is specified).
- `--show-ok`: also list `OK` and `PINNED` services.
- `--details`: print suggested Compose commands **without executing them**.
- `--all`: include stopped Compose projects where discoverable.
- `--no-cache`: request fresh registry metadata (may increase rate limits).
- `--hub-interval SEC`: minimum delay between Docker Hub operations **started by the script** (default 8 s).
- `--other-interval SEC`: minimum delay for other registries (default 1 s).

Example:

```sh
python3 compose-image-check.py --ssh root@uongo --ssh root@ccmg4 --verbose --details
```

## Status meanings

| Status | Meaning |
| --- | --- |
| `PULL` | Remote image's rootfs layers differ from the locally tagged image; download/review and recreate as appropriate. |
| `BUILD?` | Remote `FROM` layer prefix does not match the local custom build; inspect before rebuilding. **Not definitive.** |
| `RECREATE` | Locally tagged/built image ID differs from the image ID used by the existing container. |
| `OK` | The comparison performed found no difference. This is **not** vulnerability scanning. |
| `PINNED` | Reference is fixed by digest and the tool does not track tag movement. |
| `VERIFICARE` | A reliable determination could not be made; check the explanation. |

The tool compares OCI `rootfs.diff_ids` rather than complete image identities, so metadata-only changes can be missed. For build services, multistage builds, `ARG`-derived bases and build tooling can limit certainty. The tool may use cached metadata (default six hours), which is marked in the explanation; a result with cached data is **not** a live registry verification.

## Networking, data, and security

- The check reads Docker/Compose metadata and Dockerfile `FROM` references, and makes outbound HTTPS requests to registries. It does not pull images, build images or recreate containers.
- Docker CLI access can be effectively root-equivalent, even if the tool itself uses only read operations. Restrict who can run it and never expose Docker's API to the public internet.
- Registry cache is kept at `~/.cache/compose-image-check/registry-v2.json` on each scanned host; legacy `registry-v1.json` may be used to seed it. Cache stores image layer hashes and timestamps.
- A 429 from Docker Hub may prevent some checks; the script does not silently turn errors into `OK`.
- For legacy Compose, project discovery is based on container labels and may miss stacks with no containers or without the expected labels.
- `--debug` logs commands and error output with best-effort redaction only; **review it manually** before publishing.
- This project contains **no real host configuration, credentials, Compose files, or volume data**.

## Tests

Run offline unit tests (they mock SSH; no Docker daemon required):

```sh
python3 -m unittest discover -s tests -v
```

## Limitations and development

This is a small best-effort utility, not a replacement for full dependency management, release-note review, image provenance or vulnerability scanning. Please report reproducible issues with sanitized output from `--debug`. Planned improvements could include recorded build provenance and structured JSON output for central monitoring.

## License

No open-source license has been selected yet. Add a `LICENSE` file before publishing if you intend to allow reuse and modification by others.
