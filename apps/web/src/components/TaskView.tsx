"use client";

import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  createTask,
  getContextPackage,
  getTaskState,
  regroundTask,
  type Citation,
  type ContextPackageResponse,
  type RegroundReport,
  type TaskState,
} from "@/lib/api";

import { ApiTokensPanel } from "./ApiTokensPanel";
import { ConstraintLedger } from "./ConstraintLedger";
import { ContextPackagePanel } from "./ContextPackagePanel";
import { ErrorLine, errMsg, Pill } from "./ContextBits";
import { RequirementsPanel } from "./RequirementsPanel";
import { TaskIntake } from "./TaskIntake";

// The Context tab: tasks, their requirements and constraint ledger, and the
// budgeted context package an agent would be given. The API has no "list
// tasks" route, so this browser remembers the ids of tasks it created or
// opened (per repository) and says so.

const storeKey = (repoId: string) => `codeaxis:tasks:${repoId}`;

function readIds(repoId: string): string[] {
  try {
    const raw = localStorage.getItem(storeKey(repoId));
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

function writeIds(repoId: string, ids: string[]) {
  try {
    localStorage.setItem(storeKey(repoId), JSON.stringify(ids));
  } catch {
    // Storage unavailable: the list is simply not remembered.
  }
}

function Reground({
  state,
  commitSha,
  onDone,
}: {
  state: TaskState;
  commitSha: string;
  onDone: () => void;
}) {
  const taskId = state.task.id;
  const [reason, setReason] = useState(`Workspace is now at ${commitSha.slice(0, 12)}`);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<RegroundReport | null>(null);
  const differs = state.task.commit_sha !== commitSha;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!taskId || !reason.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      setReport(await regroundTask(taskId, commitSha, reason.trim()));
      onDone();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  const pinned = report?.needs_rederivation ?? state.pinned_elsewhere;
  if (!differs && !report && pinned.length === 0) return null;

  return (
    <section aria-label="Reground" className="space-y-2 rounded-md border border-amber-600/40 bg-amber-500/5 p-3 text-xs">
      {differs && (
        <>
          <p className="font-medium text-amber-700 dark:text-amber-400">
            This task is grounded at <span className="font-mono">{state.task.commit_sha}</span>, but
            the workspace is at <span className="font-mono">{commitSha}</span>.
          </p>
          <p className="text-muted">
            Regrounding moves the task to the workspace commit and marks packages built elsewhere
            stale. Records derived at the old commit are listed, not migrated.
          </p>
          <form onSubmit={submit} className="flex flex-wrap items-end gap-2">
            <label className="min-w-0 flex-1 text-muted">
              Reason (recorded)
              <input
                value={reason}
                maxLength={1000}
                onChange={(e) => setReason(e.target.value)}
                className="mt-0.5 block w-full rounded-md border border-line bg-transparent px-2 py-1.5 text-sm text-foreground"
              />
            </label>
            <button
              type="submit"
              disabled={busy || !reason.trim() || state.task.status === "archived"}
              className="rounded-md border border-line px-3 py-1.5 font-medium hover:bg-panel disabled:opacity-50"
            >
              {busy ? "Regrounding…" : "Reground to workspace commit"}
            </button>
          </form>
        </>
      )}
      <ErrorLine message={error} />
      {report && (
        <p>
          Moved from <span className="font-mono">{report.previous_commit_sha}</span>.{" "}
          {report.stale_package_ids.length} package
          {report.stale_package_ids.length === 1 ? "" : "s"} marked stale.
        </p>
      )}
      {pinned.length > 0 && (
        <div>
          <p className="font-medium">
            {pinned.length} record{pinned.length === 1 ? "" : "s"} still pinned to an older commit
            (not migrated; re-derive them):
          </p>
          <ul className="mt-1 space-y-0.5 font-mono text-[11px] text-muted">
            {pinned.map((p, i) => (
              <li key={i}>
                {p.kind} {p.ref} @ {p.commit_sha}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function TaskDetail({
  taskId,
  commitSha,
  onBack,
  onOpenCitation,
}: {
  taskId: string;
  commitSha: string;
  onBack: () => void;
  onOpenCitation?: (citation: Citation) => void;
}) {
  const [state, setState] = useState<TaskState | null>(null);
  const [pkg, setPkg] = useState<ContextPackageResponse | null>(null);
  const [pkgError, setPkgError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const s = await getTaskState(taskId);
      setState(s);
      setError(null);
      if (s.latest_package) {
        try {
          setPkg(await getContextPackage(taskId));
          setPkgError(null);
        } catch (err) {
          if (err instanceof ApiError && err.status === 404) setPkg(null);
          else setPkgError(errMsg(err));
        }
      } else {
        setPkg(null);
      }
    } catch (err) {
      setError(errMsg(err));
    }
  }, [taskId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load on mount
    void refresh();
  }, [refresh]);

  const backButton = (
    <button
      type="button"
      onClick={onBack}
      className="rounded-md border border-line px-2.5 py-1 text-xs font-medium hover:bg-panel"
    >
      All tasks
    </button>
  );

  if (!state) {
    return (
      <div className="space-y-3 p-5">
        {backButton}
        {error ? <ErrorLine message={error} /> : <p className="text-xs text-muted">Loading task…</p>}
      </div>
    );
  }

  const task = state.task;
  return (
    <div className="space-y-6 p-5">
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          {backButton}
          <Pill tone={task.status === "blocked" ? "bad" : "neutral"}>{task.status}</Pill>
          <span className="font-mono text-[11px] text-muted">{task.commit_sha}</span>
        </div>
        <h2 className="text-base font-semibold">{task.title}</h2>
        <p className="text-[11px] text-muted">
          {state.notice} A model extracted the requirements; the constraint ledger mixes rules
          people stated with detections from the repository, each labelled.
        </p>
        <ErrorLine message={error} />
        {state.problems.length > 0 && (
          <ul className="space-y-0.5 text-xs text-amber-700 dark:text-amber-400">
            {state.problems.map((p, i) => (
              <li key={i}>Inconsistency: {p}</li>
            ))}
          </ul>
        )}
      </div>

      <Reground state={state} commitSha={commitSha} onDone={refresh} />
      <TaskIntake task={task} onExtracted={refresh} />
      <RequirementsPanel
        taskId={taskId}
        requirements={state.requirements}
        decisions={state.decisions}
        archived={task.status === "archived"}
        onChanged={refresh}
        onOpenCitation={onOpenCitation}
      />
      <ConstraintLedger task={task} onChanged={refresh} onOpenCitation={onOpenCitation} />
      <ContextPackagePanel
        taskId={taskId}
        archived={task.status === "archived"}
        pkg={pkg}
        loadError={pkgError}
        onAssembled={(p) => {
          setPkg(p);
          void refresh();
        }}
        onOpenCitation={onOpenCitation}
      />
    </div>
  );
}

type Summary = { id: string; state: TaskState | null; error: string | null };

export function TaskView({
  repoId,
  commitSha,
  onOpenCitation,
}: {
  repoId: string;
  commitSha: string;
  onOpenCitation?: (citation: Citation) => void;
}) {
  const [ids, setIds] = useState<string[]>([]);
  const [summaries, setSummaries] = useState<Summary[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [intent, setIntent] = useState("");
  const [manualId, setManualId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- read per-viewer list on mount
    setIds(readIds(repoId));
  }, [repoId]);

  useEffect(() => {
    let live = true;
    Promise.all(
      ids.map(async (id): Promise<Summary> => {
        try {
          return { id, state: await getTaskState(id), error: null };
        } catch (err) {
          return { id, state: null, error: errMsg(err) };
        }
      }),
    ).then((s) => {
      if (live) setSummaries(s);
    });
    return () => {
      live = false;
    };
  }, [ids, openId]);

  function remember(id: string) {
    const next = [id, ...ids.filter((x) => x !== id)];
    setIds(next);
    writeIds(repoId, next);
  }

  function forget(id: string) {
    const next = ids.filter((x) => x !== id);
    setIds(next);
    writeIds(repoId, next);
  }

  async function create(event: React.FormEvent) {
    event.preventDefault();
    if (!title.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const task = await createTask(repoId, title.trim(), intent, commitSha);
      if (task.id) {
        remember(task.id);
        setOpenId(task.id);
      }
      setTitle("");
      setIntent("");
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  if (openId) {
    return (
      <TaskDetail
        taskId={openId}
        commitSha={commitSha}
        onBack={() => setOpenId(null)}
        onOpenCitation={onOpenCitation}
      />
    );
  }

  return (
    <div className="space-y-6 p-5">
      <section aria-label="New task" className="space-y-2">
        <h2 className="text-sm font-semibold">New task</h2>
        <p className="text-[11px] text-muted">
          Describe what you want built or changed. The text is kept verbatim and treated as
          untrusted data. The task is grounded in the static analysis at{" "}
          <span className="font-mono">{commitSha}</span>; nothing is executed.
        </p>
        <form onSubmit={create} className="space-y-2">
          <label className="block text-xs text-muted">
            Title
            <input
              value={title}
              maxLength={300}
              onChange={(e) => setTitle(e.target.value)}
              className="mt-0.5 block w-full rounded-md border border-line bg-transparent px-3 py-2 text-sm"
            />
          </label>
          <label className="block text-xs text-muted">
            Intent
            <textarea
              value={intent}
              maxLength={20000}
              rows={5}
              onChange={(e) => setIntent(e.target.value)}
              className="mt-0.5 block w-full rounded-md border border-line bg-transparent px-3 py-2 text-sm"
            />
          </label>
          <button
            type="submit"
            disabled={busy || !title.trim()}
            className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            {busy ? "Creating…" : "Create task"}
          </button>
        </form>
        <ErrorLine message={error} />
      </section>

      <section aria-label="Tasks" className="space-y-2">
        <h2 className="text-sm font-semibold">Tasks</h2>
        <p className="text-[11px] text-muted">
          The API has no route that lists a repository&apos;s tasks, so this list is only the tasks
          created or opened in this browser. A task made elsewhere can be opened by its id.
        </p>
        {summaries.length === 0 ? (
          <p className="rounded-md border border-dashed border-line px-3 py-2 text-xs text-muted">
            No tasks remembered in this browser for this repository.
          </p>
        ) : (
          <ul className="space-y-2">
            {summaries.map((s) => (
              <li
                key={s.id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-line p-3"
              >
                {s.state ? (
                  <button
                    type="button"
                    onClick={() => setOpenId(s.id)}
                    className="min-w-0 text-left text-sm font-medium hover:text-accent"
                  >
                    {s.state.task.title}
                    <span className="ml-2 text-[11px] font-normal text-muted">
                      {s.state.task.status} · {s.state.task.commit_sha.slice(0, 12)}
                      {s.state.latest_package
                        ? ` · package v${s.state.latest_package.version} ${s.state.latest_package.state}`
                        : " · no package"}
                      {s.state.task.commit_sha !== commitSha && " · different commit"}
                    </span>
                  </button>
                ) : (
                  <span className="text-xs text-rose-700 dark:text-rose-300">
                    {s.id}: {s.error}
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => forget(s.id)}
                  className="rounded-md border border-line px-2 py-1 text-[11px] hover:bg-panel"
                  title="Removes it from this browser's list only. The task is not deleted."
                >
                  Forget
                </button>
              </li>
            ))}
          </ul>
        )}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const id = manualId.trim();
            if (!id) return;
            remember(id);
            setOpenId(id);
            setManualId("");
          }}
          className="flex flex-wrap gap-2"
        >
          <label className="sr-only" htmlFor="open-task-id">
            Task id
          </label>
          <input
            id="open-task-id"
            value={manualId}
            onChange={(e) => setManualId(e.target.value)}
            placeholder="Open a task by id"
            className="min-w-0 flex-1 rounded-md border border-line bg-transparent px-3 py-1.5 font-mono text-xs"
          />
          <button
            type="submit"
            disabled={!manualId.trim()}
            className="rounded-md border border-line px-3 py-1.5 text-xs font-medium hover:bg-panel disabled:opacity-50"
          >
            Open task
          </button>
        </form>
      </section>

      <ApiTokensPanel repoId={repoId} />
    </div>
  );
}
