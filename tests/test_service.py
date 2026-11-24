"""纯规则测试：测量有效性、连通性、矛盾、两条不共用测量的路径。"""

from __future__ import annotations

import itertools
import random

from adjcheck import service
from adjcheck.geometry import Point, Rect
from adjcheck.models import Measurement, Project, Sheet, Snapshot


def sheet(no: str, x0: int = 0, y0: int = 0, x1: int = 100, y1: int = 100) -> Sheet:
    return Sheet(no, Rect(x0, y0, x1, y1))


def snapshot(measurements, sheets, version: int = 0) -> Snapshot:
    return Snapshot(
        project=Project("P", "地物", version, "", ""),
        sheets=tuple(sheets),
        measurements=tuple(measurements),
    )


def three_sheets():
    return [sheet("A"), sheet("B"), sheet("C")]


def test_validity_requires_both_points_inside_current_sheet():
    snapshot_ = snapshot(
        [Measurement("M1", "A", Point(9, 9), "B", Point(10, 10))],
        [sheet("A"), sheet("B", 0, 0, 10, 10)],
    )
    edges, validity = service.valid_edges(snapshot_)
    assert edges == []
    assert validity[0].valid is False
    assert "图幅 B" in validity[0].reasons[0]


def test_missing_sheet_invalidates_measurement():
    snapshot_ = snapshot(
        [Measurement("M1", "A", Point(1, 1), "B", Point(1, 1))], [sheet("A")]
    )
    _, validity = service.valid_edges(snapshot_)
    assert validity[0].valid is False
    assert "已不在现行目录" in validity[0].reasons[0]


def test_verified_with_two_edge_disjoint_paths():
    measurements = list(service.DEMO_MEASUREMENTS)
    result = service.compute_verification(
        snapshot(measurements, three_sheets()), "A", "C"
    )
    assert result.status == "verified"
    assert result.translation == (3, -2)
    assert len(result.paths) == 2
    used = [no for path in result.paths for no in path.measurements]
    assert len(used) == len(set(used))  # no shared measurement
    # one path is A-C, the other A-B-C
    assert sorted(tuple(path.sheets) for path in result.paths) == [
        ("A", "B", "C"),
        ("A", "C"),
    ]
    assert {tuple(path.measurements) for path in result.paths} == {("M3",), ("M1", "M2")}


def test_paths_may_share_sheets():
    measurements = [
        Measurement("m1", "A", Point(1, 1), "B", Point(1, 1)),
        Measurement("m2", "B", Point(2, 2), "C", Point(2, 2)),
        Measurement("m3", "A", Point(3, 3), "B", Point(3, 3)),
        Measurement("m4", "B", Point(4, 4), "C", Point(4, 4)),
    ]
    result = service.compute_verification(snapshot(measurements, three_sheets()), "A", "C")
    assert result.status == "verified"
    assert result.translation == (0, 0)
    assert [tuple(p.sheets) for p in result.paths] == [("A", "B", "C"), ("A", "B", "C")]
    assert not (set(result.paths[0].measurements) & set(result.paths[1].measurements))


def test_insufficient_when_both_routes_need_the_same_measurement():
    measurements = [
        Measurement("m1", "A", Point(1, 1), "B", Point(1, 1)),
        Measurement("m2", "B", Point(2, 2), "C", Point(2, 2)),
        Measurement("m3", "A", Point(3, 3), "B", Point(3, 3)),
    ]
    result = service.compute_verification(snapshot(measurements, three_sheets()), "A", "C")
    assert result.status == "insufficient"
    assert result.translation is None
    assert result.max_paths == 1


def test_insufficient_with_single_path():
    result = service.compute_verification(
        snapshot([Measurement("M3", "A", Point(60, 60), "C", Point(57, 62))], three_sheets()),
        "A",
        "C",
    )
    assert result.status == "insufficient"


def test_disconnected_when_no_valid_path():
    result = service.compute_verification(
        snapshot(service.DISCONNECTED_MEASUREMENTS, service.DISCONNECTED_SHEETS), "P", "R"
    )
    assert result.status == "disconnected"
    assert "失联" in result.message


