"""Job runner for Pass 1 (scan). Owns the pass_run lifecycle and is the only
code path that writes analyzer output to the Repo Model — see the one-way
rule in docs/03-architecture.md.

Ingestion is two-phase so an exclusion preview can sit between them:
`stage_repo`/`stage_zip_repo` materialize a checkout and register the repo,
`preview_repo` lists what a scan would include, and `run_scan_pass` scans
with the user's chosen exclusions and then deletes the scratch checkout.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from codeaxis_contracts import (
    Blob,
    Coverage,
    Edge,
    GithubSource,
    IngestSource,
    PassRun,
    PassRunStatus,
    Repo,
)
from codeaxis_graph import (
    create_repo,
    finish_pass_run,
    insert_edges,
    insert_nodes,
    start_pass_run,
    upsert_blobs,
    upsert_coverage,
    worker_connection,
)
from codeaxis_scan import ANALYZER, ANALYZER_VERSION, PreviewEntry, list_included_files, scan_repo
from codeaxis_worker.ingest import (
    MaterializedCheckout,
    is_scratch_checkout,
    materialize_github,
    materialize_local,
    materialize_zip,
)


def _register(checkout: MaterializedCheckout) -> Repo:
    try:
        with worker_connection() as conn:
            return create_repo(
                conn, Repo(remote_url=checkout.remote_url, local_path=str(checkout.local_path))
            )
    except BaseException:
        _discard_checkout(checkout.local_path)
        raise


def stage_repo(source: IngestSource) -> tuple[Repo, MaterializedCheckout]:
    if isinstance(source, GithubSource):
        checkout = materialize_github(source.remote_url, source.ref)
    else:
        checkout = materialize_local(source.local_path, source.commit_sha)
    return _register(checkout), checkout


def stage_zip_repo(zip_path: Path) -> tuple[Repo, MaterializedCheckout]:
    checkout = materialize_zip(zip_path)
    return _register(checkout), checkout


def preview_repo(
    local_path: str, extra_excludes: frozenset[str] | None = None
) -> list[PreviewEntry]:
    return list_included_files(Path(local_path), extra_excludes or frozenset())


def _discard_checkout(local_path: str | Path) -> None:
    """Delete a scratch checkout this worker created. A user's own local
    folder is never deleted — `is_scratch_checkout` only matches dirs that
    ingest.py made under the system temp dir."""
    if is_scratch_checkout(local_path):
        shutil.rmtree(local_path, ignore_errors=True)


def run_scan_pass(
    repo_id: str,
    commit_sha: str,
    local_path: str,
    extra_excludes: frozenset[str] | None = None,
) -> None:
    try:
        _run_scan_pass(repo_id, commit_sha, local_path, extra_excludes or frozenset())
    finally:
        # Runs on success and on failure; an exception from the scan still
        # propagates after cleanup because nothing here swallows it.
        _discard_checkout(local_path)


def _run_scan_pass(
    repo_id: str, commit_sha: str, local_path: str, extra_excludes: frozenset[str]
) -> None:
    with worker_connection() as conn:
        run = start_pass_run(
            conn,
            PassRun(
                repo_id=repo_id,
                commit_sha=commit_sha,
                analyzer=ANALYZER,
                analyzer_version=ANALYZER_VERSION,
                status=PassRunStatus.RUNNING,
            ),
        )
        assert run.id is not None

        try:
            root = Path(local_path)
            if not root.is_dir():
                # Scanning a missing folder would "succeed" with an empty
                # graph — a silent guess. Fail the pass loudly instead.
                raise FileNotFoundError(f"checkout not found on disk: {local_path}")

            scanned, scan_stats = scan_repo(
                root, repo_id, commit_sha, extra_excludes=extra_excludes
            )
            ids = insert_nodes(conn, [sn.node for sn in scanned])
            id_by_key = {sn.key: node_id for sn, node_id in zip(scanned, ids, strict=True)}

            edges = [
                Edge(
                    repo_id=repo_id,
                    commit_sha=commit_sha,
                    kind="contains",
                    src_id=id_by_key[sn.parent_key],
                    dst_id=id_by_key[sn.key],
                    analyzer=ANALYZER,
                    analyzer_version=ANALYZER_VERSION,
                    status="verified",
                )
                for sn in scanned
                if sn.parent_key is not None
            ]
            if edges:
                insert_edges(conn, edges)

            # Pinned source for "show the code": reuse the bytes the scan
            # already read rather than touching the checkout again.
            blobs = [
                Blob(
                    repo_id=repo_id,
                    commit_sha=commit_sha,
                    path=sn.node.path,
                    content=sn.content.decode("utf-8", "replace"),
                    content_hash=str(sn.node.attrs.get("content_hash", "")),
                )
                for sn in scanned
                if sn.node.kind == "file" and sn.content is not None and sn.node.path is not None
            ]
            if blobs:
                upsert_blobs(conn, blobs)

            files = [sn.node for sn in scanned if sn.node.kind == "file"]
            blockers: dict[str, int] = {}
            if scan_stats.truncated and scan_stats.reason:
                # The walk stopped early: files past this point were never
                # seen, so `total` undercounts. Record which cap cut it short
                # and how many files had been counted when it did.
                blockers[scan_stats.reason] = len(files)
            upsert_coverage(
                conn,
                [
                    Coverage(
                        repo_id=repo_id,
                        commit_sha=commit_sha,
                        area="/",
                        metric="files_parsed",
                        resolved=sum(1 for f in files if f.status == "verified"),
                        total=len(files),
                        blockers=blockers,
                    )
                ],
            )
        except Exception as exc:
            conn.rollback()
            finish_pass_run(conn, run.id, PassRunStatus.FAILED, error=str(exc))
            raise

        finish_pass_run(conn, run.id, PassRunStatus.SUCCEEDED)
