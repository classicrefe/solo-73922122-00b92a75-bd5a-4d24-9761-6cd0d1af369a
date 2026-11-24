"""业务规则：解析、测量有效性、连通性、路径提取与更正编排。

核心判定全部是纯函数，只读取一份 :class:`~adjcheck.models.Snapshot`
（同一次读取得到的目录与测量表），因此计算期间发生的图幅更正不会改变
本次核查使用的事实；下一次核查会读到新的快照并重新判定测量是否有效。
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Sequence

from .db import Database
from .errors import ValidationError
from .geometry import Point, Rect
from .models import (
    Measurement,
    MeasurementValidity,
    PathResult,
    Sheet,
    Snapshot,
    Verification,
)

MAX_SHEETS = 500
MAX_CORRECTION_SHEETS = MAX_SHEETS
MAX_MEASUREMENTS = 120

SHEET_NO_RE = re.compile(r"^[^\s,;，；]{1,64}$")
MEASUREMENT_NO_RE = re.compile(r"^[^\s,;，；]{1,64}$")
PROJECT_NAME_RE = re.compile(r"^[\w.\-一-鿿]{1,60}$")
FEATURE_RE = re.compile(r"^[^\r\n\t]{1,120}$")


# --------------------------------------------------------------------- parsing


def _parse_int(raw: object, label: str, reasons: list[str]) -> int | None:
    try:
        if isinstance(raw, bool):
            raise TypeError
        return int(str(raw).strip())
    except (TypeError, ValueError):
        reasons.append(f"{label} 必须是整数米，收到 {raw!r}")
        return None


def parse_sheet_lines(
    text: str, *, max_entries: int, context: str, min_entries: int = 1
) -> list[tuple[str, Rect]]:
    """解析“图幅号 x0 y0 x1 y1”行。"""

    lines = [line for line in (text or "").splitlines() if line.strip()]
    if len(lines) < min_entries:
        raise ValidationError([f"{context}至少需要 {min_entries} 张图幅"])
    if len(lines) > max_entries:
        raise ValidationError([f"{context}最多 {max_entries} 张图幅，当前 {len(lines)} 张"])
    entries: list[tuple[str, Rect]] = []
    seen: set[str] = set()
    reasons: list[str] = []
    for index, line in enumerate(lines, start=1):
        parts = [part for part in re.split(r"[\s,;，；]+", line.strip()) if part]
        if len(parts) != 5:
            reasons.append(f"第 {index} 行格式应为“图幅号 x0 y0 x1 y1”，收到 {line.strip()!r}")
            continue
        no, *coords = parts
        if not SHEET_NO_RE.match(no):
            reasons.append(f"第 {index} 行图幅号非法: {no!r}（不可含空白，最长 64 字符）")
            continue
        ints = [_parse_int(value, f"第 {index} 行坐标", reasons) for value in coords]
        if any(value is None for value in ints):
            continue
        try:
            rect = Rect(*ints)  # type: ignore[arg-type]
        except ValueError:
            reasons.append(
                f"第 {index} 行矩形必须满足 x0<x1 且 y0<y1（整数米）: {line.strip()!r}"
            )
            continue
        if no in seen:
            reasons.append(f"图幅号重复: {no}")
            continue
        seen.add(no)
        entries.append((no, rect))
    if reasons:
        raise ValidationError(reasons)
    return entries


def parse_project_name(raw: str) -> str:
    name = (raw or "").strip()
    if not PROJECT_NAME_RE.match(name):
        raise ValidationError(["项目名不可含空白或斜杠，最长 60 字符"])
    return name


def parse_feature(raw: str) -> str:
    feature = (raw or "").strip()
    if not FEATURE_RE.match(feature):
        raise ValidationError(["地物名不能为空，且不可包含换行，最长 120 字符"])
    return feature


def parse_measurement_form(form: dict, sheets: dict[str, Sheet]) -> Measurement:
    """校验并构造一条测量；坐标可以暂不在现行图幅内（届时只是不参与核查）。"""

    reasons: list[str] = []
    no = str(form.get("no") or "").strip()
    if not MEASUREMENT_NO_RE.match(no):
        reasons.append("测量号不能为空、不可含空白或分隔符，最长 64 字符")
    sheet_a = str(form.get("sheet_a") or "").strip()
    sheet_b = str(form.get("sheet_b") or "").strip()
    if sheet_a not in sheets:
        reasons.append(f"图幅 A 不在现行目录: {sheet_a!r}")
    if sheet_b not in sheets:
        reasons.append(f"图幅 B 不在现行目录: {sheet_b!r}")
    if sheet_a and sheet_a == sheet_b:
        reasons.append("一条测量的两张图幅必须不同")
    coords: dict[str, int] = {}
    for key, label in (
        ("ax", "图幅 A 的 x"),
        ("ay", "图幅 A 的 y"),
        ("bx", "图幅 B 的 x"),
        ("by", "图幅 B 的 y"),
    ):
        value = _parse_int(form.get(key), label, reasons)
        if value is not None:
            coords[key] = value
    if reasons:
        raise ValidationError(reasons)
    return Measurement(
        no=no,
        sheet_a=sheet_a,
        point_a=Point(coords["ax"], coords["ay"]),
        sheet_b=sheet_b,
        point_b=Point(coords["bx"], coords["by"]),
    )


def parse_reference_pair(form: dict, sheets: dict[str, Sheet]) -> tuple[str, str]:
    reference = str(form.get("reference") or "").strip()
    target = str(form.get("target") or "").strip()
    reasons: list[str] = []
    if reference not in sheets:
        reasons.append("请选择现行的基准片")
    if target not in sheets:
        reasons.append("请选择现行的待配准片")
    if reference and reference == target:
        reasons.append("基准片与待配准片必须不同")
    if reasons:
        raise ValidationError(reasons)
    return reference, target


# -------------------------------------------------------------------- validity


def measurement_validity(
    measurement: Measurement, sheets: dict[str, Sheet]
) -> MeasurementValidity:
    """按现行图幅重判两个测量点是否都落在各自图幅内（下界含、上界不含）。"""

    reasons: list[str] = []
    for sheet_no, point in (
        (measurement.sheet_a, measurement.point_a),
        (measurement.sheet_b, measurement.point_b),
    ):
        sheet = sheets.get(sheet_no)
        if sheet is None:
            reasons.append(f"图幅 {sheet_no} 已不在现行目录")
        elif not sheet.rect.contains(point.x, point.y):
            reasons.append(
                f"图幅 {sheet_no} 上的点 ({point.x}, {point.y}) 不在现行范围 "
                f"[{sheet.rect.x0} {sheet.rect.y0} {sheet.rect.x1} {sheet.rect.y1}) 内"
            )
    return MeasurementValidity(measurement=measurement, valid=not reasons, reasons=tuple(reasons))


def valid_edges(
    snapshot: Snapshot,
) -> tuple[list[Measurement], tuple[MeasurementValidity, ...]]:
    """筛出两个测量点都落在现行图幅内的测量。"""

    sheets = snapshot.sheet_map
    validity = tuple(measurement_validity(item, sheets) for item in snapshot.measurements)
    edges = [
        item
        for item, result in zip(snapshot.measurements, validity, strict=True)
        if result.valid
    ]
    return edges, validity


def _adjacency(
    edges: Sequence[Measurement],
) -> dict[str, list[tuple[Measurement, str, Point, Point]]]:
    adjacency: dict[str, list[tuple[Measurement, str, Point, Point]]] = {}
    for measurement in edges:
        adjacency.setdefault(measurement.sheet_a, []).append(
            (measurement, measurement.sheet_b, measurement.point_a, measurement.point_b)
        )
        adjacency.setdefault(measurement.sheet_b, []).append(
            (measurement, measurement.sheet_a, measurement.point_b, measurement.point_a)
        )
    return adjacency


def _propagate(
    reference: str, adjacency: dict[str, list[tuple[Measurement, str, Point, Point]]]
) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """从基准片出发按有效测量传播平移量，并找出与组内传播值不相容的测量。"""

    translations: dict[str, tuple[int, int]] = {reference: (0, 0)}
    queue: deque[str] = deque([reference])
    while queue:
        current = queue.popleft()
        current_t = translations[current]
        for measurement, other, point_current, point_other in adjacency.get(current, []):
            # point_current + T_current == point_other + T_other
            expected = (
                current_t[0] + point_current.x - point_other.x,
                current_t[1] + point_current.y - point_other.y,
            )
            if other not in translations:
                translations[other] = expected
                queue.append(other)

    conflicts: dict[str, str] = {}
    for sheet_no in sorted(translations):
        for measurement, other, point_current, point_other in adjacency.get(sheet_no, []):
            if other not in translations or measurement.no in conflicts:
                continue
            implied = (
                translations[sheet_no][0] + point_current.x - point_other.x,
                translations[sheet_no][1] + point_current.y - point_other.y,
            )
            if implied != translations[other]:
                conflicts[measurement.no] = (
                    f"测量 {measurement.no}（{measurement.sheet_a}–{measurement.sheet_b}）与组内"
                    f"其他测量矛盾：它要求 {other} 的平移为 {implied}，"
                    f"组内传播值为 {translations[other]}"
                )
    return translations, list(conflicts.values())


def _propagate_to(
    reference: str,
    adjacency: dict[str, list[tuple[Measurement, str, Point, Point]]],
    target: str,
) -> tuple[int, int]:
    """独立按 BFS 树上的连续测量逐条累加，得到 target 的平移量。"""

    previous: dict[str, tuple[str, tuple[int, int]]] = {reference: ("", (0, 0))}
    queue: deque[str] = deque([reference])
    while queue:
        current = queue.popleft()
        for measurement, other, point_current, point_other in adjacency.get(current, []):
            if other in previous:
                continue
            dx, dy = previous[current][1]
            step = (point_current.x - point_other.x, point_current.y - point_other.y)
            previous[other] = (current, (dx + step[0], dy + step[1]))
            queue.append(other)
    return previous[target][1]


# ------------------------------------------------------- two edge-disjoint paths


class _Arc:
    __slots__ = ("capacity", "initial", "reverse", "to")

    def __init__(self, to: object, reverse: int, capacity: int) -> None:
        self.to = to
        self.reverse = reverse
        self.capacity = capacity
        self.initial = capacity

    @property
    def flow(self) -> int:
        return self.initial - self.capacity


def _add_edge(
    graph: dict[object, list[_Arc]], u: object, v: object, capacity: int = 1
) -> _Arc:
    forward = _Arc(v, len(graph.setdefault(v, [])), capacity)
    backward = _Arc(u, len(graph.setdefault(u, [])), 0)
    graph[u].append(forward)
    graph[v].append(backward)
    return forward


def _max_flow_two(
    edges: Sequence[Measurement], reference: str, target: str
) -> tuple[int, list[tuple[list[str], list[str]]]]:
    """求两条不共用测量的路径（图幅可共用）。

    每条测量被切成容量 1 的中间节点 ``(no, "in") -> (no, "out")``，两端图幅
    分别接入、接出；因此同一测量至多承载 1 单位流。图幅节点没有容量限制，
    两条路径可以经过同一张图幅。最大流至多 2，每条单位流即一条路径。

    返回 (最大路径条数, [(按顺序的测量号, 依次经过的图幅)])。
    """

    graph: dict[object, list[_Arc]] = {reference: [], target: []}
    entries: dict[str, list[tuple[str, _Arc]]] = {}
    exits: dict[str, list[tuple[str, _Arc]]] = {}
    gates: dict[str, _Arc] = {}
    by_no: dict[str, Measurement] = {}

    for measurement in edges:
        if measurement.sheet_a == measurement.sheet_b or measurement.no in by_no:
            continue
        by_no[measurement.no] = measurement
        no = measurement.no
        gate_in = ("in", no)
        gate_out = ("out", no)
        entries[no] = [
            (measurement.sheet_a, _add_edge(graph, measurement.sheet_a, gate_in)),
            (measurement.sheet_b, _add_edge(graph, measurement.sheet_b, gate_in)),
        ]
        gates[no] = _add_edge(graph, gate_in, gate_out)
        exits[no] = [
            (measurement.sheet_a, _add_edge(graph, gate_out, measurement.sheet_a)),
            (measurement.sheet_b, _add_edge(graph, gate_out, measurement.sheet_b)),
        ]

    def push_flow() -> bool:
        parent: dict[object, tuple[object, _Arc]] = {}
        visited: set[object] = {reference}
        queue: deque[object] = deque([reference])
        while queue and target not in visited:
            node = queue.popleft()
            for arc in graph.get(node, []):
                if arc.capacity > 0 and arc.to not in visited:
                    visited.add(arc.to)
                    parent[arc.to] = (node, arc)
                    queue.append(arc.to)
                    if arc.to == target:
                        break
        if target not in visited:
            return False
        node: object = target
        while node != reference:
            previous, arc = parent[node]
            arc.capacity -= 1
            graph[node][arc.reverse].capacity += 1
            node = previous
        return True

    flow = 0
    while flow < 2 and push_flow():
        flow += 1
    if flow == 0:
        return 0, []

    # Recover the oriented traversals of every measurement carrying flow.
    traversals: list[tuple[str, str, str]] = []  # (no, from_sheet, to_sheet)
    for no in by_no:
        if gates[no].flow <= 0:
            continue
        from_sheet = next((sheet for sheet, arc in entries[no] if arc.flow > 0), None)
        to_sheet = next((sheet for sheet, arc in exits[no] if arc.flow > 0), None)
        if from_sheet is None or to_sheet is None or from_sheet == to_sheet:
            continue
        traversals.append((no, from_sheet, to_sheet))

    paths: list[tuple[list[str], list[str]]] = []
    while len(paths) < flow:
        support: dict[str, list[tuple[str, str, int]]] = {}
        for index, (no, from_sheet, to_sheet) in enumerate(traversals):
            support.setdefault(from_sheet, []).append((no, to_sheet, index))
        stack: list[tuple[str, int]] = [(reference, 0)]
        path_sheets: list[str] = [reference]
        path_measurements: list[str] = []
        visited: set[str] = {reference}
        taken: list[int] = []
        found = False
        while stack:
            sheet, cursor = stack[-1]
            options = support.get(sheet, [])
            if cursor >= len(options):
                stack.pop()
                if stack:
                    visited.discard(path_sheets.pop())
                    path_measurements.pop()
                    taken.pop()
                continue
            stack[-1] = (sheet, cursor + 1)
            no, next_sheet, index = options[cursor]
            if next_sheet in visited:
                continue
            stack.append((next_sheet, 0))
            visited.add(next_sheet)
            path_sheets.append(next_sheet)
            path_measurements.append(no)
            taken.append(index)
            if next_sheet == target:
                found = True
                break
        if not found:
            break
        drop = set(taken)
        traversals = [item for index, item in enumerate(traversals) if index not in drop]
        paths.append((path_measurements, path_sheets))
    return flow, paths


# ---------------------------------------------------------------- verification


def compute_verification(
    snapshot: Snapshot, reference: str, target: str
) -> Verification:
    """按同一份快照核对基准片与待配准片。"""

    sheets = snapshot.sheet_map
    edges, validity = valid_edges(snapshot)
    problems: list[str] = []
    if reference not in sheets:
        problems.append(f"基准片不在现行目录: {reference!r}")
    if target not in sheets:
        problems.append(f"待配准片不在现行目录: {target!r}")
    if reference and reference == target:
        problems.append("基准片与待配准片必须不同")
    if problems:
        return Verification(
            status="invalid",
            message="；".join(problems),
            reference=reference,
            target=target,
            version=snapshot.project.version,
            validity=validity,
        )

    adjacency = _adjacency(edges)
    translations, conflicts = _propagate(reference, adjacency)
    # 组内相容意味着每条路径推算出的平移量都相同；仍逐路径独立累加并在
    # 返回前核对，防止任何实现偏差被静默放过。
    component_paths = {
        sheet_no: _propagate_to(reference, adjacency, sheet_no)
        for sheet_no in translations
    }
    if any(value != translations[sheet_no] for sheet_no, value in component_paths.items()):
        conflicts = [*conflicts, "内部错误：路径累加与传播值不一致"]

    if target not in translations:
        return Verification(
            status="disconnected",
            message=(
                f"基准片 {reference} 与待配准片 {target} 之间没有由有效测量连成的路径，两片失联。"
            ),
            reference=reference,
            target=target,
            version=snapshot.project.version,
            validity=validity,
        )

    if conflicts:
        return Verification(
            status="contradiction",
            message=(
                f"基准片 {reference} 所在组的有效测量不相容，存在测量矛盾；不给出平移量。"
            ),
            reference=reference,
            target=target,
            version=snapshot.project.version,
            validity=validity,
            conflicts=tuple(conflicts),
        )

    max_paths, raw_paths = _max_flow_two(edges, reference, target)
    target_translation = translations[target]
    by_no = {measurement.no: measurement for measurement in edges}
    paths = []
    for measurements, path_sheets in raw_paths:
        dx = dy = 0
        for index, no in enumerate(measurements):
            measurement = by_no[no]
            from_sheet = path_sheets[index]
            if from_sheet == measurement.sheet_a:
                point_from, point_to = measurement.point_a, measurement.point_b
            else:
                point_from, point_to = measurement.point_b, measurement.point_a
            dx += point_from.x - point_to.x
            dy += point_from.y - point_to.y
        paths.append(
            PathResult(
                measurements=tuple(measurements),
                sheets=tuple(path_sheets),
                translation=(dx, dy),
            )
        )
    paths = tuple(paths)

    if len(paths) < 2:
        return Verification(
            status="insufficient",
            message=(
                f"两片之间只找到 {len(paths)} 条不共用测量的有效路径，需要 2 条才能交叉核对；"
                "证据不足，不给出平移量。"
            ),
            reference=reference,
            target=target,
            version=snapshot.project.version,
            validity=validity,
            max_paths=max_paths,
        )

    return Verification(
        status="verified",
        message=(
            f"基准片 {reference} 平移为 (0, 0)；待配准片 {target} 平移为 "
            f"({target_translation[0]}, {target_translation[1]}) m，"
            "已由两条不共用测量的路径交叉核对。"
        ),
        reference=reference,
        target=target,
        version=snapshot.project.version,
        validity=validity,
        max_paths=2,
        translation=target_translation,
        paths=paths[:2],
    )


# --------------------------------------------------------------------- writes


def create_project(
    db: Database, name: str, feature: str, entries: Sequence[tuple[str, Rect]]
) -> bool:
    if not entries:
        raise ValidationError(["至少需要 1 张图幅"])
    if len(entries) > MAX_SHEETS:
        raise ValidationError([f"最多 {MAX_SHEETS} 张图幅，当前 {len(entries)} 张"])
    sheets = [Sheet(no, rect) for no, rect in entries]
    seen: set[str] = set()
    for sheet in sheets:
        if sheet.no in seen:
            raise ValidationError([f"图幅号重复: {sheet.no}"])
        seen.add(sheet.no)
    return db.create_project(name, feature, sheets)


def add_measurement(db: Database, name: str, measurement: Measurement) -> None:
    snapshot = db.get_snapshot(name)
    if snapshot is None:
        raise ValidationError(["项目不存在"])
    if len(snapshot.measurements) >= MAX_MEASUREMENTS:
        raise ValidationError([f"每个项目最多保存 {MAX_MEASUREMENTS} 条测量"])
    if any(item.no == measurement.no for item in snapshot.measurements):
        raise ValidationError([f"测量号重复: {measurement.no}"])
    db.add_measurement(name, measurement)


def apply_correction(
    db: Database,
    name: str,
    entries: Sequence[tuple[str, Rect]],
    expected_version: int,
) -> tuple[bool, str]:
    """提交图幅更正；测量原坐标保持不变，下一次核查按新图幅重判。"""

    if not 1 <= len(entries) <= MAX_CORRECTION_SHEETS:
        raise ValidationError([f"一次可更正 1~{MAX_CORRECTION_SHEETS} 张图幅"])
    snapshot = db.get_snapshot(name)
    if snapshot is None:
        raise ValidationError(["项目不存在"])
    if snapshot.project.version != expected_version:
        return (
            False,
            (
                f"页面版本 v{expected_version} 已过期，当前为 v{snapshot.project.version}；"
                + "未做任何修改，请刷新后重试"
            ),
        )
    existing = {sheet.no for sheet in snapshot.sheets}
    added = [no for no, _ in entries if no not in existing]
    if len(existing) + len(added) > MAX_SHEETS:
        raise ValidationError(
            [f"更正后目录将超过 {MAX_SHEETS} 张图幅（现有 {len(existing)}，新增 {len(added)}）"]
        )
    return db.apply_correction(name, expected_version, entries)


# ----------------------------------------------------------------------- demo

DEMO_NAME = "DEMO-REG-001"
DEMO_FEATURE = "独立树"
DEMO_SHEETS = [
    Sheet("A", Rect(0, 0, 100, 100)),
    Sheet("B", Rect(0, 0, 100, 100)),
    Sheet("C", Rect(0, 0, 100, 100)),
]
# 图幅各自的量测坐标（整数米）；同一地物的坐标加上各片平移量后重合。
# 正确平移量：A=(0,0)，B=(-2,1)，C=(3,-2)。
DEMO_MEASUREMENTS = [
    Measurement("M1", "A", Point(10, 10), "B", Point(12, 9)),
    Measurement("M2", "B", Point(40, 20), "C", Point(35, 23)),
    Measurement("M3", "A", Point(60, 60), "C", Point(57, 62)),
    Measurement("M4", "A", Point(30, 70), "B", Point(32, 69)),
]
DEMO_CORRECTION_TEXT = "C 0 0 57 100\n"
DEMO_NOTES = [
    "图幅 A、B、C 均为 0 0 100 100（各自量测坐标），地物“独立树”有 4 条测量。",
    "初始核查：A 与 C 有两条不共用测量的路径（A-C 用 M3；A-B-C 用 M1+M2 或 M4+M2），"
    + "给出 C 的平移量 (3, -2)。",
    "更正为 C 0 0 57 100：测量原坐标不改；因坐标范围不含上界，M3 在 C 上的点 x=57 "
    + "越界而失效，只剩 A-B-C 一条路径，判为证据不足（原坐标保留在测量表中，可复核）。",
    "对照：改为 C 0 0 58 100 时 M3 的 x=57 仍满足 57<58，测量有效，结论保持已核实。",
]

DISCONNECTED_NAME = "DEMO-REG-002"
DISCONNECTED_FEATURE = "涵洞"
DISCONNECTED_SHEETS = [
    Sheet("P", Rect(0, 0, 100, 100)),
    Sheet("Q", Rect(0, 0, 100, 100)),
    Sheet("R", Rect(0, 0, 100, 100)),
]
DISCONNECTED_MEASUREMENTS = [
    Measurement("N1", "P", Point(10, 10), "Q", Point(12, 10)),
]

CONFLICT_NAME = "DEMO-REG-003"
CONFLICT_FEATURE = "水塔"
CONFLICT_SHEETS = [
    Sheet("X", Rect(0, 0, 100, 100)),
    Sheet("Y", Rect(0, 0, 100, 100)),
]
CONFLICT_MEASUREMENTS = [
    Measurement("K1", "X", Point(10, 10), "Y", Point(12, 10)),
    Measurement("K2", "X", Point(20, 20), "Y", Point(25, 20)),
]


def ensure_demo(db: Database) -> None:
    demo_projects = [
        (DEMO_NAME, DEMO_FEATURE, DEMO_SHEETS, DEMO_MEASUREMENTS),
        (DISCONNECTED_NAME, DISCONNECTED_FEATURE, DISCONNECTED_SHEETS, DISCONNECTED_MEASUREMENTS),
        (CONFLICT_NAME, CONFLICT_FEATURE, CONFLICT_SHEETS, CONFLICT_MEASUREMENTS),
    ]
    for name, feature, sheets, measurements in demo_projects:
        if db.get_snapshot(name) is not None:
            continue
        create_project(db, name, feature, [(sheet.no, sheet.rect) for sheet in sheets])
        for measurement in measurements:
            add_measurement(db, name, measurement)
