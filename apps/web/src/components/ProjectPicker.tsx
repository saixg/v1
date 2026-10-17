"use client";

import { useEffect, useState, type FormEvent } from "react";

import { createProject, getProjects, type Project } from "@/lib/api";

const NEW_PROJECT = "__new__";

const inputClass =
  "w-full rounded-md border border-line bg-background px-3 py-2 text-sm outline-none focus:border-accent focus:ring-2 focus:ring-accent/20";

export function ProjectPicker({
  value,
  onChange,
}: {
  value: string | null;
  onChange: (projectId: string) => void;
}) {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [creatingName, setCreatingName] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getProjects()
      .then(({ projects }) => {
        if (cancelled) return;
        setProjects(projects);
        if (projects.length === 0) setCreatingName("");
        else if (!value) onChange(projects[0].id);
      })
      .catch((err) => {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
    // Only on mount: this picks an initial project, it doesn't track `value`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function onCreate(e: FormEvent) {
    e.preventDefault();
    if (!creatingName || creatingName.trim() === "") return;
    setBusy(true);
    setError(null);
    try {
      const project = await createProject(creatingName.trim());
      setProjects((prev) => [...(prev ?? []), project]);
      onChange(project.id);
      setCreatingName(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  if (loadError) {
    return (
      <p role="alert" className="text-sm text-rose-700 dark:text-rose-300">
        Couldn&apos;t load your projects: {loadError}
      </p>
    );
  }

  if (projects === null) {
    return <p className="text-sm text-muted">Loading your projects…</p>;
  }

  return (
    <div className="space-y-2">
      <span className="block text-sm font-medium">Project</span>

      {creatingName === null ? (
        <select
          className={inputClass}
          value={value ?? ""}
          onChange={(e) => {
            if (e.target.value === NEW_PROJECT) setCreatingName("");
            else onChange(e.target.value);
          }}
        >
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
          <option value={NEW_PROJECT}>+ New project…</option>
        </select>
      ) : (
        <form onSubmit={onCreate} className="flex gap-2">
          <input
            className={inputClass}
            autoFocus
            required
            placeholder="Project name"
            value={creatingName}
            onChange={(e) => setCreatingName(e.target.value)}
          />
          <button
            type="submit"
            disabled={busy || creatingName.trim() === ""}
            className="shrink-0 rounded-md bg-accent px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            {busy ? "Creating…" : "Create"}
          </button>
          {projects.length > 0 && (
            <button
              type="button"
              onClick={() => setCreatingName(null)}
              className="shrink-0 rounded-md border border-line px-3 py-2 text-sm text-muted hover:text-foreground"
            >
              Cancel
            </button>
          )}
        </form>
      )}

      {error && (
        <p role="alert" className="text-xs text-rose-700 dark:text-rose-300">
          {error}
        </p>
      )}
    </div>
  );
}
