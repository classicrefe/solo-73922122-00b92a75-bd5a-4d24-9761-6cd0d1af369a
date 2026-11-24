"""基础几何类型。

坐标一律为整数米。图幅范围使用半开区间：包含下界、不包含上界，
因此点 ``(x0, y0)`` 在图幅内，而点 ``(x1, y1)`` 不在图幅内。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Rect:
    """一个有正面积的轴对齐整数矩形范围。"""

    x0: int
    y0: int
    x1: int
    y1: int

    def __post_init__(self) -> None:
        for value in (self.x0, self.y0, self.x1, self.y1):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"坐标必须是整数米: {self!r}")
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

    def contains(self, x: int, y: int) -> bool:
        """半开区间判定：x0 <= x < x1 且 y0 <= y < y1。"""

        return self.x0 <= x < self.x1 and self.y0 <= y < self.y1

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass(frozen=True)
class Point:
    x: int
    y: int

    def shifted(self, dx: int, dy: int) -> Point:
        return Point(self.x + dx, self.y + dy)

    def as_tuple(self) -> tuple[int, int]:
        return (self.x, self.y)
