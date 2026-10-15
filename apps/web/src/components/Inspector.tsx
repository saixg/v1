"use client";

import { useEffect, useState } from "react";

import { ApiError, getFile, getSymbol, type FileContent, type GraphNode } from "@/lib/api";

import { StatusChip } from "./StatusChip";

type Load<T> =
  | { state: "loading" }
  | { state: "loaded"; value: T }
  | { state: "error"; message: string; notFound: boolean };

function errorState(err: unknown): Load<never> {
  return {
    state: "error",
    message: err instanceof Error ? err.message : String(err),
    notFound: err instanceof ApiError && err.status === 404,
  };
}

const MAX_FILE_LINES = 2000;

const UNANALYZED_REASONS: Record<string, string> = {
  file_too_large: "The file is over the scanner's size cap, so it was not read.",
  parse_timeout: "Parsing did not finish within the time limit, so no symbols were extracted.",
};

function describeReason(reason: string): string {
  if (UNANALYZED_REASONS[reason]) return UNANALYZED_REASONS[reason];
  if (reason.startsWith("read_error:") || reason.startsWith("stat_error:"))
    return `The file could not be read (${reason.split(":")[1]}).`;
  return reason;
}

function span(node: GraphNode): string {
  if (node.line_start === null) return "";
  const end = node.line_end !== null && node.line_end !== node.line_start ? `–${node.line_end}` : "";
  return `L${node.line_start}${end}`;
}

