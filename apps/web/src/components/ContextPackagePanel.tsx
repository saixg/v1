"use client";

import { useState } from "react";

import {
  ApiError,
  assembleContext,
  exportContext,
  isPackageNotCurrent,
  type Citation,
  type ContextItem,
  type ContextItemKind,
  type ContextPackageResponse,
  type ExportFormat,
  type ManifestSection,
  type PackageNotCurrent,
  type UnresolvedEntry,
} from "@/lib/api";

import { Stages } from "./AskView";
import { ErrorLine, errMsg, EvidenceList, OriginBadge, Pill } from "./ContextBits";

const SECTION_LABEL: Record<ContextItemKind, string> = {
  task_intent: "Task intent",
  constraint: "Constraints",
  requirement: "Requirements",
  architecture: "Architecture slice",
  code: "Relevant code",
  convention: "Conventions",
  test: "Relevant tests",
  limitation: "Known limitations",
  open_question: "Unresolved questions",
};

const EXPORTS: { format: ExportFormat; label: string }[] = [
  { format: "claude_md", label: "CLAUDE.md" },
  { format: "agents_md", label: "AGENTS.md" },
  { format: "context_md", label: "context.md" },
];

export function download(filename: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function UnresolvedRow({ u }: { u: UnresolvedEntry }) {
  const { kind, detail, ref, ...rest } = u;
  const exceeds = kind === "constraints_exceed_budget";
  return (
    <li
      className={`space-y-0.5 rounded-md border px-3 py-2 text-xs ${
        exceeds ? "border-rose-600/40 bg-rose-500/5" : "border-amber-600/40 bg-amber-500/5"
      }`}
    >
      <p className="font-mono font-medium">{kind}</p>
      {detail && <p className="whitespace-pre-wrap">{detail}</p>}
      {ref && <p className="font-mono text-muted">{ref}</p>}
      {Object.keys(rest).length > 0 && (
        <p className="font-mono text-[11px] text-muted">{JSON.stringify(rest)}</p>
      )}
    </li>
  );
}

function Section({
  kind,
  meta,
  items,
  onOpen,
}: {
  kind: ContextItemKind;
  meta: ManifestSection | undefined;
  items: ContextItem[];
  onOpen?: (citation: Citation) => void;
}) {
  return (
    <li className="rounded-md border border-line p-3">
      <details>
        <summary className="cursor-pointer text-sm font-medium">
          {SECTION_LABEL[kind] ?? kind}{" "}
          <span className="text-[11px] font-normal text-muted">
            {items.reduce((n, i) => n + i.token_count, 0)} tokens · {items.length} item
            {items.length === 1 ? "" : "s"}
            {meta &&
              (meta.truncatable
                ? ` · allocated ${meta.allocated ?? 0}, dropped ${meta.dropped}`
                : " · never truncated")}
          </span>
        </summary>
        <div className="mt-2 space-y-2">
          {meta && meta.dropped > 0 && (
            <div className="rounded border border-amber-600/40 bg-amber-500/5 px-2 py-1 text-[11px]">
              <p className="text-amber-700 dark:text-amber-400">
                {meta.dropped} candidate{meta.dropped === 1 ? "" : "s"} did not fit this
                section&apos;s budget and {meta.dropped === 1 ? "was" : "were"} left out:
              </p>
              <ul className="font-mono text-muted">
                {meta.dropped_items.map((d, i) => (
                  <li key={i}>
                    {d.ref} ({d.tokens} tokens)
                  </li>
                ))}
              </ul>
            </div>
          )}
          {meta?.dropped_by_retrieval !== undefined && meta.dropped_by_retrieval > 0 && (
            <p className="text-[11px] text-amber-700 dark:text-amber-400">
              Retrieval itself dropped {meta.dropped_by_retrieval} more for its own budget.
            </p>
          )}
          {items.length === 0 && (
            <p className="text-xs text-muted">Nothing was included in this section.</p>
          )}
          {items.map((item) => (
            <div key={item.id ?? item.ordinal} className="space-y-1 border-t border-line pt-2">
              <div className="flex flex-wrap items-center gap-2">
                <OriginBadge origin={item.origin} />
                <span className="text-[11px] text-muted">
                  #{item.ordinal} · {item.token_count} tokens
                </span>
              </div>
              <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded bg-panel p-2 font-mono text-[11px] leading-relaxed">
                {item.content}
              </pre>
              <EvidenceList evidence={item.evidence} onOpen={onOpen} />
            </div>
          ))}
        </div>
      </details>
    </li>
  );
}

/** Assemble, inspect and export the task's context package. A package whose
 * freshness is not `current`, or whose status is not `assembled`, is badged
 * and never presented as ready to hand to an agent. */
export function ContextPackagePanel({
  taskId,
  archived,
  pkg,
  loadError,
  onAssembled,
  onOpenCitation,
}: {
  taskId: string;
  archived: boolean;
  pkg: ContextPackageResponse | null;
  loadError: string | null;
  onAssembled: (pkg: ContextPackageResponse) => void;
  onOpenCitation?: (citation: Citation) => void;
}) {
  const [budget, setBudget] = useState(12000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<ExportFormat | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<{ format: ExportFormat; detail: PackageNotCurrent } | null>(
    null,
  );
  const [exportNote, setExportNote] = useState<string | null>(null);

  async function assemble() {
    setBusy(true);
    setError(null);
    try {
      onAssembled(await assembleContext(taskId, budget));
      setRefusal(null);
      setExportNote(null);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  async function doExport(format: ExportFormat, allowStale: boolean) {
    setExporting(format);
    setExportError(null);
    setExportNote(null);
    try {
      const file = await exportContext(taskId, format, allowStale);
      download(file.filename, file.markdown);
      setRefusal(null);
      setExportNote(
        `Downloaded ${file.filename} (package ${file.packageStatus ?? "status unknown"}, snapshot ${file.snapshot ?? "unknown"}).` +
          (file.packageStatus && file.packageStatus !== "assembled"
            ? " The file carries a STALE/INVALID banner."
            : ""),
      );
    } catch (err) {
      if (err instanceof ApiError && err.status === 409 && isPackageNotCurrent(err.detail)) {
        setRefusal({ format, detail: err.detail });
      } else {
        setExportError(errMsg(err));
      }
    } finally {
      setExporting(null);
    }
  }

  const p = pkg?.package ?? null;
  const usable = pkg !== null && p !== null && pkg.freshness.current && p.status === "assembled";
  const sections = p?.manifest.sections ?? [];
  const items = pkg?.items ?? [];
  const known = new Set<ContextItemKind>(sections.map((s) => s.kind));
  const extraKinds = Array.from(new Set(items.map((i) => i.kind))).filter((k) => !known.has(k));
  const order: ContextItemKind[] = [...sections.map((s) => s.kind), ...extraKinds];
  const b = p?.retrieval;
  const retrievalBudget =
    b &&
    b.token_budget !== undefined &&
    b.tokens_used !== undefined &&
    b.dropped_for_budget !== undefined &&
    b.fused_candidates !== undefined
      ? {
          token_budget: b.token_budget,
          tokens_used: b.tokens_used,
          dropped_for_budget: b.dropped_for_budget,
          fused_candidates: b.fused_candidates,
        }
      : null;

  return (
    <section aria-label="Context package" className="space-y-3">
      <h3 className="text-sm font-semibold">Context package</h3>

      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs text-muted">
          Token budget (500 to 60,000)
          <input
            type="number"
            min={500}
            max={60000}
            step={500}
            value={budget}
            onChange={(e) => setBudget(Number(e.target.value))}
            className="mt-0.5 block w-32 rounded-md border border-line bg-transparent px-2 py-1.5 text-sm"
          />
        </label>
        <button
          type="button"
          onClick={assemble}
          disabled={busy || archived || budget < 500 || budget > 60000}
          className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
        >
          {busy ? "Assembling…" : pkg ? "Assemble a new version" : "Assemble package"}
        </button>
      </div>
      <p className="text-[11px] text-muted">
        Assembly grounds the task in the code graph and retrieval index, then packs sections in a
        fixed order. Everything is static analysis of the snapshot; nothing was run.
      </p>
      <ErrorLine message={error} />
      <ErrorLine message={loadError} />

      {pkg === null || p === null ? (
        !loadError && (
          <p className="rounded-md border border-dashed border-line px-3 py-2 text-xs text-muted">
            No context package has been assembled for this task yet.
          </p>
        )
      ) : (
        <div className="space-y-3">
          <div
            className={`space-y-1 rounded-md border px-3 py-2 text-xs ${
              usable ? "border-line" : "border-rose-600/40 bg-rose-500/5"
            }`}
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">Version {p.version}</span>
              {usable ? (
                <Pill tone="ok">current</Pill>
              ) : p.status === "invalid" ? (
                <Pill tone="bad">INVALID</Pill>
              ) : (
                <Pill tone="bad">STALE</Pill>
              )}
              <span className="font-mono text-muted">{p.commit_sha}</span>
              <span className="text-muted">
                {p.token_count} of {p.token_budget} tokens
              </span>
            </div>
            {!usable && (
              <p className="text-rose-700 dark:text-rose-300">
                Not current. Do not hand this to an agent.{" "}
                {pkg.freshness.reason ?? `Package status is ${p.status}.`} Assemble a new version.
              </p>
            )}
            {p.manifest.dropped_total !== undefined && (
              <p className="text-muted">
                {p.manifest.dropped_total === 0
                  ? "Nothing was dropped for budget."
                  : `${p.manifest.dropped_total} candidate${p.manifest.dropped_total === 1 ? "" : "s"} dropped for budget.`}
              </p>
            )}
          </div>

          {p.unresolved.length > 0 ? (
            <div className="space-y-1">
              <h4 className="text-xs font-medium text-muted">
                Unresolved ({p.unresolved.length})
              </h4>
              <ul className="space-y-1.5">
                {p.unresolved.map((u, i) => (
                  <UnresolvedRow key={i} u={u} />
                ))}
              </ul>
            </div>
          ) : (
            <p className="text-[11px] text-muted">The assembler recorded no unresolved entries.</p>
          )}

          <div className="space-y-1">
            <h4 className="text-xs font-medium text-muted">Retrieval stages</h4>
            {b?.stages && b.stages.length > 0 ? (
              retrievalBudget ? (
                <Stages stages={b.stages} budget={retrievalBudget} />
              ) : (
                <ul className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted">
                  {b.stages.map((s) => (
                    <li key={s.name}>
                      <span className="font-medium text-foreground">{s.name}</span>{" "}
                      {s.status === "ran" ? `ran · ${s.hits} hits` : `skipped${s.reason ? ` · ${s.reason}` : ""}`}
                    </li>
                  ))}
                </ul>
              )
            ) : (
              <p className="text-[11px] text-muted">
                No retrieval stage outcomes were recorded for this package.
              </p>
            )}
          </div>

          <div className="space-y-1">
            <h4 className="text-xs font-medium text-muted">Sections, in package order</h4>
            <ol className="space-y-2">
              {order.map((kind) => (
                <Section
                  key={kind}
                  kind={kind}
                  meta={sections.find((s) => s.kind === kind)}
                  items={items.filter((i) => i.kind === kind).sort((a, c) => a.ordinal - c.ordinal)}
                  onOpen={onOpenCitation}
                />
              ))}
            </ol>
          </div>

          <div className="space-y-2">
            <h4 className="text-xs font-medium text-muted">Export</h4>
            <div className="flex flex-wrap gap-2">
              {EXPORTS.map((e) => (
                <button
                  key={e.format}
                  type="button"
                  disabled={exporting !== null}
                  onClick={() => doExport(e.format, false)}
                  className="rounded-md border border-line px-3 py-1.5 text-xs font-medium hover:bg-panel disabled:opacity-50"
                >
                  {exporting === e.format ? "Fetching…" : `Download ${e.label}`}
                </button>
              ))}
            </div>
            {refusal && (
              <div className="space-y-1 rounded-md border border-rose-600/40 bg-rose-500/5 p-3 text-xs">
                <p className="font-medium text-rose-700 dark:text-rose-300">
                  Export refused: the package is {refusal.detail.package_status}.
                </p>
                <p>{refusal.detail.reason}</p>
                <p className="text-muted">{refusal.detail.detail}</p>
                <button
                  type="button"
                  disabled={exporting !== null}
                  onClick={() => doExport(refusal.format, true)}
                  className="rounded-md border border-rose-600/40 px-2.5 py-1 font-medium text-rose-700 hover:bg-rose-500/10 disabled:opacity-50 dark:text-rose-300"
                >
                  Export anyway with a STALE/INVALID banner
                </button>
              </div>
            )}
            {exportNote && <p className="text-[11px] text-muted">{exportNote}</p>}
            <ErrorLine message={exportError} />
          </div>
        </div>
      )}
    </section>
  );
}
