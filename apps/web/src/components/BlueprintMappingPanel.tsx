"use client";

import { useState } from "react";

import {
  ApiError,
  describeRefusal,
  KIND_MAPPING,
  patchBlueprintComponent,
  type BlueprintComponent,
  type BlueprintComponentPatch,
  type BlueprintConnection,
  type BlueprintUnresolved,
  type BlueprintValidationError,
  type Citation,
  type ComponentKind,
} from "@/lib/api";

import { ErrorItems, KIND_STYLE, KindBadge, normalizeEvidence } from "./BlueprintBits";
import { ErrorLine, errMsg, EvidenceList, Pill } from "./ContextBits";

// Part 4.G.3: what a proposed capability maps to. Kind and mapping are one
// choice here, never two: the only pairings offered are the three the server
// accepts, so a contradictory combination cannot be composed.

const PAIRS: ComponentKind[] = ["existing", "modified", "proposed"];

const field = "mt-0.5 block w-full rounded-md border border-line bg-transparent px-2 py-1.5 text-sm";

function EditForm({
  blueprintId,
  component,
  disabledReason,
  onSaved,
}: {
  blueprintId: string;
  component: BlueprintComponent;
  /** Why editing is unavailable, or null when it is. Always stated, never silent. */
  disabledReason: string | null;
  onSaved: () => void;
}) {
  const [name, setName] = useState(component.name);
  const [responsibility, setResponsibility] = useState(component.responsibility ?? "");
  const [moduleKey, setModuleKey] = useState(component.module_key ?? "");
  const [kind, setKind] = useState<ComponentKind>(component.kind);
  const [link, setLink] = useState(
    component.linked_component_id != null ? String(component.linked_component_id) : "",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const needsLink = kind !== "proposed";
  const linkNumber = Number(link);
  const linkValid = !needsLink || (link.trim() !== "" && Number.isInteger(linkNumber) && linkNumber > 0);

  const patch: BlueprintComponentPatch = {};
  if (name.trim() !== component.name) patch.name = name.trim();
  if (responsibility !== (component.responsibility ?? "")) patch.responsibility = responsibility;
  if (moduleKey.trim() !== (component.module_key ?? "")) patch.module_key = moduleKey.trim() || null;
  const nextLink = needsLink ? linkNumber : null;
  if (kind !== component.kind) {
    // Sent together, always: the server refuses a pair that does not belong.
    patch.kind = kind;
    patch.mapping = KIND_MAPPING[kind];
  }
  if (kind !== component.kind || nextLink !== (component.linked_component_id ?? null)) {
    if (linkValid) patch.linked_component_id = nextLink;
  }
  const dirty = Object.keys(patch).length > 0;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!component.id || !dirty || !name.trim() || !linkValid || busy) return;
    setBusy(true);
    setError(null);
    try {
      await patchBlueprintComponent(blueprintId, component.id, {
        ...patch,
        generation_status: "edited",
      });
      onSaved();
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setError("This component is no longer in the blueprint (it changed since this page loaded). Reloading.");
        onSaved();
      } else if (err instanceof ApiError && (err.status === 409 || err.status === 422)) {
        const r = describeRefusal(err.detail, err.message);
        setError(`The server refused this edit: ${[r.summary, ...r.reasons].join("; ")}`);
      } else {
        setError(errMsg(err));
      }
    } finally {
      setBusy(false);
    }
  }

  const locked = disabledReason !== null;
  return (
    <form onSubmit={submit} className="space-y-2 rounded-md border border-line p-3">
      <h4 className="text-xs font-medium text-muted">Edit this component</h4>
      {disabledReason && (
        <p className="rounded border border-dashed border-line px-2 py-1.5 text-[11px] text-muted">
          {disabledReason}
        </p>
      )}
      <label className="block text-xs text-muted">
        Name
        <input
          value={name}
          maxLength={300}
          disabled={locked}
          onChange={(e) => setName(e.target.value)}
          className={field}
        />
      </label>
      <label className="block text-xs text-muted">
        Responsibility
        <textarea
          value={responsibility}
          rows={3}
          disabled={locked}
          onChange={(e) => setResponsibility(e.target.value)}
          className={field}
        />
      </label>
      <label className="block text-xs text-muted">
        Module
        <input
          value={moduleKey}
          disabled={locked}
          onChange={(e) => setModuleKey(e.target.value)}
          className={field}
        />
      </label>
      <label className="block text-xs text-muted">
        Relation to the repository
        <select
          value={kind}
          disabled={locked}
          onChange={(e) => setKind(e.target.value as ComponentKind)}
          className={field}
        >
          {PAIRS.map((k) => (
            <option key={k} value={k}>
              {KIND_STYLE[k].label.toLowerCase()} · {KIND_MAPPING[k]}
            </option>
          ))}
        </select>
      </label>
      <p className="text-[11px] text-muted">{KIND_STYLE[kind].blurb}</p>
      {needsLink && (
        <label className="block text-xs text-muted">
          Repo entity id (a cluster node in the code graph)
          <input
            inputMode="numeric"
            value={link}
            disabled={locked}
            onChange={(e) => setLink(e.target.value)}
            aria-invalid={!linkValid}
            className={`${field} font-mono`}
          />
          {!linkValid && (
            <span className="text-rose-700 dark:text-rose-300">
              An existing or modified component must name a real entity. The server checks that it
              exists in the snapshot.
            </span>
          )}
        </label>
      )}
      {!needsLink && component.linked_component_id != null && (
        <p className="text-[11px] text-amber-700 dark:text-amber-400">
          Saving as proposed removes the link to repo entity #{component.linked_component_id}.
        </p>
      )}
      <ErrorLine message={error} />
      <button
        type="submit"
        disabled={locked || busy || !dirty || !name.trim() || !linkValid || !component.id}
        className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
      >
        {busy ? "Saving…" : "Save changes"}
      </button>
    </form>
  );
}

