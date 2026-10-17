"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useState } from "react";

import { CoverageView } from "@/components/CoverageView";
import { Explorer } from "@/components/Explorer";
import { Inspector } from "@/components/Inspector";
import type { GraphNode } from "@/lib/api";

type Tab = "explore" | "coverage";

export function Workspace() {
  const { id } = useParams<{ id: string }>();
  const commitSha = useSearchParams().get("commit_sha");
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [tab, setTab] = useState<Tab>("explore");

  if (!commitSha) {
    return (
      <div className="p-6 text-sm">
        <p>This workspace link is missing its commit, so there is no graph to show.</p>
        <Link href="/" className="mt-3 inline-block font-medium text-accent">
          Open a codebase
        </Link>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-b border-line px-4 py-2 text-xs text-muted">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span>
            Static analysis at <code className="font-mono text-foreground">{commitSha}</code>
          </span>
          <span aria-hidden>·</span>
          <span>Pass 1: files and symbol definitions. Nothing was executed.</span>
        </div>
        <div className="flex gap-1" role="tablist" aria-label="Workspace view">
          {(["explore", "coverage"] as const).map((t) => (
            <button
              key={t}
              type="button"
              role="tab"
              aria-selected={tab === t}
              onClick={() => setTab(t)}
              className={`rounded px-2 py-1 font-medium capitalize ${
                tab === t ? "bg-accent/15 text-foreground" : "text-muted hover:bg-panel"
              }`}
            >
              {t === "explore" ? "Explore" : "Coverage"}
            </button>
          ))}
        </div>
      </div>
      {tab === "coverage" ? (
        <section aria-label="Coverage" className="min-h-0 flex-1 overflow-auto">
          <CoverageView repoId={id} commitSha={commitSha} />
        </section>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col md:flex-row">
          <aside className="max-h-[45vh] shrink-0 overflow-auto border-b border-line md:max-h-none md:w-80 md:border-b-0 md:border-r lg:w-96">
            <Explorer
              repoId={id}
              commitSha={commitSha}
              selectedId={selected?.id ?? null}
              onSelect={setSelected}
            />
          </aside>
          <section aria-label="Component Inspector" className="min-w-0 flex-1 overflow-auto">
            {selected ? (
              <Inspector
                key={selected.id ?? undefined}
                repoId={id}
                commitSha={commitSha}
                node={selected}
                onSelectNode={setSelected}
              />
            ) : (
              <div className="p-6 text-sm text-muted">
                <p className="font-medium text-foreground">Component Inspector</p>
                <p className="mt-1">
                  Select a file or a symbol in the Explorer to see its source and where each fact
                  came from.
                </p>
              </div>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
