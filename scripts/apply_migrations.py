"""Apply supabase/migrations/*.sql to DATABASE_URL in filename order.

Exists for CI, where there is no Supabase CLI or project: the same files
`npm run db:push` ships to Supabase are replayed against a throwaway Postgres.
Pass --shim to run the Supabase-compat bootstrap first. Never point this at a
database that already has the migrations applied; it does not track state.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import LiteralString, cast

import psycopg

ROOT = Path(__file__).resolve().parent.parent

parser = argparse.ArgumentParser()
parser.add_argument("--shim", action="store_true", help="apply .github/ci/supabase_shim.sql first")
args = parser.parse_args()

url = os.environ.get("DATABASE_URL")
if not url:
    sys.exit("DATABASE_URL is not set.")

files = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
if args.shim:
    files.insert(0, ROOT / ".github" / "ci" / "supabase_shim.sql")

for f in files:
    # A fresh connection per file so a database-level search_path set by the shim
    # is in effect for every later migration. No params means the simple-query
    # protocol, which is what allows several statements in one execute.
    with psycopg.connect(url, connect_timeout=10, autocommit=True) as conn:
        # Migration files are repo-owned, never user input, so the literal-string guard is moot.
        conn.execute(cast(LiteralString, f.read_text(encoding="utf-8")))
    print(f"applied {f.name}")
