"use client";

import { ApiError, type Citation, type ContextEvidence, type OriginTag } from "@/lib/api";

import { CitationChip } from "./AskView";

// Small pieces shared by the Context tab's panels. Everything rendered here
// is repository, document or model text and therefore untrusted: it is only
// ever placed in React text nodes (escaped), never as HTML or markdown.

export function errMsg(err: unknown): string {
  if (err instanceof ApiError || err instanceof Error) return err.message;
  return String(err);
}

/** Same treatment as the Inspector's purpose section: `inferred` is read as
 * an AI observation, never as a parsed fact. */
export function OriginBadge({ origin }: { origin: OriginTag }) {
  if (origin === "inferred") {
    return (
      <span
        className="rounded border border-violet-600/40 bg-violet-500/10 px-1.5 py-0.5 text-[11px] font-medium text-violet-700 dark:text-violet-300"
        title="A model's observation about the input. It was not parsed out of source."
      >
        AI observation (inferred)
      </span>
    );
  }
  return (
    <span
      className="rounded border border-line bg-panel px-1.5 py-0.5 text-[11px] font-medium text-muted"
      title={
        origin === "configured"
          ? "An explicit rule: read from a config file or stated by a person."
          : "Read out of source by a deterministic analyzer."
      }
    >
      {origin}
    </span>
  );
}

export function Pill({
  tone,
  children,
  title,
}: {
  tone: "neutral" | "warn" | "bad" | "ok";
  children: React.ReactNode;
  title?: string;
}) {
  const cls = {
    neutral: "border-line bg-panel text-muted",
    warn: "border-amber-600/40 bg-amber-500/10 text-amber-700 dark:text-amber-400",
    bad: "border-rose-600/40 bg-rose-500/10 text-rose-700 dark:text-rose-300",
    ok: "border-emerald-600/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  }[tone];
  return (
    <span className={`rounded border px-1.5 py-0.5 text-[11px] font-medium ${cls}`} title={title}>
      {children}
    </span>
  );
}

export function ErrorLine({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p role="alert" className="text-xs text-rose-700 dark:text-rose-300">
      {message}
    </p>
  );
}

/** Spans (clickable only when they carry a node id), then document refs. An
 * empty evidence block is stated, not hidden. */
export function EvidenceList({
  evidence,
  onOpen,
}: {
  evidence: ContextEvidence;
  onOpen?: (citation: Citation) => void;
}) {
  const docs = evidence.documents ?? [];
  if (evidence.spans.length === 0 && docs.length === 0) {
    return <p className="text-[11px] text-muted">No source span or document reference recorded.</p>;
  }
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {evidence.spans.map((c, i) => (
        <CitationChip key={`s${i}`} citation={c} onOpen={onOpen} />
      ))}
      {docs.map((d, i) => (
        <span
          key={`d${i}`}
          className="rounded border border-line bg-panel px-1.5 py-0.5 text-[11px] text-muted"
          title="A place in an uploaded document or repo doc."
        >
          {[d.source, d.ref, d.page ? `p.${d.page}` : null, d.region].filter(Boolean).join(" · ")}
        </span>
      ))}
      {evidence.truncated && (
        <span className="text-[11px] text-amber-700 dark:text-amber-400">
          inputs were capped to fit a budget
        </span>
      )}
    </div>
  );
}
