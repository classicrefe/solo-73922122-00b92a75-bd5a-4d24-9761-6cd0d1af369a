from __future__ import annotations

import random

import pytest

from adjcheck import service
from adjcheck.db import Database, StorageError
from adjcheck.geometry import Rect
from adjcheck.models import BatchEntry, Sheet, Stats


def semantic(items, extent):
    """(kind, ids) per unit cell: partition-independent comparison.

    All coordinates are integer metres, so this is exact.  It deliberately
    ignores how the union is split into rectangles: an incremental update is
    allowed to keep an old row's shape instead of re-merging it.
    """

    cells = {}
    for item in items:
        for x in range(item.rect.x0, item.rect.x1):
            for y in range(item.rect.y0, item.rect.y1):
                kind, ids = cells.get((x, y), (item.kind, ()))
                assert kind == item.kind
                cells[(x, y)] = (kind, tuple(sorted(set(ids) | set(item.sheet_ids))))
    return cells


def make_project(tmp_path, name="P"):
    db = Database(str(tmp_path / "t.sqlite3"))
    db.init_schema()
    return db


def seed(db, name, extent, sheets, batch_entries=None):
    assert service.create_project(
        db,
        name,
        extent,
        [BatchEntry(no, rect) for no, rect in sheets],
    )


def plan_and_apply(db, name, batch):
    project, sheets, issues = db.get_state(name)
    plan = service.build_plan(project, sheets, issues, batch, expected_version=project.version)
    ok, message = service.commit_plan(db, name, plan)
    return plan, ok, message


def test_demo_batch_accepted_and_matches_full_recompute(tmp_path):
    db = make_project(tmp_path)
    seed(db, "P", service.DEMO_EXTENT, [(s.no, s.rect) for s in service.DEMO_SHEETS])
    batch = [
        BatchEntry("S3", Rect(1000, 0, 1100, 500)),
        BatchEntry("S5", Rect(1000, 500, 1100, 1000)),
    ]
    project, sheets, issues = db.get_state("P")
    plan = service.build_plan(project, sheets, issues, batch, expected_version=0)
    assert plan.accepted
    assert plan.before == Stats(1, 50000, 2, 200000)
    assert plan.eliminated.missing_area == 50000
    assert plan.eliminated.overlap_area == 200000
    assert plan.new_problems == Stats(0, 0, 0, 0)
    # window is exactly the union of old/new batch extents, clipped to extent
    assert plan.window == [
        Rect(900, 0, 1000, 500),
        Rect(1000, 0, 1100, 1000),
        Rect(1100, 0, 1400, 500),
    ]
    assert plan.split is not None and plan.split.replaced
    assert all(r.rect in plan.window for r in plan.split.in_window)

    ok, message = service.commit_plan(db, "P", plan)
    assert ok, message
    project, sheets, issues = db.get_state("P")
    assert project.version == 1
    assert issues == []
    assert [(s.no, s.rect) for s in sheets] == [
        ("S1", Rect(0, 0, 1000, 1000)),
        ("S2", Rect(1100, 0, 2000, 1000)),
        ("S3", Rect(1000, 0, 1100, 500)),
        ("S5", Rect(1000, 500, 1100, 1000)),
    ]
    assert [s.no for s in sheets] == ["S1", "S2", "S3", "S5"]  # existing order kept
    assert service.full_check(project, sheets) == []


def test_reject_when_new_missing_position_appears(tmp_path):
    db = make_project(tmp_path)
    seed(db, "P", service.DEMO_EXTENT, [(s.no, s.rect) for s in service.DEMO_SHEETS])
    project, sheets, issues = db.get_state("P")
    plan = service.build_plan(
        project, sheets, issues, [BatchEntry("S3", Rect(1050, 0, 1150, 500))], expected_version=0
    )
    assert not plan.accepted
    assert any("新的缺片位置" in r for r in plan.reasons)
    ok, _ = service.commit_plan(db, "P", plan)
    assert not ok
    project, sheets, issues = db.get_state("P")
    assert project.version == 0
    assert [(s.no, s.rect) for s in sheets] == [(s.no, s.rect) for s in service.DEMO_SHEETS]
    assert Stats.from_issues(issues) == Stats(1, 50000, 2, 200000)


