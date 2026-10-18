"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import Editor, { type OnMount } from "@monaco-editor/react";
import type { editor as MonacoEditorNS } from "monaco-editor";

import {
  ApiError,
  getFile,
  getNodeExplanation,
  getSymbol,
  type Citation,
  type Explanation,
  type FileContent,
  type GraphNode,
  type RelationshipEntry,
  type ResolutionStatus,
  type SymbolDetail,
} from "@/lib/api";

import { StatusChip } from "./StatusChip";

// Architectural certainty of an edge's destination — distinct from (and
// rendered separately from) the node/edge StatusChip above. Never styled to
// look more certain than it is: ambiguous/unresolved/external are visually
// distinct from resolved, per build-plan.md's "unresolved/ambiguous visible".
const RESOLUTION_STYLES: Record<ResolutionStatus, string> = {
  resolved: "border-emerald-600/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
  ambiguous: "border-amber-600/40 bg-amber-500/10 text-amber-700 dark:text-amber-400",
  unresolved: "border-dashed border-rose-600/50 bg-rose-500/10 text-rose-700 dark:text-rose-400",
  external: "border-sky-600/40 bg-sky-500/10 text-sky-700 dark:text-sky-400",
};

const RESOLUTION_LABELS: Record<ResolutionStatus, string> = {
  resolved: "Resolved",
  ambiguous: "Ambiguous",
  unresolved: "Unresolved",
  external: "External",
};

function ResolutionBadge({ status }: { status: ResolutionStatus | null }) {
  if (status === null) {
    return (
      <span className="inline-flex shrink-0 items-center rounded border border-line bg-panel px-1.5 py-px text-[10px] font-medium uppercase leading-4 tracking-wide text-muted">
        N/A
      </span>
    );
  }
  return (
    <span
      className={`inline-flex shrink-0 items-center rounded border px-1.5 py-px text-[10px] font-medium uppercase leading-4 tracking-wide ${RESOLUTION_STYLES[status]}`}
    >
      {RESOLUTION_LABELS[status]}
    </span>
  );
}

