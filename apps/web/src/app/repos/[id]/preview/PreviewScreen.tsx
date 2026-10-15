"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useMemo, useSyncExternalStore } from "react";

import { ExclusionPreview } from "@/components/ExclusionPreview";
import { recallStagedRaw, type StageRepoResponse } from "@/lib/api";

const noSubscribe = () => () => {};

export function PreviewScreen() {
  const { id } = useParams<{ id: string }>();
  // undefined on the server (no sessionStorage there); null when this tab has
  // no record of the import.
  const raw = useSyncExternalStore(
    noSubscribe,
    () => recallStagedRaw(id),
    () => undefined,
  );
  const staged = useMemo<StageRepoResponse | null | undefined>(() => {
    if (raw === undefined || raw === null) return raw;
    try {
      return JSON.parse(raw) as StageRepoResponse;
    } catch {
      return null;
    }
  }, [raw]);

  if (staged === undefined) {
    return <p className="text-sm text-muted">Loading the file list…</p>;
  }

  if (staged === null) {
    return (
      <div className="rounded-lg border border-line bg-panel p-4 text-sm">
        <p>
          The file list for this import isn&apos;t available in this browser tab. It is only kept
          for the tab that started the import.
        </p>
        <Link href="/" className="mt-3 inline-block font-medium text-accent">
          Import the codebase again
        </Link>
      </div>
    );
  }

  return <ExclusionPreview repoId={id} commitSha={staged.commit_sha} entries={staged.preview} />;
}