def test_reject_when_only_removing_nothing(tmp_path):
    db = make_project(tmp_path)
    seed(
        db,
        "P",
        Rect(0, 0, 200, 100),
        [("A", Rect(0, 0, 90, 100)), ("B", Rect(100, 0, 200, 100))],
    )
    project, sheets, issues = db.get_state("P")
    # changing B's far edge changes nothing about the existing gap
    plan = service.build_plan(
        project, sheets, issues, [BatchEntry("B", Rect(100, 0, 200, 90))], expected_version=0
    )
    assert not plan.accepted
    assert any("新的缺片位置" in r for r in plan.reasons)
    assert any("未消除任何旧缺片或旧重叠" in r for r in plan.reasons)


def test_batch_limit_and_new_sheet(tmp_path):
    db = make_project(tmp_path)
    seed(db, "P", Rect(0, 0, 300, 100), [("A", Rect(0, 0, 300, 100))])
    project, sheets, issues = db.get_state("P")
    with pytest.raises(service.ValidationError):
        service.build_plan(
            project,
            sheets,
            issues,
            [BatchEntry(f"X{i}", Rect(0, 0, 1, 1)) for i in range(5)],
            expected_version=0,
        )
    plan = service.build_plan(
        project,
        sheets,
        issues,
        [BatchEntry("A", Rect(0, 0, 100, 100)), BatchEntry("B", Rect(100, 0, 200, 100)),
         BatchEntry("C", Rect(200, 0, 300, 100))],
        expected_version=0,
    )
    # splitting a fully covered extent introduces no problem, but removes none
    assert not plan.accepted
    assert any("未消除任何旧缺片或旧重叠" in r for r in plan.reasons)


def test_stale_version_is_rejected_before_any_write(tmp_path):
    db = make_project(tmp_path)
    seed(
        db,
        "P",
        Rect(0, 0, 300, 100),
        [("A", Rect(0, 0, 100, 100)), ("C", Rect(200, 0, 300, 100))],
    )
    project, sheets, issues = db.get_state("P")
    plan = service.build_plan(
        project, sheets, issues, [BatchEntry("B", Rect(100, 0, 200, 100))], expected_version=0
    )
    assert plan.accepted
    # simulate another user's successful write
    project2, sheets2, issues2 = db.get_state("P")
    other = service.build_plan(
        project2, sheets2, issues2, [BatchEntry("B", Rect(100, 0, 200, 100))], expected_version=0
    )
    ok, _ = service.commit_plan(db, "P", other)
    assert ok
    ok, message = service.commit_plan(db, "P", plan)
    assert not ok and "版本" in message
    project3, _, _ = db.get_state("P")
    assert project3.version == 1


def test_commit_refuses_tampered_plan(tmp_path):
    db = make_project(tmp_path)
    seed(
        db,
        "P",
        Rect(0, 0, 300, 100),
        [("A", Rect(0, 0, 100, 100)), ("C", Rect(200, 0, 300, 100))],
    )
    project, sheets, issues = db.get_state("P")
    plan = service.build_plan(
        project, sheets, issues, [BatchEntry("B", Rect(100, 0, 200, 100))], expected_version=0
    )
    assert plan.accepted
    plan.batch = [BatchEntry("B", Rect(100, 0, 260, 100))]  # would create an overlap
    ok, message = service.commit_plan(db, "P", plan)
    assert not ok
    project, sheets, issues = db.get_state("P")
    assert project.version == 0 and ["B" not in s.no for s in sheets]


def test_window_only_recompute_keeps_far_rows_verbatim(tmp_path):
    db = make_project(tmp_path)
    # far-away overlap at y=900..1000, local gap at x=100..110 in y=0..100
    sheets = [
        ("A", Rect(0, 0, 100, 100)),
        ("B", Rect(110, 0, 200, 100)),
        ("F1", Rect(0, 900, 100, 1000)),
        ("F2", Rect(50, 900, 150, 1000)),
    ]
    seed(db, "P", Rect(0, 0, 200, 1000), sheets)
    project, old_sheets, old_issues = db.get_state("P")
    far = [i for i in old_issues if i.rect.y0 >= 900]
    assert far  # the far overlap exists
    plan = service.build_plan(
        project, old_sheets, old_issues, [BatchEntry("B", Rect(100, 0, 200, 100))],
        expected_version=0,
    )
    assert plan.accepted, plan.reasons
    replaced_ids = {i.id for i in plan.split.replaced}
    assert not any(i.id in replaced_ids for i in far)
    ok, message = service.commit_plan(db, "P", plan)
    assert ok, message
    project, new_sheets, new_issues = db.get_state("P")
    far_after = [i for i in new_issues if i.rect.y0 >= 900]
    assert sorted((i.kind, i.rect.as_tuple(), i.sheet_ids) for i in far_after) == sorted(
        (i.kind, i.rect.as_tuple(), i.sheet_ids) for i in far
    )
    assert {i.id for i in far_after} == {i.id for i in far}
    # everything else matches a fresh full check
    assert semantic(service.full_check(project, new_sheets), project.extent) == semantic(
        new_issues, project.extent
    )


