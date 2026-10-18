"use client";

import { useRef, useState } from "react";

import {
  ApiError,
  askRepo,
  isCitationCheckFailure,
  searchRepo,
  type ChatCitation,
  type ChatResponse,
  type Citation,
  type CitationCheckFailure,
  type RetrievalBudget,
  type RetrievedSpan,
  type SearchResponse,
  type StageOutcome,
} from "@/lib/api";

// Phase 3's two read surfaces over the semantic index: hybrid retrieval
// (/search) and grounded chat (/chat). Both are rendered the same way on
// purpose — an answer is spans plus prose about those spans, never prose
// alone. Three rules this component exists to keep visible:
//
//   1. Every sentence shows the file+line it came from. A claim with no
//      citation is labelled as such rather than read as fact.
//   2. A retrieval stage that did not run says so (`StageOutcome.skipped`),
//      because "no vector stage" and "no vector matches" are different
//      facts and the UI must not blur them.
//   3. A rejected answer is reported, not retried: the server discards an
//      answer whose citations do not resolve, and this surface shows the
//      rejection with its reasons.

type Pending = { kind: "idle" } | { kind: "running" } | { kind: "error"; message: string };

/** A chat turn as this client holds it: the question, and whatever the
 * server said about it — an answer, a no-evidence outcome, or a rejection. */
type Turn = {
  question: string;
  response?: ChatResponse;
  rejection?: CitationCheckFailure;
  error?: string;
};

function citationLabel(c: Citation): string {
  const span = c.line_start === c.line_end ? `${c.line_start}` : `${c.line_start}–${c.line_end}`;
  return `${c.path}:${span}`;
}

function CitationChip({
  citation,
  onOpen,
}: {
  citation: ChatCitation;
  onOpen?: (citation: Citation) => void;
}) {
  const label = citationLabel(citation);
  const notRetrieved = citation.in_evidence === false;
  const className = `rounded border px-1.5 py-0.5 font-mono text-[11px] ${
    notRetrieved
      ? "border-amber-600/40 bg-amber-500/10 text-amber-700 dark:text-amber-400"
      : "border-line bg-panel text-muted"
  }`;
  const title = notRetrieved
    ? "This path exists in the snapshot but was not part of the retrieved evidence."
    : "Retrieved evidence this claim was written from.";

  // Only a citation that carries a node id can become a selection: there is
  // no path → node lookup, and guessing one would be inventing a link the
  // graph does not hold.
  if (!onOpen || citation.node_id === null || citation.node_id === undefined) {
    return (
      <span className={className} title={title}>
        {label}
      </span>
    );
  }
  return (
    <button type="button" onClick={() => onOpen(citation)} className={`${className} hover:bg-accent/10`} title={title}>
      {label}
    </button>
  );
}

function Stages({ stages, budget }: { stages: StageOutcome[]; budget: RetrievalBudget }) {
  return (
    <div className="space-y-1 text-[11px] text-muted">
      <ul className="flex flex-wrap gap-x-3 gap-y-1">
        {stages.map((s) => (
          <li key={s.name}>
            <span className="font-medium text-foreground">{s.name}</span>{" "}
            {s.status === "ran" ? (
              <span>ran · {s.hits} hits</span>
            ) : (
              <span className="text-amber-700 dark:text-amber-400">
                skipped{s.reason ? ` · ${s.reason}` : ""}
              </span>
            )}
          </li>
        ))}
      </ul>
      <p>
        {budget.tokens_used} of {budget.token_budget} evidence tokens used ·{" "}
        {budget.fused_candidates} candidates fused
        {budget.dropped_for_budget > 0 && ` · ${budget.dropped_for_budget} dropped for budget`}
      </p>
    </div>
  );
}