def test_disconnected_when_the_only_link_was_invalidated_by_a_correction():
    sheets = [
        sheet("A"),
        sheet("B", 0, 0, 10, 10),
        sheet("C"),
    ]
    measurements = [
        Measurement("M1", "A", Point(1, 1), "B", Point(50, 50)),
        Measurement("M2", "B", Point(2, 2), "C", Point(2, 2)),
    ]
    result = service.compute_verification(snapshot(measurements, sheets), "A", "C")
    assert result.status == "disconnected"
    assert [v.measurement.no for v in result.invalid_measurements] == ["M1"]


def test_contradiction_when_reference_group_is_inconsistent():
    sheets = [sheet("A"), sheet("B"), sheet("C")]
    measurements = [
        # two paths from A to C, but A-B evidence disagrees
        Measurement("p1", "A", Point(1, 1), "B", Point(1, 1)),
        Measurement("p2", "B", Point(2, 2), "C", Point(2, 2)),
        Measurement("p3", "A", Point(3, 3), "C", Point(3, 3)),
        Measurement("p4", "A", Point(4, 4), "B", Point(7, 4)),
    ]
    result = service.compute_verification(snapshot(measurements, sheets), "A", "C")
    assert result.status == "contradiction"
    assert result.translation is None
    assert any("p4" in line for line in result.conflicts)


def test_measurement_outside_component_does_not_break_verification():
    sheets = [sheet("A"), sheet("B"), sheet("C"), sheet("D")]
    measurements = [
        Measurement("m1", "A", Point(1, 1), "B", Point(1, 1)),
        Measurement("m2", "B", Point(2, 2), "C", Point(2, 2)),
        Measurement("m3", "A", Point(3, 3), "C", Point(3, 3)),
        # conflicting pair elsewhere: not in A's component, must be ignored
        Measurement("z1", "D", Point(1, 1), "C", Point(1, 1)),
    ]
    result = service.compute_verification(snapshot(measurements, sheets), "A", "C")
    assert result.status == "verified"


def test_all_paths_agree_on_translation():
    # both paths must imply the same shift; a mismatch is a contradiction
    sheets = [sheet("A"), sheet("B"), sheet("C")]
    measurements = [
        Measurement("m1", "A", Point(1, 1), "B", Point(1, 1)),
        Measurement("m2", "B", Point(2, 2), "C", Point(3, 4)),
        Measurement("m3", "A", Point(3, 3), "C", Point(3, 3)),
    ]
    result = service.compute_verification(snapshot(measurements, sheets), "A", "C")
    assert result.status == "contradiction"


# ------------------------------------------------- edge-disjoint paths vs brute force


def brute_force_two_paths(
    measurements: list[Measurement], reference: str, target: str
) -> int:
    """Maximum number of pairwise edge-disjoint simple paths (small graphs)."""

    adjacency: dict[str, list[tuple[str, str]]] = {}
    for measurement in measurements:
        adjacency.setdefault(measurement.sheet_a, []).append(
            (measurement.sheet_b, measurement.no)
        )
        adjacency.setdefault(measurement.sheet_b, []).append(
            (measurement.sheet_a, measurement.no)
        )

    all_paths: list[frozenset[str]] = []

    def walk(node: str, used: frozenset[str], visited: frozenset[str]) -> None:
        if node == target:
            all_paths.append(used)
            return
        for nxt, no in adjacency.get(node, []):
            if no in used or nxt in visited:
                continue
            walk(nxt, used | {no}, visited | {nxt})

    walk(reference, frozenset(), frozenset({reference}))
    if not all_paths:
        return 0
    if any(not (a & b) for a, b in itertools.combinations(all_paths, 2)):
        return 2
    return 1


