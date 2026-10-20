import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  type ContextPackageResponse,
  type ContextPackageStatus,
  type PackageNotCurrent,
} from "@/lib/api";

const exportMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  exportContext: exportMock,
}));

import { ContextPackagePanel } from "./ContextPackagePanel";

function pkg(
  status: ContextPackageStatus,
  current: boolean,
  reason: string | null = null,
): ContextPackageResponse {
  return {
    analysis: "static",
    notice: "static",
    package: {
      id: "p1",
      task_id: "t1",
      commit_sha: "abc1234",
      version: 3,
      status,
      token_budget: 12000,
      token_count: 900,
      manifest: { sections: [], dropped_total: 0 },
      retrieval: {},
      unresolved: [],
      analyzer: "context",
      analyzer_version: "1",
      created_at: null,
    },
    freshness: { state: current ? "current" : "stale", current, reason },
    items: [],
  };
}

function renderPanel(p: ContextPackageResponse | null) {
  render(
    <ContextPackagePanel
      taskId="t1"
      archived={false}
      pkg={p}
      loadError={null}
      onAssembled={() => {}}
    />,
  );
}

beforeEach(() => exportMock.mockReset());

describe("package badging", () => {
  it("shows an assembled, current package as current with no warning", () => {
    renderPanel(pkg("assembled", true));
    expect(screen.getByText("current")).toBeInTheDocument();
    expect(screen.queryByText(/do not hand this to an agent/i)).toBeNull();
  });

  it("badges a stale package STALE, gives the reason, and warns", () => {
    renderPanel(pkg("stale", false, "The repo moved to def5678."));
    expect(screen.getByText("STALE")).toBeInTheDocument();
    expect(screen.queryByText("current")).toBeNull();
    expect(screen.getByText(/do not hand this to an agent/i)).toHaveTextContent(
      "The repo moved to def5678.",
    );
  });

  it("badges an invalid package INVALID, not STALE", () => {
    renderPanel(pkg("invalid", false));
    expect(screen.getByText("INVALID")).toBeInTheDocument();
    expect(screen.queryByText("STALE")).toBeNull();
    // With no freshness reason the status itself is the explanation.
    expect(screen.getByText(/do not hand this to an agent/i)).toHaveTextContent(
      "Package status is invalid.",
    );
  });

  it("never calls a package current when either signal disagrees", () => {
    // Assembled by status but the snapshot has moved on.
    renderPanel(pkg("assembled", false, "Snapshot changed."));
    expect(screen.queryByText("current")).toBeNull();
    expect(screen.getByText("STALE")).toBeInTheDocument();
  });

  it("says plainly that nothing was assembled when there is no package", () => {
    renderPanel(null);
    expect(screen.getByText(/no context package has been assembled/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /assemble package/i })).toBeEnabled();
  });
});

describe("exporting a package that is not current", () => {
  const refusal: PackageNotCurrent = {
    error: "package_not_current",
    package_status: "stale",
    reason: "snapshot moved",
    detail: "Re-assemble, or export with a banner.",
  };

  it("shows the refusal and only exports stale content after an explicit second click", async () => {
    const user = userEvent.setup();
    exportMock.mockRejectedValueOnce(new ApiError(409, "refused", refusal));
    renderPanel(pkg("stale", false));

    await user.click(screen.getAllByRole("button", { name: /^download/i })[0]);
    expect(await screen.findByText(/export refused: the package is stale/i)).toBeInTheDocument();
    expect(exportMock).toHaveBeenCalledTimes(1);
    expect(exportMock.mock.calls[0][2]).toBe(false);

    // jsdom has no object-URL support, which the browser download needs.
    URL.createObjectURL = vi.fn(() => "blob:x");
    URL.revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    exportMock.mockResolvedValueOnce({
      filename: "CLAUDE.md",
      markdown: "# x",
      packageStatus: "stale",
      snapshot: "abc",
    });
    await user.click(
      screen.getByRole("button", { name: /export anyway with a stale\/invalid banner/i }),
    );
    expect(exportMock).toHaveBeenCalledTimes(2);
    expect(exportMock.mock.calls[1][2]).toBe(true);
    expect(await screen.findByText(/carries a STALE\/INVALID banner/)).toBeInTheDocument();
  });

  it("shows a non-409 failure as an error, not as an offer to export anyway", async () => {
    const user = userEvent.setup();
    exportMock.mockRejectedValueOnce(new ApiError(500, "boom"));
    renderPanel(pkg("stale", false));
    await user.click(screen.getAllByRole("button", { name: /^download/i })[0]);
    expect(await screen.findByText("boom")).toBeInTheDocument();
    expect(screen.queryByText(/export anyway/i)).toBeNull();
  });
});
