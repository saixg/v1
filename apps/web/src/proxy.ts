// Clerk's Next.js middleware, kept bare per Clerk's current guidance: it
// only makes auth state available to server code downstream. Authorization
// decisions happen per-page via `await auth.protect()`, not here.
import { clerkMiddleware } from "@clerk/nextjs/server";

export default clerkMiddleware();

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico).*)",
    "/(api|trpc)(.*)",
  ],
};
