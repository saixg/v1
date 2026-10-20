import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, type BlueprintComponent } from "@/lib/api";

const patchMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  patchBlueprintComponent: patchMock,
}));

import { BlueprintMappingPanel } from "./BlueprintMappingPanel";

function component(over: Partial<BlueprintComponent> = {}): BlueprintComponent {
  return {
    id: "c1",
    key: "auth",
    name: "Auth",
    kind: "proposed",
    mapping: "new",
    linked_component_id: null,
    repo_id: "r1",
    commit_sha: "abc",
    ...over,
  };
}

function renderPanel(c: BlueprintComponent, over: { readOnlyReason?: string | null } = {}) {
  const onChanged = vi.fn();
  render(
    <BlueprintMappingPanel
      blueprintId="bp1"
      component={c}
      connections={[]}
      componentNames={new Map()}
      errors={[]}
      unresolved={[]}
      readOnlyReason={over.readOnlyReason ?? null}
      linkOpenable={false}
      linkNote="n/a"
      onChanged={onChanged}
      onOpenNode={async () => true}
      onRegenerate={() => {}}
      onSelectKey={() => {}}
      regenerating={false}
    />,
  );
  return { onChanged };
}

const relation = () => screen.getByLabelText(/relation to the repository/i) as HTMLSelectElement;
const save = () => screen.getByRole("button", { name: /save changes/i });

beforeEach(() => {
  patchMock.mockReset();
  patchMock.mockResolvedValue({});
});

describe("kind and mapping are a single choice", () => {
  it("offers exactly the three valid pairings and no free-form mapping", () => {
    renderPanel(component());
    const options = within(relation()).getAllByRole("option");
    expect(options.map((o) => [(o as HTMLOptionElement).value, o.textContent])).toEqual([
      ["existing", "existing · reuse"],
      ["modified", "modified · extend"],
      ["proposed", "proposed · new"],
    ]);
    // There is no second control through which a contradictory mapping could be typed.
    expect(screen.queryByLabelText(/^mapping$/i)).toBeNull();
  });

  it("sends kind and its paired mapping together, never kind alone", async () => {
    const user = userEvent.setup();
    const { onChanged } = renderPanel(component());
    await user.selectOptions(relation(), "modified");
    await user.type(screen.getByLabelText(/repo entity id/i), "42");
    await user.click(save());

    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1));
    const [bp, id, patch] = patchMock.mock.calls[0];
    expect([bp, id]).toEqual(["bp1", "c1"]);
    expect(patch).toMatchObject({ kind: "modified", mapping: "extend", linked_component_id: 42 });
    expect(onChanged).toHaveBeenCalled();
  });

  it.each([
    ["existing", "reuse"],
    ["modified", "extend"],
  ] as const)("pairs %s with %s", async (kind, mapping) => {
    const user = userEvent.setup();
    renderPanel(component());
    await user.selectOptions(relation(), kind);
    await user.type(screen.getByLabelText(/repo entity id/i), "7");
    await user.click(save());
    await waitFor(() => expect(patchMock).toHaveBeenCalled());
    expect(patchMock.mock.calls[0][2]).toMatchObject({ kind, mapping });
  });

  it("will not save an existing or modified component without a real entity id", async () => {
    const user = userEvent.setup();
    renderPanel(component());
    await user.selectOptions(relation(), "existing");
    expect(save()).toBeDisabled();
    const link = screen.getByLabelText(/repo entity id/i);
    await user.type(link, "abc");
    expect(save()).toBeDisabled();
    await user.clear(link);
    await user.type(link, "0");
    expect(save()).toBeDisabled();
    await user.clear(link);
    await user.type(link, "12");
    expect(save()).toBeEnabled();
  });

  it("clears the link when a component is saved as proposed, and says so", async () => {
    const user = userEvent.setup();
    renderPanel(component({ kind: "existing", mapping: "reuse", linked_component_id: 9 }));
    await user.selectOptions(relation(), "proposed");
    expect(screen.getByText(/removes the link to repo entity #9/i)).toBeInTheDocument();
    await user.click(save());
    await waitFor(() => expect(patchMock).toHaveBeenCalled());
    expect(patchMock.mock.calls[0][2]).toMatchObject({
      kind: "proposed",
      mapping: "new",
      linked_component_id: null,
    });
  });

  it("does not send anything when nothing changed", () => {
    renderPanel(component());
    expect(save()).toBeDisabled();
    expect(patchMock).not.toHaveBeenCalled();
  });
});

describe("refusals and locking", () => {
  it("shows the server's reasons when it refuses an edit with 422", async () => {
    patchMock.mockRejectedValue(
      new ApiError(422, "x", { detail: "Contradictory component", reasons: ["mapping must be reuse"] }),
    );
    const user = userEvent.setup();
    const { onChanged } = renderPanel(component());
    await user.clear(screen.getByLabelText(/^name$/i));
    await user.type(screen.getByLabelText(/^name$/i), "Auth v2");
    await user.click(save());
    expect(await screen.findByText(/server refused this edit/i)).toHaveTextContent(
      "Contradictory component; mapping must be reuse",
    );
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("reloads rather than leaving a stale form when the component vanished (404)", async () => {
    patchMock.mockRejectedValue(new ApiError(404, "gone"));
    const user = userEvent.setup();
    const { onChanged } = renderPanel(component());
    await user.type(screen.getByLabelText(/^name$/i), "!");
    await user.click(save());
    expect(await screen.findByText(/no longer in the blueprint/i)).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalled();
  });

  it("disables every field and states why when the component is locked", () => {
    renderPanel(component({ locked: true, generation_status: "locked" }));
    expect(screen.getByText(/locked: a person approved this component/i)).toBeInTheDocument();
    expect(relation()).toBeDisabled();
    expect(screen.getByLabelText(/^name$/i)).toBeDisabled();
    expect(save()).toBeDisabled();
    expect(screen.getByRole("button", { name: /regenerate this component/i })).toBeDisabled();
  });

  it("explains a read-only older version instead of silently disabling", () => {
    renderPanel(component(), { readOnlyReason: "This is an older version." });
    expect(screen.getByText("This is an older version.")).toBeInTheDocument();
    expect(relation()).toBeDisabled();
  });
});
