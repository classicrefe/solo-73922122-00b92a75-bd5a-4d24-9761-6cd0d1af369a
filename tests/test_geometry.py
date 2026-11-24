from __future__ import annotations

import pytest

from adjcheck.geometry import Point, Rect


def test_rect_uses_half_open_bounds():
    rect = Rect(0, 0, 10, 10)
    assert rect.contains(0, 0)
    assert rect.contains(9, 9)
    assert not rect.contains(10, 0)
    assert not rect.contains(0, 10)
    assert not rect.contains(-1, 0)


def test_rect_rejects_zero_area_and_non_integers():
    with pytest.raises(ValueError):
        Rect(1, 0, 1, 5)
    with pytest.raises(TypeError):
        Rect(0, 0, 1.5, 5)  # type: ignore[arg-type]


def test_point_shift():
    assert Point(3, 4).shifted(-1, 2) == Point(2, 6)
