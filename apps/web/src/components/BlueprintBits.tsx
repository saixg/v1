"use client";

import {
  KIND_MAPPING,
  type BlueprintEvidence,
  type BlueprintStatus,
  type BlueprintValidationError,
  type ComponentKind,
  type ComponentMapping,
  type ContextEvidence,
} from "@/lib/api";

import { Pill } from "./ContextBits";

// Small pieces shared by the Build tab's panels. Component names, responsibilities
// and purposes are model-authored text: they are only ever placed in React text
// nodes (escaped), never as HTML or markdown, and never read as instructions.

/** The existing/proposed/modified distinction is the most important thing the
 * Build tab shows, so it is carried by border style and label as well as hue. */
export const KIND_STYLE: Record<
  ComponentKind,
  { label: string; box: string; tag: string; blurb: string }
> = {
  existing: {
    label: "EXISTING",
    box: "border-2 border-solid border-sky-600 bg-sky-500/10",
    tag: "border-sky-600/50 bg-sky-500/15 text-sky-800 dark:text-sky-300",
    blurb: "Already in the repository; the design reuses it as it is.",
  },
  modified: {
    label: "MODIFIED",
    box: "border-4 border-double border-amber-600 bg-amber-500/10",
    tag: "border-amber-600/50 bg-amber-500/15 text-amber-800 dark:text-amber-300",
    blurb: "Already in the repository; the design changes or extends it.",
  },
  proposed: {
    label: "PROPOSED",
    box: "border-2 border-dashed border-violet-600 bg-violet-500/5",
    tag: "border-dashed border-violet-600/60 bg-violet-500/10 text-violet-800 dark:text-violet-300",
    blurb: "Not in the repository yet; the design would add it.",
  },
};

export function KindBadge({ kind, mapping }: { kind: ComponentKind; mapping: ComponentMapping }) {
  const s = KIND_STYLE[kind];
  const consistent = KIND_MAPPING[kind] === mapping;
  return (
    <span
      className={`inline-flex items-center rounded border px-1.5 py-px text-[10px] font-semibold uppercase leading-4 tracking-wide ${s.tag}`}
      title={s.blurb}
    >
      {s.label} · {mapping}
      {!consistent && " (contradiction)"}
    </span>
  );
}

export function VersionStatusPill({ status }: { status: BlueprintStatus }) {
  const tone = status === "invalid" ? "bad" : status === "draft" ? "warn" : "ok";
  const title = {
    draft: "Generated or edited, and not validated since.",
    validated: "Passed every validation check.",
    invalid: "Failed validation. The reasons are listed with it.",
    approved: "Approved by a person.",
  }[status];
  return (
    <Pill tone={tone} title={title}>
      {status === "invalid" ? "INVALID" : status}
    </Pill>
  );
}

export function errorKey(kind: string | null | undefined, key: string | null | undefined): string {
  return `${kind ?? ""}:${key ?? ""}`;
}

/** Validation errors grouped by the entity they name. Errors that name no
 * entity, or one that is not in this version, land under `unattached` rather
 * than disappearing. */
export function groupErrors(
  errors: BlueprintValidationError[],
  exists: (kind: string, key: string) => boolean,
): { byEntity: Map<string, BlueprintValidationError[]>; unattached: BlueprintValidationError[] } {
  const byEntity = new Map<string, BlueprintValidationError[]>();
  const unattached: BlueprintValidationError[] = [];
  for (const e of errors) {
    if (e.entity_kind && e.entity_key && exists(e.entity_kind, e.entity_key)) {
      const k = errorKey(e.entity_kind, e.entity_key);
      byEntity.set(k, [...(byEntity.get(k) ?? []), e]);
    } else {
      unattached.push(e);
    }
  }
  return { byEntity, unattached };
}

export function ErrorItems({ errors }: { errors: BlueprintValidationError[] }) {
  return (
    <ul className="space-y-1">
      {errors.map((e, i) => (
        <li key={i} className="text-xs">
          <span className="font-mono font-medium">{e.code}</span>{" "}
          <span className="whitespace-pre-wrap">{e.message}</span>
          {e.entity_kind && (
            <span className="ml-1 font-mono text-[11px] text-muted">
              ({e.entity_kind}
              {e.entity_key ? ` ${e.entity_key}` : ""})
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

/** Blueprint evidence may omit any collection; `EvidenceList` needs them all. */
export function normalizeEvidence(e: BlueprintEvidence | undefined): ContextEvidence {
  return {
    spans: e?.spans ?? [],
    node_ids: e?.node_ids ?? [],
    chunk_ids: e?.chunk_ids ?? [],
    documents: e?.documents ?? [],
    notes: e?.notes ?? null,
    truncated: e?.truncated ?? false,
  };
}
