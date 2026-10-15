"""Check the Supabase connection in DATABASE_URL. Read-only."""

import os
import sys

import psycopg
from dotenv import load_dotenv

load_dotenv()

url = os.environ.get("DATABASE_URL")
if not url:
    sys.exit("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")

with psycopg.connect(url, connect_timeout=10) as conn:
    print("server:", conn.execute("show server_version").fetchone()[0])
    rows = conn.execute(
        "select name, default_version, installed_version from pg_available_extensions"
        " where name in ('vector', 'pgmq') order by name"
    ).fetchall()
    for name, default, installed in rows:
        print(f"{name}: available {default}, installed {installed or 'no'}")
    if len(rows) < 2:
        print("warning: vector and/or pgmq not available on this server")
