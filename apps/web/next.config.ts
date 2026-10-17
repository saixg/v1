import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */
  // Every real page in this app calls `await auth.protect()` (Clerk), so
  // every route is fully dynamic by design — partial prerendering has
  // nothing static left to buy, and fighting Clerk's own server-side cookie
  // read for a benefit that doesn't apply here isn't worth it.
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
