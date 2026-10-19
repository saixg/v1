"use client";

import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  putBlueprintLayout,
  type BlueprintComponent,
  type BlueprintConnection,
  type BlueprintLayout,
  type ConnectionKind,
} from "@/lib/api";
import type { Point } from "@/lib/blueprintLayout";

import { KIND_STYLE, KindBadge } from "./BlueprintBits";
import { errMsg } from "./ContextBits";

// The workflow, drawn from the blueprint document. Layout is positions only and
// lives apart from the design: dragging a node writes to the layout route and
// can never create a blueprint version or change what the design says.

type CompNode = Node<{ component: BlueprintComponent; issues: number }, "component">;

function ComponentNode({ data, selected }: NodeProps<CompNode>) {
  const c = data.component;
  const style = KIND_STYLE[c.kind] ?? KIND_STYLE.proposed;
  return (
    <div
      className={`w-52 rounded-lg px-3 py-2 text-xs shadow-sm ${style.box} ${
        selected ? "ring-2 ring-accent ring-offset-1 ring-offset-background" : ""
      } ${data.issues > 0 ? "outline outline-2 outline-rose-600" : ""}`}
    >
      <Handle type="target" position={Position.Left} />
      <div className="flex flex-wrap items-center gap-1">
        <KindBadge kind={c.kind} mapping={c.mapping} />
        {c.locked && (
          <span className="rounded border border-line bg-panel px-1 text-[10px] font-medium text-muted">
            locked
          </span>
        )}
      </div>
      <p className="mt-1 break-words text-sm font-medium leading-snug text-foreground">{c.name}</p>
      {c.kind === "proposed" ? (
        <p className="mt-0.5 text-[11px] text-muted">not in the repo yet</p>
      ) : c.linked_component_id === null || c.linked_component_id === undefined ? (
        <p className="mt-0.5 text-[11px] font-medium text-rose-700 dark:text-rose-300">
          no linked repo entity
        </p>
      ) : (
        <p className="mt-0.5 text-[11px] text-muted">repo entity #{c.linked_component_id}</p>
      )}
      {data.issues > 0 && (
        <p className="mt-1 text-[11px] font-medium text-rose-700 dark:text-rose-300">
          {data.issues} validation issue{data.issues === 1 ? "" : "s"}
        </p>
      )}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const NODE_TYPES = { component: ComponentNode };

export const EDGE_STYLE: Record<
  ConnectionKind,
  { label: string; color: string; dash?: string; blurb: string }
> = {
  control_flow: {
    label: "control flow",
    color: "#71717a",
    blurb: "One step hands control to the next.",
  },
  data_flow: {
    label: "data flow",
    color: "#3b82f6",
    dash: "7 4",
    blurb: "Data moves from source to target.",
  },
  dependency: {
    label: "dependency",
    color: "#a1a1aa",
    dash: "2 4",
    blurb: "The source needs the target to exist. Cycles are invalid here.",
  },
  event: {
    label: "event",
    color: "#8b5cf6",
    dash: "10 3 2 3",
    blurb: "The source emits something the target reacts to.",
  },
};

const ERROR_COLOR = "#e11d48";

export function Legend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted">
      {(Object.keys(KIND_STYLE) as (keyof typeof KIND_STYLE)[]).map((k) => (
        <span key={k} className="inline-flex items-center gap-1.5" title={KIND_STYLE[k].blurb}>
          <span className={`inline-block h-3 w-5 rounded-sm ${KIND_STYLE[k].box}`} aria-hidden />
          {KIND_STYLE[k].label.toLowerCase()}
        </span>
      ))}
      <span aria-hidden>|</span>
      {(Object.keys(EDGE_STYLE) as ConnectionKind[]).map((k) => (
        <span key={k} className="inline-flex items-center gap-1.5" title={EDGE_STYLE[k].blurb}>
          <svg width="26" height="8" aria-hidden>
            <line
              x1="0"
              y1="4"
              x2="26"
              y2="4"
              stroke={EDGE_STYLE[k].color}
              strokeWidth="2"
              strokeDasharray={EDGE_STYLE[k].dash}
            />
          </svg>
          {EDGE_STYLE[k].label}
        </span>
      ))}
    </div>
  );
}

const SAVE_DELAY_MS = 800;

type SaveState = "idle" | "pending" | "saving" | "saved" | { error: string };

