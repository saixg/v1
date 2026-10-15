"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { rememberStaged, stageRepo, uploadZip, type StageRepoResponse } from "@/lib/api";

type Mode = "github" | "local" | "zip";

const TABS: { mode: Mode; label: string }[] = [
  { mode: "github", label: "GitHub URL" },
  { mode: "local", label: "Local folder" },
  { mode: "zip", label: "ZIP archive" },
];

const inputClass =
  "w-full rounded-md border border-line bg-background px-3 py-2 text-sm outline-none focus:border-accent focus:ring-2 focus:ring-accent/20";

export function ImportForm() {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("github");
  const [remoteUrl, setRemoteUrl] = useState("");
  const [ref, setRef] = useState("");
  const [localPath, setLocalPath] = useState("");
  const [commitSha, setCommitSha] = useState("");
  const [zipFile, setZipFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      let staged: StageRepoResponse;
      if (mode === "github") {
        staged = await stageRepo({
          kind: "github",
          remote_url: remoteUrl.trim(),
          ref: ref.trim() || null,
        });
      } else if (mode === "local") {
        staged = await stageRepo({
          kind: "local",
          local_path: localPath.trim(),
          commit_sha: commitSha.trim(),
        });
      } else {
        if (!zipFile) throw new Error("Choose a .zip file to upload.");
        staged = await uploadZip(zipFile);
      }
      rememberStaged(staged);
      router.push(
        `/repos/${staged.id}/preview?commit_sha=${encodeURIComponent(staged.commit_sha)}`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  const canSubmit =
    !busy &&
    (mode === "github"
      ? remoteUrl.trim() !== ""
      : mode === "local"
        ? localPath.trim() !== "" && commitSha.trim() !== ""
        : zipFile !== null);

  return (
    <form onSubmit={onSubmit} className="space-y-5">
      <div role="tablist" className="flex gap-1 rounded-lg border border-line bg-panel p-1">
        {TABS.map((t) => (
          <button
            key={t.mode}
            type="button"
            role="tab"
            aria-selected={mode === t.mode}
            onClick={() => {
              setMode(t.mode);
              setError(null);
            }}
            className={`flex-1 rounded-md px-3 py-1.5 text-sm ${
              mode === t.mode ? "bg-background font-medium shadow-sm" : "text-muted hover:text-foreground"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {mode === "github" && (
        <div className="space-y-4">
          <label className="block space-y-1.5">
            <span className="text-sm font-medium">Repository URL</span>
            <input
              className={inputClass}
              type="url"
              required
              placeholder="https://github.com/owner/repo"
              value={remoteUrl}
              onChange={(e) => setRemoteUrl(e.target.value)}
            />
          </label>
          <label className="block space-y-1.5">
            <span className="text-sm font-medium">
              Branch, tag or commit <span className="font-normal text-muted">(optional)</span>
            </span>
            <input
              className={inputClass}
              placeholder="default branch"
              value={ref}
              onChange={(e) => setRef(e.target.value)}
            />
            <span className="block text-xs text-muted">
              The analysis is pinned to the exact commit this resolves to.
            </span>
          </label>
        </div>
      )}

      {mode === "local" && (
        <div className="space-y-4">
          <label className="block space-y-1.5">
            <span className="text-sm font-medium">Folder path on this machine</span>
            <input
              className={inputClass}
              required
              placeholder={"C:\\code\\my-project"}
              value={localPath}
              onChange={(e) => setLocalPath(e.target.value)}
            />
          </label>
          <label className="block space-y-1.5">
            <span className="text-sm font-medium">Version label</span>
            <input
              className={inputClass}
              required
              placeholder="e.g. a commit SHA or a name like working-copy"
              value={commitSha}
              onChange={(e) => setCommitSha(e.target.value)}
            />
            <span className="block text-xs text-muted">
              A local folder has no pinned commit, so you name the snapshot. Every result is
              keyed to this label.
            </span>
          </label>
        </div>
      )}

      {mode === "zip" && (
        <label className="block space-y-1.5">
          <span className="text-sm font-medium">ZIP archive</span>
          <input
            className={`${inputClass} file:mr-3 file:rounded file:border-0 file:bg-panel file:px-2 file:py-1 file:text-sm`}
            type="file"
            accept=".zip,application/zip"
            required
            onChange={(e) => setZipFile(e.target.files?.[0] ?? null)}
          />
        </label>
      )}

      {error && (
        <p role="alert" className="rounded-md border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-700 dark:text-rose-300">
          {error}
        </p>
      )}

      <button
        type="submit"
        disabled={!canSubmit}
        className="w-full rounded-md bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
      >
        {busy ? (mode === "github" ? "Cloning and listing files…" : "Listing files…") : "Review files"}
      </button>
    </form>
  );
}