function RelationshipList({
  entries,
  onOpenNode,
}: {
  entries: RelationshipEntry[];
  onOpenNode?: (nodeId: number) => void;
}) {
  if (entries.length === 0) {
    return <p className="text-xs text-muted">None found.</p>;
  }
  return (
    <ul className="space-y-1.5">
      {entries.map((entry, i) => (
        <li key={i} className="flex items-center gap-2 text-xs">
          <span className="shrink-0 font-mono text-muted">{entry.kind}</span>
          <ResolutionBadge status={entry.resolution_status} />
          {entry.node ? (
            <button
              type="button"
              onClick={() => entry.node?.id !== null && onOpenNode?.(entry.node!.id!)}
              disabled={!onOpenNode || entry.node.id === null}
              className="min-w-0 truncate text-left font-mono text-foreground underline-offset-2 hover:underline disabled:no-underline"
              title={entry.node.path ?? entry.node.name}
            >
              {entry.node.name}
            </button>
          ) : (
            <span className="truncate text-muted">
              {typeof entry.attrs.callee_name === "string"
                ? `“${entry.attrs.callee_name}” — not resolved to a node`
                : "not resolved to a node"}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

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

const MONACO_LANGUAGE_BY_EXT: Record<string, string> = {
  py: "python",
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  ts: "typescript",
  tsx: "typescript",
  go: "go",
  rs: "rust",
  java: "java",
  rb: "ruby",
  php: "php",
  c: "c",
  h: "c",
  cpp: "cpp",
  cc: "cpp",
  cxx: "cpp",
  hpp: "cpp",
  cs: "csharp",
  json: "json",
  yml: "yaml",
  yaml: "yaml",
  md: "markdown",
  sh: "shell",
  bash: "shell",
  sql: "sql",
  html: "html",
  css: "css",
  toml: "ini",
};

function monacoLanguage(path: string | null): string {
  if (!path) return "plaintext";
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return MONACO_LANGUAGE_BY_EXT[ext] ?? "plaintext";
}

/** Tracks the OS/browser light-dark preference so Monaco's theme follows
 * the same `prefers-color-scheme` signal the rest of the app's `dark:`
 * Tailwind classes already respond to (globals.css has no manual toggle). */
function usePrefersDark(): boolean {
  const [dark, setDark] = useState(
    () => typeof window !== "undefined" && window.matchMedia("(prefers-color-scheme: dark)").matches,
  );
  useEffect(() => {
    const mql = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (e: MediaQueryListEvent) => setDark(e.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);
  return dark;
}

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
  path,
  content,
  from,
  to,
}: {
  path: string | null;
  content: string;
  from: number | null;
  to: number | null;
}) {
  const dark = usePrefersDark();
  const editorRef = useRef<MonacoEditorNS.IStandaloneCodeEditor | null>(null);
  const decorationsRef = useRef<MonacoEditorNS.IEditorDecorationsCollection | null>(null);

  const highlightSpan = useCallback(
    (editor: MonacoEditorNS.IStandaloneCodeEditor) => {
      decorationsRef.current?.clear();
      if (from === null) return;
      decorationsRef.current = editor.createDecorationsCollection([
        {
          range: { startLineNumber: from, startColumn: 1, endLineNumber: to ?? from, endColumn: 1 },
          options: { isWholeLine: true, className: "codeaxis-symbol-span-line" },
        },
      ]);
      editor.revealLineInCenter(from);
    },
    [from, to],
  );

  const handleMount: OnMount = (editor) => {
    editorRef.current = editor;
    highlightSpan(editor);
  };

  // SourceView is remounted (keyed by node id) whenever the selected node
  // changes, so this only re-runs within one node's own lifetime — it
  // exists for completeness, not because `from`/`to` are expected to change
  // under a stable key.
  useEffect(() => {
    if (editorRef.current) highlightSpan(editorRef.current);
  }, [highlightSpan]);

  return (
    <div className="overflow-hidden rounded-md border border-line">
      <Editor
        height="60vh"
        language={monacoLanguage(path)}
        value={content}
        theme={dark ? "vs-dark" : "light"}
        onMount={handleMount}
        options={{
          readOnly: true,
          domReadOnly: true,
          minimap: { enabled: false },
          fontSize: 12,
          fontFamily: "ui-monospace, SFMono-Regular, Cascadia Code, Consolas, monospace",
          scrollBeyondLastLine: false,
          renderLineHighlight: from === null ? "line" : "none",
          wordWrap: "off",
        }}
      />
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

function citationLabel(c: Citation): string {
  const lines = c.line_start === c.line_end ? `${c.line_start}` : `${c.line_start}–${c.line_end}`;
  return `${c.path}:${lines}`;
}

/** The stored grounded explanation for this node, or an honest account of
 * why there is none. Three things this never does: generate prose on
 * request (the server only serves what the summary pass wrote), present the
 * text as parsed fact (it is labelled by its `origin`), or show a sentence
 * without the spans it was written from. */
function PurposeSection({ load }: { load: Load<Explanation> }) {
  if (load.state === "loading") {
    return <p className="text-xs text-muted">Loading explanation…</p>;
  }
  if (load.state === "error") {
    return (
      <p className="rounded-md border border-dashed border-line px-3 py-2 text-xs text-muted">
        {load.notFound ? load.message : `Couldn't load the explanation: ${load.message}`}
      </p>
    );
  }

  const e = load.value;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded border border-violet-600/40 bg-violet-500/10 px-1.5 py-0.5 text-[11px] font-medium text-violet-700 dark:text-violet-300">
          {e.origin === "inferred" ? "AI observation (inferred)" : e.origin}
        </span>
        <span className="text-[11px] text-muted">
          {e.level} scope · {e.analyzer} {e.analyzer_version}
          {e.model && ` · ${e.model}`}
        </span>
      </div>
      <p className="text-sm">{e.summary}</p>
      {e.citations.length === 0 ? (
        <p className="text-[11px] text-amber-700 dark:text-amber-400">
          Stored without citations — nothing here is backed by a source span.
        </p>
      ) : (
        <ul className="flex flex-wrap gap-1.5">
          {e.citations.map((c, i) => (
            <li
              key={i}
              className="rounded border border-line bg-panel px-1.5 py-0.5 font-mono text-[11px] text-muted"
              title="A span of this snapshot the explanation was written from."
            >
              {citationLabel(c)}
            </li>
          ))}
        </ul>
      )}
      <p className="text-[11px] text-muted">
        Written from {e.evidence.chunk_ids.length} indexed chunk
        {e.evidence.chunk_ids.length === 1 ? "" : "s"}
        {e.evidence.children.length > 0 &&
          ` and ${e.evidence.children.length} child summar${
            e.evidence.children.length === 1 ? "y" : "ies"
          }`}
        {e.evidence.truncated && ", truncated to fit the input budget"}. Static analysis only —
        nothing was executed.
      </p>
    </div>
  );
}

export function Inspector({
  repoId,
  commitSha,
  node,
  onSelectNode,
}: {
  repoId: string;
  commitSha: string;
  node: GraphNode;
  /** Lets a relationship entry's node become the next selection, reusing
   * whatever selection state the parent (Workspace) already holds — same
   * wiring as Explorer's onSelect, just triggered from inside the
   * Inspector instead of the tree. */
  onSelectNode?: (node: GraphNode) => void;
}) {
  // The parent keys this component by node id, so each selection mounts a
  // fresh Inspector and these initial "loading" states are never stale.
  const nodeId = node.id;
  const path = node.path;
  const showsSource = (node.kind === "file" || node.kind === "symbol") && path !== null;
  const [facts, setFacts] = useState<Load<SymbolDetail>>({ state: "loading" });
  const [source, setSource] = useState<Load<FileContent> | null>(
    showsSource ? { state: "loading" } : null,
  );
  const [purpose, setPurpose] = useState<Load<Explanation>>({ state: "loading" });

  async function openNode(id: number) {
    if (!onSelectNode) return;
    try {
      const detail = await getSymbol(repoId, id, commitSha);
      onSelectNode(detail);
    } catch {
      // Best-effort navigation: if the fetch fails, stay on the current
      // selection rather than surfacing a second error state.
    }
  }

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
    if (nodeId === null) return;
    getNodeExplanation(repoId, nodeId, commitSha).then(
      (value) => !cancelled && setPurpose({ state: "loaded", value }),
      (err) => !cancelled && setPurpose(errorState(err)),
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
        <Section title="Purpose">
          <PurposeSection load={purpose} />
        </Section>
      </div>

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
                  path={source.value.path}
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
          <Section title="Control-flow summary">
            <NotYetComputed what="Control-flow summaries" />
          </Section>
        )}

        {(n.kind === "file" || n.kind === "symbol") && (
          <>
            <Section title="Incoming relationships">
              {facts.state === "loading" ? (
                <p className="text-xs text-muted">Loading…</p>
              ) : facts.state === "error" ? (
                <p className="text-xs text-muted">Couldn&apos;t load relationships.</p>
              ) : (
                <RelationshipList entries={facts.value.relationships.incoming} onOpenNode={openNode} />
              )}
            </Section>

            <Section title="Outgoing relationships">
              {facts.state === "loading" ? (
                <p className="text-xs text-muted">Loading…</p>
              ) : facts.state === "error" ? (
                <p className="text-xs text-muted">Couldn&apos;t load relationships.</p>
              ) : (
                <RelationshipList entries={facts.value.relationships.outgoing} onOpenNode={openNode} />
              )}
            </Section>

            {/* Impact reuses the outgoing-relationships data under an
                "if this changes" framing rather than a third fetch: what's
                one hop reachable from this node is exactly what could be
                affected by changing it. A separate impact endpoint would
                return the same edges today, so this avoids a duplicate
                round-trip; if impact analysis later needs multi-hop or
                different edge kinds, it should become its own fetch. */}
            <Section title="Impact">
              {facts.state === "loading" ? (
                <p className="text-xs text-muted">Loading…</p>
              ) : facts.state === "error" ? (
                <p className="text-xs text-muted">Couldn&apos;t load impact data.</p>
              ) : (
                <>
                  <p className="text-xs text-muted">
                    If this changes, these may be affected (one hop out, same edges as above):
                  </p>
                  <RelationshipList
                    entries={facts.value.relationships.outgoing}
                    onOpenNode={openNode}
                  />
                </>
              )}
            </Section>

            <Section title="Tests">
              {facts.state === "loading" ? (
                <p className="text-xs text-muted">Loading…</p>
              ) : facts.state === "error" ? (
                <p className="text-xs text-muted">Couldn&apos;t load test associations.</p>
              ) : (
                (() => {
                  const testEdges = [
                    ...facts.value.relationships.incoming,
                    ...facts.value.relationships.outgoing,
                  ].filter((e) => e.kind === "tests");
                  if (testEdges.length === 0) {
                    return (
                      <p className="text-xs text-muted">
                        No test association found for this node.
                      </p>
                    );
                  }
                  return (
                    <ul className="space-y-1.5">
                      {testEdges.map((e, i) => (
                        <li key={i} className="space-y-0.5 text-xs">
                          <div className="flex items-center gap-2">
                            <ResolutionBadge status={e.resolution_status} />
                            <span className="font-mono">{e.node?.name ?? "(unresolved)"}</span>
                          </div>
                          {typeof e.attrs.evidence === "string" && (
                            <p className="pl-1 text-muted">{e.attrs.evidence}</p>
                          )}
                        </li>
                      ))}
                    </ul>
                  );
                })()
              )}
            </Section>
          </>
        )}

        <Section title="History">
          {n.attrs.churn_commits !== undefined || n.attrs.recent_commits !== undefined ? (
            <div className="space-y-1.5 text-xs">
              {n.attrs.churn_commits !== undefined && (
                <p>
                  <span className="text-muted">Churn:</span>{" "}
                  {String(n.attrs.churn_commits)} commit(s) touched this file.
                </p>
              )}
              {Array.isArray(n.attrs.recent_commits) && n.attrs.recent_commits.length > 0 && (
                <ul className="space-y-1">
                  {(n.attrs.recent_commits as Array<Record<string, unknown>>).map((c, i) => (
                    <li key={i} className="font-mono">
                      {String(c.sha).slice(0, 8)} · {String(c.date)} · {String(c.subject)}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ) : (
            <p className="text-xs text-muted">
              No git history was recorded for this node (no `.git` directory at scan time, or it
              has never been committed).
            </p>
          )}
        </Section>

        <Section title="Execution context">
          {n.attrs.entry_point === true ? (
            <p className="text-xs">
              Detected entry point
              {n.attrs.entry_point_reason
                ? ` — ${
                    Array.isArray(n.attrs.entry_point_reason)
                      ? n.attrs.entry_point_reason.join(", ")
                      : String(n.attrs.entry_point_reason)
                  }.`
                : "."}
            </p>
          ) : (
            <p className="text-xs text-muted">Not a detected entry point.</p>
          )}
        </Section>
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
