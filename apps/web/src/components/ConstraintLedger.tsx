"use client";

import { useCallback, useEffect, useState } from "react";

import {
  CONSTRAINT_KINDS,
  confirmConstraint,
  createConstraint,
  deleteConstraint,
  listTaskConstraints,
  patchConstraint,
  type Citation,
  type Constraint,
  type ConstraintInput,
  type Task,
} from "@/lib/api";

import { ErrorLine, errMsg, EvidenceList, OriginBadge, Pill } from "./ContextBits";

const EMPTY: ConstraintInput = { kind: "other", scope: "", statement: "", severity: "hard", rationale: "" };

function ConstraintForm({
  initial,
  submitLabel,
  onSubmit,
  onCancel,
}: {
  initial: ConstraintInput;
  submitLabel: string;
  onSubmit: (value: ConstraintInput) => Promise<void>;
  onCancel?: () => void;
}) {
  const [value, setValue] = useState<ConstraintInput>(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const valid = value.scope.trim() !== "" && value.statement.trim() !== "";

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onSubmit({ ...value, rationale: value.rationale?.trim() ? value.rationale : null });
      setValue(initial);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  const field = "rounded-md border border-line bg-transparent px-2 py-1.5 text-sm";
  return (
    <form onSubmit={submit} className="space-y-2 rounded-md border border-line p-3">
      <div className="flex flex-wrap gap-2">
        <label className="text-xs text-muted">
          Kind
          <select
            value={value.kind}
            onChange={(e) =>
              setValue({ ...value, kind: e.target.value as ConstraintInput["kind"] })
            }
            className={`${field} ml-1`}
          >
            {CONSTRAINT_KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-muted">
          Severity
          <select
            value={value.severity}
            onChange={(e) =>
              setValue({ ...value, severity: e.target.value as ConstraintInput["severity"] })
            }
            className={`${field} ml-1`}
          >
            <option value="hard">hard</option>
            <option value="soft">soft</option>
          </select>
        </label>
        <label className="min-w-0 flex-1 text-xs text-muted">
          Scope (path, glob, component or &quot;repo&quot;)
          <input
            value={value.scope}
            maxLength={500}
            onChange={(e) => setValue({ ...value, scope: e.target.value })}
            className={`${field} mt-0.5 w-full font-mono`}
          />
        </label>
      </div>
      <label className="block text-xs text-muted">
        Statement
        <textarea
          value={value.statement}
          maxLength={4000}
          rows={2}
          onChange={(e) => setValue({ ...value, statement: e.target.value })}
          className={`${field} mt-0.5 w-full`}
        />
      </label>
      <label className="block text-xs text-muted">
        Rationale (optional)
        <input
          value={value.rationale ?? ""}
          maxLength={4000}
          onChange={(e) => setValue({ ...value, rationale: e.target.value })}
          className={`${field} mt-0.5 w-full`}
        />
      </label>
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={!valid || busy}
          className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
        >
          {busy ? "Saving…" : submitLabel}
        </button>
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md border border-line px-3 py-1.5 text-xs font-medium hover:bg-panel"
          >
            Cancel
          </button>
        )}
      </div>
      <ErrorLine message={error} />
    </form>
  );
}

function ConstraintRow({
  c,
  task,
  archived,
  onChanged,
  onOpenCitation,
}: {
  c: Constraint;
  task: Task;
  archived: boolean;
  onChanged: () => void;
  onOpenCitation?: (citation: Citation) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const editable = c.source === "user_stated" && c.task_id === task.id;

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      onChanged();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  if (editing && c.id) {
    const id = c.id;
    return (
      <li>
        <ConstraintForm
          initial={{
            kind: c.kind,
            scope: c.scope,
            statement: c.statement,
            severity: c.severity,
            rationale: c.rationale ?? "",
          }}
          submitLabel="Save changes"
          onCancel={() => setEditing(false)}
          onSubmit={async (v) => {
            await patchConstraint(task.id as string, id, v);
            setEditing(false);
            onChanged();
          }}
        />
      </li>
    );
  }

  return (
    <li className="space-y-2 rounded-md border border-line p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={c.severity === "hard" ? "bad" : "neutral"}>{c.severity}</Pill>
        <Pill tone="neutral">{c.kind}</Pill>
        <OriginBadge origin={c.origin} />
        <Pill tone="neutral">{c.source === "user_stated" ? "user stated" : "auto-detected"}</Pill>
        {c.confirmed ? (
          <Pill tone="ok" title={c.confirmed_at ? `Confirmed ${c.confirmed_at}` : undefined}>
            confirmed
          </Pill>
        ) : (
          <Pill tone="warn" title="Shown as a detection, not as an agreed rule.">
            unconfirmed
          </Pill>
        )}
      </div>
      <p className="font-mono text-xs">{c.scope}</p>
      <p className="whitespace-pre-wrap text-sm">{c.statement}</p>
      {c.rationale && <p className="whitespace-pre-wrap text-xs text-muted">Why: {c.rationale}</p>}
      {c.source === "auto_detected" && (
        <p className="text-[11px] text-muted">
          Detected by {c.analyzer ?? "an analyzer"} {c.analyzer_version ?? ""} from the repository.
          Read-only: you can confirm it, but not rewrite or delete it, because the repository, not
          you, is its source.
        </p>
      )}
      {c.source === "user_stated" && !editable && (
        <p className="text-[11px] text-muted">
          Stated on another task, so it cannot be edited from this one.
        </p>
      )}
      <EvidenceList evidence={c.evidence} onOpen={onOpenCitation} />

      <div className="flex flex-wrap items-center gap-2">
        {c.id && (
          <button
            type="button"
            disabled={busy || archived}
            onClick={() => run(() => confirmConstraint(task.id as string, c.id as string, !c.confirmed))}
            className="rounded-md border border-line px-2.5 py-1 text-xs font-medium hover:bg-panel disabled:opacity-50"
          >
            {c.confirmed ? "Withdraw confirmation" : "Confirm"}
          </button>
        )}
        {editable && !archived && (
          <>
            <button
              type="button"
              disabled={busy}
              onClick={() => setEditing(true)}
              className="rounded-md border border-line px-2.5 py-1 text-xs font-medium hover:bg-panel disabled:opacity-50"
            >
              Edit
            </button>
            {confirmingDelete ? (
              <span className="flex items-center gap-2 text-xs">
                Delete this constraint?
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => run(() => deleteConstraint(task.id as string, c.id as string))}
                  className="rounded-md border border-rose-600/40 px-2.5 py-1 font-medium text-rose-700 hover:bg-rose-500/10 disabled:opacity-50 dark:text-rose-300"
                >
                  Delete constraint
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmingDelete(false)}
                  className="rounded-md border border-line px-2.5 py-1 hover:bg-panel"
                >
                  Keep
                </button>
              </span>
            ) : (
              <button
                type="button"
                disabled={busy}
                onClick={() => setConfirmingDelete(true)}
                className="rounded-md border border-line px-2.5 py-1 text-xs font-medium hover:bg-panel disabled:opacity-50"
              >
                Delete
              </button>
            )}
          </>
        )}
      </div>
      <ErrorLine message={error} />
    </li>
  );
}

