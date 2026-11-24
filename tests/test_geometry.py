from __future__ import annotations

import random

from adjcheck.geometry import (
    Rect,
    Region,
    analyze_rects,
    area_outside,
    merge_regions,
    subtract_rect,
    union_area,
    union_decompose,
)
from adjcheck.models import Stats


def grid_cells(regions, extent: Rect) -> dict[tuple[int, int], tuple[str, tuple[str, ...]]]:
    """Per-unit-cell (x, y) -> (kind, ids) for the full extent.

    Points covered by exactly one sheet are absent, matching the rule that such
    points are not problems.
    """

    cells: dict[tuple[int, int], tuple[str, tuple[str, ...]]] = {}
    for region in regions:
        for x in range(region.rect.x0, region.rect.x1):
            for y in range(region.rect.y0, region.rect.y1):
                key = (x, y)
                kind, ids = cells.get(key, (region.kind, ()))
                assert kind == region.kind, "overlapping problem regions of different kinds"
                cells[key] = (kind, tuple(sorted(set(ids) | set(region.sheet_ids))))
    return cells


def brute_cells(extent: Rect, sheets) -> dict[tuple[int, int], tuple[str, tuple[str, ...]]]:
    cells = {}
    for x in range(extent.x0, extent.x1):
        for y in range(extent.y0, extent.y1):
            cover = tuple(
                sorted(n for n, rect in sheets if rect.x0 <= x < rect.x1 and rect.y0 <= y < rect.y1)
            )
            if not cover:
                cells[(x, y)] = ("missing", ())
            elif len(cover) >= 2:
                cells[(x, y)] = ("overlap", cover)
    return cells


def test_basic_missing_and_overlap():
    extent = Rect(0, 0, 2000, 1000)
    sheets = [
        ("S1", Rect(0, 0, 1000, 1000)),
        ("S2", Rect(1100, 0, 2000, 1000)),
        ("S3", Rect(900, 0, 1400, 500)),
    ]
    regions = analyze_rects(extent, sheets)
    missing = [r for r in regions if r.kind == "missing"]
    overlap = [r for r in regions if r.kind == "overlap"]
    assert [(r.rect.as_tuple(), r.area) for r in missing] == [((1000, 500, 1100, 1000), 50000)]
    assert {(r.rect.as_tuple(), r.sheet_ids, r.area) for r in overlap} == {
        ((900, 0, 1000, 500), ("S1", "S3"), 50000),
        ((1100, 0, 1400, 500), ("S2", "S3"), 150000),
    }


def test_overlap_lists_every_sheet():
    extent = Rect(0, 0, 100, 100)
    sheets = [
        ("A", Rect(0, 0, 60, 100)),
        ("B", Rect(20, 0, 80, 100)),
        ("C", Rect(40, 0, 100, 100)),
    ]
    regions = analyze_rects(extent, sheets)
    triple = [r for r in regions if r.sheet_ids == ("A", "B", "C")]
    assert len(triple) == 1
    assert triple[0].rect.as_tuple() == (40, 0, 60, 100)
    pair = [r for r in regions if r.kind == "overlap" and len(r.sheet_ids) == 2]
    assert {r.sheet_ids for r in pair} == {("A", "B"), ("B", "C")}


def test_touching_edges_are_not_overlaps():
    extent = Rect(0, 0, 200, 100)
    sheets = [("A", Rect(0, 0, 100, 100)), ("B", Rect(100, 0, 200, 100))]
    assert analyze_rects(extent, sheets) == []


def test_sheets_outside_extent_are_clipped_and_ignored():
    extent = Rect(0, 0, 100, 100)
    sheets = [
        ("A", Rect(-50, -50, 60, 60)),
        ("B", Rect(60, 60, 150, 150)),
        ("FAR", Rect(500, 500, 600, 600)),
    ]
    regions = analyze_rects(extent, sheets)
    assert {r.rect.as_tuple() for r in regions} == {(60, 0, 100, 60), (0, 60, 60, 100)}
    assert all(r.kind == "missing" for r in regions)
    assert sum(r.area for r in regions) == 100 * 100 - 60 * 60 - 40 * 40


def test_region_partition_matches_direct_coverage():
    rng = random.Random(7)
    extent = Rect(0, 0, 40, 30)
    for _ in range(40):
        sheets = []
        for index in range(rng.randint(1, 6)):
            x0 = rng.randint(-5, 35)
            y0 = rng.randint(-5, 25)
            sheets.append(
                (f"S{index}", Rect(x0, y0, x0 + rng.randint(1, 15), y0 + rng.randint(1, 15)))
            )
        regions = analyze_rects(extent, sheets)
        for r in regions:
            assert extent.x0 <= r.rect.x0 < r.rect.x1 <= extent.x1
            assert extent.y0 <= r.rect.y0 < r.rect.y1 <= extent.y1
        assert grid_cells(regions, extent) == brute_cells(extent, sheets)