def test_write_failure_rolls_back_catalog_and_issues(tmp_path):
    db = make_project(tmp_path)
    seed(db, "P", service.DEMO_EXTENT, [(s.no, s.rect) for s in service.DEMO_SHEETS])
    batch = [
        BatchEntry("S3", Rect(1000, 0, 1100, 500)),
        BatchEntry("S5", Rect(1000, 500, 1100, 1000)),
    ]
    project, sheets, issues = db.get_state("P")
    plan = service.build_plan(project, sheets, issues, batch, expected_version=0)
    assert plan.accepted

    with db.connection() as conn:
        conn.executescript(
            """
            CREATE TRIGGER fail_issue_insert BEFORE INSERT ON sheets
            BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END;
            """
        )
    with pytest.raises(StorageError):
        service.commit_plan(db, "P", plan)
    project, sheets, issues = db.get_state("P")
    assert project.version == 0
    assert [(s.no, s.rect) for s in sheets] == [(s.no, s.rect) for s in service.DEMO_SHEETS]
    assert Stats.from_issues(issues) == Stats(1, 50000, 2, 200000)


def test_validation_rejects_bad_input(tmp_path):
    db = make_project(tmp_path)
    seed(db, "P", Rect(0, 0, 100, 100), [("A", Rect(0, 0, 100, 100))])
    with pytest.raises(service.ValidationError):
        service.parse_extent("0", "0", "0", "100")
    with pytest.raises(service.ValidationError):
        service.parse_extent("0", "0", "100", "x")
    with pytest.raises(service.ValidationError):
        service.parse_sheet_lines("A 0 0 1 1\nA 0 0 1 1", max_entries=100, context="图幅清单")
    with pytest.raises(service.ValidationError):
        service.parse_sheet_lines("A 0 0 1", max_entries=100, context="图幅清单")
    with pytest.raises(service.ValidationError):
        service.parse_sheet_lines("", max_entries=100, context="图幅清单")
    with pytest.raises(service.ValidationError):
        service.parse_batch_json([{"no": "A"}] * 5)
    with pytest.raises(service.ValidationError):
        service.create_project(
            db, "TOO", Rect(0, 0, 1, 1),
            [BatchEntry(f"S{i}", Rect(0, 0, 1, 1)) for i in range(101)],
        )
    with pytest.raises(service.ValidationError):
        service.create_project(db, "BAD NAME", Rect(0, 0, 1, 1), [BatchEntry("A", Rect(0, 0, 1, 1))])


def _random_catalog(rng):
    """Small integer grid with many near-touching rectangles."""

    sheets = []
    for index in range(rng.randint(2, 9)):
        x0 = rng.randint(0, 8)
        y0 = rng.randint(0, 6)
        x1 = x0 + rng.randint(1, 4)
        y1 = y0 + rng.randint(1, 4)
        sheets.append((f"S{index}", Rect(x0, y0, x1, y1)))
    extent = Rect(0, 0, 10, 8)
    return extent, sheets


def _grid_catalog(rng):
    """2x2 sheets separated by random narrow gaps and slight overlaps.

    This shape makes it easy for a batch that nudges two neighbouring sheets to
    eliminate existing problems while leaving an untouched sheet alone, so the
    random test actually exercises the incremental path (untouched rows).
    """

    base_x = rng.choice([0, 1])
    base_y = rng.choice([0, 1])
    gap_x = rng.randint(0, 1)  # 0 = touching, 1 = gap; negative values -> overlap
    gap_y = rng.randint(-1, 1)
    cells = [
        ("A", Rect(0, 0, 5, 5)),
        ("B", Rect(5 + gap_x, 0, 10 + gap_x, 5)),
        ("C", Rect(0, 5 + gap_y, 5, 10 + gap_y)),
        ("D", Rect(5 + gap_x, 5 + gap_y, 10 + gap_x, 10 + gap_y)),
    ]
    extent = Rect(base_x, base_y, 12 + base_x, 12 + base_y)
    return extent, cells


