import { Suspense } from "react";

import { Workspace } from "./Workspace";

export default function WorkspacePage() {
  return (
    <main className="flex h-[calc(100dvh-3rem)] min-h-0 flex-col">
      <Suspense fallback={<p className="p-6 text-sm text-muted">Opening the workspace…</p>}>
        <Workspace />
      </Suspense>
    </main>
  );
}
