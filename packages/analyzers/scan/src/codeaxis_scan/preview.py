"""Exclusion preview: shown to the user before a scan runs, so excluded
directories, user-added exclusion patterns, and the size cap are visible up
front. Walks with the exact same `scanner.walk()` the real scan uses, so the
preview and the real scan structurally cannot drift apart. No DB, no
network, no exec.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .limits import DEFAULT_LIMITS, ScanLimits
from .scanner import LANGUAGE_BY_EXT, _posix_rel, walk


@dataclass(frozen=True)
class PreviewEntry:
    path: str
    kind: Literal["file", "directory"]
    language: str | None
    size_bytes: int | None
    included: bool
    reason: str | None  # "excluded_dir" | "over_size_cap" | None


def list_included_files(
    root: Path,
    extra_excludes: frozenset[str] = frozenset(),
    limits: ScanLimits = DEFAULT_LIMITS,
) -> list[PreviewEntry]:
    root = root.resolve()
    entries: list[PreviewEntry] = []

    for dirpath, dirnames, filenames in walk(root, extra_excludes):
        rel_dir = _posix_rel(dirpath, root)

        try:
            on_disk_subdirs = sorted(e.name for e in dirpath.iterdir() if e.is_dir())
        except OSError:
            on_disk_subdirs = list(dirnames)
        kept = set(dirnames)
        for name in on_disk_subdirs:
            if name in kept:
                continue
            child_rel = f"{rel_dir}/{name}" if rel_dir else name
            entries.append(
                PreviewEntry(
                    path=child_rel,
                    kind="directory",
                    language=None,
                    size_bytes=None,
                    included=False,
                    reason="excluded_dir",
                )
            )

        for filename in filenames:
            file_path = dirpath / filename
            rel_file = f"{rel_dir}/{filename}" if rel_dir else filename
            language = LANGUAGE_BY_EXT.get(file_path.suffix.lower())
            try:
                size = file_path.stat().st_size
            except OSError:
                size = None

            if size is not None and size > limits.max_file_size_bytes:
                entries.append(
                    PreviewEntry(
                        path=rel_file,
                        kind="file",
                        language=language,
                        size_bytes=size,
                        included=False,
                        reason="over_size_cap",
                    )
                )
            else:
                entries.append(
                    PreviewEntry(
                        path=rel_file,
                        kind="file",
                        language=language,
                        size_bytes=size,
                        included=True,
                        reason=None,
                    )
                )

    return entries
