import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  resolveApiUrl,
  exportContext,
  getCoverage,
  isCitationCheckFailure,
  isModelFailure,
  isPackageNotCurrent,
  setTokenGetter,
} from "./api";

function jsonResponse(status: number, body: unknown, statusText = ""): Response {
  return new Response(JSON.stringify(body), {
    status,
    statusText,
    headers: { "Content-Type": "application/json" },
  });
}

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  setTokenGetter(async () => null);
});

afterEach(() => {
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});

async function failure(): Promise<ApiError> {
  try {
    await getCoverage("repo-1", "abc");
  } catch (err) {
    expect(err).toBeInstanceOf(ApiError);
    return err as ApiError;
  }
  throw new Error("expected the request to fail");
}

describe("send() error shaping", () => {
  it("keeps a string detail verbatim as the message", async () => {
    fetchMock.mockResolvedValue(jsonResponse(404, { detail: "repo not found" }));
    const err = await failure();
    expect(err.status).toBe(404);
    expect(err.message).toBe("repo not found");
    expect(err.detail).toBe("repo not found");
  });

  it("flattens a structured detail into the message but keeps the parsed object", async () => {
    const detail = { error: "citation_check_failed", detail: "2 unsupported", rejected: [] };
    fetchMock.mockResolvedValue(jsonResponse(422, { detail }));
    const err = await failure();
    expect(err.message).toBe(JSON.stringify(detail));
    expect(err.detail).toEqual(detail);
    expect(isCitationCheckFailure(err.detail)).toBe(true);
  });

  it("falls back to the status line when the error body is not JSON", async () => {
    fetchMock.mockResolvedValue(
      new Response("<html>bad gateway</html>", { status: 502, statusText: "Bad Gateway" }),
    );
    const err = await failure();
    expect(err.status).toBe(502);
    expect(err.message).toBe("502 Bad Gateway");
    expect(err.detail).toBeUndefined();
  });

  it("falls back to the status line when a JSON body has no detail", async () => {
    fetchMock.mockResolvedValue(jsonResponse(500, { oops: true }, "Internal Server Error"));
    const err = await failure();
    expect(err.message).toBe("500 Internal Server Error");
  });

  it("reports a network failure as status 0 naming the API address", async () => {
    fetchMock.mockRejectedValue(new TypeError("fetch failed"));
    const err = await failure();
    expect(err.status).toBe(0);
    // Names the base URL the request actually used, not a build-time constant.
    expect(err.message).toContain(await resolveApiUrl());
  });

  it("sends the bearer token when a getter is registered, and none otherwise", async () => {
    fetchMock.mockImplementation(async () => jsonResponse(200, { coverage: [], unresolved: [] }));
    setTokenGetter(async () => "tok_123");
    await getCoverage("repo-1", "abc");
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get("Authorization")).toBe(
      "Bearer tok_123",
    );

    setTokenGetter(async () => null);
    await getCoverage("repo-1", "abc");
    expect(new Headers(fetchMock.mock.calls[1][1]?.headers).has("Authorization")).toBe(false);
  });

  it("URL-encodes the repo id so it cannot rewrite the path", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { coverage: [], unresolved: [] }));
    await getCoverage("a/b?c", "abc");
    expect(String(fetchMock.mock.calls[0][0])).toContain("/repos/a%2Fb%3Fc/coverage");
  });
});

describe("exportContext", () => {
  it("refuses a response that is not markdown rather than downloading it", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { hello: "world" }));
    await expect(exportContext("t1", "claude_md")).rejects.toMatchObject({
      name: "ApiError",
      message: expect.stringContaining("Unexpected export content type"),
    });
  });

  it("reads filename and package status from the response headers", async () => {
    fetchMock.mockResolvedValue(
      new Response("# ctx", {
        status: 200,
        headers: {
          "Content-Type": "text/markdown; charset=utf-8",
          "Content-Disposition": 'attachment; filename="CLAUDE.md"',
          "X-CodeAxis-Package-Status": "stale",
          "X-CodeAxis-Snapshot": "deadbeef",
        },
      }),
    );
    const file = await exportContext("t1", "claude_md", true);
    expect(file).toEqual({
      filename: "CLAUDE.md",
      markdown: "# ctx",
      packageStatus: "stale",
      snapshot: "deadbeef",
    });
    expect(String(fetchMock.mock.calls[0][0])).toContain("allow_stale=true");
  });
});

describe("detail discriminators", () => {
  it("accept exactly their own error tag", () => {
    expect(isModelFailure({ error: "model_output_invalid", detail: "x", task_id: "t" })).toBe(true);
    expect(isModelFailure({ error: "model_call_failed", detail: "x", task_id: "t" })).toBe(true);
    expect(isPackageNotCurrent({ error: "package_not_current" })).toBe(true);
    expect(isCitationCheckFailure({ error: "citation_check_failed" })).toBe(true);
  });

  it("do not accept each other's tags", () => {
    const model = { error: "model_call_failed" };
    const pkg = { error: "package_not_current" };
    const cite = { error: "citation_check_failed" };
    expect(isPackageNotCurrent(model)).toBe(false);
    expect(isCitationCheckFailure(model)).toBe(false);
    expect(isModelFailure(pkg)).toBe(false);
    expect(isModelFailure(cite)).toBe(false);
    expect(isCitationCheckFailure(pkg)).toBe(false);
  });

  it("reject the shapes a plain string or missing detail produces", () => {
    for (const bad of [undefined, null, "model_call_failed", 502, [], { error: 1 }, {}]) {
      expect(isModelFailure(bad)).toBe(false);
      expect(isPackageNotCurrent(bad)).toBe(false);
      expect(isCitationCheckFailure(bad)).toBe(false);
    }
  });
});
