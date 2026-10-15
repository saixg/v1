# 00 — Product Spec

## What CodeAxis is

A tool that makes an unfamiliar repository understandable through multiple connected views of one shared code graph, where every claim is traceable to source and every gap in understanding is reported rather than hidden.

## Input

| Mode | Behavior |
| --- | --- |
| GitHub URL | Clone at a resolved commit SHA, full history. The SHA is displayed throughout the workspace. |
| Local folder | Browser-side File System Access API read; parsing can run locally with only the graph leaving the machine. |
| ZIP upload | Unpacked in the sandbox with expansion-ratio limits. |

Before analysis begins, the user sees exactly which files will be included, can exclude paths, and is told what will be sent to the model provider.

## Output

A persistent workspace pinned to one commit, containing thirteen surfaces (see `02-features.md`) over one Repo Model, with one Component Inspector reachable from all of them.

## The one principle

Deterministic analysis establishes facts. The language model narrates facts it is given. The model never discovers structure.

This decides most design questions. It is also the only defensible market position: generated repository wikis are commodity — DeepWiki has pre-indexed tens of thousands of public repositories and produces diagrams and grounded chat for free, and open-source clones exist. What none of them do well is tell you how much of the repository they actually resolved.

## In scope for 1.0

Import → Understand → Explore → Trace → Investigate, built completely. Every surface in `02-features.md` ships.

## Explicitly out of scope

| Out | Why |
| --- | --- |
| Writing or editing code, agent-driven changes, patch generation | Different product; needs the graph as a foundation first |
| Runtime tracing and profiling | Requires executing imported code, which violates a non-negotiable |
| Multi-repository and cross-service graphs | One repository per workspace in 1.0 |
| IDE replacement, terminal, autonomous dev environment | Scope explosion with no 1.0 payoff |

The graph, evidence model, and inspector built here are the foundation an agent layer would later sit on. Build them so that remains true.

## Target user

A developer opening a repository they did not write: a new hire, an open-source contributor, a reviewer, an engineer inheriting a system. They need the right parts in the right order, not all of it.

## Success looks like

The user can answer "what is this, how does it work, where do I start, and what breaks if I change this" within twenty minutes on a 2,000-file repository they have never seen — and can verify every answer by clicking through to source.