export function BlueprintCanvas({
  blueprintId,
  components,
  connections,
  initialPositions,
  baseLayout,
  persist,
  issueCountByComponent,
  issueKeysForConnections,
  selectedKey,
  onSelect,
}: {
  blueprintId: string;
  components: BlueprintComponent[];
  connections: BlueprintConnection[];
  initialPositions: Record<string, Point>;
  /** The stored layout, kept so a save preserves its edge routes and viewport. */
  baseLayout: BlueprintLayout;
  /** False for an older version: it can be arranged on screen but not saved. */
  persist: boolean;
  issueCountByComponent: Map<string, number>;
  issueKeysForConnections: Set<string>;
  selectedKey: string | null;
  onSelect: (key: string | null) => void;
}) {
  const [positions, setPositions] = useState(initialPositions);
  const positionsRef = useRef(initialPositions);
  const [save, setSave] = useState<SaveState>("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dirty = useRef(false);
  const live = useRef(true);

  const keys = useMemo(() => components.map((c) => c.key), [components]);

  const buildLayout = useCallback((): BlueprintLayout => {
    const known = new Set(keys);
    return {
      ...baseLayout,
      nodes: keys.map((k) => ({ key: k, ...positionsRef.current[k] })).filter((n) => known.has(n.key)),
    };
  }, [keys, baseLayout]);

  const flush = useCallback(async () => {
    timer.current = null;
    if (!dirty.current || !persist) return;
    dirty.current = false;
    if (live.current) setSave("saving");
    try {
      await putBlueprintLayout(blueprintId, buildLayout());
      if (live.current) setSave((s) => (dirty.current ? s : "saved"));
    } catch (err) {
      dirty.current = true;
      if (live.current) setSave({ error: errMsg(err) });
    }
  }, [blueprintId, buildLayout, persist]);

  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      if (timer.current) clearTimeout(timer.current);
      // Do not lose a drag made less than a debounce interval before leaving.
      if (dirty.current && persist) {
        dirty.current = false;
        void putBlueprintLayout(blueprintId, buildLayout()).catch(() => {});
      }
    };
  }, [blueprintId, buildLayout, persist]);

  const onNodesChange = useCallback(
    (changes: NodeChange<CompNode>[]) => {
      const moved: Record<string, Point> = {};
      for (const c of changes) {
        if (c.type === "position" && c.position) moved[c.id] = c.position;
      }
      if (Object.keys(moved).length > 0) {
        const next = { ...positionsRef.current, ...moved };
        positionsRef.current = next;
        setPositions(next);
      }
    },
    [],
  );

  const onDragStop = useCallback(() => {
    if (!persist) return;
    dirty.current = true;
    setSave("pending");
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => void flush(), SAVE_DELAY_MS);
  }, [persist, flush]);

  const nodes: CompNode[] = useMemo(
    () =>
      components.map((c) => ({
        id: c.key,
        type: "component",
        position: positions[c.key] ?? { x: 0, y: 0 },
        selected: c.key === selectedKey,
        data: { component: c, issues: issueCountByComponent.get(c.key) ?? 0 },
      })),
    [components, positions, selectedKey, issueCountByComponent],
  );

  // An edge whose endpoint is not a component of this version cannot be drawn;
  // the caller lists those instead of this view inventing a node for them.
  const edges: Edge[] = useMemo(() => {
    const present = new Set(keys);
    return connections
      .filter((e) => present.has(e.source_key) && present.has(e.target_key))
      .map((e) => {
        const s = EDGE_STYLE[e.kind];
        const bad = issueKeysForConnections.has(e.key);
        const color = bad ? ERROR_COLOR : s.color;
        return {
          id: e.key,
          source: e.source_key,
          target: e.target_key,
          label: `${s.label}${bad ? " (invalid)" : ""}`,
          markerEnd: { type: MarkerType.ArrowClosed, color },
          style: { stroke: color, strokeWidth: bad ? 3 : 2, strokeDasharray: s.dash },
          labelStyle: { fontSize: 10, fill: "var(--foreground)" },
          labelBgStyle: { fill: "var(--background)" },
          labelBgPadding: [4, 2] as [number, number],
          selectable: false,
          focusable: false,
        };
      });
  }, [connections, keys, issueKeysForConnections]);

  return (
    <div className="space-y-1">
      <div
        className="h-[28rem] rounded-md border border-line"
        role="group"
        aria-label="Blueprint workflow. The component list below offers the same selection by keyboard."
      >
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={NODE_TYPES}
          onNodesChange={onNodesChange}
          onNodeDragStop={onDragStop}
          onNodeClick={(_, n) => onSelect(n.id)}
          onPaneClick={() => onSelect(null)}
          nodesConnectable={false}
          deleteKeyCode={null}
          defaultViewport={baseLayout.viewport}
          fitView={!baseLayout.viewport}
          fitViewOptions={{ padding: 0.2 }}
          minZoom={0.2}
          colorMode="system"
          proOptions={{ hideAttribution: true }}
        >
          <Background />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      <p className="text-[11px] text-muted" aria-live="polite">
        {!persist
          ? "Older version: you can rearrange it on screen, but its layout is not saved."
          : save === "pending" || save === "saving"
            ? "Saving layout…"
            : save === "saved"
              ? "Layout saved. Arranging nodes never creates a new version."
              : typeof save === "object"
                ? `Layout not saved: ${save.error}`
                : "Drag nodes to arrange them. Positions are saved apart from the design and never create a new version."}
      </p>
    </div>
  );
}
