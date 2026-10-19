"""Materialize a repository onto local disk so the scan pass can read it.

Three sources: a GitHub (or any https git) URL, a local folder, or a ZIP
upload. Nothing here executes code from the repository: git is only ever
asked to fetch and check out, and ZIP entries are copied byte-for-byte into
plain files. All repository content is untrusted data (CLAUDE.md).

Every failure raises `IngestError` (a ValueError) with a message that says
what went wrong, so the API can surface it as a 400 instead of a 500.

Known gap: this runs on the host, not inside the analysis container —
container isolation is deferred (see infra/sandbox/README.md).
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import shutil
import socket
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from app.analysis.scan import ScanLimits

SCRATCH_PREFIX = "codeaxis-"

# A local folder is read in place, not copied — see `materialize_local`.
# Disabled by default: any authenticated user can otherwise reach it via
# POST /repos/stage simply by creating their own project, which would turn
# it into an arbitrary-host-path read. An operator opts in per deployment.
LOCAL_INGEST_ALLOWED_ROOTS_ENV = "LOCAL_INGEST_ALLOWED_ROOTS"

GIT_TIMEOUT_SECONDS = 120
# Remote transports git may use. `file`/`ext` are excluded: `ext::` runs an
# arbitrary command, `file` would let a caller read other folders on the host.
ALLOWED_GIT_SCHEMES = frozenset({"https"})

ZIP_LIMITS = ScanLimits(max_total_size_bytes=500 * 1024 * 1024)
MAX_ZIP_EXPANSION_RATIO = 100.0
_CHUNK = 65536


class IngestError(ValueError):
    """A source could not be materialized. The message is safe to show the user."""


@dataclass(frozen=True)
class MaterializedCheckout:
    local_path: Path
    commit_sha: str
    remote_url: str | None


def _new_scratch_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX))


def is_scratch_checkout(path: str | Path) -> bool:
    """True only for directories this module created under the system temp
    dir. The worker deletes a checkout after scanning it; this guard is what
    stops it from ever deleting a user's own local folder."""
    resolved = Path(path).resolve()
    return resolved.parent == Path(tempfile.gettempdir()).resolve() and resolved.name.startswith(
        SCRATCH_PREFIX
    )


# ---------------------------------------------------------------- local


def _allowed_local_roots() -> list[Path]:
    """Resolved roots an operator has explicitly allowlisted for local-path
    ingestion. Empty (the default) means the capability is disabled."""
    raw = os.environ.get(LOCAL_INGEST_ALLOWED_ROOTS_ENV, "")
    return [Path(p).resolve() for p in raw.split(";") if p.strip()]


def materialize_local(path: str, commit_sha: str) -> MaterializedCheckout:
    """A folder already on disk. Read in place, never copied or deleted.

    Restricted to an operator-configured allowlist of root directories
    (`LOCAL_INGEST_ALLOWED_ROOTS`); disabled entirely when that allowlist is
    empty, since any authenticated user can otherwise reach this via
    POST /repos/stage simply by creating their own project.
    """
    if not commit_sha.strip():
        raise IngestError("commit_sha is required for a local folder")
    allowed_roots = _allowed_local_roots()
    if not allowed_roots:
        raise IngestError(
            "local-path ingestion is disabled on this server "
            f"(set {LOCAL_INGEST_ALLOWED_ROOTS_ENV} to enable it)"
        )
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as exc:
        raise IngestError(f"local path does not exist: {path}") from exc
    if not resolved.is_dir():
        raise IngestError(f"local path is not a directory: {path}")
    if not any(resolved.is_relative_to(root) for root in allowed_roots):
        raise IngestError(f"local path is outside the allowed roots: {path}")
    return MaterializedCheckout(local_path=resolved, commit_sha=commit_sha, remote_url=None)


# ---------------------------------------------------------------- git


