"use client";

import { useEffect, useState } from "react";

import { getChildren, getTreeRoot, type GraphNode } from "@/lib/api";

import { StatusChip } from "./StatusChip";

type Children =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "loaded"; nodes: GraphNode[] }
  | { state: "error"; message: string };

const EXPANDABLE = new Set(["directory", "file"]);

const KIND_GLYPH: Record<string, string> = {
  directory: "▸",
  file: "·",
  symbol: "ƒ",
};

function symbolLabel(node: GraphNode): string {
  const t = typeof node.attrs.node_type === "string" ? node.attrs.node_type : "";
  if (t.startsWith("class")) return "class";
  if (t.startsWith("interface")) return "interface";
  if (t.startsWith("method")) return "method";
  if (t.startsWith("function")) return "function";
  return "symbol";
}

interface TreeProps {
  repoId: string;
  commitSha: string;
  selectedId: number | null;
  onSelect: (node: GraphNode) => void;
}

function TreeItem({ node, depth, ...props }: TreeProps & { node: GraphNode; depth: number }) {
  const [open, setOpen] = useState(false);
  const [children, setChildren] = useState<Children>({ state: "idle" });
  const expandable = EXPANDABLE.has(node.kind) && node.id !== null;
  const selected = node.id !== null && node.id === props.selectedId;

  async function load() {
    if (node.id === null) return;
    setChildren({ state: "loading" });
    try {
      const nodes = await getChildren(props.repoId, node.id, props.commitSha);
      // Folders first, then files, then symbols in source order (server order).
      const rank = (n: GraphNode) => (n.kind === "directory" ? 0 : n.kind === "file" ? 1 : 2);
      nodes.sort((a, b) => rank(a) - rank(b) || (rank(a) < 2 ? a.name.localeCompare(b.name) : 0));
      setChildren({ state: "loaded", nodes });
    } catch (err) {
      setChildren({ state: "error", message: err instanceof Error ? err.message : String(err) });
    }
  }

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && (children.state === "idle" || children.state === "error")) void load();
  }

  function activate() {
    if (node.kind === "directory") toggle();
    else props.onSelect(node);
  }

  const indent = { paddingLeft: `${depth * 14 + 6}px` };

  return (
    <li>
      <div
        className={`group flex items-center gap-1.5 py-0.5 pr-2 text-sm ${
          selected ? "bg-accent/15" : "hover:bg-panel"
        }`}
        style={indent}
      >
        {expandable ? (
          <button
            type="button"
            onClick={toggle}
            aria-label={open ? `Collapse ${node.name}` : `Expand ${node.name}`}
            aria-expanded={open}
            className="w-4 shrink-0 text-center text-xs text-muted"
          >
            <span className={`inline-block ${open ? "rotate-90" : ""}`}>▸</span>
          </button>
        ) : (
          <span className="w-4 shrink-0 text-center text-xs text-muted">
            {KIND_GLYPH[node.kind] ?? "·"}
          </span>
        )}
        <button
          type="button"
          onClick={activate}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
          title={node.path ?? node.name}
        >
          <span className={`truncate ${node.kind === "symbol" ? "font-mono text-xs" : ""}`}>
            {node.name}
            {node.kind === "directory" ? "/" : ""}
          </span>
          {node.kind === "symbol" && (
            <span className="shrink-0 text-[10px] text-muted">
              {symbolLabel(node)} · L{node.line_start}
            </span>
          )}
          <span className="ml-auto" />
          <StatusChip status={node.status} />
        </button>
      </div>

      {open && (
        <>
          {children.state === "loading" && (
            <p className="py-0.5 text-xs text-muted" style={{ paddingLeft: `${(depth + 1) * 14 + 26}px` }}>
              Loading…
            </p>
          )}
          {children.state === "error" && (
            <p className="py-0.5 text-xs text-rose-700 dark:text-rose-300" style={{ paddingLeft: `${(depth + 1) * 14 + 26}px` }}>
              {children.message}
            </p>
          )}
          {children.state === "loaded" && children.nodes.length === 0 && (
            <p className="py-0.5 text-xs text-muted" style={{ paddingLeft: `${(depth + 1) * 14 + 26}px` }}>
              {node.kind === "file"
                ? node.status === "unanalyzed"
                  ? "Not parsed, so no symbols were extracted."
                  : "No functions or classes found."
                : "Empty folder."}
            </p>
          )}
          {children.state === "loaded" && children.nodes.length > 0 && (
            <ul>
              {children.nodes.map((child) => (
                <TreeItem key={child.id ?? child.name} node={child} depth={depth + 1} {...props} />
              ))}
            </ul>
          )}
        </>
      )}
    </li>
  );
}

export function Explorer(props: TreeProps) {
  const { repoId, commitSha } = props;
  const [top, setTop] = useState<Children>({ state: "loading" });
  const [rootName, setRootName] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const root = await getTreeRoot(repoId, commitSha);
        if (cancelled || root.id === null) return;
        setRootName(root.name);
        const nodes = await getChildren(repoId, root.id, commitSha);
        const rank = (n: GraphNode) => (n.kind === "directory" ? 0 : 1);
        nodes.sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
        if (!cancelled) setTop({ state: "loaded", nodes });
      } catch (err) {
        if (!cancelled) {
          setTop({ state: "error", message: err instanceof Error ? err.message : String(err) });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [repoId, commitSha]);

  return (
    <nav aria-label="Explorer" className="text-sm">
      <div className="sticky top-0 z-10 border-b border-line bg-background px-3 py-2">
        <p className="text-xs font-semibold uppercase tracking-wide text-muted">Explorer</p>
        {rootName && <p className="truncate font-mono text-xs">{rootName}</p>}
      </div>
      {top.state === "loading" && <p className="px-3 py-2 text-xs text-muted">Loading the file tree…</p>}
      {top.state === "error" && (
        <p className="px-3 py-2 text-xs text-rose-700 dark:text-rose-300">{top.message}</p>
      )}
      {top.state === "loaded" && top.nodes.length === 0 && (
        <p className="px-3 py-2 text-xs text-muted">The scan found no files at this commit.</p>
      )}
      {top.state === "loaded" && (
        <ul className="py-1">
          {top.nodes.map((n) => (
            <TreeItem key={n.id ?? n.name} node={n} depth={0} {...props} />
          ))}
        </ul>
      )}
    </nav>
  );
}
