"use client";

import { useState } from "react";

import {
  resolveDecision,
  type Citation,
  type Decision,
  type Requirement,
  type RequirementKind,
  type RequirementStatus,
} from "@/lib/api";

import { ErrorLine, errMsg, EvidenceList, OriginBadge, Pill } from "./ContextBits";

const KIND_ORDER: RequirementKind[] = [
  "objective",
  "functional",
  "technical",
  "architectural",
  "existing_behavior",
  "acceptance_criteria",
  "open_question",
];

const KIND_LABEL: Record<RequirementKind, string> = {
  objective: "Objective",
  functional: "Functional requirements",
  technical: "Technical constraints",
  architectural: "Architectural constraints",
  existing_behavior: "Existing behavior",
  acceptance_criteria: "Acceptance criteria",
  open_question: "Open questions",
};

function StatusPill({ status }: { status: RequirementStatus }) {
  const tone =
    status === "ambiguous" || status === "unresolved"
      ? "warn"
      : status === "satisfied"
        ? "ok"
        : status === "rejected"
          ? "bad"
          : "neutral";
  return <Pill tone={tone}>{status}</Pill>;
}

function RequirementRow({
  r,
  onOpen,
}: {
  r: Requirement;
  onOpen?: (citation: Citation) => void;
}) {
  const unclean = r.status === "ambiguous" || r.status === "unresolved";
  return (
    <li className="space-y-2 rounded-md border border-line p-3">
      <div className="flex flex-wrap items-center gap-2">
        <OriginBadge origin={r.origin} />
        <StatusPill status={r.status} />
        {r.priority && <Pill tone="neutral">{r.priority}</Pill>}
        {r.confidence !== null && (
          <span className="text-[11px] text-muted">confidence {r.confidence.toFixed(2)}</span>
        )}
        {r.model && <span className="text-[11px] text-muted">{r.model}</span>}
      </div>
      <p className="whitespace-pre-wrap text-sm">{r.statement}</p>
      {unclean && r.question && (
        <div className="rounded-md border border-amber-600/40 bg-amber-500/10 px-3 py-2 text-sm">
          <p className="text-[11px] font-medium uppercase tracking-wide text-amber-700 dark:text-amber-400">
            {r.status === "ambiguous" ? "Ambiguous: what is unclear" : "Unresolved: what is missing"}
          </p>
          <p className="mt-0.5 whitespace-pre-wrap">{r.question}</p>
        </div>
      )}
      {!unclean && r.question && (
        <p className="whitespace-pre-wrap text-xs text-muted">Question: {r.question}</p>
      )}
      <EvidenceList evidence={r.evidence} onOpen={onOpen} />
    </li>
  );
}

function DecisionRow({
  taskId,
  decision,
  disabled,
  onResolved,
}: {
  taskId: string;
  decision: Decision;
  disabled: boolean;
  onResolved: () => void;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!decision.id || !text.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await resolveDecision(taskId, decision.id, text.trim());
      onResolved();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="space-y-2 rounded-md border border-line p-3">
      <div className="flex flex-wrap items-center gap-2">
        <OriginBadge origin={decision.origin} />
        <Pill tone={decision.status === "open" ? "warn" : "neutral"}>{decision.status}</Pill>
        {decision.locked && <Pill tone="neutral">locked</Pill>}
      </div>
      <p className="whitespace-pre-wrap text-sm">{decision.question}</p>
      {decision.decision && (
        <p className="whitespace-pre-wrap text-sm">
          <span className="text-muted">Decision: </span>
          {decision.decision}
        </p>
      )}
      {decision.status === "open" && !decision.locked && (
        <form onSubmit={submit} className="flex flex-wrap gap-2">
          <label className="sr-only" htmlFor={`decision-${decision.id}`}>
            Your answer
          </label>
          <input
            id={`decision-${decision.id}`}
            value={text}
            onChange={(e) => setText(e.target.value)}
            maxLength={4000}
            placeholder="Your answer (recorded as yours, then locked)"
            className="min-w-0 flex-1 rounded-md border border-line bg-transparent px-3 py-1.5 text-sm"
          />
          <button
            type="submit"
            disabled={disabled || busy || !text.trim()}
            className="rounded-md border border-line px-3 py-1.5 text-xs font-medium hover:bg-panel disabled:opacity-50"
          >
            {busy ? "Saving…" : "Resolve decision"}
          </button>
        </form>
      )}
      <ErrorLine message={error} />
    </li>
  );
}

/** Extracted requirements grouped by kind, then the task's decisions. A gap
 * (ambiguous, unresolved, open question) is content here, not an empty
 * state: it is shown with the question the server stored for it. */
export function RequirementsPanel({
  taskId,
  requirements,
  decisions,
  archived,
  onChanged,
  onOpenCitation,
}: {
  taskId: string;
  requirements: Requirement[];
  decisions: Decision[];
  archived: boolean;
  onChanged: () => void;
  onOpenCitation?: (citation: Citation) => void;
}) {
  const gaps = requirements.filter(
    (r) => r.status === "ambiguous" || r.status === "unresolved" || r.kind === "open_question",
  ).length;

  return (
    <section aria-label="Requirements" className="space-y-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 className="text-sm font-semibold">Requirements</h3>
        <span className="text-[11px] text-muted">
          {requirements.length} stored · {gaps} open question or gap{gaps === 1 ? "" : "s"}
        </span>
      </div>
      <p className="text-[11px] text-muted">
        Statements below were extracted by a model from the task text and documents. Each was
        checked against the input it cites before it was stored; none is a parsed fact about the
        code.
      </p>

      {requirements.length === 0 ? (
        <p className="rounded-md border border-dashed border-line px-3 py-2 text-xs text-muted">
          No requirements have been extracted for this task yet. Run extraction in the intake
          panel; nothing is assumed in the meantime.
        </p>
      ) : (
        KIND_ORDER.map((kind) => {
          const rows = requirements
            .filter((r) => r.kind === kind)
            .sort((a, b) => a.ordinal - b.ordinal);
          if (rows.length === 0) return null;
          return (
            <div key={kind} className="space-y-2">
              <h4 className="text-xs font-medium text-muted">
                {KIND_LABEL[kind]} ({rows.length})
              </h4>
              <ul className="space-y-2">
                {rows.map((r) => (
                  <RequirementRow key={r.id ?? r.ordinal} r={r} onOpen={onOpenCitation} />
                ))}
              </ul>
            </div>
          );
        })
      )}

      {decisions.length > 0 && (
        <div className="space-y-2">
          <h4 className="text-xs font-medium text-muted">Decisions ({decisions.length})</h4>
          <ul className="space-y-2">
            {decisions.map((d) => (
              <DecisionRow
                key={d.id ?? d.question}
                taskId={taskId}
                decision={d}
                disabled={archived}
                onResolved={onChanged}
              />
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