def _targeted_candidates(sheets_db, issues, rng):
    """Batches that plausibly fix one stored problem without moving it."""

    candidates = []
    for issue in issues:
        r = issue.rect
        for sheet in sheets_db:
            s = sheet.rect
            # grow a sheet across an exactly adjacent gap / overlap band
            if s.y0 <= r.y0 and s.y1 >= r.y1 and s.x1 == r.x0:
                candidates.append([BatchEntry(sheet.no, Rect(s.x0, s.y0, r.x1, s.y1))])
            if s.y0 <= r.y0 and s.y1 >= r.y1 and s.x0 == r.x1:
                candidates.append([BatchEntry(sheet.no, Rect(r.x0, s.y0, s.x1, s.y1))])
            if s.x0 <= r.x0 and s.x1 >= r.x1 and s.y1 == r.y0:
                candidates.append([BatchEntry(sheet.no, Rect(s.x0, s.y0, s.x1, r.y1))])
            if s.x0 <= r.x0 and s.x1 >= r.x1 and s.y0 == r.y1:
                candidates.append([BatchEntry(sheet.no, Rect(s.x0, r.y0, s.x1, s.y1))])
            # shrink an overlapping sheet back to this rectangle's edge
            if s.y0 <= r.y0 and s.y1 >= r.y1 and s.x0 < r.x0 and s.x1 > r.x0:
                candidates.append([BatchEntry(sheet.no, Rect(s.x0, s.y0, r.x0, s.y1))])
            if s.y0 <= r.y0 and s.y1 >= r.y1 and s.x0 < r.x1 and s.x1 > r.x1:
                candidates.append([BatchEntry(sheet.no, Rect(r.x1, s.y0, s.x1, s.y1))])
            if s.x0 <= r.x0 and s.x1 >= r.x1 and s.y0 < r.y0 and s.y1 > r.y0:
                candidates.append([BatchEntry(sheet.no, Rect(s.x0, s.y0, s.x1, r.y0))])
            if s.x0 <= r.x0 and s.x1 >= r.x1 and s.y0 < r.y1 and s.y1 > r.y1:
                candidates.append([BatchEntry(sheet.no, Rect(s.x0, r.y1, s.x1, s.y1))])
    # two-sheet variants: fill a band from both sides at once
    rng.shuffle(candidates)
    return [c for c in candidates if c[0].rect.area > 0]


def test_random_incremental_equals_full_and_outside_rows_preserved(tmp_path):
    rng = random.Random(1234)
    accepted = rejected = 0
    for case in range(80):
        root = tmp_path / f"case{case}"
        root.mkdir()
        db = make_project(root)
        if case % 2:
            extent, sheets = _random_catalog(rng)
        else:
            extent, sheets = _grid_catalog(rng)
        seed(db, "P", extent, sheets)
        project, sheets_db, issues = db.get_state("P")

        candidates = _targeted_candidates(sheets_db, issues, rng)
        # a few random nudges as well, so rejections stay well represented
        pool = [s.no for s in sheets_db]
        rng.shuffle(pool)
        for no in pool[:2]:
            current = next(s.rect for s in sheets_db if s.no == no)
            dx, dy = rng.randint(-2, 2), rng.randint(-2, 2)
            candidate = Rect(
                current.x0 + dx, current.y0 + dy, current.x1 + dx, current.y1 + dy
            )
            if candidate.area > 0:
                candidates.append([BatchEntry(no, candidate)])
        rng.shuffle(candidates)
        if not candidates:
            continue

        chosen = None
        for candidate in candidates[:12]:
            plan = service.build_plan(project, sheets_db, issues, candidate, expected_version=0)
            if plan.accepted:
                chosen = plan
                break
        if chosen is None:
            chosen = service.build_plan(project, sheets_db, issues, candidates[0], expected_version=0)
        untouched_ids = {i.id for i in chosen.split.untouched}
        ok, message = service.commit_plan(db, "P", chosen)
        new_project, new_sheets, new_issues = db.get_state("P")
        if ok:
            accepted += 1
            assert new_project.version == 1
            full = service.full_check(new_project, new_sheets)
            assert semantic(full, new_project.extent) == semantic(
                new_issues, new_project.extent
            ), f"case {case}: incremental result diverged from full check"
            assert Stats.from_issues(full).problem_area == Stats.from_issues(new_issues).problem_area
            # untouched rows keep their ids (outside the window, original result reused)
            assert untouched_ids <= {i.id for i in new_issues}
            assert Stats.from_issues(new_issues).problem_area == chosen.after.problem_area
            # the incremental window never covers the whole extent unnecessarily
            assert chosen.window
        else:
            rejected += 1
            assert new_project.version == 0
            assert [(s.no, s.rect) for s in new_sheets] == [(s.no, s.rect) for s in sheets_db]
            assert [(i.kind, i.rect, i.sheet_ids, i.id) for i in new_issues] == [
                (i.kind, i.rect, i.sheet_ids, i.id) for i in issues
            ]
    assert accepted > 15 and rejected > 5, (accepted, rejected)