/** The task's constraint ledger: hard before soft. Constraints are the one
 * package section that is never truncated, so they are shown whole. Any edit
 * makes existing packages stale on the server; `onChanged` lets the parent
 * re-read them rather than keep presenting one as current. */
export function ConstraintLedger({
  task,
  onChanged,
  onOpenCitation,
}: {
  task: Task;
  onChanged: () => void;
  onOpenCitation?: (citation: Citation) => void;
}) {
  const taskId = task.id;
  const [rows, setRows] = useState<Constraint[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const archived = task.status === "archived";

  const reload = useCallback(async () => {
    if (taskId === null) return;
    try {
      setRows((await listTaskConstraints(taskId)).constraints);
      setError(null);
    } catch (err) {
      setError(errMsg(err));
    }
  }, [taskId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load on mount
    void reload();
  }, [reload]);

  function changed() {
    void reload();
    onChanged();
  }

  const sorted = [...(rows ?? [])].sort(
    (a, b) =>
      Number(a.severity === "soft") - Number(b.severity === "soft") ||
      a.scope.localeCompare(b.scope),
  );
  const hard = sorted.filter((c) => c.severity === "hard");
  const soft = sorted.filter((c) => c.severity === "soft");

  return (
    <section aria-label="Constraint ledger" className="space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold">Constraint ledger</h3>
        {!archived && (
          <button
            type="button"
            onClick={() => setAdding((v) => !v)}
            className="rounded-md border border-line px-2.5 py-1 text-xs font-medium hover:bg-panel"
          >
            {adding ? "Close form" : "Add constraint"}
          </button>
        )}
      </div>
      <p className="text-[11px] text-muted">
        Constraints are always included in a context package and never truncated. A detection is
        not an agreed rule until someone confirms it.
      </p>
      <ErrorLine message={error} />

      {adding && task.id && (
        <ConstraintForm
          initial={EMPTY}
          submitLabel="Add constraint"
          onSubmit={async (v) => {
            await createConstraint(task.id as string, v);
            setAdding(false);
            changed();
          }}
        />
      )}

      {rows === null ? (
        !error && <p className="text-xs text-muted">Loading constraints…</p>
      ) : rows.length === 0 ? (
        <p className="rounded-md border border-dashed border-line px-3 py-2 text-xs text-muted">
          The ledger is empty for this task: no constraint was stated and none was auto-detected.
          That is not the same as the repository having none.
        </p>
      ) : (
        <>
          {[
            ["Hard constraints", hard],
            ["Soft constraints", soft],
          ].map(([label, list]) =>
            (list as Constraint[]).length === 0 ? null : (
              <div key={label as string} className="space-y-2">
                <h4 className="text-xs font-medium text-muted">
                  {label as string} ({(list as Constraint[]).length})
                </h4>
                <ul className="space-y-2">
                  {(list as Constraint[]).map((c) => (
                    <ConstraintRow
                      key={c.id ?? c.scope + c.statement}
                      c={c}
                      task={task}
                      archived={archived}
                      onChanged={changed}
                      onOpenCitation={onOpenCitation}
                    />
                  ))}
                </ul>
              </div>
            ),
          )}
        </>
      )}
    </section>
  );
}
