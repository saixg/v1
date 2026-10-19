"use client";

import {
  type BlueprintValidationError,
  type ProposedFile,
  type ProposedFileStatus,
} from "@/lib/api";

import { ErrorItems } from "./BlueprintBits";
import { Pill } from "./ContextBits";

// Part 4.G.7: the proposed file structure. Every path is a proposal for the
// snapshot it was designed against; nothing here was created on disk.

const STATUS_STYLE: Record<ProposedFileStatus, { tone: "ok" | "warn" | "neutral"; blurb: string }> = {
  new: { tone: "ok", blurb: "Does not exist in the snapshot; the design would create it." },
  modified: { tone: "warn", blurb: "Exists in the snapshot; the design would change it." },
  unchanged: { tone: "neutral", blurb: "Exists in the snapshot; the design leaves it alone." },
};

interface DirNode {
  name: string;
  dirs: Map<string, DirNode>;
  files: ProposedFile[];
}

function buildTree(files: ProposedFile[]): DirNode {
  const root: DirNode = { name: "", dirs: new Map(), files: [] };
  for (const f of files) {
    const parts = f.path.split("/").filter(Boolean);
    let at = root;
    for (const dir of parts.slice(0, -1)) {
      let next = at.dirs.get(dir);
      if (!next) {
        next = { name: dir, dirs: new Map(), files: [] };
        at.dirs.set(dir, next);
      }
      at = next;
    }
    at.files.push(f);
  }
  return root;
}

function FileRow({
  file,
  errors,
  componentName,
  onSelectComponent,
}: {
  file: ProposedFile;
  errors: BlueprintValidationError[];
  /** Name of the component this file implements, or undefined if it is not in this version. */
  componentName: string | undefined;
  onSelectComponent: (key: string) => void;
}) {
  const s = STATUS_STYLE[file.status];
  const base = file.path.split("/").pop() ?? file.path;
  const refs = file.conventions_refs ?? [];
  const implKey = file.implements_component_key;
  return (
    <li
      className={`space-y-1 rounded-md border p-2.5 text-xs ${
        errors.length > 0 ? "border-rose-600/40 bg-rose-500/5" : "border-line"
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={s.tone} title={s.blurb}>
          {file.status}
        </Pill>
        <span className="font-mono text-sm font-medium" title={file.path}>
          {base}
        </span>
        {file.locked && <Pill tone="neutral">locked</Pill>}
      </div>
      <p className="whitespace-pre-wrap">{file.purpose}</p>
      <p className="text-muted">
        {implKey ? (
          componentName !== undefined ? (
            <>
              Implements{" "}
              <button
                type="button"
                onClick={() => onSelectComponent(implKey)}
                className="font-medium text-foreground hover:text-accent"
              >
                {componentName}
              </button>
            </>
          ) : (
            <span className="text-amber-700 dark:text-amber-400">
              Names component <span className="font-mono">{implKey}</span>, which is not in this
              version.
            </span>
          )
        ) : (
          "Implements no particular component."
        )}
      </p>
      {refs.length > 0 ? (
        <p className="text-muted">
          Placement follows convention{refs.length === 1 ? "" : "s"}{" "}
          <span className="font-mono">{refs.map((r) => `#${r}`).join(" ")}</span>.
        </p>
      ) : (
        <p className="text-amber-700 dark:text-amber-400">
          Uncited placement: no repository convention was recorded as justifying this path, so it
          is a guess about where it belongs, not an observed pattern.
        </p>
      )}
      {errors.length > 0 && <ErrorItems errors={errors} />}
    </li>
  );
}

function Dir({
  node,
  depth,
  ...rest
}: {
  node: DirNode;
  depth: number;
  errorsFor: (path: string) => BlueprintValidationError[];
  nameFor: (key: string) => string | undefined;
  onSelectComponent: (key: string) => void;
}) {
  const dirs = [...node.dirs.values()].sort((a, b) => a.name.localeCompare(b.name));
  const files = [...node.files].sort((a, b) => a.path.localeCompare(b.path));
  return (
    <ul className={`space-y-1.5 ${depth > 0 ? "ml-3 border-l border-line pl-3" : ""}`}>
      {dirs.map((d) => (
        <li key={d.name}>
          <p className="py-0.5 font-mono text-xs font-medium text-muted">{d.name}/</p>
          <Dir node={d} depth={depth + 1} {...rest} />
        </li>
      ))}
      {files.map((f) => (
        <FileRow
          key={f.path}
          file={f}
          errors={rest.errorsFor(f.path)}
          componentName={f.implements_component_key ? rest.nameFor(f.implements_component_key) : undefined}
          onSelectComponent={rest.onSelectComponent}
        />
      ))}
    </ul>
  );
}

export function BlueprintFilesPanel({
  files,
  errorsFor,
  nameFor,
  onSelectComponent,
}: {
  files: ProposedFile[];
  errorsFor: (path: string) => BlueprintValidationError[];
  nameFor: (key: string) => string | undefined;
  onSelectComponent: (key: string) => void;
}) {
  const counts = { new: 0, modified: 0, unchanged: 0 };
  for (const f of files) counts[f.status] += 1;
  const uncited = files.filter((f) => (f.conventions_refs ?? []).length === 0).length;
  return (
    <section aria-label="Proposed file structure" className="space-y-2">
      <h3 className="text-sm font-semibold">Proposed file structure</h3>
      <p className="text-[11px] text-muted">
        Where the design would put code, against the snapshot it was grounded in. Nothing was
        created or changed. Convention numbers are ids from the repository&apos;s detected
        conventions; this view cannot yet look them up.
      </p>
      {files.length === 0 ? (
        <p className="rounded-md border border-dashed border-line px-3 py-2 text-xs text-muted">
          This version proposes no files. That is what the design says, not a sign the structure was
          checked and found complete.
        </p>
      ) : (
        <>
          <p className="text-xs text-muted">
            {counts.new} new · {counts.modified} modified · {counts.unchanged} unchanged
            {uncited > 0 && ` · ${uncited} uncited placement${uncited === 1 ? "" : "s"}`}
          </p>
          <Dir
            node={buildTree(files)}
            depth={0}
            errorsFor={errorsFor}
            nameFor={nameFor}
            onSelectComponent={onSelectComponent}
          />
        </>
      )}
    </section>
  );
}