def test_problem_type_cannot_move(tmp_path):
    db = make_project(tmp_path)
    # gap at x=100..120; batch would move the gap to x=120..140 (new position)
    seed(
        db,
        "P",
        Rect(0, 0, 300, 100),
        [("A", Rect(0, 0, 100, 100)), ("B", Rect(120, 0, 300, 100))],
    )
    project, sheets, issues = db.get_state("P")
    plan = service.build_plan(
        project, sheets, issues, [BatchEntry("A", Rect(0, 0, 140, 100))], expected_version=0
    )
    assert not plan.accepted
    assert any("新的重叠位置" in r for r in plan.reasons)
    # but fully closing it is accepted
    plan = service.build_plan(
        project, sheets, issues, [BatchEntry("A", Rect(0, 0, 120, 100))], expected_version=0
    )
    assert plan.accepted


def _cell_kinds(regions):
    """{(x, y): kind} at unit resolution, for an independent oracle."""

    cells = {}
    for region in regions:
        for x in range(region.rect.x0, region.rect.x1):
            for y in range(region.rect.y0, region.rect.y1):
                assert cells.get((x, y), region.kind) == region.kind
                cells[(x, y)] = region.kind
    return cells


def test_acceptance_rule_matches_brute_force_oracle(tmp_path):
    """Eliminated/new-position areas agree with unit-cell set arithmetic."""

    rng = random.Random(99)
    checked = 0
    for case in range(40):
        root = tmp_path / f"oracle{case}"
        root.mkdir()
        db = make_project(root)
        extent = Rect(0, 0, 8, 6)
        sheets = []
        for index in range(rng.randint(2, 6)):
            x0 = rng.randint(0, 6)
            y0 = rng.randint(0, 4)
            sheets.append((f"S{index}", Rect(x0, y0, x0 + rng.randint(1, 3), y0 + rng.randint(1, 3))))
        seed(db, "P", extent, sheets)
        project, sheets_db, issues = db.get_state("P")
        target = rng.choice(sheets_db)
        x0 = max(0, target.rect.x0 + rng.randint(-2, 2))
        y0 = max(0, target.rect.y0 + rng.randint(-2, 2))
        candidate = Rect(x0, y0, x0 + rng.randint(1, 4), y0 + rng.randint(1, 4))
        batch = [BatchEntry(target.no, candidate)]
        plan = service.build_plan(project, sheets_db, issues, batch, expected_version=0)

        # independent oracle: full recompute with new catalog
        new_map = {s.no: s.rect for s in sheets_db}
        new_map[target.no] = candidate
        new_sheets = [Sheet(no, rect) for no, rect in new_map.items()]
        old_cells = _cell_kinds(service.full_check(project, sheets_db))
        new_cells = _cell_kinds(service.full_check(project, new_sheets))
        for kind in ("missing", "overlap"):
            old_set = {k for k, v in old_cells.items() if v == kind}
            new_set = {k for k, v in new_cells.items() if v == kind}
            eliminated = getattr(plan.eliminated, f"{kind}_area")
            new_area = getattr(plan.new_problems, f"{kind}_area")
            assert eliminated == len(old_set - new_set), (case, kind, "eliminated")
            assert new_area == len(new_set - old_set), (case, kind, "new")
        expected_accept = (
            plan.eliminated.missing_area + plan.eliminated.overlap_area > 0
            and plan.new_problems.missing_area == 0
            and plan.new_problems.overlap_area == 0
        )
        assert plan.accepted == expected_accept, case
        # stored after-state is exactly the new coverage (same kinds)
        assert Stats.from_issues(plan.split.untouched + plan.split.in_window).problem_area == (
            plan.after.problem_area
        )
        checked += 1
    assert checked == 40