function SourceView({
  content,
  from,
  to,
}: {
  content: string;
  from: number | null;
  to: number | null;
}) {
  const [showAll, setShowAll] = useState(false);
  const lines = content.split(/\r?\n/);
  let start = 1;
  let end = lines.length;
  if (from !== null) {
    start = from;
    end = to ?? from;
  } else if (!showAll && lines.length > MAX_FILE_LINES) {
    end = MAX_FILE_LINES;
  }
  const shown = lines.slice(start - 1, end);
  const gutter = String(end).length;

  return (
    <div>
      <pre className="max-h-[60vh] overflow-auto rounded-md border border-line bg-panel py-2 font-mono text-xs leading-5">
        {shown.map((line, i) => (
          <div key={start + i} className="flex">
            <span
              className="select-none pl-3 pr-4 text-right text-muted"
              style={{ minWidth: `${gutter + 3}ch` }}
            >
              {start + i}
            </span>
            <code className="whitespace-pre pr-3">{line || " "}</code>
          </div>
        ))}
      </pre>
      {from === null && end < lines.length && (
        <button
          type="button"
          onClick={() => setShowAll(true)}
          className="mt-2 text-xs font-medium text-accent"
        >
          Show all {lines.length} lines
        </button>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">{title}</h3>
      {children}
    </section>
  );
}

function NotYetComputed({ what }: { what: string }) {
  return (
    <p className="rounded-md border border-dashed border-line px-3 py-2 text-xs text-muted">
      {what} arrive after call-graph resolution (Step 2). They have not been computed for this
      repository yet, so none are shown. This does not mean there are none.
    </p>
  );
}

export function Inspector({
  repoId,
  commitSha,
  node,
}: {
  repoId: string;
  commitSha: string;
  node: GraphNode;
}) {
  // The parent keys this component by node id, so each selection mounts a
  // fresh Inspector and these initial "loading" states are never stale.
  const nodeId = node.id;
  const path = node.path;
  const showsSource = (node.kind === "file" || node.kind === "symbol") && path !== null;
  const [facts, setFacts] = useState<Load<GraphNode>>({ state: "loading" });
  const [source, setSource] = useState<Load<FileContent> | null>(
    showsSource ? { state: "loading" } : null,
  );

  useEffect(() => {
    let cancelled = false;
    if (nodeId === null) return;
    getSymbol(repoId, nodeId, commitSha).then(
      (value) => !cancelled && setFacts({ state: "loaded", value }),
      (err) => !cancelled && setFacts(errorState(err)),
    );
    return () => {
      cancelled = true;
    };
  }, [repoId, commitSha, nodeId]);

  useEffect(() => {
    let cancelled = false;
    if (!showsSource || path === null) return;
    getFile(repoId, path, commitSha).then(
      (value) => !cancelled && setSource({ state: "loaded", value }),
      (err) => !cancelled && setSource(errorState(err)),
    );
    return () => {
      cancelled = true;
    };
  }, [repoId, commitSha, path, showsSource]);

  // Render the server's copy of the node once it arrives; the tree's copy
  // until then (they are the same row).
  const n = facts.state === "loaded" ? facts.value : node;

  return (
    <article className="space-y-6 p-5">
      <header className="space-y-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="font-mono text-base font-semibold">{n.name}</h2>
          <StatusChip status={n.status} />
          <span className="text-xs text-muted">{n.kind}</span>
        </div>
        {n.path !== null && (
          <p className="font-mono text-xs text-muted">
            {n.path || "(repository root)"}
            {span(n) && ` · ${span(n)}`}
          </p>
        )}
        {n.status === "unanalyzed" && n.reason && (
          <p className="text-xs text-muted">Unanalyzed: {describeReason(n.reason)}</p>
        )}
        {n.status === "unanalyzed" && !n.reason && n.kind === "file" && (
          <p className="text-xs text-muted">
            Unanalyzed: CodeAxis does not parse this file type, so it is listed but not analyzed.
          </p>
        )}
        {facts.state === "error" && (
          <p className="text-xs text-rose-700 dark:text-rose-300">
            Couldn&apos;t load this node&apos;s facts: {facts.message}
          </p>
        )}
      </header>

      <div className="space-y-5">
        <h2 className="text-sm font-semibold">Mechanics</h2>

        {showsSource && (
          <Section title={n.kind === "symbol" ? `Source span ${span(n)}` : "Source"}>
            {source === null || source.state === "loading" ? (
              <p className="text-xs text-muted">Loading source…</p>
            ) : source.state === "error" ? (
              <p className="rounded-md border border-dashed border-line px-3 py-2 text-xs text-muted">
                {source.notFound ? "Source not available: " : "Couldn't load source: "}
                {source.message}
              </p>
            ) : (
              <>
                <SourceView
                  key={`${n.id}`}
                  content={source.value.content}
                  from={n.kind === "symbol" ? n.line_start : null}
                  to={n.kind === "symbol" ? n.line_end : null}
                />
                <p className="text-[11px] text-muted">
                  Pinned text of <span className="font-mono">{source.value.path}</span> as scanned at{" "}
                  <span className="font-mono">{commitSha}</span> (sha256{" "}
                  <span className="font-mono">{source.value.content_hash.slice(0, 12)}</span>).
                </p>
              </>
            )}
          </Section>
        )}

        {n.kind === "symbol" && (
          <>
            <Section title="Callees">
              <NotYetComputed what="Callee lists" />
            </Section>
            <Section title="Control-flow summary">
              <NotYetComputed what="Control-flow summaries" />
            </Section>
          </>
        )}
        {n.kind === "file" && (
          <Section title="Imports and dependents">
            <NotYetComputed what="Import and call relationships" />
          </Section>
        )}
      </div>

      <Section title="Provenance">
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-xs">
          <dt className="text-muted">Status</dt>
          <dd>{n.status}</dd>
          <dt className="text-muted">Analyzer</dt>
          <dd className="font-mono">
            {n.analyzer} {n.analyzer_version}
          </dd>
          <dt className="text-muted">Commit</dt>
          <dd className="font-mono">{n.commit_sha}</dd>
          {n.line_start !== null && (
            <>
              <dt className="text-muted">Span</dt>
              <dd className="font-mono">
                {n.line_start}:{n.col_start ?? "?"} – {n.line_end ?? "?"}:{n.col_end ?? "?"}
              </dd>
            </>
          )}
          {n.source_file && (
            <>
              <dt className="text-muted">Source file</dt>
              <dd className="font-mono">{n.source_file}</dd>
            </>
          )}
          {n.confidence !== null && (
            <>
              <dt className="text-muted">Confidence</dt>
              <dd>{n.confidence}</dd>
            </>
          )}
          {n.reason && (
            <>
              <dt className="text-muted">Reason</dt>
              <dd className="font-mono">{n.reason}</dd>
            </>
          )}
          <dt className="text-muted">Node id</dt>
          <dd className="font-mono">{n.id}</dd>
        </dl>
      </Section>

      {Object.keys(n.attrs).length > 0 && (
        <Section title="Attributes">
          <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-xs">
            {Object.entries(n.attrs).map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-muted">{k}</dt>
                <dd className="break-all font-mono">{v === null ? "—" : String(v)}</dd>
              </div>
            ))}
          </dl>
        </Section>
      )}
    </article>
  );
}