def test_incremental_parts_match_full_analysis():
    rng = random.Random(3)
    extent = Rect(0, 0, 50, 40)
    sheets = []
    for index in range(6):
        x0 = rng.randint(-5, 40)
        y0 = rng.randint(-5, 35)
        sheets.append(
            (f"S{index}", Rect(x0, y0, x0 + rng.randint(5, 20), y0 + rng.randint(5, 20)))
        )
    full = analyze_rects(extent, sheets)
    # split the extent into three disjoint vertical slabs
    parts = [Rect(0, 0, 10, 40), Rect(10, 0, 30, 40), Rect(30, 0, 50, 40)]
    incremental = analyze_rects(extent, sheets, clip_parts=parts)
    assert grid_cells(incremental, extent) == grid_cells(full, extent)
    assert union_area(r.rect for r in incremental) == union_area(r.rect for r in full)


def test_subtract_and_union_decompose_are_exact():
    rng = random.Random(11)
    for _ in range(50):
        x0 = rng.randint(-20, 20)
        y0 = rng.randint(-20, 20)
        rect = Rect(x0, y0, x0 + rng.randint(1, 20), y0 + rng.randint(1, 20))
        cx0 = rng.randint(-25, 25)
        cy0 = rng.randint(-25, 25)
        cutter = Rect(cx0, cy0, cx0 + rng.randint(1, 20), cy0 + rng.randint(1, 20))
        pieces = subtract_rect(rect, cutter)
        for i, a in enumerate(pieces):
            for b in pieces[i + 1 :]:
                assert not a.intersects(b)
        overlap_area = rect.clipped(cutter).area if rect.clipped(cutter) else 0
        assert union_area(pieces) == rect.area - overlap_area
        assert union_area([rect, cutter]) == rect.area + cutter.area - overlap_area
        decomposed = union_decompose([rect, cutter])
        assert union_area(decomposed) == union_area([rect, cutter])
        for i, a in enumerate(decomposed):
            for b in decomposed[i + 1 :]:
                assert not a.intersects(b)


def test_merge_regions_preserves_geometry():
    regions = [
        Region("missing", Rect(0, 0, 10, 10)),
        Region("missing", Rect(10, 0, 20, 10)),
        Region("missing", Rect(60, 0, 70, 10)),
        Region("overlap", Rect(20, 0, 30, 10), ("A", "B")),
        Region("missing", Rect(0, 10, 20, 20)),
    ]
    merged = merge_regions(regions)
    assert union_area(r.rect for r in merged) == union_area(r.rect for r in regions)
    assert grid_cells(merged, Rect(0, 0, 100, 30)) == grid_cells(regions, Rect(0, 0, 100, 30))
    assert len(merged) == 3


def test_area_outside():
    assert area_outside([Rect(0, 0, 10, 10)], [Rect(5, 5, 15, 15)]) == 75
    assert area_outside([Rect(0, 0, 10, 10)], []) == 100
    assert area_outside([], [Rect(0, 0, 1, 1)]) == 0
    assert area_outside([Rect(0, 0, 10, 10)], [Rect(0, 0, 10, 10)]) == 0


def test_stats_from_regions():
    regions = analyze_rects(
        Rect(0, 0, 2000, 1000),
        [
            ("S1", Rect(0, 0, 1000, 1000)),
            ("S2", Rect(1100, 0, 2000, 1000)),
            ("S3", Rect(900, 0, 1400, 500)),
        ],
    )
    stats = Stats.from_issues(regions)
    assert stats == Stats(missing_count=1, missing_area=50000, overlap_count=2, overlap_area=200000)


def test_cost_independent_of_coordinate_span():
    """Same layout, coordinates scaled by 10^6, must produce identical work."""

    def layout(scale: int):
        extent = Rect(0, 0, 2000 * scale, 1000 * scale)
        return extent, [
            ("S1", Rect(0, 0, 1000 * scale, 1000 * scale)),
            ("S2", Rect(1100 * scale, 0, 2000 * scale, 1000 * scale)),
            ("S3", Rect(900 * scale, 0, 1400 * scale, 500 * scale)),
        ]

    small = analyze_rects(*layout(1))
    huge = analyze_rects(*layout(10**6))
    assert len(small) == len(huge)
    assert [r.rect.area for r in small] != [r.rect.area for r in huge]
    assert [r.sheet_ids for r in small] == [r.sheet_ids for r in huge]
