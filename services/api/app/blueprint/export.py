"""Export a blueprint as a context package (build-plan.md Part 4.G.8:
"implementation context per component ... exportable as a context package
(feeds Pillar 2)").

This module builds `ContextItem` candidates and hands them to
`app.context.package.assemble_context_package` through its `extra_sections`
hook. It deliberately owns no budgeting, no ordering and no renderer: the
package assembler already implements Part 4.F.5's section order and its
"constraints are always included, never truncated" rule, and
`app.context.export.render_export` already renders a stored package as
`CLAUDE.md` / `AGENTS.md` / `context.md` with every untrusted string fenced
and every value scrubbed for secrets. A second copy of any of that would be
a second place for those rules to be wrong.

The result is a real `context_packages` row for the blueprint's task, so the
existing REST, export and read-only MCP delivery paths serve it unchanged —
which is what "feeds Pillar 2" has to mean in practice.

Where each part of Part 4.G.8's per-component context comes from:

| asked for | source |
| --- | --- |
| responsibility | the component's stored `responsibility` |
| relevant existing code | the component's evidence spans and node ids |
| dependencies | the version's connections touching the component |
| requirements | the package's own requirement section (see the gap below) |
| constraints | the package's own constraint section, never truncated |
| suggested location | the proposed files implementing the component |
| verification criteria | the task's acceptance criteria (see the gap below) |

**The gap, stated rather than papered over:** requirements and acceptance
criteria are *not* linked to individual blueprint components anywhere in this
data model, so a component's item cannot say "these three requirements are
yours". Each component item says so explicitly and points at the package's
requirement section; a `limitation` item records it once for the package as a
whole. Splitting them per component would mean a model deciding which
requirement belongs to which component, which is precisely the kind of
structure the model is not allowed to discover.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import psycopg

from app.analysis.semantic.retrieve import QueryEmbedder
from app.context.export import scrub_secrets
from app.context.package import (
    DEFAULT_PACKAGE_BUDGET,
    AssembledPackage,
    Candidate,
    assemble_context_package,
)
from app.llm.tokens import estimate_tokens
from app.models import (
    Blueprint,
    BlueprintComponent,
    BlueprintConnection,
    BlueprintVersion,
    ContextEvidence,
    ContextItem,
    ContextItemKind,
    ProposedFile,
)

from .document import build_document
from .validate import BLOCKING_CODES

ANALYZER = "blueprint.export"
ANALYZER_VERSION = "0.1.0"

_KIND_LABEL = {
    "existing": "already exists in the repository and is reused as it stands",
    "modified": "already exists in the repository and is extended",
    "proposed": "does not exist yet and is new work",
}

VERIFICATION_NOTE = (
    "Verification: see this package's acceptance criteria. CodeAxis does not link an "
    "acceptance criterion to an individual component, so which of them apply here is the "
    "reviewer's call, not a stored fact."
)


class InvalidBlueprintError(Exception):
    """The version failed its own validation, so it is not exportable as
    implementation context. Raised rather than returned: handing an agent a
    design that references entities the validator could not resolve is the
    one outcome Part 4.G.5 exists to prevent."""


def _item(
    blueprint: Blueprint,
    kind: ContextItemKind,
    content: str,
    evidence: ContextEvidence,
) -> ContextItem:
    # `inferred` for every row here: a blueprint is authored, and this
    # codebase's `inferred` already means "a model produced this prose".
    # Neither `parsed` nor `configured` would be true of a proposed design,
    # and the migration's own note is that blueprint rows are not in the
    # origin vocabulary at all — `ContextItem.origin` has no fourth value, so
    # `inferred` is the honest one of the three.
    text = scrub_secrets(content)
    return ContextItem(
        task_id=blueprint.task_id,
        repo_id=blueprint.repo_id,
        commit_sha=blueprint.commit_sha,
        kind=kind,
        content=text,
        origin="inferred",
        evidence=evidence,
        token_count=estimate_tokens(text),
        analyzer=ANALYZER,
        analyzer_version=ANALYZER_VERSION,
    )


def component_item(
    blueprint: Blueprint,
    version: BlueprintVersion,
    component: BlueprintComponent,
    connections: Sequence[BlueprintConnection],
    files: Sequence[ProposedFile],
) -> ContextItem:
    """One component's implementation context. Every line is a stored value;
    nothing is computed by asking a model."""
    outgoing = [c for c in connections if c.source_key == component.key]
    incoming = [c for c in connections if c.target_key == component.key]
    mine = [f for f in files if f.implements_component_key == component.key]

    lines = [
        f"Component {component.key} — {component.name}",
        f"Mapping: {component.mapping} ({component.kind}): "
        f"{_KIND_LABEL.get(component.kind, component.kind)}.",
    ]
    if component.module_key:
        lines.append(f"Module: {component.module_key}")
    if component.confidence is not None:
        lines.append(f"Mapping confidence (model-stated): {component.confidence:.2f}")
    lines.append(
        "Responsibility: " + (component.responsibility or "none stated by the design.")
    )
    if component.interfaces:
        lines.append("Interfaces:")
        lines.extend(
            f"- {i.name} ({i.kind})" + (f": {i.signature}" if i.signature else "")
            for i in component.interfaces
        )
    if component.evidence.spans:
        lines.append(
            "Relevant existing code: "
            + ", ".join(
                f"{s.path}:{s.line_start}-{s.line_end}" for s in component.evidence.spans
            )
            + (" (list capped)" if component.evidence.truncated else "")
        )
    elif component.kind == "proposed":
        lines.append("Relevant existing code: none — this component is new work.")
    if outgoing:
        lines.append(
            "Depends on / sends to: "
            + "; ".join(f"{c.target_key} ({c.kind})" for c in outgoing)
        )
    if incoming:
        lines.append(
            "Used by / receives from: "
            + "; ".join(f"{c.source_key} ({c.kind})" for c in incoming)
        )
    if mine:
        lines.append("Suggested location:")
        lines.extend(f"- {f.path} [{f.status}] — {f.purpose}" for f in mine)
    else:
        lines.append("Suggested location: no file was proposed for this component.")
    lines.append(
        "Requirements and constraints: this package's requirement and constraint sections "
        "apply to the whole task; they are not linked per component."
    )
    lines.append(VERIFICATION_NOTE)

    return _item(
        blueprint,
        "architecture",
        "\n".join(lines),
        ContextEvidence(
            spans=component.evidence.spans,
            node_ids=component.evidence.node_ids,
            documents=[
                {
                    "source": "repo_doc",
                    "ref": f"blueprint:{blueprint.id}/v{version.version}",
                    "component": component.key,
                    "mapping": component.mapping,
                    "generation_status": component.generation_status,
                }
            ],
            notes=component.evidence.notes,
            truncated=component.evidence.truncated,
        ),
    )


def blueprint_candidates(
    conn: psycopg.Connection, version_id: str, *, allow_invalid: bool = False
) -> tuple[dict[str, list[Candidate]], list[dict], str]:
    """`(extra sections, extra unresolved, task id)` for one version.

    Components become `architecture` items, the design's open points become
    `open_question` items, and what the design cannot state becomes
    `limitation` items. Read-only."""
    document = build_document(conn, version_id)
    version, blueprint = document.version, document.blueprint
    blocking = [e for e in version.validation_errors if e.code in BLOCKING_CODES]
    if blocking and not allow_invalid:
        raise InvalidBlueprintError(
            f"blueprint version {version_id} is {version.status} with {len(blocking)} blocking "
            f"validation error(s) (first: {blocking[0].code}); fix or regenerate it before "
            "exporting it as implementation context"
        )

    sections: dict[str, list[Candidate]] = {
        "architecture": [],
        "open_question": [],
        "limitation": [],
    }
    for component in document.components:
        sections["architecture"].append(
            Candidate(
                component_item(
                    blueprint,
                    version,
                    component,
                    document.connections,
                    document.proposed_files,
                ),
                ref=f"blueprint_component:{component.key}",
            )
        )

    unassigned = [f for f in document.proposed_files if f.implements_component_key is None]
    if unassigned:
        sections["architecture"].append(
            Candidate(
                _item(
                    blueprint,
                    "architecture",
                    "Proposed files not attached to any component:\n"
                    + "\n".join(f"- {f.path} [{f.status}] — {f.purpose}" for f in unassigned),
                    ContextEvidence(
                        notes="proposed_files rows with no implements_component_key",
                    ),
                ),
                ref="blueprint_files:unassigned",
            )
        )

    for entry in document.unresolved:
        detail = str(entry.get("detail") or entry.get("kind") or "unresolved")
        sections["open_question"].append(
            Candidate(
                _item(
                    blueprint,
                    "open_question",
                    detail,
                    ContextEvidence(
                        documents=[
                            {
                                "source": "repo_doc",
                                "ref": f"blueprint:{blueprint.id}/v{version.version}",
                                "kind": str(entry.get("kind") or "unresolved"),
                            }
                        ]
                    ),
                ),
                ref=f"blueprint_unresolved:{entry.get('ref') or entry.get('kind')}",
            )
        )

    limitations = [
        "This design is a proposal reviewed against static analysis of the snapshot. No part "
        "of it has been built, run or tested.",
        "Requirements and acceptance criteria are not linked to individual blueprint "
        "components in this data model; the per-component items say what is known about each "
        "component and nothing about which requirement is its own.",
    ]
    if blocking:
        limitations.append(
            f"This blueprint version is {version.status}: "
            + ", ".join(sorted({e.code for e in blocking}))
            + ". It was exported anyway at the caller's explicit request."
        )
    for text in limitations:
        sections["limitation"].append(
            Candidate(
                _item(
                    blueprint,
                    "limitation",
                    text,
                    ContextEvidence(
                        documents=[
                            {
                                "source": "repo_doc",
                                "ref": f"blueprint:{blueprint.id}/v{version.version}",
                            }
                        ]
                    ),
                ),
                ref=f"blueprint_limitation:{len(sections['limitation'])}",
            )
        )

    extra_unresolved = [
        {
            "kind": "blueprint_exported",
            "ref": f"blueprint:{blueprint.id}/v{version.version}",
            "status": version.status,
            "components": len(document.components),
            "proposed_files": len(document.proposed_files),
        }
    ]
    return sections, extra_unresolved, blueprint.task_id


def export_blueprint_context(
    conn: psycopg.Connection,
    version_id: str,
    *,
    token_budget: int = DEFAULT_PACKAGE_BUDGET,
    embedder: QueryEmbedder | None = None,
    allocation: Mapping[str, float] | None = None,
    allow_invalid: bool = False,
) -> AssembledPackage:
    """Assemble and persist a context package for the blueprint's task with
    this version's per-component implementation context folded in. Needs
    `worker_connection`.

    Raises `InvalidBlueprintError` for a version with blocking validation
    errors unless `allow_invalid=True`, in which case the package carries a
    `limitation` item saying so.

    Render the result with `app.context.export.render_export` — there is no
    blueprint-specific renderer, by design.
    """
    sections, unresolved, task_id = blueprint_candidates(
        conn, version_id, allow_invalid=allow_invalid
    )
    return assemble_context_package(
        conn,
        task_id,
        token_budget=token_budget,
        embedder=embedder,
        allocation=allocation,
        extra_sections=sections,
        extra_unresolved=unresolved,
    )


