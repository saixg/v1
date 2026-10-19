"use client";

import { useState } from "react";

import {
  ApiError,
  extractRequirements,
  isModelFailure,
  previewDocuments,
  type DocumentPreview,
  type ExtractionResult,
  type Task,
} from "@/lib/api";

import { ErrorLine, errMsg, Pill } from "./ContextBits";

/** Intake for one task: the stored intent (read-only, verbatim), optional
 * documents, and the two server calls that use them. The document check
 * stores nothing, and extraction needs the files sent again — said plainly
 * below, not left to be inferred. */
export function TaskIntake({
  task,
  onExtracted,
}: {
  task: Task;
  onExtracted: () => void;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [includeImages, setIncludeImages] = useState(false);
  const [checkContradictions, setCheckContradictions] = useState(true);
  const [preview, setPreview] = useState<DocumentPreview | null>(null);
  const [result, setResult] = useState<ExtractionResult | null>(null);
  const [busy, setBusy] = useState<"preview" | "extract" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const archived = task.status === "archived";

  async function check() {
    if (task.id === null || files.length === 0) return;
    setBusy("preview");
    setError(null);
    try {
      setPreview(await previewDocuments(task.id, files, includeImages));
    } catch (err) {
      setPreview(null);
      setError(errMsg(err));
    } finally {
      setBusy(null);
    }
  }

  async function extract() {
    if (task.id === null) return;
    setBusy("extract");
    setError(null);
    try {
      setResult(await extractRequirements(task.id, files, { includeImages, checkContradictions }));
      onExtracted();
    } catch (err) {
      if (err instanceof ApiError && isModelFailure(err.detail)) {
        setError(
          `Extraction failed (${err.detail.error}): ${err.detail.detail}. The task's previous requirements were left as they were.`,
        );
      } else {
        setError(errMsg(err));
      }
    } finally {
      setBusy(null);
    }
  }

  return (
    <section aria-label="Task intake" className="space-y-3">
      <h3 className="text-sm font-semibold">Task intake</h3>

      <div className="space-y-1">
        <p className="text-xs font-medium text-muted">Intent as written (untrusted text, shown verbatim)</p>
        {task.intent ? (
          <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded bg-panel p-2 text-xs">
            {task.intent}
          </pre>
        ) : (
          <p className="rounded-md border border-dashed border-line px-3 py-2 text-xs text-muted">
            No intent text was entered. Only the title is available to extraction.
          </p>
        )}
      </div>

      <div className="space-y-2">
        <label className="block text-xs font-medium text-muted" htmlFor="intake-files">
          Attach documents (optional)
        </label>
        <input
          id="intake-files"
          type="file"
          multiple
          disabled={archived}
          onChange={(e) => {
            setFiles(Array.from(e.target.files ?? []));
            setPreview(null);
          }}
          className="block w-full text-xs"
        />
        <p className="text-[11px] text-muted">
          Documents are not saved. Checking one only previews what the server can read from it;
          extraction needs the files attached again each time it runs, and keeps only the
          requirements it extracts.
        </p>
        <label className="flex items-start gap-2 text-xs">
          <input
            type="checkbox"
            checked={includeImages}
            onChange={(e) => setIncludeImages(e.target.checked)}
            className="mt-0.5"
          />
          <span>
            Allow images
            <span className="block text-[11px] text-muted">
              Off by default. If on, an attached image is sent to a vision model, which can read
              text the secret scanner cannot. Images are refused unless this is checked.
            </span>
          </span>
        </label>
        <label className="flex items-center gap-2 text-xs">
          <input
            type="checkbox"
            checked={checkContradictions}
            onChange={(e) => setCheckContradictions(e.target.checked)}
          />
          Also ask a model to look for contradicting requirements
        </label>
      </div>

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={check}
          disabled={archived || busy !== null || files.length === 0}
          className="rounded-md border border-line px-3 py-1.5 text-xs font-medium hover:bg-panel disabled:opacity-50"
        >
          {busy === "preview" ? "Checking…" : "Check documents (stores nothing)"}
        </button>
        <button
          type="button"
          onClick={extract}
          disabled={archived || busy !== null}
          className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
        >
          {busy === "extract" ? "Extracting…" : "Extract requirements"}
        </button>
      </div>
      {archived && <p className="text-xs text-muted">This task is archived and read-only.</p>}
      <ErrorLine message={error} />

      {preview && (
        <div className="space-y-1 rounded-md border border-line p-3 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <Pill tone="warn">stored: {String(preview.stored)}</Pill>
            <span className="text-muted">Preview only. Nothing from these files was saved.</span>
          </div>
          <ul className="space-y-0.5">
            {preview.documents.map((d) => (
              <li key={d.name}>
                <span className="font-mono">{d.name}</span> · {d.chunks} chunk
                {d.chunks === 1 ? "" : "s"} · {d.secrets_redacted} secret
                {d.secrets_redacted === 1 ? "" : "s"} redacted
              </li>
            ))}
            {preview.images.map((i) => (
              <li key={`img-${i.chunk_id}`}>
                <span className="font-mono">{i.name}</span> · image, will be sent to a vision model
              </li>
            ))}
          </ul>
          {preview.unreadable.length > 0 && (
            <p className="text-amber-700 dark:text-amber-400">
              Could not be read: {JSON.stringify(preview.unreadable)}
            </p>
          )}
        </div>
      )}

      {result && <ExtractionReport result={result} />}
    </section>
  );
}

function ExtractionReport({ result }: { result: ExtractionResult }) {
  return (
    <div className="space-y-2 rounded-md border border-line p-3 text-xs">
      <p className="font-medium">
        Extraction finished: {result.requirements.length} requirement
        {result.requirements.length === 1 ? "" : "s"} stored, task is {result.task_status}.
      </p>
      <p className="text-muted">
        {result.secrets_redacted} secret{result.secrets_redacted === 1 ? "" : "s"} redacted from
        the input before it reached the model. Contradiction check:{" "}
        {result.contradiction_check === "ran"
          ? "ran"
          : result.contradiction_check === "skipped_by_caller"
            ? "skipped (you turned it off), so contradictions were not looked for"
            : result.contradiction_check}
        .
      </p>

      {result.dropped.length > 0 && (
        <div>
          <p className="font-medium text-amber-700 dark:text-amber-400">
            {result.dropped.length} model output{result.dropped.length === 1 ? "" : "s"} dropped
            (not stored)
          </p>
          <ul className="mt-1 space-y-0.5">
            {result.dropped.map((d, i) => (
              <li key={i} className="text-muted">
                {d.key ?? "unkeyed"} · {d.kind} · {d.reason}: {d.detail}
              </li>
            ))}
          </ul>
        </div>
      )}

      {result.contradictions.length > 0 && (
        <div>
          <p className="font-medium text-rose-700 dark:text-rose-300">
            Contradictions found by a model (AI observation)
          </p>
          <ul className="mt-1 space-y-0.5">
            {result.contradictions.map((c, i) => (
              <li key={i}>
                <span className="font-mono">
                  {c.first} vs {c.second}
                </span>{" "}
                <span className="text-muted">{c.explanation}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {result.constraint_conflicts.length > 0 && (
        <div>
          <p className="font-medium text-rose-700 dark:text-rose-300">
            Requirements touching constrained paths
          </p>
          <ul className="mt-1 space-y-0.5">
            {result.constraint_conflicts.map((c, i) => (
              <li key={i} className="text-muted">
                <span className="font-mono text-foreground">{c.requirement_key}</span> mentions{" "}
                <span className="font-mono">{c.path}</span>, within {c.severity}{" "}
                {c.constraint_kind} constraint <span className="font-mono">{c.scope}</span>
                {c.confirmed ? "" : " (not yet confirmed)"}
              </li>
            ))}
          </ul>
        </div>
      )}

      {result.unreadable.length > 0 && (
        <p className="text-amber-700 dark:text-amber-400">
          Not readable, so not used: {JSON.stringify(result.unreadable)}
        </p>
      )}
    </div>
  );
}
