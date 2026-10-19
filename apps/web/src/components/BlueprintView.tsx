"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  createBlueprint,
  describeRefusal,
  exportBlueprint,
  generateBlueprint,
  getBlueprint,
  getBlueprintLayout,
  getBlueprintVersion,
  getJob,
  getTaskState,
  listBlueprints,
  listBlueprintVersions,
  regenerateBlueprint,
  validateBlueprint,
  type Blueprint,
  type BlueprintDocument,
  type BlueprintLayout,
  type BlueprintVersion,
  type Citation,
  type Refusal,
} from "@/lib/api";
import { autoLayout, type Point } from "@/lib/blueprintLayout";

import { errorKey, ErrorItems, groupErrors, KindBadge, VersionStatusPill } from "./BlueprintBits";
import { BlueprintCanvas, EDGE_STYLE, Legend } from "./BlueprintCanvas";
import { BlueprintFilesPanel } from "./BlueprintFilesPanel";
import { BlueprintMappingPanel } from "./BlueprintMappingPanel";
import { download } from "./ContextPackagePanel";
import { ErrorLine, errMsg, Pill } from "./ContextBits";
import { readIds } from "./TaskView";

// The Build tab (build-plan Part 7; Part 4.G). A blueprint is a typed design for
// work on one snapshot: which parts of the repository it reuses or changes, which
// parts it would add, how they connect, and where the files would go. It is static
// design over static analysis. Nothing in it was run, and nothing it proposes
// exists yet.

type Load<T> =
  | { state: "loading" }
  | { state: "loaded"; value: T }
  | { state: "error"; message: string; notFound: boolean };

function errorState(err: unknown): Load<never> {
  return {
    state: "error",
    message: errMsg(err),
    notFound: err instanceof ApiError && err.status === 404,
  };
}

