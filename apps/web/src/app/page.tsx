import { auth } from "@clerk/nextjs/server";

import { ImportForm } from "@/components/ImportForm";

export default async function OpenCodebasePage() {
  await auth.protect();
  return (
    <main className="mx-auto w-full max-w-xl px-4 py-12">
      <h1 className="text-2xl font-semibold tracking-tight">Open a codebase</h1>
      <p className="mt-2 text-sm text-muted">
        Point CodeAxis at a repository. You will see exactly which files it plans to read before
        anything is analyzed.
      </p>

      <div className="mt-8">
        <ImportForm />
      </div>

      <section className="mt-8 rounded-lg border border-line bg-panel p-4 text-xs leading-5 text-muted">
        <h2 className="mb-1 font-medium text-foreground">What happens to the code</h2>
        <ul className="list-disc space-y-1 pl-4">
          <li>
            It is read and parsed statically by the CodeAxis worker. Nothing in it is installed,
            built or run.
          </li>
          <li>Nothing is sent to a language-model provider. This build has no narration.</li>
          <li>
            The file tree, the symbols found, and the text of files that were read are stored in
            the CodeAxis database configured for this install, so they can be shown back to you.
          </li>
          <li>
            GitHub repositories are cloned from GitHub. Temporary copies from GitHub and ZIP
            imports are deleted once the scan finishes. A local folder is never modified.
          </li>
        </ul>
      </section>
    </main>
  );
}
