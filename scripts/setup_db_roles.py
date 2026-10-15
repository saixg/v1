"""Create codeaxis_worker (read/write) and codeaxis_api (read-only) roles and
grant them on the codeaxis schema. Idempotent: skips a role that already
exists rather than rotating its password. Connects with DATABASE_URL, which
must be the project owner connection (it is, by default, in .env).

Prints the API_DATABASE_URL to paste into .env for any role this run created.
"""

import os
import secrets
import sys

import psycopg
from psycopg import sql
from dotenv import load_dotenv

load_dotenv()

url = os.environ.get("DATABASE_URL")
if not url:
    sys.exit("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")

project_ref = os.environ.get("SUPABASE_URL", "").removeprefix("https://").split(".")[0]
pooler_host_db = url.split("@", 1)[1] if "@" in url else None

GRANTS = {
    "codeaxis_worker": """
        grant usage on schema codeaxis to codeaxis_worker;
        grant usage on schema extensions to codeaxis_worker;
        grant select, insert, update, delete on all tables in schema codeaxis to codeaxis_worker;
        grant usage, select on all sequences in schema codeaxis to codeaxis_worker;
        alter default privileges in schema codeaxis grant select, insert, update, delete on tables to codeaxis_worker;
        alter default privileges in schema codeaxis grant usage, select on sequences to codeaxis_worker;
    """,
    "codeaxis_api": """
        grant usage on schema codeaxis to codeaxis_api;
        grant usage on schema extensions to codeaxis_api;
        grant select on all tables in schema codeaxis to codeaxis_api;
        alter default privileges in schema codeaxis grant select on tables to codeaxis_api;
    """,
}

with psycopg.connect(url, connect_timeout=10, autocommit=True) as conn:
    for role, grants in GRANTS.items():
        exists = conn.execute(
            "select 1 from pg_roles where rolname = %s", (role,)
        ).fetchone()
        if exists:
            print(f"{role}: already exists, skipping create (grants re-applied)")
        else:
            password = secrets.token_urlsafe(24)
            conn.execute(
                sql.SQL("create role {} login password {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
            print(f"{role}: created")
            if pooler_host_db and project_ref:
                print(
                    f"  API_DATABASE_URL=postgresql://{role}.{project_ref}:{password}@{pooler_host_db}"
                )
            else:
                print(f"  password: {password}")
        conn.execute(grants)
        print(f"{role}: grants applied")
