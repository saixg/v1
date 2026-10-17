import { auth } from "@clerk/nextjs/server";

import { ImportingScreen } from "./ImportingScreen";

export default async function ImportingPage() {
  await auth.protect();
  return (
    <main className="mx-auto w-full max-w-xl px-4 py-12">
      <h1 className="text-2xl font-semibold tracking-tight">Analyzing</h1>
      <div className="mt-6">
        <ImportingScreen />
      </div>
    </main>
  );
}
