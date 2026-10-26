"""G048-02: affine XY bounds must not inherit huge Z rounding."""
import math

import ezdxf
import pytest
from ezdxf.math import Matrix44

from hvac_cad.hatch_bounds import hatch_bounds, lwpolyline_bounds


def hatch():
    doc = ezdxf.new()
    e = doc.modelspace().add_hatch()
    e.paths.add_polyline_path([(0, 0), (2, 0), (2, 3), (0, 3)], is_closed=True)
    return e


def test_z_translation_does_not_expand_xy():
    e = hatch()
    a = hatch_bounds(e, Matrix44.translate(5, -7, 0))
    b = hatch_bounds(e, Matrix44.translate(5, -7, 7.2e99))
    assert b == a
    assert b[2] - b[0] == pytest.approx(2, abs=1e-10)
    assert b[3] - b[1] == pytest.approx(3, abs=1e-10)


def test_curved_polyline_uses_same_safe_xy_transform():
    doc = ezdxf.new()
    e = doc.modelspace().add_lwpolyline([(0, 0, 1), (2, 0, 0)], format="xyb")
    a = lwpolyline_bounds(e, Matrix44())
    b = lwpolyline_bounds(e, Matrix44.translate(0, 0, 7.2e99))
    assert b == a
    assert max(b[2] - b[0], b[3] - b[1]) < 3


def test_actual_z_to_x_projection_is_preserved():
    e = hatch()
    e.dxf.elevation = (0, 0, 7)
    b = hatch_bounds(e, Matrix44.y_rotate(math.pi / 2))
    assert b[0] <= 7 <= b[2]
    assert b[0] > 6.999999999


def test_non_affine_matrix_is_rejected():
    m = Matrix44()
    m[0, 3] = 0.01
    assert hatch_bounds(hatch(), m) is None


def test_overflowing_affine_result_is_unavailable_not_clamped():
    e = hatch()
    b = hatch_bounds(e, Matrix44.translate(1e308, -1e308, 0))
    assert b is not None
    huge = hatch_bounds(e, Matrix44.scale(1e308, 1e308, 1))
    assert huge is None
