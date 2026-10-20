import path from "node:path";

import { defineConfig } from "vitest/config";

export default defineConfig({
  // No React plugin: the automatic JSX runtime is all the tests need, and it
  // keeps the dependency list to what is actually exercised.
  esbuild: { jsx: "automatic" },
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
});
