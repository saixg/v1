"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { ApiError, deleteRepo } from "@/lib/api";

import { ErrorLine, errMsg } from "./ContextBits";

/** Permanently deletes this repository's analysis. The confirmation names
 * what is destroyed, because the delete is irreversible and the server does
 * not keep a recoverable copy. */
export function DeleteRepoControl({
  repoId,
  onDeleted,
}: {
  repoId: string;
  /** Defaults to navigating home; overridable so the control is testable. */
  onDeleted?: () => void;
}) {
  const router = useRouter();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function done() {
    if (onDeleted) onDeleted();
    else router.push("/");
  }

  async function remove() {
    setBusy(true);
    setError(null);
    try {
      await deleteRepo(repoId);
      done();
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        // Already gone (deleted in another tab): nothing left to protect.
        done();
        return;
      }
      if (err instanceof ApiError && err.status === 409) {
        setError(`Not deleted: ${err.message}. Try again once the scan has finished.`);
      } else {
        setError(errMsg(err));
      }
      setBusy(false);
    }
  }

  if (!confirming) {
    return (
      <div className="flex flex-col items-end gap-1">
        <button
          type="button"
          onClick={() => setConfirming(true)}
          className="rounded px-2 py-1 font-medium text-muted hover:bg-rose-500/10 hover:text-rose-700 dark:hover:text-rose-300"
        >
          Delete repository…
        </button>
      </div>
    );
  }

  return (
    <div
      role="alertdialog"
      aria-label="Delete this repository"
      className="max-w-sm space-y-2 rounded-md border border-rose-600/40 bg-rose-500/5 p-3 text-xs"
    >
      <p className="font-medium text-rose-700 dark:text-rose-300">
        Permanently delete this repository from CodeAxis?
      </p>
      <p>
        This destroys everything analyzed from it: the file tree, symbols and relationships,
        coverage, the stored text of scanned files, summaries and search index, chat threads, agent
        context tasks and packages, blueprints, and API tokens for it. It cannot be undone.
        Re-importing starts a fresh analysis. Your original source (the GitHub repository or local
        folder) is not touched.
      </p>
      <p className="text-muted">A scan that is still running blocks deletion.</p>
      <div className="flex gap-2">
        <button
          type="button"
          onClick={remove}
          disabled={busy}
          className="rounded-md border border-rose-600/40 px-2.5 py-1 font-medium text-rose-700 hover:bg-rose-500/10 disabled:opacity-50 dark:text-rose-300"
        >
          {busy ? "Deleting…" : "Delete permanently"}
        </button>
        <button
          type="button"
          onClick={() => {
            setConfirming(false);
            setError(null);
          }}
          disabled={busy}
          className="rounded-md border border-line px-2.5 py-1 hover:bg-panel disabled:opacity-50"
        >
          Keep it
        </button>
      </div>
      <ErrorLine message={error} />
    </div>
  );
}
