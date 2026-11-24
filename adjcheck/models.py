"""Domain data structures shared by the storage, service and web layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .geometry import Rect, Region


@dataclass(frozen=True)
class Project:
    name: str
    extent: Rect
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Sheet:
    no: str
    rect: Rect


@dataclass(frozen=True)
class Issue:
    """A stored missing/overlap region."""

    kind: str
    rect: Rect
    sheet_ids: tuple[str, ...] = ()
    id: int | None = None


@dataclass(frozen=True)
class BatchEntry:
    sheet_no: str
    rect: Rect


@dataclass(frozen=True)
class Stats:
    missing_count: int = 0
    missing_area: int = 0
    overlap_count: int = 0
    overlap_area: int = 0

    @classmethod
    def from_issues(cls, issues: Iterable[Issue | Region]) -> "Stats":
        missing_count = missing_area = overlap_count = overlap_area = 0
        for issue in issues:
            if issue.kind == "missing":
                missing_count += 1
                missing_area += issue.rect.area
            elif issue.kind == "overlap":
                overlap_count += 1
                overlap_area += issue.rect.area
            else:  # pragma: no cover - guarded by the database CHECK too
                raise ValueError(f"未知问题类型: {issue.kind!r}")
        return cls(missing_count, missing_area, overlap_count, overlap_area)

    @property
    def problem_area(self) -> int:
        return self.missing_area + self.overlap_area


@dataclass
class SplitResult:
    """How stored issue rows relate to an incremental recomputation window."""

    untouched: list[Issue] = field(default_factory=list)
    replaced: list[Issue] = field(default_factory=list)
    outside: list[Region] = field(default_factory=list)
    in_window: list[Region] = field(default_factory=list)


@dataclass
class CorrectionPlan:
    """A complete, side-effect-free description of one correction attempt."""

    status: str  # accepted | rejected | stale
    reasons: list[str]
    batch: list[BatchEntry]
    window: list[Rect]
    before: Stats = Stats()
    after: Stats = Stats()
    eliminated: Stats = Stats()
    new_problems: Stats = Stats()
    split: SplitResult | None = None
    project_version: int = 0
    expected_version: int | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"
