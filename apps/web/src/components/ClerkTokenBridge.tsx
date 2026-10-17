"use client";

import { useAuth } from "@clerk/nextjs";
import { useEffect } from "react";

import { setTokenGetter } from "@/lib/api";

// api.ts is a plain module (called from many client components), so it has
// no hook of its own to read the session token from. This bridges Clerk's
// `useAuth().getToken` — the supported client-side way to get one — into
// that module once, instead of threading a token through every call site.
export function ClerkTokenBridge() {
  const { getToken } = useAuth();
  useEffect(() => {
    setTokenGetter(getToken);
  }, [getToken]);
  return null;
}
