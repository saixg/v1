import { ClerkProvider, Show, SignInButton, UserButton } from "@clerk/nextjs";
import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

import { ClerkTokenBridge } from "@/components/ClerkTokenBridge";

export const metadata: Metadata = {
  title: "CodeAxis",
  description: "Evidence-backed views of an unfamiliar codebase.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="flex min-h-full flex-col">
        <ClerkProvider>
          <ClerkTokenBridge />
          <header className="flex h-12 shrink-0 items-center justify-between border-b border-line px-4">
            <Link href="/" className="text-sm font-semibold tracking-tight">
              CodeAxis
            </Link>
            <Show when="signed-in">
              <UserButton />
            </Show>
            <Show when="signed-out">
              <SignInButton>
                <button className="text-xs font-medium text-accent">Sign in</button>
              </SignInButton>
            </Show>
          </header>
          <div className="flex min-h-0 flex-1 flex-col">{children}</div>
        </ClerkProvider>
      </body>
    </html>
  );
}
