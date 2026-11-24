"""Exact axis-aligned rectangle geometry for the adjacency checker.

All coordinates are integers (metres in one coordinate system).  The code is
deliberately based on coordinate compression / a plane sweep rather than a
raster: *no* work is proportional to the coordinate span or to an area.

A "region" produced by :func:`analyze_rects` is a maximal-enough rectangle on
which the set of covering sheets is constant.  Missing regions have an empty
sheet set, overlap regions have two or more sheet numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True, order=True)
class Rect:
    """A positive-area, axis-aligned rectangular range in metre coordinates."""

    x0: int
    y0: int
    x1: int
    y1: int

    def __post_init__(self) -> None:
        if not (self.x0 < self.x1 and self.y0 < self.y1):
            raise ValueError(f"矩形必须满足 x0<x1 且 y0<y1: {self!r}")

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def area(self) -> int:
        return self.width * self.height

    def clipped(self, other: "Rect") -> "Rect | None":
        x0 = max(self.x0, other.x0)
        y0 = max(self.y0, other.y0)
        x1 = min(self.x1, other.x1)
        y1 = min(self.y1, other.y1)
        if x0 < x1 and y0 < y1:
            return Rect(x0, y0, x1, y1)
        return None

    def intersects(self, other: "Rect") -> bool:
        return self.clipped(other) is not None

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass(frozen=True)
class Region:
    """A problem rectangle with a stable kind and the full covering set."""

    kind: str  # "missing" or "overlap"
    rect: Rect
    sheet_ids: tuple[str, ...] = ()

    @property
    def area(self) -> int:
        return self.rect.area


def clip_rect(rect: Rect, bounds: Rect) -> Rect | None:
    return rect.clipped(bounds)


def subtract_rect(rect: Rect, cutter: Rect) -> tuple[Rect, ...]:
    """Return the positive-area pieces of ``rect`` outside ``cutter``.

    The result is disjoint.  Up to four pieces are returned.
    """

    inter = rect.clipped(cutter)
    if inter is None:
        return (rect,)

    pieces: list[Rect] = []
    # full-height strips left and right of the cut
    if rect.x0 < inter.x0:
        pieces.append(Rect(rect.x0, rect.y0, inter.x0, rect.y1))
    if inter.x1 < rect.x1:
        pieces.append(Rect(inter.x1, rect.y0, rect.x1, rect.y1))
    # middle strips below and above the cut
    if rect.y0 < inter.y0:
        pieces.append(Rect(inter.x0, rect.y0, inter.x1, inter.y0))
    if inter.y1 < rect.y1:
        pieces.append(Rect(inter.x0, inter.y1, inter.x1, rect.y1))
    return tuple(pieces)


def _merged_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted(intervals)
    merged: list[list[int]] = []
    for start, end in ordered:
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def union_decompose(rects: Iterable[Rect]) -> list[Rect]:
    """Decompose the union of rectangles into disjoint, positive-area rects.

    Adjacent x slabs with exactly the same union y interval are merged into a
    single rectangle.  This is used to turn the union of a batch's old and new
    extents into an exact, non-overlapping recomputation window.
    """

    source = [r for r in rects if r.area > 0]
    if not source:
        return []
    xs = sorted({r.x0 for r in source} | {r.x1 for r in source})
    result: list[list[int]] = []  # mutable [x0, y0, x1, y1]
    previous: dict[tuple[int, int], list[int]] = {}

    for xa, xb in zip(xs, xs[1:]):
        if xa >= xb:
            continue
        active = [r for r in source if r.x0 <= xa and r.x1 >= xb]
        intervals = _merged_intervals((r.y0, r.y1) for r in active)
        current: dict[tuple[int, int], list[int]] = {}
        for y0, y1 in intervals:
            key = (y0, y1)
            piece = previous.pop(key, None)
            if piece is None:
                piece = [xa, y0, xb, y1]
            else:
                piece[2] = xb
            current[key] = piece
        result.extend(previous.values())
        previous = current
    result.extend(previous.values())
    return [Rect(*piece) for piece in result]


def _analyze_part(part: Rect, sheets: Sequence[tuple[str, Rect]]) -> list[Region]:
    """Analyze one clipping rectangle against all sheets."""

    clipped: list[tuple[str, Rect]] = []
    for sheet_no, rect in sheets:
        piece = rect.clipped(part)
        if piece is not None:
            clipped.append((sheet_no, piece))

    xs = {part.x0, part.x1}
    for _, rect in clipped:
        xs.add(rect.x0)
        xs.add(rect.x1)
    xs_sorted = sorted(xs)

    result: list[Region] = []
    previous: dict[tuple[int, int, str, tuple[str, ...]], dict] = {}

    for xa, xb in zip(xs_sorted, xs_sorted[1:]):
        if xa >= xb:
            continue
        active = [(sid, rect) for sid, rect in clipped if rect.x0 <= xa and rect.x1 >= xb]

        ys = {part.y0, part.y1}
        for _, rect in active:
            ys.add(rect.y0)
            ys.add(rect.y1)
        ys_sorted = sorted(ys)

        runs: list[dict] = []
        for ya, yb in zip(ys_sorted, ys_sorted[1:]):
            if ya >= yb:
                continue
            cover = tuple(
                sorted(
                    sid
                    for sid, rect in active
                    if rect.y0 <= ya and rect.y1 >= yb
                )
            )
            if not cover:
                kind = "missing"
            elif len(cover) >= 2:
                kind = "overlap"
            else:
                continue

            if (
                runs
                and runs[-1]["y1"] == ya
                and runs[-1]["kind"] == kind
                and runs[-1]["ids"] == cover
            ):
                runs[-1]["y1"] = yb
            else:
                runs.append(
                    {
                        "x0": xa,
                        "y0": ya,
                        "x1": xb,
                        "y1": yb,
                        "kind": kind,
                        "ids": cover,
                    }
                )

        current: dict[tuple[int, int, str, tuple[str, ...]], dict] = {}
        for run in runs:
            key = (run["y0"], run["y1"], run["kind"], run["ids"])
            previous_run = previous.pop(key, None)
            if previous_run is None:
                previous_run = run
            else:
                previous_run["x1"] = xb
            current[key] = previous_run
        result.extend(
            Region(p["kind"], Rect(p["x0"], p["y0"], p["x1"], p["y1"]), p["ids"])
            for p in previous.values()
        )
        previous = current

    result.extend(
        Region(p["kind"], Rect(p["x0"], p["y0"], p["x1"], p["y1"]), p["ids"])
        for p in previous.values()
    )
    result.sort(
        key=lambda r: (
            r.kind,
            r.rect.x0,
            r.rect.y0,
            r.rect.x1,
            r.rect.y1,
            r.sheet_ids,
        )
    )
    return result


def analyze_rects(
    extent: Rect,
    sheets: Iterable[tuple[str, Rect]],
    clip_parts: Sequence[Rect] | None = None,
) -> list[Region]:
    """List missing/overlap regions within ``extent``.

    ``sheets`` may extend beyond ``extent``; they are clipped for analysis.
    ``clip_parts`` is an optional list of disjoint rectangles (already inside
    ``extent``) used for an incremental recomputation.  With it omitted the
    whole extent is checked.
    """

    sheet_list = list(sheets)
    parts: list[Rect] = []
    if clip_parts is None:
        parts = [extent]
    else:
        for part in clip_parts:
            clipped = part.clipped(extent)
            if clipped is not None:
                parts.append(clipped)

    regions: list[Region] = []
    for part in parts:
        regions.extend(_analyze_part(part, sheet_list))
    regions.sort(
        key=lambda r: (
            r.kind,
            r.rect.x0,
            r.rect.y0,
            r.rect.x1,
            r.rect.y1,
            r.sheet_ids,
        )
    )
    return regions


def merge_regions(regions: Iterable[Region]) -> list[Region]:
    """Merge edge-adjacent regions that share kind and covering set.

    Only rectangles sharing a full edge (and thus the same constant covering
    set on both sides) are merged, so the union and every point's sheet set are
    preserved.  Purely cosmetic: it keeps incrementally-updated rows tidy.
    """

    groups: dict[tuple[str, tuple[str, ...]], list[Rect]] = {}
    for region in regions:
        groups.setdefault((region.kind, region.sheet_ids), []).append(region.rect)

    merged: list[Region] = []
    for (kind, ids), rects in groups.items():
        merged.extend(Region(kind, rect, ids) for rect in _merge_rects(rects))

    merged.sort(
        key=lambda r: (
            r.kind,
            r.rect.x0,
            r.rect.y0,
            r.rect.x1,
            r.rect.y1,
            r.sheet_ids,
        )
    )
    return merged


def _merge_one_pass(rects: Sequence[Rect], horizontal: bool) -> list[Rect]:
    """Merge chains sharing an edge along one axis.  O(n log n) per pass."""

    buckets: dict[tuple[int, int], list[Rect]] = {}
    if horizontal:
        for rect in rects:
            buckets.setdefault((rect.y0, rect.y1), []).append(rect)
    else:
        for rect in rects:
            buckets.setdefault((rect.x0, rect.x1), []).append(rect)

    output: list[Rect] = []
    for bucket in buckets.values():
        bucket.sort(key=lambda r: (r.x0 if horizontal else r.y0))
        current = bucket[0]
        for rect in bucket[1:]:
            combined = _merge_pair(current, rect)
            if combined is not None:
                current = combined
            else:
                output.append(current)
                current = rect
        output.append(current)
    return output


def _merge_rects(rects: Sequence[Rect]) -> list[Rect]:
    current = list(rects)
    while True:
        before = len(current)
        current = _merge_one_pass(_merge_one_pass(current, True), False)
        if len(current) == before:
            return current


def _merge_pair(a: Rect, b: Rect) -> Rect | None:
    if a.y0 == b.y0 and a.y1 == b.y1:
        if a.x1 == b.x0:
            return Rect(a.x0, a.y0, b.x1, a.y1)
        if b.x1 == a.x0:
            return Rect(b.x0, a.y0, a.x1, a.y1)
    if a.x0 == b.x0 and a.x1 == b.x1:
        if a.y1 == b.y0:
            return Rect(a.x0, a.y0, a.x1, b.y1)
        if b.y1 == a.y0:
            return Rect(a.x0, b.y0, a.x1, a.y1)
    return None


def _union_interval_length(intervals: Iterable[tuple[int, int]]) -> int:
    return sum(end - start for start, end in _merged_intervals(intervals))


def union_area(rects: Iterable[Rect]) -> int:
    """Exact area of a union of rectangles (no coordinate-span loop)."""

    source = list(rects)
    if not source:
        return 0
    xs = sorted({r.x0 for r in source} | {r.x1 for r in source})
    total = 0
    for xa, xb in zip(xs, xs[1:]):
        intervals = [
            (r.y0, r.y1)
            for r in source
            if r.x0 <= xa and r.x1 >= xb
        ]
        total += (xb - xa) * _union_interval_length(intervals)
    return total


def _length_not_covered(
    intervals: Sequence[tuple[int, int]], covers: Sequence[tuple[int, int]]
) -> int:
    """Length of ``intervals`` (already merged) outside ``covers`` (merged)."""

    total = 0
    for start, end in intervals:
        cursor = start
        for cov_start, cov_end in covers:
            if cov_end <= cursor:
                continue
            if cov_start >= end:
                break
            if cov_start > cursor:
                total += min(cov_start, end) - cursor
            if cov_end > cursor:
                cursor = cov_end
            if cursor >= end:
                break
        if cursor < end:
            total += end - cursor
    return total


def area_outside(
    subject: Iterable[Rect], background: Iterable[Rect]
) -> int:
    """Area of the union of ``subject`` that is not covered by ``background``."""

    subject_rects = list(subject)
    background_rects = list(background)
    if not subject_rects:
        return 0
    if not background_rects:
        return union_area(subject_rects)

    xs = sorted(
        {r.x0 for r in subject_rects}
        | {r.x1 for r in subject_rects}
        | {r.x0 for r in background_rects}
        | {r.x1 for r in background_rects}
    )
    total = 0
    for xa, xb in zip(xs, xs[1:]):
        subject_intervals = _merged_intervals(
            (r.y0, r.y1)
            for r in subject_rects
            if r.x0 <= xa and r.x1 >= xb
        )
        background_intervals = _merged_intervals(
            (r.y0, r.y1)
            for r in background_rects
            if r.x0 <= xa and r.x1 >= xb
        )
        total += (xb - xa) * _length_not_covered(
            subject_intervals, background_intervals
        )
    return total
