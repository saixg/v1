# infra/sandbox

Analysis container: no outbound network, read-only source mount, non-root, no exec path,
no credentials. Requires Docker. Built in Step 1 (`docs/12-build-order.md`, `docs/08-security.md`).

Docker isn't installed yet, so the container boundary itself is deferred. In the meantime,
the resource-bomb caps that boundary would eventually enforce (max file count, max per-file
size, max cumulative repo size, max per-file parse time) live as logic in
`packages/analyzers/scan/src/codeaxis_scan/limits.py` (the `ScanLimits`/`DEFAULT_LIMITS`
caps, overridable via `CODEAXIS_SCAN_MAX_*` env vars) and `scanner.py` (`scan_repo` stops
walking and returns `ScanStats(truncated=True, reason=...)` once a cap is hit; `_scan_file`
marks oversized files `unanalyzed`/`file_too_large`; `_scan_symbols`/`_parse_with_timeout`
bound tree-sitter parse time with a thread-pool timeout). The parse-time cap is explicitly
best-effort — the worker thread is abandoned, not killed — since true preemptive wall-clock
enforcement needs this container's process boundary.
