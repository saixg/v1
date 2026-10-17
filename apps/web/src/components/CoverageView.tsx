"use client";

import { useEffect, useState } from "react";

import { getCoverage, type CoverageResponse, type ResolutionStatus } from "@/lib/api";

type Load<T> =
  | { state: "loading" }
  | { state: "loaded"; value: T }
  | { state: "error"; message: string };

function errorState(err: unknown): Load<never> {
  return { state: "error", message: err instanceof Error ? err.message : String(err) };
}

const RESOLUTION_ORDER: ResolutionStatus[] = ["resolved", "ambiguous", "unresolved", "external"];

const RESOLUTION_TEXT: Record<ResolutionStatus, string> = {
  resolved: "text-emerald-700 dark:text-emerald-400",
  ambiguous: "text-amber-700 dark:text-amber-400",
  unresolved: "text-rose-700 dark:text-rose-300",
  external: "text-sky-700 dark:text-sky-400",
};

/** File/metric coverage counters and the edge-level resolution breakdown
 * for one repo at one commit — build-plan.md Part 10's "coverage/unresolved
 * view": analysis coverage and unresolved relationships, visible rather
 * than hidden. Standalone (does not touch Explorer.tsx), fetched once per
 * repo+commit. */
export function CoverageView({ repoId, commitSha }: { repoId: string; commitSha: string }) {
  const [data, setData] = useState<Load<CoverageResponse>>({ state: "loading" });

  useEffect(() => {
    let cancelled = false;
    getCoverage(repoId, commitSha).then(
      (value) => !cancelled && setData({ state: "loaded", value }),
      (err) => !cancelled && setData(errorState(err)),
    );
    return () => {
      cancelled = true;
    };
  }, [repoId, commitSha]);

  if (data.state === "loading") {
    return <p className="p-4 text-xs text-muted">Loading coverage…</p>;
  }
  if (data.state === "error") {
    return (
      <p className="p-4 text-xs text-rose-700 dark:text-rose-300">
        Couldn&apos;t load coverage: {data.message}
      </p>
    );
  }

  const { coverage, unresolved } = data.value;
  const kinds = Array.from(new Set(unresolved.map((r) => r.kind))).sort();

  return (
    <div className="space-y-6 p-5 text-sm">
      <section className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">
          Analysis coverage
        </h3>
        {coverage.length === 0 ? (
          <p className="text-xs text-muted">No coverage counters recorded for this commit.</p>
        ) : (
          <ul className="space-y-1">
            {coverage.map((row) => (
              <li key={`${row.area}:${row.metric}`} className="font-mono text-xs">
                <span className="text-muted">
                  {row.area} · {row.metric}:
                </span>{" "}
                {row.resolved}/{row.total}
                {Object.keys(row.blockers).length > 0 && (
                  <span className="text-muted">
                    {" "}
                    ({Object.entries(row.blockers)
                      .map(([reason, count]) => `${count} ${reason}`)
                      .join(", ")})
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">
          Unresolved relationships
        </h3>
        {unresolved.length === 0 ? (
          <p className="text-xs text-muted">No edges recorded for this commit.</p>
        ) : (
          <table className="w-full border-collapse text-xs">
            <thead>
              <tr className="border-b border-line text-left text-muted">
                <th className="py-1 pr-3 font-medium">Edge kind</th>
                {RESOLUTION_ORDER.map((status) => (
                  <th key={status} className="py-1 pr-3 font-medium capitalize">
                    {status}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {kinds.map((kind) => {
                const byStatus = new Map(
                  unresolved
                    .filter((r) => r.kind === kind)
                    .map((r) => [r.resolution_status, r.count] as const),
                );
                return (
                  <tr key={kind} className="border-b border-line/50">
                    <td className="py-1 pr-3 font-mono">{kind}</td>
                    {RESOLUTION_ORDER.map((status) => (
                      <td key={status} className={`py-1 pr-3 ${RESOLUTION_TEXT[status]}`}>
                        {byStatus.get(status) ?? 0}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
