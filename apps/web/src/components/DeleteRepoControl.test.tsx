import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";

const deleteMock = vi.hoisted(() => vi.fn());
const pushMock = vi.hoisted(() => vi.fn());

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: pushMock }) }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  deleteRepo: deleteMock,
}));

import { DeleteRepoControl } from "./DeleteRepoControl";

beforeEach(() => {
  deleteMock.mockReset();
  pushMock.mockReset();
});

async function openConfirm(onDeleted?: () => void) {
  const user = userEvent.setup();
  render(<DeleteRepoControl repoId="repo-1" onDeleted={onDeleted} />);
  await user.click(screen.getByRole("button", { name: /delete repository/i }));
  return user;
}

describe("DeleteRepoControl", () => {
  it("deletes nothing until the person confirms, and names what is destroyed", async () => {
    await openConfirm();
    expect(deleteMock).not.toHaveBeenCalled();
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveTextContent(/cannot be undone/i);
    for (const what of [/file tree/i, /stored text/i, /chat threads/i, /blueprints/i, /API tokens/i]) {
      expect(dialog).toHaveTextContent(what);
    }
  });

  it("backing out leaves the repository alone", async () => {
    const user = await openConfirm();
    await user.click(screen.getByRole("button", { name: /keep it/i }));
    expect(deleteMock).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("deletes the right repo and then leaves the workspace", async () => {
    deleteMock.mockResolvedValue(undefined);
    const user = await openConfirm();
    await user.click(screen.getByRole("button", { name: /delete permanently/i }));
    expect(deleteMock).toHaveBeenCalledWith("repo-1");
    expect(pushMock).toHaveBeenCalledWith("/");
  });

  it("stays put and explains when a scan is running (409)", async () => {
    deleteMock.mockRejectedValue(
      new ApiError(409, "a scan is in progress for this repo; wait for it to finish before deleting"),
    );
    const onDeleted = vi.fn();
    const user = await openConfirm(onDeleted);
    await user.click(screen.getByRole("button", { name: /delete permanently/i }));
    expect(await screen.findByText(/a scan is in progress/i)).toBeInTheDocument();
    expect(onDeleted).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /delete permanently/i })).toBeEnabled();
  });

  it("treats an already-deleted repo (404) as done rather than as an error", async () => {
    deleteMock.mockRejectedValue(new ApiError(404, "not found"));
    const onDeleted = vi.fn();
    const user = await openConfirm(onDeleted);
    await user.click(screen.getByRole("button", { name: /delete permanently/i }));
    expect(onDeleted).toHaveBeenCalled();
  });

  it("shows any other failure and does not navigate away", async () => {
    deleteMock.mockRejectedValue(new ApiError(403, "forbidden"));
    const user = await openConfirm();
    await user.click(screen.getByRole("button", { name: /delete permanently/i }));
    expect(await screen.findByText("forbidden")).toBeInTheDocument();
    expect(pushMock).not.toHaveBeenCalled();
  });
});
