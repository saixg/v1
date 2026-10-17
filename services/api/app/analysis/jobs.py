"""Job runner for Pass 1 (scan). Owns the pass_run lifecycle and is the only
code path that writes analyzer output to the Repo Model — see the one-way
rule in docs/03-architecture.md.

Ingestion is two-phase so an exclusion preview can sit between them:
`stage_repo`/`stage_zip_repo` materialize a checkout and register the repo,
`preview_repo` lists what a scan would include, and `run_scan_pass` scans
with the user's chosen exclusions and then deletes the scratch checkout.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from app.knowledge import (
    create_repo,
    finish_pass_run,
    insert_edges,
    insert_nodes,
    set_job_status,
    start_pass_run,
    upsert_blobs,
    upsert_coverage,
    worker_connection,
)
from app.models import (
    Blob,
    Coverage,
    Edge,
    GithubSource,
    IngestSource,
    JobStatus,
    PassRun,
    PassRunStatus,
    Repo,
)
from app.repository.ingest import (
    MaterializedCheckout,
    is_scratch_checkout,
    materialize_github,
    materialize_local,
    materialize_zip,
)

from .architecture import write_architecture_pass
from .scan import (
    ANALYZER,
    ANALYZER_VERSION,
    PreviewEntry,
    annotate_entry_points,
    collect_history,
    extract_manifest_dependencies,
    extract_relationships,
    list_included_files,
    scan_repo,
)

logger = logging.getLogger(__name__)


def _register(checkout: MaterializedCheckout, project_id: str) -> Repo:
    try:
        with worker_connection() as conn:
            return create_repo(
                conn,
                Repo(
                    project_id=project_id,
                    remote_url=checkout.remote_url,
                    local_path=str(checkout.local_path),
                    staged_commit_sha=checkout.commit_sha,
                ),
            )
    except BaseException:
        discard_checkout_if_scratch(checkout.local_path)
        raise


def stage_repo(source: IngestSource, project_id: str) -> tuple[Repo, MaterializedCheckout]:
    """Materialize `source` and register it under `project_id`. The caller
    must already have checked that the requesting user is a member."""
    if isinstance(source, GithubSource):
        checkout = materialize_github(source.remote_url, source.ref)
    else:
        checkout = materialize_local(source.local_path, source.commit_sha)
    return _register(checkout, project_id), checkout


def stage_zip_repo(zip_path: Path, project_id: str) -> tuple[Repo, MaterializedCheckout]:
    checkout = materialize_zip(zip_path)
    return _register(checkout, project_id), checkout


def preview_repo(
    local_path: str, extra_excludes: frozenset[str] | None = None
) -> list[PreviewEntry]:
    return list_included_files(Path(local_path), extra_excludes or frozenset())


def discard_checkout_if_scratch(local_path: str | Path) -> None:
    """Delete a scratch checkout this worker created. A user's own local
    folder is never deleted — `is_scratch_checkout` only matches dirs that
    ingest.py made under the system temp dir. Public so callers outside this
    module (e.g. the DELETE /repos/{id} route) can clean up a staged-but-
    never-scanned repo's checkout too, not just a post-scan one."""
    if is_scratch_checkout(local_path):
        shutil.rmtree(local_path, ignore_errors=True)


def run_scan_job(
    job_id: str,
    repo_id: str,
    commit_sha: str,
    local_path: str,
    extra_excludes: frozenset[str] | None = None,
) -> None:
    """Run the scan pass on behalf of a codeaxis.jobs row, moving the job
    queued -> running -> succeeded|failed. The job row is the user-facing
    status (GET /jobs/{id}); the pass_run keeps the analyzer-level record.

    A scan failure is recorded on the job (and on the pass_run) and logged,
    not re-raised: this runs as a background task after the response has
    been sent, so there is no caller left to receive the exception."""
    # STUB(CodeAxis): runs in-process via FastAPI BackgroundTasks. If the API
    # process dies mid-scan the job stays `queued`/`running` forever: no queue
    # (pgmq, build-plan.md Part 2), no retry, no stale-job reaper (Part 9).
    try:
        with worker_connection() as conn:
            set_job_status(conn, job_id, JobStatus.RUNNING)
        run_scan_pass(repo_id, commit_sha, local_path, extra_excludes)
    except Exception as exc:
        logger.exception("scan job %s failed", job_id)
        # run_scan_pass already discards the checkout; this covers a failure
        # before it ran (e.g. the RUNNING update itself). Idempotent.
        discard_checkout_if_scratch(local_path)
        with worker_connection() as conn:
            set_job_status(conn, job_id, JobStatus.FAILED, error=str(exc) or type(exc).__name__)
        return
    with worker_connection() as conn:
        set_job_status(conn, job_id, JobStatus.SUCCEEDED)


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
        discard_checkout_if_scratch(local_path)


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

            scanned, scan_stats, trees_by_file = scan_repo(
                root, repo_id, commit_sha, extra_excludes=extra_excludes
            )

            # Heuristic attrs annotations happen before insert, on the Node
            # objects scan_repo already built, so they land in the one
            # insert_nodes call below rather than needing a follow-up update.
            annotate_entry_points(scanned, root)
            history_by_path = collect_history(root)
            for sn in scanned:
                if sn.node.kind == "file" and sn.node.path in history_by_path:
                    sn.node.attrs.update(history_by_path[sn.node.path])

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
                    ),
                    Coverage(
                        repo_id=repo_id,
                        commit_sha=commit_sha,
                        area="/",
                        metric="secrets_redacted",
                        resolved=sum(1 for f in files if f.attrs.get("secrets_redacted") is True),
                        total=len(files),
                        blockers={},
                    ),
                ],
            )

            relationship_edges = extract_relationships(
                scanned, trees_by_file, id_by_key, repo_id, commit_sha
            )
            if relationship_edges:
                insert_edges(conn, relationship_edges)

            dependency_nodes = extract_manifest_dependencies(root, repo_id, commit_sha)
            if dependency_nodes:
                insert_nodes(conn, dependency_nodes)

            # Pass 1c: cluster the import/call graph into candidate
            # components and classify file layers. Reads only rows already
            # written above in this same transaction/connection -- no
            # checkout access needed, so this stays inside _run_scan_pass
            # rather than becoming its own PassRun (see writer.py's
            # module docstring for the reasoning).
            write_architecture_pass(conn, repo_id, commit_sha)
        except Exception as exc:
            conn.rollback()
            finish_pass_run(conn, run.id, PassRunStatus.FAILED, error=str(exc))
            raise

        finish_pass_run(conn, run.id, PassRunStatus.SUCCEEDED)