function SpanRow({
  span,
  onOpen,
}: {
  span: RetrievedSpan;
  onOpen?: (citation: Citation) => void;
}) {
  const citation: Citation = {
    path: span.path,
    line_start: span.line_start,
    line_end: span.line_end,
    symbol: span.symbol,
    node_id: span.node_id,
    chunk_id: span.chunk_id,
  };
  return (
    <li className="space-y-1 rounded-md border border-line p-2">
      <div className="flex flex-wrap items-center gap-2">
        <CitationChip citation={citation} onOpen={onOpen} />
        {span.symbol && <span className="font-mono text-xs">{span.symbol}</span>}
        {span.kind && <span className="text-[11px] text-muted">{span.kind}</span>}
        <span className="text-[11px] text-muted">
          score {span.score.toFixed(3)} · {span.stages.join(", ") || "no stage"} ·{" "}
          {span.token_count} tokens
        </span>
      </div>
      {span.content !== undefined && (
        <pre className="max-h-56 overflow-auto rounded bg-panel p-2 font-mono text-[11px] leading-relaxed">
          {span.content}
        </pre>
      )}
    </li>
  );
}

function AnswerBlock({
  turn,
  onOpen,
}: {
  turn: Turn;
  onOpen?: (citation: Citation) => void;
}) {
  const { response, rejection, error } = turn;

  return (
    <article className="space-y-3 border-t border-line pt-3">
      <p className="text-sm font-medium">{turn.question}</p>

      {error !== undefined && (
        <p className="text-xs text-rose-700 dark:text-rose-300">{error}</p>
      )}

      {rejection !== undefined && (
        <div className="space-y-2 rounded-md border border-rose-600/40 bg-rose-500/5 p-3 text-xs">
          <p className="font-medium text-rose-700 dark:text-rose-300">
            Answer discarded: its citations do not resolve in this snapshot.
          </p>
          <p className="text-muted">{rejection.detail}</p>
          <p className="text-muted">
            {rejection.checked} citations checked · {rejection.unsupported_claims} claims left
            unsupported. The answer was not reworded or re-tried, and nothing was saved.
          </p>
          {rejection.rejected.length > 0 && (
            <ul className="space-y-1">
              {rejection.rejected.map((r, i) => (
                <li key={i} className="font-mono text-[11px] text-muted">
                  {JSON.stringify(r)}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {response?.status === "no_evidence" && (
        <p className="rounded-md border border-dashed border-line px-3 py-2 text-xs text-muted">
          {response.unsupported}
        </p>
      )}

      {response?.status === "ok" && (
        <div className="space-y-3">
          <ul className="space-y-2">
            {response.claims.map((claim, i) => (
              <li key={i} className="space-y-1 text-sm">
                <p>{claim.text}</p>
                <div className="flex flex-wrap items-center gap-1.5">
                  {claim.citations.length === 0 ? (
                    <span className="text-[11px] text-amber-700 dark:text-amber-400">
                      no citation — this sentence is not backed by a retrieved span
                    </span>
                  ) : (
                    claim.citations.map((c, j) => (
                      <CitationChip key={j} citation={c} onOpen={onOpen} />
                    ))
                  )}
                </div>
              </li>
            ))}
          </ul>
          {response.unsupported && (
            <p className="rounded-md border border-dashed border-line px-3 py-2 text-xs text-muted">
              Not answered from the evidence: {response.unsupported}
            </p>
          )}
          {response.citation_check && (
            <p className="text-[11px] text-muted">
              {response.citation_check.checked} citations checked against this snapshot,{" "}
              {response.citation_check.rejected} rejected.
            </p>
          )}
        </div>
      )}

      {response && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted">
            Evidence ({response.evidence.length} spans) and retrieval stages
          </summary>
          <div className="mt-2 space-y-2">
            <Stages stages={response.stages} budget={response.budget} />
            <ul className="space-y-2">
              {response.evidence.map((span, i) => (
                <SpanRow key={span.chunk_id ?? i} span={span} onOpen={onOpen} />
              ))}
            </ul>
            <p className="text-[11px] text-muted">{response.streaming}</p>
          </div>
        </details>
      )}
    </article>
  );
}

/** Hybrid search and grounded chat over one snapshot's semantic index.
 * `onOpenCitation` lets a citation become a selection in the Explorer
 * (Workspace owns that state) — the same model, reached from a different
 * surface, rather than a second copy of it. */
export function AskView({
  repoId,
  commitSha,
  onOpenCitation,
}: {
  repoId: string;
  commitSha: string;
  onOpenCitation?: (citation: Citation) => void;
}) {
  const [mode, setMode] = useState<"ask" | "search">("ask");
  const [input, setInput] = useState("");
  const [pending, setPending] = useState<Pending>({ kind: "idle" });
  const [turns, setTurns] = useState<Turn[]>([]);
  const [results, setResults] = useState<SearchResponse | null>(null);
  // One thread per mounted view: the server owns the transcript, this is
  // just the handle for continuing it.
  const threadId = useRef<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const text = input.trim();
    if (!text || pending.kind === "running") return;
    setPending({ kind: "running" });

    if (mode === "search") {
      try {
        setResults(await searchRepo(repoId, commitSha, text));
        setPending({ kind: "idle" });
      } catch (err) {
        setPending({
          kind: "error",
          message: err instanceof Error ? err.message : String(err),
        });
      }
      return;
    }

    try {
      const response = await askRepo(repoId, commitSha, text, threadId.current);
      threadId.current = response.thread_id;
      setTurns((prev) => [...prev, { question: text, response }]);
      setInput("");
      setPending({ kind: "idle" });
    } catch (err) {
      if (err instanceof ApiError && isCitationCheckFailure(err.detail)) {
        threadId.current = err.detail.thread_id;
        setTurns((prev) => [...prev, { question: text, rejection: err.detail as CitationCheckFailure }]);
        setInput("");
        setPending({ kind: "idle" });
        return;
      }
      const message = err instanceof Error ? err.message : String(err);
      setTurns((prev) => [...prev, { question: text, error: message }]);
      setPending({ kind: "idle" });
    }
  }

  return (
    <div className="space-y-4 p-5">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex gap-1" role="tablist" aria-label="Ask mode">
          {(["ask", "search"] as const).map((m) => (
            <button
              key={m}
              type="button"
              role="tab"
              aria-selected={mode === m}
              onClick={() => setMode(m)}
              className={`rounded px-2 py-1 text-xs font-medium ${
                mode === m ? "bg-accent/15 text-foreground" : "text-muted hover:bg-panel"
              }`}
            >
              {m === "ask" ? "Ask" : "Search"}
            </button>
          ))}
        </div>
        <p className="text-[11px] text-muted">
          {mode === "ask"
            ? "Answers are written only from retrieved spans, and every citation is checked against this snapshot before the answer is shown."
            : "Hybrid retrieval over the chunk index: exact symbol, full-text, graph neighbours and (when indexed) embeddings."}
        </p>
      </div>

      <form onSubmit={submit} className="flex flex-wrap gap-2">
        <label className="sr-only" htmlFor="ask-input">
          {mode === "ask" ? "Question about this codebase" : "Search this codebase"}
        </label>
        <input
          id="ask-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={
            mode === "ask" ? "Where is the import pipeline run?" : "parse tarball"
          }
          className="min-w-0 flex-1 rounded-md border border-line bg-transparent px-3 py-2 text-sm"
        />
        <button
          type="submit"
          disabled={pending.kind === "running" || input.trim() === ""}
          className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          {pending.kind === "running" ? "Working…" : mode === "ask" ? "Ask" : "Search"}
        </button>
      </form>

      {pending.kind === "error" && (
        <p className="text-xs text-rose-700 dark:text-rose-300">{pending.message}</p>
      )}

      {mode === "ask" ? (
        turns.length === 0 ? (
          <p className="text-xs text-muted">
            Nothing asked yet. Each answer arrives as separate claims, each with the file and line
            range it was written from.
          </p>
        ) : (
          <div className="space-y-4">
            {turns.map((turn, i) => (
              <AnswerBlock key={i} turn={turn} onOpen={onOpenCitation} />
            ))}
          </div>
        )
      ) : results === null ? (
        <p className="text-xs text-muted">
          No search run yet. Results are spans of real code with their file and line range, not
          generated text.
        </p>
      ) : (
        <div className="space-y-3">
          <Stages stages={results.stages} budget={results.budget} />
          {results.results.length === 0 ? (
            <p className="rounded-md border border-dashed border-line px-3 py-2 text-xs text-muted">
              No indexed span matched <span className="font-mono">{results.query}</span> at this
              commit. The stages above say which retrieval paths ran.
            </p>
          ) : (
            <ul className="space-y-2">
              {results.results.map((span, i) => (
                <SpanRow key={span.chunk_id ?? i} span={span} onOpen={onOpenCitation} />
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
