"use client";

import { useParams, useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { AnalysisProgress } from "@/components/AnalysisProgress";

function Importing() {
  const { id } = useParams<{ id: string }>();
  const commitSha = useSearchParams().get("commit_sha");
  if (!commitSha) {
    return <p className="text-sm text-muted">This link is missing its commit. Start the import again.</p>;
  }
  return <AnalysisProgress repoId={id} commitSha={commitSha} />;
}

export default function ImportingPage() {
  return (
    <main className="mx-auto w-full max-w-xl px-4 py-12">
      <h1 className="text-2xl font-semibold tracking-tight">Analyzing</h1>
      <div className="mt-6">
        <Suspense fallback={<p className="text-sm text-muted">Checking analysis status…</p>}>
          <Importing />
        </Suspense>
      </div>
    </main>
  );
}
