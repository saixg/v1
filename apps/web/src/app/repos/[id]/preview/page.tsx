import { auth } from "@clerk/nextjs/server";
import { Suspense } from "react";

import { PreviewScreen } from "./PreviewScreen";

export default async function PreviewPage() {
  await auth.protect();
  return (
    <main className="mx-auto w-full max-w-4xl px-4 py-10">
      <h1 className="text-2xl font-semibold tracking-tight">Review what will be analyzed</h1>
      <p className="mt-2 text-sm text-muted">
        Nothing has been analyzed yet. Check the list, leave out any folders you don&apos;t want
        read, then start the analysis.
      </p>
      <div className="mt-8">
        <Suspense fallback={<p className="text-sm text-muted">Loading the file list…</p>}>
          <PreviewScreen />
        </Suspense>
      </div>
    </main>
  );
}
