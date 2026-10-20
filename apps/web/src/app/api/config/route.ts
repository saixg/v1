// Runtime configuration for the browser bundle.
//
// Why this exists: Next.js inlines `NEXT_PUBLIC_*` at build time, so pointing
// the app at a different backend would otherwise mean a rebuild. This route
// reads a plain server-side variable instead, which a deployment can change
// and have take effect on the next request — no rebuild, no new bundle.
//
// Deliberately unauthenticated. `src/proxy.ts` runs `clerkMiddleware()` over
// this path but that only exposes auth state; it does not gate. Nothing here
// is a secret: the API's base URL is visible in every network request the
// browser makes anyway. Nothing else may be added to this payload without
// re-reading that sentence.
import { NextResponse } from "next/server";

// Never prerendered or cached: a build-time snapshot of a runtime value is
// exactly the thing this route exists to avoid.
export const dynamic = "force-dynamic";
export const revalidate = 0;

export function GET() {
  // CODEAXIS_API_URL is the runtime value and wins. NEXT_PUBLIC_API_URL is
  // accepted as a fallback so an existing deployment that only sets the old
  // variable keeps working.
  const apiUrl = (process.env.CODEAXIS_API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "")
    .trim()
    .replace(/\/$/, "");

  return NextResponse.json(
    {
      // Empty string rather than a localhost guess: the client falls back to
      // its own build-time hint, and a wrong-but-plausible URL here would be
      // harder to diagnose than an absent one.
      apiUrl,
      configured: apiUrl.length > 0,
    },
    { headers: { "Cache-Control": "no-store" } },
  );
}
