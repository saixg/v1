// Apply packages/graph/supabase/migrations to the database in DATABASE_URL.
// Run via `npm run db:push`, which loads .env with --env-file-if-exists.
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";

const url = process.env.DATABASE_URL;
if (!url) {
  console.error("DATABASE_URL is not set. Copy .env.example to .env and fill it in.");
  process.exit(1);
}

const cli = createRequire(import.meta.url).resolve("supabase/dist/supabase.js");
const args = ["db", "push", "--workdir", "packages/graph", "--db-url", url, ...process.argv.slice(2)];
const { status } = spawnSync(process.execPath, [cli, ...args], { stdio: "inherit" });
process.exit(status ?? 1);
