"""领域数据结构。"""

from __future__ import annotations

from dataclasses import dataclass

from .geometry import Point, Rect


@dataclass(frozen=True)
class Project:
    name: str
    feature: str
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Sheet:
    no: str
    rect: Rect


@dataclass(frozen=True)
class Measurement:
    """同一点在两张图幅上的一次量测（原始坐标永不因更正而改写）。"""

    no: str
    sheet_a: str
    point_a: Point
    sheet_b: str
    point_b: Point


@dataclass(frozen=True)
class Snapshot:
    """一次读取得到、彼此一致的目录与测量表快照。"""

    project: Project
    sheets: tuple[Sheet, ...]
    measurements: tuple[Measurement, ...]

    @property
    def sheet_map(self) -> dict[str, Sheet]:
        return {sheet.no: sheet for sheet in self.sheets}


@dataclass(frozen=True)
class MeasurementValidity:
    measurement: Measurement
    valid: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PathResult:
    """一条从基准片到待配准片的测量路径。"""

    measurements: tuple[str, ...]
    sheets: tuple[str, ...]
    translation: tuple[int, int]

    @property
    def translation_x(self) -> int:
        return self.translation[0]

    @property
    def translation_y(self) -> int:
        return self.translation[1]


STATUS_LABELS = {
    "verified": "已核实",
    "contradiction": "测量矛盾",
    "insufficient": "证据不足",
    "disconnected": "失联",
    "invalid": "输入无效",
}


@dataclass(frozen=True)
class Verification:
    status: str
    message: str
    reference: str
    target: str
    version: int
    validity: tuple[MeasurementValidity, ...]
    conflicts: tuple[str, ...] = ()
    max_paths: int = 0
    translation: tuple[int, int] | None = None
    paths: tuple[PathResult, ...] = ()

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def valid_measurements(self) -> tuple[MeasurementValidity, ...]:
        return tuple(v for v in self.validity if v.valid)

    @property
    def invalid_measurements(self) -> tuple[MeasurementValidity, ...]:
        return tuple(v for v in self.validity if not v.valid)

    @property
    def translation_x(self) -> int | None:
        return None if self.translation is None else self.translation[0]

    @property
    def translation_y(self) -> int | None:
        return None if self.translation is None else self.translation[1]
