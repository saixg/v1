-- Replace Supabase Auth with Clerk (user instruction, 2026-10-17). build-plan.md
-- Part 2's Auth row now reads "Clerk", not "Supabase Auth (email + GitHub OAuth)".
--
-- Clerk users never exist in auth.users and Clerk's JWTs never populate
-- Postgres's `request.jwt.claims` setting (that's Supabase Auth/PostgREST
-- machinery), so every auth.uid()-based policy below is now unreachable —
-- keeping it would look like a real access control when it structurally
-- cannot ever fire. The real, exercised gate stays exactly what the
-- original migration's own comment already said it was: app-layer
-- membership checks in FastAPI (app/auth.py), with the codeaxis_api /
-- codeaxis_worker role policies as the operative DB-level boundary. There is
-- no browser pathway to Postgres in this app (the frontend only ever talks
-- to FastAPI), so there is nothing left for an `authenticated`-role policy
-- to defend — removed rather than left as decoration.

drop trigger if exists on_auth_user_created on auth.users;
drop function if exists codeaxis.handle_new_user();

drop policy if exists profiles_select_self on codeaxis.profiles;
drop policy if exists projects_select_member on codeaxis.projects;
drop policy if exists memberships_select_member on codeaxis.project_memberships;
drop policy if exists jobs_select_member on codeaxis.jobs;
-- Added by a later migration (20261016120000_llm_calls.sql) on the same
-- function; must go before the function itself can be dropped.
drop policy if exists llm_calls_select_member on codeaxis.llm_calls;

drop function if exists codeaxis.is_project_member(uuid);

revoke usage on schema codeaxis from authenticated;
revoke select on codeaxis.profiles, codeaxis.projects, codeaxis.project_memberships,
  codeaxis.jobs, codeaxis.llm_calls from authenticated;

-- codeaxis.profiles.id becomes the Clerk user id (e.g. "user_2abc..."), a
-- string with no fixed format — not a uuid, and no longer an FK into
-- auth.users. Profiles are now provisioned lazily by the app
-- (app/knowledge/projects.py: ensure_profile) on first project creation,
-- not by a signup trigger.
--
-- profiles.id is the referenced side of two more FKs (projects.owner_id,
-- project_memberships.user_id), which Postgres won't let you retype out from
-- under — drop both, retype all three columns, then recreate them.
alter table codeaxis.profiles drop constraint if exists profiles_id_fkey;
alter table codeaxis.projects drop constraint projects_owner_id_fkey;
alter table codeaxis.project_memberships drop constraint project_memberships_user_id_fkey;

alter table codeaxis.profiles alter column id type text using id::text;
alter table codeaxis.projects alter column owner_id type text using owner_id::text;
alter table codeaxis.project_memberships alter column user_id type text using user_id::text;

alter table codeaxis.projects
  add constraint projects_owner_id_fkey foreign key (owner_id) references codeaxis.profiles(id);
alter table codeaxis.project_memberships
  add constraint project_memberships_user_id_fkey foreign key (user_id)
    references codeaxis.profiles(id) on delete cascade;