def git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # never hang waiting for credentials
    env["GIT_ALLOW_PROTOCOL"] = ":".join(sorted(ALLOWED_GIT_SCHEMES))
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    return env


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one git subcommand. Returns the completed process; a timeout is
    reported as a non-zero result rather than an exception."""
    cmd = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "submodule.recurse=false", *args]
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=git_env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd, returncode=124, stdout="", stderr=f"timed out after {GIT_TIMEOUT_SECONDS}s"
        )


def _is_blocked_ip(ip_str: str) -> bool:
    """True for loopback, link-local (includes the 169.254.169.254 cloud
    metadata address), private RFC1918/ULA, and other non-public ranges a
    git remote must never resolve to."""
    ip = ipaddress.ip_address(ip_str)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_and_block_private_hosts(hostname: str) -> None:
    """Resolve every address `hostname` has today and reject if any of them
    land in a private/loopback/link-local/metadata/reserved range.

    Runs once, synchronously, before the git subprocess ever starts. This
    does not defend against DNS rebinding: git's own HTTPS transport
    re-resolves the same hostname independently when it actually connects,
    with no guarantee it gets the same address this check saw. Closing that
    fully would mean pinning the resolved IP for the real connection (a
    filtering proxy, or `curl --resolve` in place of git's own transport) —
    a materially larger change accepted as a known residual risk here,
    since it requires an attacker to control DNS for the target domain and
    win a narrow race on every single fetch.
    """
    try:
        addrinfo = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise IngestError(f"could not resolve repository host {hostname!r}: {exc}") from exc
    resolved_ips = {str(info[4][0]) for info in addrinfo}
    blocked = sorted(ip for ip in resolved_ips if _is_blocked_ip(ip))
    if blocked:
        raise IngestError(
            f"repository host {hostname!r} resolves to a disallowed "
            f"address ({', '.join(blocked)}); only public hosts are allowed"
        )


def _validate_remote(remote_url: str, ref: str | None) -> None:
    parsed = urlparse(remote_url)
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_GIT_SCHEMES:
        allowed = ", ".join(sorted(ALLOWED_GIT_SCHEMES))
        raise IngestError(f"unsupported repository URL scheme {scheme!r}; allowed: {allowed}")
    # A leading '-' would be parsed by git as an option, not a URL/ref.
    if remote_url.startswith("-"):
        raise IngestError("repository URL must not start with '-'")
    if ref is not None and (not ref.strip() or ref.startswith("-")):
        raise IngestError(f"invalid ref: {ref!r}")
    # The SSRF check only applies to a network destination. `https` is the
    # only scheme ALLOWED_GIT_SCHEMES ever carries in production; `file` is
    # also permitted in some tests (see test_ingest.py's `origin` fixture)
    # to exercise git-fetch logic against a local repo without a network —
    # a file:// URL has no host and nothing for this check to resolve.
    if scheme == "https":
        if not parsed.hostname:
            raise IngestError(f"repository URL has no host: {remote_url!r}")
        _resolve_and_block_private_hosts(parsed.hostname)


def materialize_github(remote_url: str, ref: str | None) -> MaterializedCheckout:
    """Shallow-fetch `ref` (default: the remote's HEAD). If the host refuses a
    shallow fetch of that ref — e.g. an unadvertised commit SHA — fall back to
    a full clone and check `ref` out. The pinned commit_sha always comes from
    `git rev-parse HEAD` on the result, never from the caller."""
    _validate_remote(remote_url, ref)

    scratch = _new_scratch_dir()
    try:
        fetch_err = _try_shallow_fetch(scratch, remote_url, ref)
        if fetch_err is not None:
            shutil.rmtree(scratch, ignore_errors=True)
            scratch = _new_scratch_dir()
            clone_err = _try_full_clone(scratch, remote_url, ref)
            if clone_err is not None:
                raise IngestError(
                    f"could not fetch {remote_url} at {ref or 'HEAD'}.\n"
                    f"shallow fetch: {fetch_err}\nfull clone: {clone_err}"
                )

        head = run_git(["rev-parse", "HEAD"], scratch)
        if head.returncode != 0:
            raise IngestError(f"could not resolve HEAD: {head.stderr.strip()}")
        return MaterializedCheckout(
            local_path=scratch.resolve(), commit_sha=head.stdout.strip(), remote_url=remote_url
        )
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise


def _try_shallow_fetch(scratch: Path, remote_url: str, ref: str | None) -> str | None:
    """Returns None on success, else the failing step's stderr."""
    steps = [
        ["init", "--quiet"],
        ["remote", "add", "origin", remote_url],
        ["fetch", "--depth", "1", "--no-tags", "origin", ref or "HEAD"],
        ["checkout", "--quiet", "--detach", "FETCH_HEAD"],
    ]
    for args in steps:
        result = run_git(args, scratch)
        if result.returncode != 0:
            return f"git {args[0]}: {result.stderr.strip()}"
    return None


def _try_full_clone(scratch: Path, remote_url: str, ref: str | None) -> str | None:
    result = run_git(
        ["clone", "--quiet", "--no-recurse-submodules", "--", remote_url, "."], scratch
    )
    if result.returncode != 0:
        return f"git clone: {result.stderr.strip()}"
    if ref is not None:
        result = run_git(["checkout", "--quiet", "--detach", ref, "--"], scratch)
        if result.returncode != 0:
            return f"git checkout {ref}: {result.stderr.strip()}"
    return None


# ---------------------------------------------------------------- zip


def materialize_zip(upload_path: Path) -> MaterializedCheckout:
    """Extract an uploaded archive into a fresh scratch dir.

    Bomb protection happens twice: the declared (central-directory) sizes are
    checked against the total-size cap and the expansion ratio before any
    byte is written, and the actual decompressed bytes are counted during
    extraction so an archive whose headers lie is still stopped mid-stream.
    """
    compressed_size = upload_path.stat().st_size
    commit_sha = "zip:" + _sha256_file(upload_path)[:12]

    try:
        zf = zipfile.ZipFile(upload_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise IngestError(f"not a valid ZIP archive: {exc}") from exc

    with zf:
        _check_declared_sizes(zf, compressed_size)
        dest = _new_scratch_dir()
        try:
            _extract_stream(zf, dest, compressed_size)
        except (zipfile.BadZipFile, EOFError) as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise IngestError(f"corrupt ZIP archive: {exc}") from exc
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise

    return MaterializedCheckout(local_path=dest.resolve(), commit_sha=commit_sha, remote_url=None)


def _sha256_file(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def _check_declared_sizes(zf: zipfile.ZipFile, compressed_size: int) -> None:
    infos = zf.infolist()
    if len(infos) > ZIP_LIMITS.max_file_count:
        raise IngestError(
            f"archive has {len(infos)} entries; the limit is {ZIP_LIMITS.max_file_count}"
        )
    declared = sum(info.file_size for info in infos)
    if declared > ZIP_LIMITS.max_total_size_bytes:
        raise IngestError(
            f"archive expands to {declared} bytes; the limit is {ZIP_LIMITS.max_total_size_bytes}"
        )
    ratio = declared / max(compressed_size, 1)
    if ratio > MAX_ZIP_EXPANSION_RATIO:
        raise IngestError(
            f"archive expansion ratio {ratio:.0f}x exceeds the limit of "
            f"{MAX_ZIP_EXPANSION_RATIO:.0f}x (possible ZIP bomb)"
        )


def _safe_destination(dest_root: Path, name: str) -> Path:
    """Resolve an entry name inside dest_root, rejecting zip-slip paths
    (`../`, absolute paths, drive letters, backslash tricks)."""
    normalized = name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts or (pure.parts and ":" in pure.parts[0]):
        raise IngestError(f"archive entry escapes the extraction directory: {name!r}")
    target = (dest_root / Path(*pure.parts)).resolve() if pure.parts else dest_root
    if not target.is_relative_to(dest_root):
        raise IngestError(f"archive entry escapes the extraction directory: {name!r}")
    return target


def _extract_stream(
    zf: zipfile.ZipFile,
    dest_root: Path,
    compressed_size: int,
    max_total_bytes: int | None = None,
) -> int:
    """Copy every entry into dest_root as a plain file, counting real
    decompressed bytes. Raises as soon as the running total passes the cap
    or the expansion ratio. Returns the number of bytes written."""
    cap = ZIP_LIMITS.max_total_size_bytes if max_total_bytes is None else max_total_bytes
    ratio_cap = MAX_ZIP_EXPANSION_RATIO * max(compressed_size, 1)
    dest_root = dest_root.resolve()
    written = 0

    for info in zf.infolist():
        target = _safe_destination(dest_root, info.filename)
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # Entries are always written as regular files; symlink entries
        # (unix mode bits) are never materialized as links.
        with zf.open(info) as src, target.open("wb") as out:
            while chunk := src.read(_CHUNK):
                written += len(chunk)
                if written > cap:
                    raise IngestError(f"archive exceeded {cap} bytes while extracting; aborted")
                if written > ratio_cap:
                    raise IngestError(
                        f"archive exceeded the {MAX_ZIP_EXPANSION_RATIO:.0f}x expansion "
                        "ratio while extracting; aborted (possible ZIP bomb)"
                    )
                out.write(chunk)
    return written
