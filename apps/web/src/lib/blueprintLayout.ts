// Deterministic fallback layout for a blueprint version that has no stored
// positions yet. Positions are presentation only: nothing here is a claim about
// the design, and none of it is ever written back as semantics.

export interface Point {
  x: number;
  y: number;
}

const COL_WIDTH = 300;
const ROW_HEIGHT = 170;

/** Layered left-to-right layout. Edges that would close a cycle (retries and
 * pub/sub legitimately loop) are ignored for layering only; they are still
 * drawn. Edges with an unknown endpoint are ignored too. */
export function autoLayout(
  keys: string[],
  edges: { source: string; target: string }[],
): Record<string, Point> {
  const known = new Set(keys);
  const out = new Map<string, string[]>(keys.map((k) => [k, []]));
  for (const e of edges) {
    if (known.has(e.source) && known.has(e.target) && e.source !== e.target) {
      out.get(e.source)!.push(e.target);
    }
  }

  // DFS to drop back edges, leaving a DAG.
  const state = new Map<string, 0 | 1 | 2>();
  const dag = new Map<string, string[]>(keys.map((k) => [k, []]));
  const visit = (start: string) => {
    const stack: { key: string; i: number }[] = [{ key: start, i: 0 }];
    state.set(start, 1);
    while (stack.length > 0) {
      const top = stack[stack.length - 1];
      const next = out.get(top.key)![top.i++];
      if (next === undefined) {
        state.set(top.key, 2);
        stack.pop();
        continue;
      }
      const s = state.get(next) ?? 0;
      if (s === 1) continue; // back edge: closes a cycle
      dag.get(top.key)!.push(next);
      if (s === 0) {
        state.set(next, 1);
        stack.push({ key: next, i: 0 });
      }
    }
  };
  for (const k of keys) if (!state.has(k)) visit(k);

  // Longest-path layering over the DAG (Kahn order).
  const indeg = new Map<string, number>(keys.map((k) => [k, 0]));
  for (const targets of dag.values()) for (const t of targets) indeg.set(t, indeg.get(t)! + 1);
  const layer = new Map<string, number>(keys.map((k) => [k, 0]));
  const queue = keys.filter((k) => indeg.get(k) === 0);
  while (queue.length > 0) {
    const k = queue.shift()!;
    for (const t of dag.get(k)!) {
      layer.set(t, Math.max(layer.get(t)!, layer.get(k)! + 1));
      indeg.set(t, indeg.get(t)! - 1);
      if (indeg.get(t) === 0) queue.push(t);
    }
  }

  const rows = new Map<number, number>();
  const result: Record<string, Point> = {};
  for (const k of keys) {
    const l = layer.get(k)!;
    const row = rows.get(l) ?? 0;
    rows.set(l, row + 1);
    result[k] = { x: l * COL_WIDTH, y: row * ROW_HEIGHT };
  }
  return result;
}
