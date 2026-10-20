import { describe, expect, it } from "vitest";

import { autoLayout } from "./blueprintLayout";

const e = (source: string, target: string) => ({ source, target });

describe("autoLayout", () => {
  it("places every key exactly once, including isolated ones", () => {
    const pos = autoLayout(["a", "b", "c"], [e("a", "b")]);
    expect(Object.keys(pos).sort()).toEqual(["a", "b", "c"]);
  });

  it("puts a target in a later column than its source", () => {
    const pos = autoLayout(["a", "b", "c"], [e("a", "b"), e("b", "c")]);
    expect(pos.a.x).toBeLessThan(pos.b.x);
    expect(pos.b.x).toBeLessThan(pos.c.x);
  });

  it("uses the longest path, so a node reached two ways sits after both", () => {
    const pos = autoLayout(["a", "b", "c"], [e("a", "b"), e("b", "c"), e("a", "c")]);
    expect(pos.c.x).toBeGreaterThan(pos.b.x);
  });

  it("terminates on a cycle and still lays every node out", () => {
    const keys = ["a", "b", "c"];
    const pos = autoLayout(keys, [e("a", "b"), e("b", "c"), e("c", "a")]);
    expect(Object.keys(pos).sort()).toEqual(keys);
    for (const k of keys) {
      expect(Number.isFinite(pos[k].x)).toBe(true);
      expect(Number.isFinite(pos[k].y)).toBe(true);
    }
  });

  it("survives self-loops and edges naming keys that are not in the version", () => {
    const pos = autoLayout(["a", "b"], [e("a", "a"), e("a", "ghost"), e("ghost", "b"), e("a", "b")]);
    expect(Object.keys(pos).sort()).toEqual(["a", "b"]);
    expect(pos.a.x).toBeLessThan(pos.b.x);
  });

  it("never stacks two nodes on the same point", () => {
    const keys = ["a", "b", "c", "d", "e"];
    const pos = autoLayout(keys, [e("a", "c"), e("b", "c")]);
    const spots = keys.map((k) => `${pos[k].x},${pos[k].y}`);
    expect(new Set(spots).size).toBe(keys.length);
  });

  it("is deterministic and handles an empty design", () => {
    const edges = [e("a", "b"), e("c", "b")];
    expect(autoLayout(["a", "b", "c"], edges)).toEqual(autoLayout(["a", "b", "c"], edges));
    expect(autoLayout([], [])).toEqual({});
  });
});
