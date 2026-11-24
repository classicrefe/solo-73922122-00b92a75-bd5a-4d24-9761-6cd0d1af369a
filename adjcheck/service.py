"""Application rules: parsing, incremental verification, correction planning.

The correction flow is intentionally split into a pure planning step and a
transactional write step:

* planning reads the catalog and stored verification result, validates the
  batch, recomputes *only* the rectangles covered by the batch's old and new
  extents, and decides whether the batch may be applied;
* writing replaces the batch sheets and the affected issue rows in one SQLite
  transaction guarded by the project version.

Nothing in the planning path loops over coordinate units or areas, so the cost
depends on the number of sheets and the number of stored issue rectangles,
never on the coordinate span.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from .db import Database, StorageError
from .geometry import (
    Rect,
    Region,
    analyze_rects,
    area_outside,
    merge_regions,
    subtract_rect,
    union_decompose,
)
from .models import (
    BatchEntry,
    CorrectionPlan,
    Issue,
    Project,
    Sheet,
    SplitResult,
    Stats,
)

MAX_SHEETS = 100
MAX_BATCH = 4
SHEET_NO_RE = re.compile(r"^[^\s,;，；]{1,64}$")
PROJECT_NAME_RE = re.compile(r"^[\w.\-\u4e00-\u9fff]{1,60}$")


class ValidationError(ValueError):
    def __init__(self, reasons: Iterable[str]):
        self.reasons = list(reasons)
        super().__init__("；".join(self.reasons) or "输入无效")


def _parse_int(raw: str, label: str, reasons: list[str]) -> int | None:
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        reasons.append(f"{label} 必须是整数米，收到 {raw!r}")
        return None


def parse_extent(x0: str, y0: str, x1: str, y1: str) -> Rect:
    reasons: list[str] = []
    values = [
        _parse_int(x0, "应交范围 x0", reasons),
        _parse_int(y0, "应交范围 y0", reasons),
        _parse_int(x1, "应交范围 x1", reasons),
        _parse_int(y1, "应交范围 y1", reasons),
    ]
    if reasons:
        raise ValidationError(reasons)
    try:
        rect = Rect(*values)  # type: ignore[arg-type]
    except ValueError:
        raise ValidationError(["应交范围必须满足 x0<x1 且 y0<y1"])
    if rect.width <= 0 or rect.height <= 0:
        raise ValidationError(["应交范围必须满足 x0<x1 且 y0<y1"])
    return rect


def parse_sheet_line(line: str, line_no: int) -> BatchEntry:
    parts = [part for part in re.split(r"[\s,;，；]+", line.strip()) if part]
    if len(parts) != 5:
        raise ValidationError(
            [f"第 {line_no} 行格式应为“图幅号 x0 y0 x1 y1”，收到 {line.strip()!r}"]
        )
    no, *coords = parts
    if not SHEET_NO_RE.match(no):
        raise ValidationError([f"第 {line_no} 行图幅号非法: {no!r}（不可含空白，最长 64 字符）"])
    reasons: list[str] = []
    ints = [_parse_int(value, f"第 {line_no} 行坐标", reasons) for value in coords]
    if reasons:
        raise ValidationError(reasons)
    try:
        rect = Rect(*ints)  # type: ignore[arg-type]
    except ValueError:
        raise ValidationError([f"第 {line_no} 行矩形必须满足 x0<x1 且 y0<y1: {line.strip()!r}"])
    return BatchEntry(no, rect)


def parse_sheet_lines(text: str, *, max_entries: int, context: str) -> list[BatchEntry]:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines:
        raise ValidationError([f"{context}至少需要 1 张图幅"])
    if len(lines) > max_entries:
        raise ValidationError([f"{context}最多 {max_entries} 张图幅，当前 {len(lines)} 张"])
    entries: list[BatchEntry] = []
    seen: set[str] = set()
    reasons: list[str] = []
    for index, line in enumerate(lines, start=1):
        try:
            entry = parse_sheet_line(line, index)
        except ValidationError as exc:
            reasons.extend(exc.reasons)
            continue
        if entry.sheet_no in seen:
            reasons.append(f"图幅号重复: {entry.sheet_no}")
            continue
        seen.add(entry.sheet_no)
        entries.append(entry)
    if reasons:
        raise ValidationError(reasons)
    return entries


def parse_batch_json(payload: object) -> list[BatchEntry]:
    if not isinstance(payload, list):
        raise ValidationError(["batch 必须是数组"])
    if not payload:
        raise ValidationError(["至少提供 1 张待更正图幅"])
    if len(payload) > MAX_BATCH:
        raise ValidationError([f"一次最多更正 {MAX_BATCH} 张图幅"])
    reasons: list[str] = []
    entries: list[BatchEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            reasons.append(f"第 {index} 项应为对象")
            continue
        no = str(item.get("no", "")).strip()
        if not SHEET_NO_RE.match(no):
            reasons.append(f"第 {index} 项图幅号非法")
            continue
        if no in seen:
            reasons.append(f"图幅号重复: {no}")
            continue
        try:
            rect = Rect(
                int(item["x0"]),
                int(item["y0"]),
                int(item["x1"]),
                int(item["y1"]),
            )
        except (KeyError, TypeError, ValueError):
            reasons.append(f"第 {index} 项坐标必须是满足 x0<x1、y0<y1 的整数")
            continue
        seen.add(no)
        entries.append(BatchEntry(no, rect))
    if reasons:
        raise ValidationError(reasons)
    return entries


def validate_catalog(sheets: Sequence[Sheet]) -> None:
    reasons: list[str] = []
    if not sheets:
        reasons.append("至少需要 1 张图幅")
    if len(sheets) > MAX_SHEETS:
        reasons.append(f"最多 {MAX_SHEETS} 张图幅，当前 {len(sheets)} 张")
    seen: set[str] = set()
    for sheet in sheets:
        if not SHEET_NO_RE.match(sheet.no):
            reasons.append(f"图幅号非法: {sheet.no!r}")
        if sheet.no in seen:
            reasons.append(f"图幅号重复: {sheet.no}")
        seen.add(sheet.no)
    if reasons:
        raise ValidationError(reasons)


# --------------------------------------------------------------------- reads


def full_check(project: Project, sheets: Sequence[Sheet]) -> list[Region]:
    return analyze_rects(project.extent, [(s.no, s.rect) for s in sheets])


def _subtract_many(rect: Rect, cutters: Sequence[Rect]) -> list[Rect]:
    remaining = [rect]
    for cutter in cutters:
        next_remaining: list[Rect] = []
        for piece in remaining:
            next_remaining.extend(subtract_rect(piece, cutter))
        remaining = next_remaining
        if not remaining:
            break
    return remaining


def _clip_parts(rect: Rect, parts: Sequence[Rect]) -> list[Rect]:
    clipped = []
    for part in parts:
        piece = rect.clipped(part)
        if piece is not None:
            clipped.append(piece)
    return clipped


@dataclass
class _BatchContext:
    old_batch: list[tuple[str, Rect]]
    new_batch: list[tuple[str, Rect]]
    window: list[Rect]
    new_sheets: list[tuple[str, Rect]]
    added: list[str]
    replaced: list[str]


def _build_context(
    project: Project,
    sheets: Sequence[Sheet],
    batch: Sequence[BatchEntry],
) -> _BatchContext:
    existing = {sheet.no: sheet.rect for sheet in sheets}
    reasons: list[str] = []
    if not 1 <= len(batch) <= MAX_BATCH:
        reasons.append(f"一次最多更正 {MAX_BATCH} 张图幅")
    old_batch: list[tuple[str, Rect]] = []
    new_batch: list[tuple[str, Rect]] = []
    added: list[str] = []
    replaced: list[str] = []
    for entry in batch:
        if entry.sheet_no in existing:
            old_batch.append((entry.sheet_no, existing[entry.sheet_no]))
            replaced.append(entry.sheet_no)
        else:
            added.append(entry.sheet_no)
        new_batch.append((entry.sheet_no, entry.rect))
    if len({entry.sheet_no for entry in batch}) != len(batch):
        reasons.append("批内图幅号重复")
    if len(existing) + len(added) > MAX_SHEETS:
        reasons.append(
            f"更正后目录将超过 {MAX_SHEETS} 张图幅（现有 {len(existing)}，新增 {len(added)}）"
        )
    if reasons:
        raise ValidationError(reasons)

    # New catalog order: keep existing order, append newly added sheets.
    new_map: dict[str, Rect] = dict(existing)
    for entry in batch:
        new_map[entry.sheet_no] = entry.rect
    order = [sheet.no for sheet in sheets]
    order.extend(no for no in added if no not in order)
    new_sheets = [(no, new_map[no]) for no in order]

    raw_window = [rect for _, rect in old_batch] + [rect for _, rect in new_batch]
    window: list[Rect] = []
    for piece in union_decompose(raw_window):
        clipped = piece.clipped(project.extent)
        if clipped is not None:
            window.append(clipped)
    return _BatchContext(old_batch, new_batch, window, new_sheets, added, replaced)


def _split_issues(
    issues: Sequence[Issue], window: Sequence[Rect]
) -> SplitResult:
    untouched: list[Issue] = []
    replaced: list[Issue] = []
    outside: list[Region] = []
    for issue in issues:
        if not _clip_parts(issue.rect, window):
            untouched.append(issue)
            continue
        replaced.append(issue)
        for piece in _subtract_many(issue.rect, window):
            if piece.area > 0:
                outside.append(Region(issue.kind, piece, issue.sheet_ids))
    return SplitResult(untouched=untouched, replaced=replaced, outside=outside)


def _kind_rects(items: Iterable[Issue | Region], kind: str) -> list[Rect]:
    return [item.rect for item in items if item.kind == kind]


def build_plan(
    project: Project,
    sheets: Sequence[Sheet],
    issues: Sequence[Issue],
    batch: Sequence[BatchEntry],
    *,
    expected_version: int | None = None,
) -> CorrectionPlan:
    """Compute a complete, side-effect-free correction plan."""

    context = _build_context(project, sheets, batch)
    before = Stats.from_issues(issues)

    if expected_version is not None and expected_version != project.version:
        return CorrectionPlan(
            status="stale",
            reasons=[
                f"页面版本 {expected_version} 已过期，当前版本 {project.version}；未做任何修改，请刷新后重试"
            ],
            batch=list(batch),
            window=context.window,
            before=before,
            project_version=project.version,
            expected_version=expected_version,
        )

    split = _split_issues(issues, context.window)
    fresh = analyze_rects(project.extent, context.new_sheets, clip_parts=context.window)
    # Store one tidy row per contiguous region, including the window border.
    split.in_window = merge_regions([*split.outside, *fresh])
    split.outside = []

    new_rows: list[Issue | Region] = [
        *split.untouched,
        *split.outside,
        *split.in_window,
    ]
    after = Stats.from_issues(new_rows)

    eliminated: dict[str, int] = {}
    new_positions: dict[str, int] = {}
    for kind in ("missing", "overlap"):
        old_rects = _kind_rects(issues, kind)
        fresh_rects = _kind_rects(new_rows, kind)
        eliminated[kind] = area_outside(old_rects, fresh_rects)
        new_positions[kind] = area_outside(fresh_rects, old_rects)

    eliminated_stats = Stats(
        missing_count=0,
        missing_area=eliminated["missing"],
        overlap_count=0,
        overlap_area=eliminated["overlap"],
    )
    new_stats = Stats(
        missing_count=0,
        missing_area=new_positions["missing"],
        overlap_count=0,
        overlap_area=new_positions["overlap"],
    )

    reasons: list[str] = []
    if new_positions["missing"] > 0:
        reasons.append(f"出现新的缺片位置：{new_positions['missing']} m²")
    if new_positions["overlap"] > 0:
        reasons.append(f"出现新的重叠位置：{new_positions['overlap']} m²")
    if eliminated["missing"] == 0 and eliminated["overlap"] == 0:
        reasons.append("未消除任何旧缺片或旧重叠（消除面积均为 0）")

    status = "accepted" if not reasons else "rejected"
    if status == "accepted":
        reasons = [
            f"消除旧缺片 {eliminated['missing']} m²、旧重叠 {eliminated['overlap']} m²，"
            "未在任何新位置产生缺片或重叠"
        ]

    return CorrectionPlan(
        status=status,
        reasons=reasons,
        batch=list(batch),
        window=context.window,
        before=before,
        after=after,
        eliminated=eliminated_stats,
        new_problems=new_stats,
        split=split,
        project_version=project.version,
        expected_version=expected_version,
    )


# -------------------------------------------------------------------- writes


def create_project(
    db: Database,
    name: str,
    extent: Rect,
    entries: Sequence[BatchEntry],
) -> bool:
    if not PROJECT_NAME_RE.match(name):
        raise ValidationError(["项目名不可含空白或斜杠，最长 60 字符"])
    sheets = [Sheet(entry.sheet_no, entry.rect) for entry in entries]
    validate_catalog(sheets)
    project = Project(name=name, extent=extent, version=0, created_at="", updated_at="")
    issues = full_check(project, sheets)
    try:
        return db.create_project(name, extent, sheets, issues)
    except StorageError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise StorageError(f"创建项目失败: {exc}") from exc


def commit_plan(
    db: Database,
    name: str,
    plan: CorrectionPlan,
) -> tuple[bool, str]:
    """Persist a plan, re-deriving it from the freshly loaded state.

    The displayed plan is never trusted blindly: the batch is planned again
    inside the read snapshot and the result must still be accepted.  Because
    planning is deterministic and the version check fails on any concurrent
    catalog change, this cannot silently apply a different result than the one
    shown to the user.
    """

    if not plan.accepted:
        return False, "；".join(plan.reasons)
    state = db.get_state(name)
    if state is None:
        return False, "项目不存在"
    project, sheets, issues = state
    if plan.expected_version is not None and project.version != plan.expected_version:
        return (
            False,
            f"项目版本已变化（页面 {plan.expected_version}，当前 {project.version}），请刷新后重试",
        )
    fresh = build_plan(
        project,
        sheets,
        issues,
        plan.batch,
        expected_version=plan.expected_version,
    )
    if not fresh.accepted:
        if fresh.status == "stale":
            return False, "；".join(fresh.reasons)
        return False, "；".join(fresh.reasons)
    # StorageError propagates: the caller reports the cause and the transaction
    # has already rolled back, so the catalog and result are unchanged.
    return db.apply_correction(name, project.version, fresh)


# --------------------------------------------------------------------- demo

DEMO_NAME = "DEMO-ADJ-001"
DEMO_EXTENT = Rect(0, 0, 2000, 1000)
DEMO_SHEETS = [
    Sheet("S1", Rect(0, 0, 1000, 1000)),
    Sheet("S2", Rect(1100, 0, 2000, 1000)),
    Sheet("S3", Rect(900, 0, 1400, 500)),
]
DEMO_BATCH_TEXT = "S3 1000 0 1100 500\nS5 1000 500 1100 1000\n"
DEMO_REJECT_TEXT = "S3 1050 0 1150 500\n"
DEMO_NOTES = [
    "应交范围 0 0 2000 1000 m。S1、S2 之间在 y=500~1000 处有 100 m 宽的缺片带；"
    "S3 横跨接缝，与 S1 重叠 50 m（x=900~1000）、与 S2 重叠 300 m（x=1100~1400）。",
    "示例更正：把 S3 改为 1000 0 1100 500，精确填住上段缝；同时新增 S5 1000 500 1100 1000 "
    "填住下段缝。整批替换后重叠全部消除，原有缺片位置上的缺片也全部消除，未产生新位置问题。",
    "示例退回：只把 S3 改为 1050 0 1150 500，会在 x=1000~1050 处产生原先没有的缺片位置，"
    "因此被退回且不写入。",
]


def ensure_demo(db: Database) -> None:
    if db.get_project(DEMO_NAME) is not None:
        return
    create_project(db, DEMO_NAME, DEMO_EXTENT, [
        BatchEntry(sheet.no, sheet.rect) for sheet in DEMO_SHEETS
    ])


# ------------------------------------------------------------------- display


def svg_geometry(extent: Rect) -> dict[str, int]:
    return {
        "min_x": extent.x0,
        "min_y": extent.y0,
        "max_y": extent.y1,
        "width": extent.width,
        "height": extent.height,
    }


def svg_rect(rect: Rect, extent: Rect) -> dict[str, int]:
    return {
        "x": rect.x0 - extent.x0,
        "y": extent.y1 - rect.y1,
        "width": rect.width,
        "height": rect.height,
    }
