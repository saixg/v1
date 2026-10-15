"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { getStatus, type PassRun } from "@/lib/api";

const POLL_MS = 1000;

type View =
  | { phase: "waiting" }
  | { phase: "running"; run: PassRun }
  | { phase: "failed"; run: PassRun }
  | { phase: "succeeded"; run: PassRun };

export function AnalysisProgress({ repoId, commitSha }: { repoId: string; commitSha: string }) {
  const router = useRouter();
  const [view, setView] = useState<View>({ phase: "waiting" });
  const [pollError, setPollError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function poll() {
      try {
        const { runs } = await getStatus(repoId);
        if (cancelled) return;
        setPollError(null);
        // Runs come back oldest first; the latest one for this commit decides.
        const forCommit = runs.filter((r) => r.commit_sha === commitSha);
        const latest = forCommit[forCommit.length - 1];
        if (!latest) {
          setView({ phase: "waiting" });
        } else if (latest.status === "succeeded") {
          setView({ phase: "succeeded", run: latest });
          router.replace(`/repos/${repoId}?commit_sha=${encodeURIComponent(commitSha)}`);
          return;
        } else if (latest.status === "failed") {
          setView({ phase: "failed", run: latest });
          return;
        } else {
          setView({ phase: "running", run: latest });
        }
      } catch (err) {
        if (cancelled) return;
        setPollError(err instanceof Error ? err.message : String(err));
      }
      timer = setTimeout(poll, POLL_MS);
    }

    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [repoId, commitSha, router]);

  if (view.phase === "failed") {
    return (
      <div className="space-y-4">
        <div className="rounded-lg border border-rose-500/40 bg-rose-500/10 p-4">
          <p className="text-sm font-medium text-rose-700 dark:text-rose-300">The scan failed.</p>
          <p className="mt-2 text-xs text-muted">
            {view.run.analyzer} {view.run.analyzer_version} reported:
          </p>
          <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-xs">
            {view.run.error ?? "No error message was recorded."}
          </pre>
        </div>
        <Link href="/" className="inline-block text-sm font-medium text-accent">
          Import a codebase
        </Link>
      </div>
    );
  }

  const message =
    view.phase === "waiting"
      ? "Waiting for the analysis worker to pick up this import…"
      : view.phase === "running"
        ? `Scanning files and extracting symbols with ${view.run.analyzer} ${view.run.analyzer_version}…`
        : "Scan finished. Opening the workspace…";

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3 rounded-lg border border-line bg-panel p-4">
        <span aria-hidden className="h-2 w-2 animate-pulse rounded-full bg-accent motion-reduce:animate-none" />
        <p className="text-sm" role="status">
          {message}
        </p>
      </div>
      <p className="text-xs text-muted">
        Pass 1 of the static analysis: walk the files, parse each supported one, and record its
        functions and classes. Nothing is executed. This page checks progress every second.
      </p>
      {pollError && (
        <p role="alert" className="text-xs text-rose-700 dark:text-rose-300">
          Couldn&apos;t check progress: {pollError} Retrying…
        </p>
      )}
    </div>
  );
}
