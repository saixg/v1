# infra/sandbox — analysis isolation

The container boundary now exists. This file says exactly what it provides,
what it does not, and which parts of the original sandbox vision are still
unmet. Read it before claiming anything about isolation.

The container is `services/api/Dockerfile`. There is deliberately **no second
image**: the api and the worker share one codebase (`services/api/pyproject.toml`)
and, today, one process — scan jobs run in-process via FastAPI
`BackgroundTasks` (`app/analysis/jobs.py`). A separate analysis image would be
a fiction until that job runner is separated.

- Self-hosted / local: `infra/sandbox/run-hardened.sh` applies the Docker
  flags below.
- Fly.io: `fly.toml` at the workspace root. A Fly machine is a Firecracker
  microVM, so the Docker-level flags (`--read-only`, `--cap-drop`, `--tmpfs`)
  do not exist there; the microVM is the boundary instead.
- Contract tests: `infra/sandbox/tests/test_sandbox_env.py` parses the
  Dockerfile and the run script and fails if a property below is silently
  dropped. They need no Docker and run in CI.

## What the container does provide

| Property | How |
| --- | --- |
| Process + filesystem isolation from the host | Container namespaces; on Fly, a Firecracker microVM |
| Non-root execution | `USER 10001:10001`, a system account with `/usr/sbin/nologin` |
| Application code immutable to the application | `.venv` and `app/` are copied root-owned 0755; the process is uid 10001 |
| No in-image privilege escalation path | Every setuid/setgid bit stripped at build (`find / -perm /6000`), plus `--security-opt no-new-privileges` |
| No toolchain for an analyzed repo to invoke | The only package installed on top of `python:3.12-slim-bookworm` is `git` (plus `ca-certificates`). No compiler, no make/cmake, no node/npm/yarn, no curl/wget, no git-lfs. The builder stage has no compiler either — every native wheel in `uv.lock` is manylinux-prebuilt. Caveat, not papered over: the base image ships CPython's own `pip`, which this build does not remove |
| Immutable root filesystem | `--read-only` (self-hosted). Possible because the only path the service writes is scratch |
| Scratch that cannot execute | `--tmpfs /tmp:noexec,nosuid,nodev`. A checked-out repository lands here, so `noexec` is the flag that matters most |
| No capabilities | `--cap-drop ALL`, none added back; neither uvicorn nor `git fetch` needs one |
| Wall-clock / memory / fork bounds | `--cpus`, `--memory`, `--pids-limit` |
| Reproducible dependency set | `uv sync --frozen` against `uv.lock`; no loose `pip install` anywhere |

## What it does not provide — name these, do not imply otherwise

1. **Not a container per analysis.** One long-lived container serves HTTP and
   runs every scan in-process. Two repositories analyzed concurrently share a
   process, a filesystem and a memory limit. Per-job container isolation needs
   the job runner extracted behind a real queue first
   (`app/analysis/jobs.py`'s `STUB(CodeAxis)` note: no queue, no retry, no
   stale-job reaper). This is the largest remaining gap.
2. **Not network-isolated.** An earlier version of this file claimed "no
   outbound network". That was never achievable for this process and is not
   claimed now: it must reach Supabase Postgres, Clerk's JWKS endpoint, the
   Anthropic and Voyage APIs, and GitHub over https for `git fetch`.
   `--network none` would break ingestion outright. What bounds egress is
   `app/repository/ingest.py`'s SSRF validation and its `https`-only
   `GIT_ALLOW_PROTOCOL`, not the container.
3. **No seccomp profile beyond the Docker default.** No custom syscall
   allowlist has been written or measured against what tree-sitter, igraph and
   `git` actually call. Fly applies its own; neither is this project's work.
4. **No user namespace remapping.** uid 10001 inside the container is uid
   10001 on the host unless the operator has enabled `userns-remap`
   themselves.
5. **No read-only source mount.** The original design mounted repository
   source read-only. Ingestion writes the checkout itself (`git clone`, ZIP
   extraction) into scratch, so the directory the scanner reads is one the
   scanner's own process created. `noexec` on that tmpfs is the mitigation;
   read-only is not available without splitting ingest from scan.
6. **The parse-time cap is still best-effort.** `ScanLimits.max_parse_seconds`
   (`app/analysis/scan/limits.py`) bounds tree-sitter via a thread-pool
   timeout that *abandons* the worker thread rather than killing it — Python
   cannot preempt a thread blocked in a C extension. A pathological file
   therefore leaks a thread for the life of the process. The container's
   `--cpus` and `--memory` limits bound the damage; they do not reclaim the
   thread. Fixing this properly needs a subprocess per parse, or the per-job
   container from gap 1.
7. **On Fly, the Docker hardening flags do not apply.** `--read-only`,
   `--cap-drop`, `--tmpfs` and `--pids-limit` have no `fly.toml` equivalent.
   The microVM is a stronger host boundary than a namespace, and a weaker
   in-guest one: a compromised process there runs against a writable rootfs.
   Only the image-level properties (non-root, no setuid, root-owned code, no
   toolchain) survive onto Fly.

## Resource caps live in application code, not in the container

Independent of any container flag, and still the first line of defence
because they are what produces a *useful* result instead of a kill:
`app/analysis/scan/limits.py` (`ScanLimits` / `DEFAULT_LIMITS`, each field
overridable via `CODEAXIS_SCAN_MAX_*`) and `scanner.py`, where `scan_repo`
stops walking and returns `ScanStats(truncated=True, reason=...)` once a cap
is hit, `_scan_file` marks an oversized file `unanalyzed`/`file_too_large`,
and `_scan_symbols`/`_parse_with_timeout` bound parse time. Archive bombs are
capped separately in `app/repository/ingest.py` (`ZIP_LIMITS`,
`MAX_ZIP_EXPANSION_RATIO`).
