"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { confirmScan, type PreviewEntry } from "@/lib/api";

const REASON_LABELS: Record<string, string> = {
  excluded_dir: "Skipped folder (dependencies, build output or VCS data)",
  over_size_cap: "Too large to analyze",
};

function reasonLabel(reason: string | null): string {
  if (!reason) return "Not included";
  return REASON_LABELS[reason] ?? reason;
}

function formatBytes(n: number | null): string {
  if (n === null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// The scanner matches exclusions as fnmatch globs against folder paths. Escape
// glob metacharacters so a ticked folder excludes exactly that folder.
function globEscape(path: string): string {
  return path.replace(/[[*?]/g, (c) => `[${c}]`);
}

function ancestors(path: string): string[] {
  const parts = path.split("/");
  const out: string[] = [];
  for (let i = 1; i < parts.length; i++) out.push(parts.slice(0, i).join("/"));
  return out;
}

export function ExclusionPreview({
  repoId,
  commitSha,
  entries,
}: {
  repoId: string;
  commitSha: string;
  entries: PreviewEntry[];
}) {
  const router = useRouter();
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const files = useMemo(() => entries.filter((e) => e.kind === "file"), [entries]);
  const skippedDirs = useMemo(() => entries.filter((e) => e.kind === "directory"), [entries]);

  // Folders the scan would enter, with how many included files sit under each.
  const folders = useMemo(() => {
    const counts = new Map<string, number>();
    for (const f of files) {
      if (!f.included) continue;
      for (const dir of ancestors(f.path)) counts.set(dir, (counts.get(dir) ?? 0) + 1);
    }
    return [...counts.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [files]);

  const isUserExcluded = (path: string) => ancestors(path).some((d) => excluded.has(d));

  const includedCount = files.filter((f) => f.included && !isUserExcluded(f.path)).length;

  function toggle(dir: string) {
    setExcluded((prev) => {
      const next = new Set(prev);
      if (next.has(dir)) next.delete(dir);
      else next.add(dir);
      return next;
    });
  }

  async function start() {
    setBusy(true);
    setError(null);
    // Send only top-most ticked folders; nested ones are already covered.
    const roots = [...excluded].filter((d) => !ancestors(d).some((a) => excluded.has(a)));
    try {
      await confirmScan(repoId, commitSha, roots.map(globEscape));
      router.push(`/repos/${repoId}/importing?commit_sha=${encodeURIComponent(commitSha)}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-line bg-panel px-4 py-3">
        <p className="text-sm">
          <span className="font-semibold">{includedCount}</span> of {files.length} files will be
          analyzed at <code className="font-mono text-xs">{commitSha}</code>.
        </p>
        <button
          type="button"
          onClick={start}
          disabled={busy}
          className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          {busy ? "Starting analysis…" : "Start analysis"}
        </button>
      </div>

      {error && (
        <p role="alert" className="rounded-md border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-700 dark:text-rose-300">
          {error}
        </p>
      )}

      {folders.length > 0 && (
        <section>
          <h2 className="text-sm font-semibold">Exclude folders</h2>
          <p className="mt-1 text-xs text-muted">
            Tick a folder to leave it and everything under it out of the analysis. Exclusions apply
            to folders, not single files.
          </p>
          <ul className="mt-3 max-h-72 overflow-auto rounded-lg border border-line">
            {folders.map(([dir, count]) => {
              const coveredByParent = ancestors(dir).some((a) => excluded.has(a));
              return (
                <li key={dir} className="border-b border-line last:border-b-0">
                  <label className="flex cursor-pointer items-center gap-3 px-3 py-1.5 text-sm hover:bg-panel">
                    <input
                      type="checkbox"
                      checked={excluded.has(dir) || coveredByParent}
                      disabled={coveredByParent}
                      onChange={() => toggle(dir)}
                    />
                    <span className="font-mono text-xs" style={{ paddingLeft: `${(dir.split("/").length - 1) * 12}px` }}>
                      {dir.split("/").pop()}/
                    </span>
                    <span className="ml-auto text-xs text-muted">{count} files</span>
                  </label>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      <section>
        <h2 className="text-sm font-semibold">Files</h2>
        <div className="mt-3 max-h-[28rem] overflow-auto rounded-lg border border-line">
          <table className="w-full text-left text-xs">
            <thead className="sticky top-0 bg-panel text-muted">
              <tr>
                <th className="px-3 py-2 font-medium">Path</th>
                <th className="px-3 py-2 font-medium">Language</th>
                <th className="px-3 py-2 text-right font-medium">Size</th>
                <th className="px-3 py-2 font-medium">Outcome</th>
              </tr>
            </thead>
            <tbody>
              {files.map((f) => {
                const userExcluded = f.included && isUserExcluded(f.path);
                const included = f.included && !userExcluded;
                return (
                  <tr key={f.path} className={`border-t border-line ${included ? "" : "text-muted"}`}>
                    <td className="px-3 py-1.5 font-mono">{f.path}</td>
                    <td className="px-3 py-1.5">{f.language ?? "other"}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums">{formatBytes(f.size_bytes)}</td>
                    <td className="px-3 py-1.5">
                      {included ? "Included" : userExcluded ? "Excluded by you" : reasonLabel(f.reason)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {skippedDirs.length > 0 && (
          <p className="mt-3 text-xs text-muted">
            Always skipped: {skippedDirs.map((d) => d.path).join(", ")} ({reasonLabel("excluded_dir").toLowerCase()}).
          </p>
        )}
      </section>
    </div>
  );
}
