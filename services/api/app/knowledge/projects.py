"""codeaxis.projects / codeaxis.project_memberships: who may see what.

These queries are the app-layer authorization gate (build-plan.md Part 2):
FastAPI's DB roles are not scoped to an end user, so every repo-scoped route
must ask `get_membership_role` before reading or writing.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import class_row

from app.models import Project, ProjectRole, ProjectWithRole


def ensure_profile(conn: psycopg.Connection, user_id: str, email: str | None = None) -> None:
    """Idempotently create the caller's codeaxis.profiles row. Clerk has no
    signup hook into this database, so a profile is provisioned on first use
    (the first time a Clerk-authenticated user creates a project) rather
    than by a trigger — any validly signed-in user can always get one."""
    conn.execute(
        "insert into codeaxis.profiles (id, email) values (%s, %s) on conflict (id) do nothing",
        (user_id, email),
    )


def create_project(
    conn: psycopg.Connection, name: str, owner_id: str, owner_email: str | None = None
) -> ProjectWithRole:
    """Create a project and its owner membership in one transaction,
    provisioning the owner's profile first if this is their first project."""
    ensure_profile(conn, owner_id, owner_email)
    with conn.cursor(row_factory=class_row(Project)) as cur:
        project = cur.execute(
            """
            insert into codeaxis.projects (name, owner_id) values (%s, %s)
            returning id, name, owner_id, created_at
            """,
            (name, owner_id),
        ).fetchone()
        assert project is not None
        cur.execute(
            """
            insert into codeaxis.project_memberships (project_id, user_id, role)
            values (%s, %s, 'owner')
            """,
            (project.id, owner_id),
        )
    conn.commit()
    return ProjectWithRole(**project.model_dump(), role="owner")


def list_projects_for_user(conn: psycopg.Connection, user_id: str) -> list[ProjectWithRole]:
    with conn.cursor(row_factory=class_row(ProjectWithRole)) as cur:
        return cur.execute(
            """
            select p.id, p.name, p.owner_id, p.created_at, m.role
            from codeaxis.project_memberships m
            join codeaxis.projects p on p.id = m.project_id
            where m.user_id = %s
            order by p.created_at, p.id
            """,
            (user_id,),
        ).fetchall()


def get_membership_role(
    conn: psycopg.Connection, project_id: str, user_id: str
) -> ProjectRole | None:
    """The user's role in the project, or None if they are not a member."""
    row = conn.execute(
        "select role from codeaxis.project_memberships where project_id = %s and user_id = %s",
        (project_id, user_id),
    ).fetchone()
    return None if row is None else row[0]