export function BlueprintMappingPanel({
  blueprintId,
  component,
  connections,
  componentNames,
  errors,
  unresolved,
  readOnlyReason,
  linkOpenable,
  linkNote,
  onChanged,
  onOpenNode,
  onOpenCitation,
  onRegenerate,
  onSelectKey,
  regenerating,
}: {
  blueprintId: string;
  component: BlueprintComponent;
  connections: BlueprintConnection[];
  componentNames: Map<string, string>;
  errors: BlueprintValidationError[];
  unresolved: BlueprintUnresolved[];
  /** Set when the shown version is not editable (an older version). */
  readOnlyReason: string | null;
  linkOpenable: boolean;
  linkNote: string | null;
  onChanged: () => void;
  onOpenNode: (nodeId: number) => Promise<boolean>;
  onOpenCitation?: (citation: Citation) => void;
  onRegenerate: (key: string) => void;
  onSelectKey: (key: string) => void;
  regenerating: boolean;
}) {
  const [lockBusy, setLockBusy] = useState(false);
  const [lockError, setLockError] = useState<string | null>(null);
  const [openNote, setOpenNote] = useState<string | null>(null);

  const linked = component.linked_component_id;
  const locked = component.locked === true || component.generation_status === "locked";
  const touching = connections.filter(
    (c) => c.source_key === component.key || c.target_key === component.key,
  );
  const open = unresolved.filter((u) => u.affects?.includes(component.key));
  const nameOf = (k: string) => componentNames.get(k) ?? `${k} (not in this version)`;

  async function setLocked(next: boolean) {
    if (!component.id) return;
    setLockBusy(true);
    setLockError(null);
    try {
      await patchBlueprintComponent(blueprintId, component.id, {
        locked: next,
        generation_status: next ? "locked" : "edited",
      });
      onChanged();
    } catch (err) {
      setLockError(errMsg(err));
    } finally {
      setLockBusy(false);
    }
  }

  async function openLinked() {
    if (linked == null) return;
    setOpenNote(null);
    const ok = await onOpenNode(linked);
    if (!ok) setOpenNote(`Entity #${linked} could not be opened at the workspace commit.`);
  }

  return (
    <section aria-label={`Mapping for ${component.name}`} className="space-y-4">
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <KindBadge kind={component.kind} mapping={component.mapping} />
          {locked ? (
            <Pill tone="neutral" title="A person approved this. Regeneration may not alter it.">
              locked
            </Pill>
          ) : (
            <Pill tone="neutral">{component.generation_status ?? "generated"}</Pill>
          )}
          {component.module_key && (
            <span className="font-mono text-[11px] text-muted">module {component.module_key}</span>
          )}
        </div>
        <h3 className="break-words text-base font-semibold">{component.name}</h3>
        <p className="font-mono text-[11px] text-muted">{component.key}</p>
      </div>

      {errors.length > 0 && (
        <div className="space-y-1 rounded-md border border-rose-600/40 bg-rose-500/5 p-3">
          <p className="text-xs font-medium text-rose-700 dark:text-rose-300">
            Validation found {errors.length} problem{errors.length === 1 ? "" : "s"} with this
            component.
          </p>
          <ErrorItems errors={errors} />
        </div>
      )}

      <div className="space-y-1">
        <h4 className="text-xs font-medium text-muted">Maps to</h4>
        {component.kind === "proposed" ? (
          <p className="rounded-md border border-dashed border-violet-600/50 px-3 py-2 text-xs">
            Not in the repo yet. This is a proposal, so there is no existing entity to link to.
          </p>
        ) : linked == null ? (
          <p className="rounded-md border border-rose-600/40 bg-rose-500/5 px-3 py-2 text-xs text-rose-700 dark:text-rose-300">
            This component says it exists in the repository but names no entity. That is a defect
            in the design, not a missing link to ignore.
          </p>
        ) : (
          <div className="space-y-1 rounded-md border border-line px-3 py-2 text-xs">
            <p>
              Repo entity <span className="font-mono">#{linked}</span>
            </p>
            {linkOpenable ? (
              <button
                type="button"
                onClick={openLinked}
                className="rounded-md border border-line px-2.5 py-1 font-medium hover:bg-panel"
              >
                Open in Explorer and Inspector
              </button>
            ) : (
              <p className="text-muted">{linkNote}</p>
            )}
            {openNote && (
              <p role="alert" className="text-rose-700 dark:text-rose-300">
                {openNote}
              </p>
            )}
          </div>
        )}
      </div>

      <div className="space-y-1">
        <h4 className="text-xs font-medium text-muted">Responsibility</h4>
        {component.responsibility ? (
          <p className="whitespace-pre-wrap text-sm">{component.responsibility}</p>
        ) : (
          <p className="text-xs text-muted">No responsibility was recorded.</p>
        )}
      </div>

      <div className="space-y-1">
        <h4 className="text-xs font-medium text-muted">Interfaces</h4>
        {(component.interfaces ?? []).length === 0 ? (
          <p className="text-xs text-muted">No interfaces were recorded for this component.</p>
        ) : (
          <ul className="space-y-1">
            {component.interfaces!.map((i, n) => (
              <li key={n} className="text-xs">
                <Pill tone="neutral">{i.kind}</Pill> <span className="font-medium">{i.name}</span>
                {i.signature && <code className="ml-1 font-mono text-[11px] text-muted">{i.signature}</code>}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="space-y-1">
        <h4 className="text-xs font-medium text-muted">Evidence</h4>
        <EvidenceList evidence={normalizeEvidence(component.evidence)} onOpen={onOpenCitation} />
        {component.evidence?.notes && (
          <p className="whitespace-pre-wrap text-[11px] text-muted">{component.evidence.notes}</p>
        )}
        <p className="text-[11px] text-muted">
          {component.confidence == null
            ? "No confidence was recorded for this mapping."
            : `Mapping confidence ${component.confidence.toFixed(2)}.`}
        </p>
      </div>

      {open.length > 0 && (
        <div className="space-y-1">
          <h4 className="text-xs font-medium text-muted">Open questions blocking this</h4>
          <ul className="space-y-1">
            {open.map((u, i) => (
              <li
                key={i}
                className="rounded-md border border-amber-600/40 bg-amber-500/10 px-3 py-2 text-sm"
              >
                <span className="whitespace-pre-wrap">{u.question}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="space-y-1">
        <h4 className="text-xs font-medium text-muted">Connections ({touching.length})</h4>
        {touching.length === 0 ? (
          <p className="text-xs text-muted">No connection touches this component.</p>
        ) : (
          <ul className="space-y-0.5 text-xs">
            {touching.map((c) => {
              const outgoing = c.source_key === component.key;
              const other = outgoing ? c.target_key : c.source_key;
              return (
                <li key={c.key}>
                  <Pill tone="neutral">{c.kind.replace("_", " ")}</Pill> {outgoing ? "to" : "from"}{" "}
                  <button
                    type="button"
                    onClick={() => onSelectKey(other)}
                    disabled={!componentNames.has(other)}
                    className="font-medium hover:text-accent disabled:hover:text-inherit"
                  >
                    {nameOf(other)}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={readOnlyReason !== null || lockBusy || !component.id}
            onClick={() => setLocked(!locked)}
            className="rounded-md border border-line px-3 py-1.5 text-xs font-medium hover:bg-panel disabled:opacity-50"
            title={
              locked
                ? "Allow edits and regeneration to change this component again."
                : "Approve this component: regeneration will refuse to alter it."
            }
          >
            {lockBusy ? "Saving…" : locked ? "Unlock" : "Lock as approved"}
          </button>
          <button
            type="button"
            disabled={readOnlyReason !== null || locked || regenerating}
            onClick={() => onRegenerate(component.key)}
            className="rounded-md border border-line px-3 py-1.5 text-xs font-medium hover:bg-panel disabled:opacity-50"
            title={
              locked ? "Locked components are not regenerated. Unlock it first." : undefined
            }
          >
            Regenerate this component
          </button>
        </div>
        <ErrorLine message={lockError} />
      </div>

      <EditForm
        key={`${component.key}:${component.id ?? ""}:${component.name}:${component.kind}:${component.generation_status ?? ""}`}
        blueprintId={blueprintId}
        component={component}
        disabledReason={
          readOnlyReason ??
          (locked
            ? "Locked: a person approved this component, so it cannot be edited while it is locked. Unlock it to change it."
            : null)
        }
        onSaved={onChanged}
      />
    </section>
  );
}