def test_flow_matches_brute_force_on_random_small_graphs():
    rng = random.Random(20261124)
    names = ["A", "B", "C", "D", "E"]
    for _ in range(300):
        count = rng.randint(0, 8)
        edge_keys = list(itertools.combinations(names, 2))
        rng.shuffle(edge_keys)
        chosen = edge_keys[:count]
        measurements = [
            Measurement(f"m{index}", a, Point(0, 0), b, Point(0, 0))
            for index, (a, b) in enumerate(chosen)
        ]
        sheets = [sheet(name) for name in names]
        result = service.compute_verification(snapshot(measurements, sheets), "A", "E")
        expected = brute_force_two_paths(measurements, "A", "E")
        # all-zero coordinates are trivially consistent, so the status is
        # determined solely by connectivity / path count
        if expected == 0:
            assert result.status == "disconnected", (measurements, result.status)
        elif expected == 1:
            assert result.status == "insufficient", (measurements, result.status)
        else:
            assert result.status == "verified", (measurements, result.status)
            used = [no for path in result.paths for no in path.measurements]
            assert len(used) == len(set(used))


def test_two_paths_found_even_when_greedy_order_could_block():
    # A-C direct, A-B, B-C, B-D, D-C: two paths exist (A-C and A-B-D-C)
    measurements = [
        Measurement("m1", "A", Point(0, 0), "C", Point(0, 0)),
        Measurement("m2", "A", Point(0, 0), "B", Point(0, 0)),
        Measurement("m3", "B", Point(0, 0), "C", Point(0, 0)),
        Measurement("m4", "B", Point(0, 0), "D", Point(0, 0)),
        Measurement("m5", "D", Point(0, 0), "C", Point(0, 0)),
    ]
    sheets = [sheet(name) for name in "ABCD"]
    result = service.compute_verification(snapshot(measurements, sheets), "A", "C")
    assert result.status == "verified"
    used = [no for path in result.paths for no in path.measurements]
    assert len(used) == len(set(used))


def test_lower_bound_is_inside_and_upper_bound_is_outside():
    sheets = [sheet("A", 0, 0, 10, 10), sheet("B", 5, 5, 15, 15)]
    measurements = [
        Measurement("low", "A", Point(0, 0), "B", Point(5, 5)),
        Measurement("high", "A", Point(9, 9), "B", Point(15, 15)),
    ]
    _, validity = service.valid_edges(snapshot(measurements, sheets))
    assert validity[0].valid is True
    assert validity[1].valid is False


def test_measurement_with_out_of_range_point_is_still_stored_and_can_become_valid():
    sheets = [sheet("A"), sheet("B", 0, 0, 10, 10)]
    measurement = Measurement("M1", "A", Point(1, 1), "B", Point(42, 7))
    result = service.compute_verification(snapshot([measurement], sheets), "A", "B")
    # the only measurement is invalid, so there is no valid path at all
    assert result.status == "disconnected"
    # its raw coordinates are still reported for review
    assert result.invalid_measurements[0].measurement.point_b == Point(42, 7)
    # widening B makes the same stored measurement valid again
    wider = [sheet("A"), sheet("B", 0, 0, 100, 100)]
    assert service.valid_edges(snapshot([measurement], wider))[1][0].valid is True


def test_conflict_on_a_branch_not_used_by_the_paths_still_blocks():
    # A->C has two independent paths (A-C and A-B-C), but a side branch A-D
    # carries contradictory evidence.  The whole reference group must agree.
    sheets = [sheet(name) for name in "ABCD"]
    measurements = [
        Measurement("m1", "A", Point(10, 10), "B", Point(10, 10)),
        Measurement("m2", "B", Point(20, 20), "C", Point(20, 20)),
        Measurement("m3", "A", Point(30, 30), "C", Point(30, 30)),
        Measurement("side1", "A", Point(40, 40), "D", Point(40, 40)),
        Measurement("side2", "A", Point(50, 50), "D", Point(53, 50)),
    ]
    result = service.compute_verification(snapshot(measurements, sheets), "A", "C")
    assert result.status == "contradiction"
    assert all("side" not in path.measurements for path in result.paths)
