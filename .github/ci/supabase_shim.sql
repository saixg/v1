-- The minimum of a Supabase project that supabase/migrations/ assumes to exist,
-- for CI against a plain pgvector image. Real Supabase provides all of this;
-- nothing here is applied to production.

-- repo_model.sql: `create extension vector with schema extensions`.
create schema if not exists extensions;

-- Supabase puts `extensions` on every role's search_path; the migrations rely on
-- that for unqualified opclasses (vector_cosine_ops) and tests rely on it for the
-- `<=>` operator. Database-level so codeaxis_worker/api sessions inherit it.
do $$
begin
  execute format('alter database %I set search_path = "$user", public, extensions',
                 current_database());
end
$$;

-- platform_projects.sql grants/policies reference these roles, and the Clerk
-- migration later revokes from them. Their existence is all that matters.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role nologin;
  end if;
end
$$;

-- platform_projects.sql creates a FK and a trigger on auth.users and calls
-- auth.uid() inside policies; clerk_auth.sql removes all of them again.
create schema if not exists auth;

create table if not exists auth.users (
  id    uuid primary key,
  email text
);

create or replace function auth.uid() returns uuid
language sql stable
as $$ select null::uuid $$;

-- The two roles scripts/setup_db_roles.py grants to. It skips CREATE for a
-- role that already exists, so creating them here with a known password is
-- how CI learns the credentials the script would otherwise only print.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'codeaxis_worker') then
    create role codeaxis_worker login password 'ci-worker';
  end if;
  if not exists (select 1 from pg_roles where rolname = 'codeaxis_api') then
    create role codeaxis_api login password 'ci-api';
  end if;
end
$$;