const POLL_MS = 1500;
const POLL_MAX = 300;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function Versions({
  versions,
  currentVersion,
  viewing,
  onView,
}: {
  versions: BlueprintVersion[];
  currentVersion: number | null;
  viewing: number | null;
  onView: (version: number | null) => void;
}) {
  return (
    <section aria-label="Version history" className="space-y-2">
      <h3 className="text-sm font-semibold">Version history</h3>
      <p className="text-[11px] text-muted">
        Every edit, validation or regeneration is a new version, newest first. Arranging nodes is
        not: layout is stored apart from the design.
      </p>
      {versions.length === 0 ? (
        <p className="rounded-md border border-dashed border-line px-3 py-2 text-xs text-muted">
          No versions exist yet.
        </p>
      ) : (
        <ul className="space-y-1.5">
          {versions.map((v) => {
            const isCurrent = v.version === currentVersion;
            const shown = viewing === null ? isCurrent : v.version === viewing;
            return (
              <li
                key={v.version}
                className={`flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-xs ${
                  shown ? "border-accent" : "border-line"
                }`}
              >
                <span className="font-medium">v{v.version}</span>
                <VersionStatusPill status={v.status} />
                {isCurrent && <Pill tone="neutral">current</Pill>}
                <span className="text-muted">{v.origin_reason ?? "origin not recorded"}</span>
                {(v.validation_errors ?? []).length > 0 && (
                  <span className="text-rose-700 dark:text-rose-300">
                    {v.validation_errors!.length} validation error
                    {v.validation_errors!.length === 1 ? "" : "s"}
                  </span>
                )}
                <span className="text-muted">
                  {v.created_at ? new Date(v.created_at).toLocaleString() : ""}
                </span>
                <span className="font-mono text-[11px] text-muted">
                  {v.analyzer} {v.analyzer_version}
                </span>
                <button
                  type="button"
                  disabled={shown}
                  onClick={() => onView(isCurrent ? null : v.version)}
                  className="ml-auto rounded-md border border-line px-2 py-1 text-[11px] font-medium hover:bg-panel disabled:opacity-50"
                >
                  {shown ? "Viewing" : "View"}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function RefusalBox({ refusal, onDismiss }: { refusal: Refusal; onDismiss: () => void }) {
  return (
    <div
      role="alert"
      className="space-y-1 rounded-md border border-rose-600/40 bg-rose-500/5 p-3 text-xs"
    >
      <p className="font-medium text-rose-700 dark:text-rose-300">
        Regeneration refused: it would have altered a locked entity. Nothing was changed.
      </p>
      <p className="whitespace-pre-wrap">{refusal.summary}</p>
      {refusal.reasons.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-4">
          {refusal.reasons.map((r, i) => (
            <li key={i} className="whitespace-pre-wrap">
              {r}
            </li>
          ))}
        </ul>
      )}
      <p className="text-muted">
        Unlock the entity (or regenerate only components that are not locked), then try again. This
        was not retried.
      </p>
      <button
        type="button"
        onClick={onDismiss}
        className="rounded-md border border-line px-2.5 py-1 font-medium hover:bg-panel"
      >
        Dismiss
      </button>
    </div>
  );
}

function BlueprintDetail({
  summary,
  commitSha,
  onBack,
  onOpenCitation,
  onOpenNode,
}: {
  summary: Blueprint;
  commitSha: string;
  onBack: () => void;
  onOpenCitation?: (citation: Citation) => void;
  onOpenNode: (nodeId: number) => Promise<boolean>;
}) {
  const id = summary.id;
  const [current, setCurrent] = useState<Load<BlueprintDocument | null>>({ state: "loading" });
  const [versions, setVersions] = useState<BlueprintVersion[]>([]);
  const [versionsError, setVersionsError] = useState<string | null>(null);
  const [layout, setLayout] = useState<BlueprintLayout | null>(null);
  const [layoutNote, setLayoutNote] = useState<string | null>(null);
  const [viewing, setViewing] = useState<number | null>(null);
  const [past, setPast] = useState<Load<BlueprintDocument> | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const seq = useRef(0);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    const mine = ++seq.current;
    try {
      let doc: BlueprintDocument | null = null;
      try {
        doc = await getBlueprint(id);
      } catch (err) {
        // 404 is "no version has been generated yet", which is a real state.
        if (!(err instanceof ApiError && err.status === 404)) throw err;
      }
      const [vs, lay] = await Promise.all([
        listBlueprintVersions(id).then(
          (v) => ({ ok: true as const, v }),
          (e: unknown) => ({ ok: false as const, e }),
        ),
        doc
          ? getBlueprintLayout(id).then(
              (l) => ({ ok: true as const, l }),
              (e: unknown) => ({ ok: false as const, e }),
            )
          : Promise.resolve(null),
      ]);
      if (mine !== seq.current || !mounted.current) return;
      setCurrent({ state: "loaded", value: doc });
      setVersions(vs.ok ? vs.v : []);
      setVersionsError(vs.ok ? null : errMsg(vs.e));
      if (lay === null) {
        setLayout(null);
        setLayoutNote(null);
      } else if (lay.ok) {
        setLayout(lay.l);
        setLayoutNote(null);
      } else {
        setLayout(doc?.layout ?? null);
        setLayoutNote(
          `The stored layout could not be loaded (${errMsg(lay.e)}). Showing the layout carried in the document, or an automatic one.`,
        );
      }
    } catch (err) {
      if (mine === seq.current && mounted.current) setCurrent(errorState(err));
    }
  }, [id]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load on mount
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (viewing === null) return;
    let live = true;
    getBlueprintVersion(id, viewing).then(
      (value) => live && setPast({ state: "loaded", value }),
      (err) => live && setPast(errorState(err)),
    );
    return () => {
      live = false;
    };
  }, [id, viewing]);

  function viewVersion(version: number | null) {
    setViewing(version);
    setPast(version === null ? null : { state: "loading" });
    setSelectedKey(null);
  }

  /** Start a long-running job and wait for it the way the scan flow does:
   * poll the job until it settles. Failures are reported with their reason. */
  async function runJob(label: string, start: () => Promise<{ job_id: string }>) {
    setBusy(label);
    setActionError(null);
    setRefusal(null);
    setNote(null);
    try {
      const { job_id } = await start();
      for (let i = 0; ; i++) {
        if (!mounted.current) return;
        if (i >= POLL_MAX) {
          setActionError(`${label} is still running. Reload this tab to check on it later.`);
          return;
        }
        try {
          const job = await getJob(job_id);
          setPollError(null);
          if (job.status === "succeeded") break;
          if (job.status === "failed") {
            setActionError(`${label} failed: ${job.error ?? "the job recorded no reason"}.`);
            await refresh();
            return;
          }
        } catch (err) {
          setPollError(errMsg(err));
        }
        await sleep(POLL_MS);
      }
      viewVersion(null);
      await refresh();
      setNote(`${label} finished. The result is a new draft version until it is validated.`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 422) {
        setRefusal(describeRefusal(err.detail, err.message));
      } else {
        setActionError(errMsg(err));
      }
    } finally {
      if (mounted.current) {
        setBusy(null);
        setPollError(null);
      }
    }
  }

  async function validate() {
    setBusy("Validation");
    setActionError(null);
    setRefusal(null);
    setNote(null);
    try {
      const v = await validateBlueprint(id);
      await refresh();
      const n = v.validation_errors?.length ?? 0;
      setNote(
        v.status === "invalid"
          ? `Validation failed with ${n} error${n === 1 ? "" : "s"}; they are listed below.`
          : `Validation finished: version ${v.version} is ${v.status}.`,
      );
    } catch (err) {
      setActionError(errMsg(err));
    } finally {
      setBusy(null);
    }
  }

  async function doExport(doc: BlueprintDocument) {
    setBusy("Export");
    setActionError(null);
    setNote(null);
    try {
      const file = await exportBlueprint(id);
      download(file.filename, file.markdown);
      const s = doc.version.status;
      setNote(
        `Downloaded ${file.filename}.` +
          (s === "validated" || s === "approved"
            ? ""
            : ` The blueprint is ${s}, so the file describes a design that has not passed validation.`),
      );
    } catch (err) {
      setActionError(errMsg(err));
    } finally {
      setBusy(null);
    }
  }

  const currentDoc = current.state === "loaded" ? current.value : null;
  const displayLoad: Load<BlueprintDocument | null> =
    viewing !== null && past ? past : current;
  const doc = displayLoad.state === "loaded" ? displayLoad.value : null;
  const readOnly = viewing !== null && doc !== null && doc.version.version !== currentDoc?.version.version;

  const components = useMemo(() => doc?.components ?? [], [doc]);
  const connections = useMemo(() => doc?.connections ?? [], [doc]);
  const files = useMemo(() => doc?.proposed_files ?? [], [doc]);
  const names = useMemo(() => new Map(components.map((c) => [c.key, c.name])), [components]);
  const errors = useMemo(() => doc?.version.validation_errors ?? [], [doc]);

  const grouped = useMemo(() => {
    const connKeys = new Set(connections.map((c) => c.key));
    const filePaths = new Set(files.map((f) => f.path));
    return groupErrors(errors, (kind, key) =>
      kind === "component"
        ? names.has(key)
        : kind === "connection"
          ? connKeys.has(key)
          : kind === "proposed_file"
            ? filePaths.has(key)
            : false,
    );
  }, [errors, names, connections, files]);

  const issueCounts = useMemo(() => {
    const m = new Map<string, number>();
    for (const [k, v] of grouped.byEntity) {
      if (k.startsWith("component:")) m.set(k.slice("component:".length), v.length);
    }
    return m;
  }, [grouped]);
  const badConnections = useMemo(() => {
    const s = new Set<string>();
    for (const k of grouped.byEntity.keys()) {
      if (k.startsWith("connection:")) s.add(k.slice("connection:".length));
    }
    return s;
  }, [grouped]);

  const baseLayout: BlueprintLayout = useMemo(
    () => (viewing !== null ? doc?.layout : layout) ?? {},
    [viewing, doc, layout],
  );
  const initialPositions = useMemo(() => {
    const auto = autoLayout(
      components.map((c) => c.key),
      connections.map((c) => ({ source: c.source_key, target: c.target_key })),
    );
    const stored: Record<string, Point> = {};
    for (const n of baseLayout.nodes ?? []) stored[n.key] = { x: n.x, y: n.y };
    const out: Record<string, Point> = {};
    for (const c of components) out[c.key] = stored[c.key] ?? auto[c.key] ?? { x: 0, y: 0 };
    return out;
  }, [components, connections, baseLayout]);
  const hasStored = (baseLayout.nodes ?? []).some((n) => names.has(n.key));

  const backButton = (
    <button
      type="button"
      onClick={onBack}
      className="rounded-md border border-line px-2.5 py-1 text-xs font-medium hover:bg-panel"
    >
      All blueprints
    </button>
  );

  if (displayLoad.state === "loading" || current.state === "loading") {
    return (
      <div className="space-y-3 p-5">
        {backButton}
        <p className="text-xs text-muted">Loading blueprint…</p>
      </div>
    );
  }
  if (current.state === "error") {
    return (
      <div className="space-y-3 p-5">
        {backButton}
        <ErrorLine message={`Couldn't load the blueprint: ${current.message}`} />
        <button
          type="button"
          onClick={() => void refresh()}
          className="rounded-md border border-line px-3 py-1.5 text-xs font-medium hover:bg-panel"
        >
          Try again
        </button>
      </div>
    );
  }

  const stale = summary.commit_sha !== commitSha;
  const generateLabel = currentDoc ? "Regenerate" : "Generate design";
  const selected = components.find((c) => c.key === selectedKey) ?? null;

  const header = (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {backButton}
        {doc && <VersionStatusPill status={doc.version.status} />}
        {doc && <span className="text-[11px] text-muted">version {doc.version.version}</span>}
        <span className="font-mono text-[11px] text-muted">{summary.commit_sha}</span>
        {stale && <Pill tone="warn">different commit</Pill>}
      </div>
      <h2 className="text-base font-semibold">{summary.title}</h2>
      <p className="text-[11px] text-muted">
        A design for work on this snapshot. Boxes marked existing or modified are real parts of the
        repository; proposed ones are not there yet. It comes from static analysis and a model
        authoring against it; nothing was run.
      </p>
    </div>
  );

  const actions = (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        {!currentDoc ? (
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => runJob("Generation", () => generateBlueprint(id))}
            className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
          >
            {busy === "Generation" ? "Generating…" : generateLabel}
          </button>
        ) : (
          <>
            <button
              type="button"
              disabled={busy !== null || readOnly}
              onClick={() => validate()}
              className="rounded-md border border-line px-3 py-1.5 text-xs font-medium hover:bg-panel disabled:opacity-50"
            >
              {busy === "Validation" ? "Validating…" : "Validate"}
            </button>
            <button
              type="button"
              disabled={busy !== null || readOnly}
              onClick={() => runJob("Regeneration", () => regenerateBlueprint(id))}
              className="rounded-md border border-line px-3 py-1.5 text-xs font-medium hover:bg-panel disabled:opacity-50"
              title="Regenerates everything that is not locked. The server refuses if that would alter a locked entity."
            >
              {busy === "Regeneration" ? "Regenerating…" : "Regenerate"}
            </button>
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => doc && doExport(doc)}
              className="rounded-md border border-line px-3 py-1.5 text-xs font-medium hover:bg-panel disabled:opacity-50"
            >
              {busy === "Export" ? "Fetching…" : "Export as markdown"}
            </button>
          </>
        )}
      </div>
      {busy && busy !== "Validation" && busy !== "Export" && (
        <p className="text-[11px] text-muted" aria-live="polite">
          {busy} is running as a background job. This page checks on it every few seconds.
          {pollError && ` Couldn't check progress: ${pollError} Retrying…`}
        </p>
      )}
      {refusal && <RefusalBox refusal={refusal} onDismiss={() => setRefusal(null)} />}
      <ErrorLine message={actionError} />
      {note && (
        <p className="text-[11px] text-muted" aria-live="polite">
          {note}
        </p>
      )}
    </div>
  );

  const history = (
    <>
      <ErrorLine message={versionsError && `Couldn't load version history: ${versionsError}`} />
      <Versions
        versions={versions}
        currentVersion={currentDoc?.version.version ?? null}
        viewing={viewing}
        onView={viewVersion}
      />
    </>
  );

  if (displayLoad.state === "error") {
    return (
      <div className="space-y-4 p-5">
        {header}
        <ErrorLine message={`Couldn't load version ${viewing}: ${displayLoad.message}`} />
        {history}
      </div>
    );
  }

  if (!doc) {
    return (
      <div className="space-y-4 p-5">
        {header}
        <p className="rounded-md border border-dashed border-line px-3 py-2 text-xs text-muted">
          No design has been generated for this blueprint yet, so there is no workflow, mapping or
          file structure to show. Generation reads the task&apos;s requirements and constraints and
          the repository graph at <span className="font-mono">{summary.commit_sha}</span>.
        </p>
        {actions}
        {history}
      </div>
    );
  }

  const status = doc.version.status;
  const unresolved = doc.unresolved ?? [];
  const linkOpenable = !stale;
  const linkNote = stale
    ? `This blueprint was grounded at ${summary.commit_sha.slice(0, 12)}, not the workspace commit, so the entity cannot be opened in the Explorer without risking showing a different thing.`
    : null;

  return (
    <div className="space-y-6 p-5">
      {header}

      {stale && (
        <p className="rounded-md border border-amber-600/40 bg-amber-500/5 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
          This blueprint is grounded at <span className="font-mono">{summary.commit_sha}</span> but the
          workspace is at <span className="font-mono">{commitSha}</span>. Its claims about the
          repository are about the older commit.
        </p>
      )}
      {readOnly && (
        <p className="flex flex-wrap items-center gap-2 rounded-md border border-amber-600/40 bg-amber-500/5 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
          Viewing version {doc.version.version}, not the current version{" "}
          {currentDoc?.version.version}. It is read-only.
          <button
            type="button"
            onClick={() => viewVersion(null)}
            className="rounded-md border border-amber-600/40 px-2 py-0.5 font-medium hover:bg-amber-500/10"
          >
            Back to current
          </button>
        </p>
      )}
      {status === "invalid" && (
        <div
          role="alert"
          className="space-y-2 rounded-md border border-rose-600/40 bg-rose-500/5 p-3"
        >
          <p className="text-sm font-medium text-rose-700 dark:text-rose-300">
            This blueprint failed validation. Do not build from it.
          </p>
          {errors.length === 0 ? (
            <p className="text-xs">
              It is marked invalid but the server returned no reasons. That is itself a defect.
            </p>
          ) : (
            <>
              <p className="text-xs text-muted">
                {errors.length} problem{errors.length === 1 ? "" : "s"}
                {grouped.unattached.length > 0 &&
                  `, ${grouped.unattached.length} not tied to an item that is in this version`}
                . Each one is also shown on the item it names.
              </p>
              <ErrorItems errors={errors} />
            </>
          )}
        </div>
      )}
      {status === "draft" && (
        <p className="rounded-md border border-amber-600/40 bg-amber-500/5 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
          Draft: generated or edited and not validated since. Validate it to check that every
          connection, mapping and file path holds together.
        </p>
      )}
      {status !== "invalid" && grouped.unattached.length > 0 && (
        <div className="space-y-1 rounded-md border border-amber-600/40 bg-amber-500/5 p-3">
          <p className="text-xs font-medium text-amber-700 dark:text-amber-400">
            Notes from validation
          </p>
          <ErrorItems errors={grouped.unattached} />
        </div>
      )}

      {actions}

      <section aria-label="Open questions" className="space-y-2">
        <h3 className="text-sm font-semibold">Open questions ({unresolved.length})</h3>
        {unresolved.length === 0 ? (
          <p className="text-[11px] text-muted">
            No open decisions were recorded with this version.
          </p>
        ) : (
          <ul className="space-y-1.5">
            {unresolved.map((u, i) => (
              <li
                key={i}
                className="space-y-1 rounded-md border border-amber-600/40 bg-amber-500/10 px-3 py-2"
              >
                <p className="whitespace-pre-wrap text-sm">{u.question}</p>
                <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted">
                  <Pill tone={u.status === "decided" ? "neutral" : "warn"}>{u.status ?? "open"}</Pill>
                  {(u.affects ?? []).length > 0 && (
                    <span>
                      blocks{" "}
                      {u.affects!.map((k, n) => (
                        <span key={k}>
                          {n > 0 && ", "}
                          <button
                            type="button"
                            onClick={() => names.has(k) && setSelectedKey(k)}
                            className="font-medium text-foreground hover:text-accent"
                          >
                            {names.get(k) ?? k}
                          </button>
                        </span>
                      ))}
                    </span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
        <p className="text-[11px] text-muted">
          Grounded in {doc.requirement_ids?.length ?? 0} requirement
          {(doc.requirement_ids?.length ?? 0) === 1 ? "" : "s"} and {doc.constraint_ids?.length ?? 0}{" "}
          constraint{(doc.constraint_ids?.length ?? 0) === 1 ? "" : "s"} from the task (Context tab).
        </p>
      </section>

      <section aria-label="Workflow" className="space-y-2">
        <h3 className="text-sm font-semibold">Workflow</h3>
        <Legend />
        <ErrorLine message={layoutNote} />
        {components.length === 0 ? (
          <p className="rounded-md border border-dashed border-line px-3 py-2 text-xs text-muted">
            This version has no components, so there is nothing to draw.
          </p>
        ) : (
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_24rem]">
            <div className="min-w-0 space-y-3">
              <BlueprintCanvas
                key={`${id}:${doc.version.version}:${components.map((c) => c.key).join("|")}`}
                blueprintId={id}
                components={components}
                connections={connections}
                initialPositions={initialPositions}
                baseLayout={baseLayout}
                persist={!readOnly}
                issueCountByComponent={issueCounts}
                issueKeysForConnections={badConnections}
                selectedKey={selectedKey}
                onSelect={setSelectedKey}
              />
              {!hasStored && (
                <p className="text-[11px] text-muted">
                  No layout is stored for this version, so the positions were arranged
                  automatically. Dragging a node stores a layout.
                </p>
              )}

              <div className="space-y-1">
                <h4 className="text-xs font-medium text-muted">Components ({components.length})</h4>
                <ul className="flex flex-wrap gap-1.5">
                  {components.map((c) => (
                    <li key={c.key}>
                      <button
                        type="button"
                        onClick={() => setSelectedKey(c.key)}
                        aria-pressed={c.key === selectedKey}
                        className={`flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs hover:bg-panel ${
                          c.key === selectedKey ? "border-accent" : "border-line"
                        } ${issueCounts.get(c.key) ? "outline outline-1 outline-rose-600" : ""}`}
                      >
                        <KindBadge kind={c.kind} mapping={c.mapping} />
                        <span className="font-medium">{c.name}</span>
                        {issueCounts.get(c.key) ? (
                          <span className="text-rose-700 dark:text-rose-300">
                            {issueCounts.get(c.key)} issue{issueCounts.get(c.key) === 1 ? "" : "s"}
                          </span>
                        ) : null}
                      </button>
                    </li>
                  ))}
                </ul>
              </div>

              <details className="rounded-md border border-line p-3">
                <summary className="cursor-pointer text-xs font-medium">
                  Connections ({connections.length})
                </summary>
                {connections.length === 0 ? (
                  <p className="mt-2 text-xs text-muted">This version has no connections.</p>
                ) : (
                  <ul className="mt-2 space-y-1.5">
                    {connections.map((c) => {
                      const errs = grouped.byEntity.get(errorKey("connection", c.key)) ?? [];
                      const drawable = names.has(c.source_key) && names.has(c.target_key);
                      const sem = c.semantics;
                      return (
                        <li
                          key={c.key}
                          className={`space-y-0.5 rounded border px-2 py-1.5 text-xs ${
                            errs.length > 0 || !drawable
                              ? "border-rose-600/40 bg-rose-500/5"
                              : "border-line"
                          }`}
                        >
                          <p>
                            <Pill tone="neutral" title={EDGE_STYLE[c.kind]?.blurb}>
                              {EDGE_STYLE[c.kind]?.label ?? c.kind}
                            </Pill>{" "}
                            <span className="font-medium">{names.get(c.source_key) ?? c.source_key}</span>
                            {" to "}
                            <span className="font-medium">{names.get(c.target_key) ?? c.target_key}</span>
                          </p>
                          {!drawable && (
                            <p className="text-rose-700 dark:text-rose-300">
                              An endpoint is not a component of this version, so this connection
                              cannot be drawn.
                            </p>
                          )}
                          {(sem?.description || sem?.protocol || sem?.trigger) && (
                            <p className="text-muted">
                              {[sem.description, sem.protocol && `protocol ${sem.protocol}`, sem.trigger && `trigger ${sem.trigger}`]
                                .filter(Boolean)
                                .join(" · ")}
                            </p>
                          )}
                          {errs.length > 0 && <ErrorItems errors={errs} />}
                        </li>
                      );
                    })}
                  </ul>
                )}
              </details>

              {(doc.modules ?? []).length > 0 && (
                <details className="rounded-md border border-line p-3">
                  <summary className="cursor-pointer text-xs font-medium">
                    Modules ({doc.modules!.length})
                  </summary>
                  <ul className="mt-2 space-y-1 text-xs">
                    {doc.modules!.map((m) => (
                      <li key={m.key}>
                        <span className="font-medium">{m.name ?? m.key}</span>{" "}
                        <span className="text-muted">
                          {m.components.map((k) => names.get(k) ?? k).join(", ")}
                        </span>
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </div>

            <aside className="min-w-0 rounded-md border border-line p-4">
              {selected ? (
                <BlueprintMappingPanel
                  blueprintId={id}
                  component={selected}
                  connections={connections}
                  componentNames={names}
                  errors={grouped.byEntity.get(errorKey("component", selected.key)) ?? []}
                  unresolved={unresolved}
                  readOnlyReason={
                    readOnly
                      ? `Version ${doc.version.version} is not the current version, so it cannot be edited.`
                      : null
                  }
                  linkOpenable={linkOpenable}
                  linkNote={linkNote}
                  onChanged={() => void refresh()}
                  onOpenNode={onOpenNode}
                  onOpenCitation={onOpenCitation}
                  onRegenerate={(key) =>
                    runJob("Regeneration", () => regenerateBlueprint(id, [key]))
                  }
                  onSelectKey={setSelectedKey}
                  regenerating={busy !== null}
                />
              ) : (
                <div className="text-sm text-muted">
                  <p className="font-medium text-foreground">Mapping</p>
                  <p className="mt-1">
                    Select a component to see what it maps to in the repository, where that is
                    supported, and to edit or lock it.
                  </p>
                </div>
              )}
            </aside>
          </div>
        )}
      </section>

      <BlueprintFilesPanel
        files={files}
        errorsFor={(path) => grouped.byEntity.get(errorKey("proposed_file", path)) ?? []}
        nameFor={(key) => names.get(key)}
        onSelectComponent={setSelectedKey}
      />

      {history}
    </div>
  );
}

type TaskOption = { id: string; title: string; commit: string };

export function BlueprintView({
  repoId,
  commitSha,
  onOpenCitation,
  onOpenNode,
}: {
  repoId: string;
  commitSha: string;
  onOpenCitation?: (citation: Citation) => void;
  onOpenNode: (nodeId: number) => Promise<boolean>;
}) {
  const [list, setList] = useState<Load<Blueprint[]>>({ state: "loading" });
  const [open, setOpen] = useState<Blueprint | null>(null);
  const [tasks, setTasks] = useState<TaskOption[]>([]);
  const [taskId, setTaskId] = useState("");
  const [manualTask, setManualTask] = useState("");
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setList({ state: "loaded", value: await listBlueprints(repoId) });
    } catch (err) {
      setList(errorState(err));
    }
  }, [repoId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load on mount
    void reload();
  }, [reload, open]);

  useEffect(() => {
    let live = true;
    Promise.all(
      readIds(repoId).map(async (tid): Promise<TaskOption | null> => {
        try {
          const s = await getTaskState(tid);
          return { id: tid, title: s.task.title, commit: s.task.commit_sha };
        } catch {
          return null;
        }
      }),
    ).then((opts) => {
      if (live) setTasks(opts.filter((o): o is TaskOption => o !== null));
    });
    return () => {
      live = false;
    };
  }, [repoId]);

  const chosenTask = manualTask.trim() || taskId || tasks[0]?.id || "";

  async function create(event: React.FormEvent) {
    event.preventDefault();
    if (!title.trim() || !chosenTask || busy) return;
    setBusy(true);
    setError(null);
    try {
      const created = await createBlueprint(repoId, chosenTask, title.trim(), commitSha);
      setTitle("");
      setOpen(created);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setError("That task was not found, so no blueprint was created.");
      } else {
        setError(errMsg(err));
      }
    } finally {
      setBusy(false);
    }
  }

  if (open) {
    return (
      <BlueprintDetail
        key={open.id}
        summary={open}
        commitSha={commitSha}
        onBack={() => setOpen(null)}
        onOpenCitation={onOpenCitation}
        onOpenNode={onOpenNode}
      />
    );
  }

  return (
    <div className="space-y-6 p-5">
      <section aria-label="New blueprint" className="space-y-2">
        <h2 className="text-sm font-semibold">New blueprint</h2>
        <p className="text-[11px] text-muted">
          A blueprint turns one Context task into a design: which parts of this repository to reuse
          or change, what to add, how they connect, and where the files would go. It is grounded in
          the static analysis at <span className="font-mono">{commitSha}</span>; nothing is
          executed.
        </p>
        <form onSubmit={create} className="space-y-2">
          <label className="block text-xs text-muted">
            Task
            <select
              value={taskId || tasks[0]?.id || ""}
              onChange={(e) => setTaskId(e.target.value)}
              disabled={tasks.length === 0}
              className="mt-0.5 block w-full rounded-md border border-line bg-transparent px-3 py-2 text-sm"
            >
              {tasks.length === 0 && <option value="">No tasks remembered in this browser</option>}
              {tasks.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.title} ({t.commit.slice(0, 12)})
                </option>
              ))}
            </select>
          </label>
          <label className="block text-xs text-muted">
            Or a task id
            <input
              value={manualTask}
              onChange={(e) => setManualTask(e.target.value)}
              placeholder="Paste a task id to use instead"
              className="mt-0.5 block w-full rounded-md border border-line bg-transparent px-3 py-2 font-mono text-xs"
            />
          </label>
          {!chosenTask && (
            <p className="text-[11px] text-muted">
              A blueprint needs a task. Create one in the Context tab first; that is where the
              requirements and constraints it is designed against come from.
            </p>
          )}
          <label className="block text-xs text-muted">
            Title
            <input
              value={title}
              maxLength={300}
              onChange={(e) => setTitle(e.target.value)}
              className="mt-0.5 block w-full rounded-md border border-line bg-transparent px-3 py-2 text-sm"
            />
          </label>
          <button
            type="submit"
            disabled={busy || !title.trim() || !chosenTask}
            className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            {busy ? "Creating…" : "Create blueprint"}
          </button>
        </form>
        <ErrorLine message={error} />
      </section>

      <section aria-label="Blueprints" className="space-y-2">
        <h2 className="text-sm font-semibold">Blueprints</h2>
        {list.state === "loading" ? (
          <p className="text-xs text-muted">Loading blueprints…</p>
        ) : list.state === "error" ? (
          <ErrorLine message={`Couldn't load blueprints: ${list.message}`} />
        ) : list.value.length === 0 ? (
          <p className="rounded-md border border-dashed border-line px-3 py-2 text-xs text-muted">
            No blueprints exist for this repository yet.
          </p>
        ) : (
          <ul className="space-y-2">
            {list.value.map((b) => (
              <li
                key={b.id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-line p-3"
              >
                <button
                  type="button"
                  onClick={() => setOpen(b)}
                  className="min-w-0 text-left text-sm font-medium hover:text-accent"
                >
                  {b.title}
                  <span className="ml-2 text-[11px] font-normal text-muted">
                    {b.commit_sha.slice(0, 12)}
                    {b.commit_sha !== commitSha && " · different commit"}
                    {!b.current_version_id && " · nothing generated yet"}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
