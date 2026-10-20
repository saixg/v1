// Covers the runtime API-URL resolution added so the backend can move
// without rebuilding the bundle. The ordering rules here are the whole point
// of the feature, so they are pinned rather than assumed.
//
// `api.ts` reads NEXT_PUBLIC_API_URL at module scope, so each case re-imports
// the module with `vi.resetModules()` after setting the environment — editing
// `process.env` after the fact would not change the already-captured value.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ORIGINAL = process.env.NEXT_PUBLIC_API_URL;

async function loadApi(buildTimeUrl: string | undefined) {
  vi.resetModules();
  if (buildTimeUrl === undefined) delete process.env.NEXT_PUBLIC_API_URL;
  else process.env.NEXT_PUBLIC_API_URL = buildTimeUrl;
  return import("./api");
}

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  if (ORIGINAL === undefined) delete process.env.NEXT_PUBLIC_API_URL;
  else process.env.NEXT_PUBLIC_API_URL = ORIGINAL;
});

describe("resolveApiUrl", () => {
  it("uses the build-time value without asking the server", async () => {
    const { resolveApiUrl } = await loadApi("https://api.build-time.example");
    await expect(resolveApiUrl()).resolves.toBe("https://api.build-time.example");
    // The point of preferring it: no request at all on the common path.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("strips a trailing slash so paths do not end up doubled", async () => {
    const { resolveApiUrl } = await loadApi("https://api.example.com/");
    await expect(resolveApiUrl()).resolves.toBe("https://api.example.com");
  });

  it("asks /api/config when no build-time value was compiled in", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ apiUrl: "https://api.runtime.example", configured: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const { resolveApiUrl } = await loadApi(undefined);
    await expect(resolveApiUrl()).resolves.toBe("https://api.runtime.example");
    expect(fetchMock).toHaveBeenCalledWith("/api/config", { cache: "no-store" });
  });

  it("resolves once and shares the result across concurrent callers", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ apiUrl: "https://api.once.example" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const { resolveApiUrl } = await loadApi(undefined);
    const [a, b, c] = await Promise.all([resolveApiUrl(), resolveApiUrl(), resolveApiUrl()]);
    expect([a, b, c]).toEqual([
      "https://api.once.example",
      "https://api.once.example",
      "https://api.once.example",
    ]);
    // Concurrent first calls must share one request, not race to make three.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("falls back to localhost when the config route is unreachable", async () => {
    fetchMock.mockRejectedValue(new TypeError("fetch failed"));
    const { resolveApiUrl } = await loadApi(undefined);
    await expect(resolveApiUrl()).resolves.toBe("http://localhost:8000");
  });

  it("falls back when the config route answers without a usable apiUrl", async () => {
    // An unconfigured deployment returns an empty string rather than guessing;
    // the client must not treat that as an address.
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ apiUrl: "", configured: false }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const { resolveApiUrl } = await loadApi(undefined);
    await expect(resolveApiUrl()).resolves.toBe("http://localhost:8000");
  });

  it("falls back when the config route errors", async () => {
    fetchMock.mockResolvedValue(new Response("nope", { status: 500 }));
    const { resolveApiUrl } = await loadApi(undefined);
    await expect(resolveApiUrl()).resolves.toBe("http://localhost:8000");
  });
});
