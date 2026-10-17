-- Platform: profiles, projects, project_memberships. build-plan.md Part 6
-- ("Platform") and Part 2 (Authorization row).
--
-- The primary access gate is app-layer: FastAPI verifies the Supabase JWT and
-- checks project membership before touching any repo-scoped data, because it
-- connects as codeaxis_api / codeaxis_worker, which RLS cannot meaningfully
-- scope to an end user. RLS here is defense in depth for any *direct* browser
-- read (role `authenticated`, identity from auth.uid()).

-- ---------------------------------------------------------------- profiles
-- One row per auth.users row, created by the trigger below on signup.

create table codeaxis.profiles (
  id          uuid primary key references auth.users(id) on delete cascade,
  email       text,
  created_at  timestamptz not null default now()
);

create function codeaxis.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into codeaxis.profiles (id, email)
  values (new.id, new.email)
  on conflict (id) do nothing;
  return new;
end;
$$;

revoke execute on function codeaxis.handle_new_user() from public;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function codeaxis.handle_new_user();

-- Users who signed up before this migration get a profile too.
insert into codeaxis.profiles (id, email)
select id, email from auth.users
on conflict (id) do nothing;

-- ---------------------------------------------------------------- projects

create table codeaxis.projects (
  id          uuid primary key default gen_random_uuid(),
  name        text not null check (length(btrim(name)) between 1 and 200),
  -- No cascade: deleting a user who still owns projects must be an explicit
  -- decision (their repos and graph rows hang off the project).
  owner_id    uuid not null references codeaxis.profiles(id),
  created_at  timestamptz not null default now()
);

create index projects_owner_id_idx on codeaxis.projects (owner_id);

create table codeaxis.project_memberships (
  project_id  uuid not null references codeaxis.projects(id) on delete cascade,
  user_id     uuid not null references codeaxis.profiles(id) on delete cascade,
  role        text not null check (role in ('owner', 'member')),
  created_at  timestamptz not null default now(),
  primary key (project_id, user_id)
);

-- The PK leads with project_id; "which projects is this user in" needs this.
create index project_memberships_user_id_idx on codeaxis.project_memberships (user_id);

-- Membership lookup used by RLS policies. security definer so the policy on
-- project_memberships can consult project_memberships without recursing;
-- it only ever answers for the calling user (auth.uid()).
create function codeaxis.is_project_member(p_project_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from codeaxis.project_memberships m
    where m.project_id = p_project_id and m.user_id = (select auth.uid())
  );
$$;

revoke execute on function codeaxis.is_project_member(uuid) from public;
grant execute on function codeaxis.is_project_member(uuid) to authenticated;

-- ---------------------------------------------------------------- RLS

alter table codeaxis.profiles enable row level security;
alter table codeaxis.projects enable row level security;
alter table codeaxis.project_memberships enable row level security;

-- End users (direct browser reads). Read-only: every write goes through
-- FastAPI, so no insert/update/delete policy exists for `authenticated`.
create policy profiles_select_self on codeaxis.profiles
  for select to authenticated
  using (id = (select auth.uid()));

create policy projects_select_member on codeaxis.projects
  for select to authenticated
  using ((select codeaxis.is_project_member(id)));

create policy memberships_select_member on codeaxis.project_memberships
  for select to authenticated
  using ((select codeaxis.is_project_member(project_id)));

grant usage on schema codeaxis to authenticated;
grant select on codeaxis.profiles, codeaxis.projects, codeaxis.project_memberships
  to authenticated;

-- Service roles (scripts/setup_db_roles.py). They are not BYPASSRLS, so with
-- RLS enabled they need explicit policies; scoping to a user happens in the
-- app layer for these connections.
create policy profiles_api_read on codeaxis.profiles
  for select to codeaxis_api using (true);
create policy projects_api_read on codeaxis.projects
  for select to codeaxis_api using (true);
create policy memberships_api_read on codeaxis.project_memberships
  for select to codeaxis_api using (true);

create policy profiles_worker_all on codeaxis.profiles
  for all to codeaxis_worker using (true) with check (true);
create policy projects_worker_all on codeaxis.projects
  for all to codeaxis_worker using (true) with check (true);
create policy memberships_worker_all on codeaxis.project_memberships
  for all to codeaxis_worker using (true) with check (true);

grant select, insert, update, delete
  on codeaxis.profiles, codeaxis.projects, codeaxis.project_memberships
  to codeaxis_worker;
grant select on codeaxis.profiles, codeaxis.projects, codeaxis.project_memberships
  to codeaxis_api;
