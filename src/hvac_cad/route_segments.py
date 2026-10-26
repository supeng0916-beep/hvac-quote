"""Bounded, internal CAD geometry evidence; no specification or quantity adoption.

Coordinates and residuals are drawing units (DU). Segment indices are zero-based
in the original normalized polyline. t runs from that segment's start (0) to end
(1). A supplied metre conversion is always unconfirmed here: the caller owns
file/scope/scale adoption and upstream issues. No endpoint snapping, nearest-
segment selection, missing connections, loss or billable lengths are inferred.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, localcontext
import math
import re
from typing import TypedDict

from ezdxf.entities import Hatch
from ezdxf.entities.boundary_paths import PolylinePath

from .piping import _decimal_string, _length


class RouteSegmentError(ValueError):
    """Stable technical failure; the caller retains the original source issues."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class TriangleBoundary(TypedDict):
    source: dict
    vertices: list[list[float]]


class SegmentProjection(TypedDict):
    entityId: str
    source: dict
    segmentIndex: int
    t: float
    anchor: list[float]
    point: list[float]
    residualDU: float


class RoutePiece(TypedDict):
    vertices: list[list[float]]
    lengthDU: str
    lengthM: str | None


class RouteSplit(TypedDict):
    entityId: str
    source: dict
    segmentIndex: int
    t: float
    point: list[float]
    originalLengthDU: str
    originalLengthM: str | None
    before: RoutePiece
    after: RoutePiece
    conservationResidualDU: str
    metresPerDrawingUnit: str | None
    scaleStatus: str


def _require(condition, code):
    if not condition:
        raise RouteSegmentError(code)


def _number(value):
    _require(type(value) in (int, float), 'GEOMETRY_NUMBER_INVALID')
    try:
        value = float(value)
    except OverflowError as exc:
        raise RouteSegmentError('GEOMETRY_NUMBER_INVALID') from exc
    _require(math.isfinite(value), 'GEOMETRY_NUMBER_INVALID')
    return value


def _point(value):
    _require(isinstance(value, (list, tuple)) and len(value) == 3, 'GEOMETRY_POINT_INVALID')
    return [_number(n) for n in value]


def _source(value):
    _require(isinstance(value, dict) and all(
        isinstance(value.get(k), str) and value[k].strip()
        for k in ('fileVersionId', 'view', 'entityHandle')
    ), 'SOURCE_REQUIRED')
    return deepcopy(value)


def _route(record):
    _require(isinstance(record, dict) and record.get('type') == 'LWPOLYLINE'
             and record.get('supported') is True, 'ROUTE_TYPE_UNSUPPORTED')
    _require(isinstance(record.get('id'), str) and record['id'], 'SOURCE_REQUIRED')
    _source(record.get('source'))
    g = record.get('geometry')
    _require(isinstance(g, dict), 'ROUTE_GEOMETRY_REQUIRED')
    _require(g.get('closed') is False, 'ROUTE_CLOSED')
    _require(g.get('coordinateSystem') == 'OCS'
             and g.get('extrusion') == [0, 0, 1], 'ROUTE_PLANE_UNSUPPORTED')
    z = _number(g.get('elevation'))
    vertices = g.get('vertices')
    _require(isinstance(vertices, list) and len(vertices) >= 2, 'ROUTE_DEGENERATE')
    points = []
    for vertex in vertices:
        x, y, bulge = _point(vertex)
        _require(bulge == 0, 'ROUTE_CURVED')
        points.append([x, y, z])
    for a, b in zip(points, points[1:]):
        length = math.dist(a, b)
        _require(math.isfinite(length) and length > 0, 'ROUTE_DEGENERATE')
    return points


def _segment_index(index, count):
    _require(type(index) is int and 0 <= index < count, 'SEGMENT_INDEX_INVALID')
    return index


def extract_triangle_boundary(entity: Hatch, *, source: dict) -> TriangleBoundary:
    """Root SOLID HATCH only, one closed straight triangular OCS polyline.

    This does not expand/transform INSERT instances or infer a triangle's inlet.
    The caller must use the root entity from the read-only DXF and identify the
    corresponding file/view. Tilted planes, holes, patterns and arcs are rejected.
    """
    ref = _source(source)
    _require(isinstance(entity, Hatch) and entity.dxf.solid_fill == 1,
             'TRIANGLE_TYPE_UNSUPPORTED')
    _require(entity.dxf.handle == ref['entityHandle'], 'TRIANGLE_SOURCE_MISMATCH')
    layout = entity.get_layout()
    _require(layout is not None and layout.is_any_layout, 'TRIANGLE_INSTANCE_UNSUPPORTED')
    _require(ref['view'] == ('model' if layout.is_modelspace else layout.name),
             'TRIANGLE_SOURCE_MISMATCH')
    _require(list(entity.dxf.extrusion) == [0, 0, 1], 'TRIANGLE_PLANE_UNSUPPORTED')
    elevation = _point(list(entity.dxf.elevation))
    _require(elevation[:2] == [0, 0], 'TRIANGLE_PLANE_UNSUPPORTED')
    paths = entity.paths
    _require(len(paths) == 1 and isinstance(paths[0], PolylinePath)
             and paths[0].is_closed, 'TRIANGLE_BOUNDARY_UNSUPPORTED')
    vertices = [_point(v) for v in paths[0].vertices]
    if len(vertices) == 4 and vertices[-1] == vertices[0]:
        vertices.pop()
    _require(len(vertices) == 3 and all(v[2] == 0 for v in vertices),
             'TRIANGLE_BOUNDARY_UNSUPPORTED')
    points = [[v[0], v[1], elevation[2]] for v in vertices]
    a, b, c = points
    area2 = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    _require(math.isfinite(area2) and area2 != 0, 'TRIANGLE_DEGENERATE')
    return {'source': ref, 'vertices': points}


def project_anchor_to_segment(
    record: dict, anchor: list[float], *, max_residual_du: float,
    segment_index: int | None = None,
) -> SegmentProjection:
    """Project an explicit anchor; require exactly one permitted segment.

    The caller supplies its explicit residual bound, in DU. No default tolerance
    or engineering approval is implied. Zero/multiple candidates are errors even
    when one candidate is closer. An explicit index narrows only to that segment.
    Projections outside [0,1] are rejected, never clamped to a nearest endpoint.
    """
    points = _route(record)
    anchor = _point(anchor)
    _require(anchor[2] == points[0][2], 'ANCHOR_PLANE_MISMATCH')
    bound = _number(max_residual_du)
    _require(bound >= 0, 'PROJECTION_BOUND_INVALID')
    indices = range(len(points) - 1) if segment_index is None else [
        _segment_index(segment_index, len(points) - 1)
    ]
    candidates = []
    for i in indices:
        a, b = points[i:i + 2]
        length = math.dist(a, b)
        # Unit-vector projection avoids squaring large coordinate differences.
        ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
        # Exact source vertices are not a tolerance-based endpoint snap. Preserve
        # them before floating projection (e.g. sqrt(2) can round t below 1).
        t = 0.0 if anchor == a else 1.0 if anchor == b else (
            ((anchor[0] - a[0]) * ux + (anchor[1] - a[1]) * uy) / length
        )
        if not 0 <= t <= 1:
            continue
        point = a.copy() if t == 0 else b.copy() if t == 1 else [
            a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), a[2]
        ]
        residual = math.dist(anchor, point)
        if math.isfinite(residual) and residual <= bound:
            candidates.append({'entityId': record['id'], 'source': _source(record['source']),
                               'segmentIndex': i, 't': t, 'anchor': anchor.copy(),
                               'point': point, 'residualDU': residual})
    _require(len(candidates) > 0, 'PROJECTION_NOT_FOUND')
    _require(len(candidates) == 1, 'PROJECTION_AMBIGUOUS')
    return candidates[0]


def split_route_at_parameter(
    record: dict, segment_index: int, t: float, *,
    metres_per_drawing_unit: str | None = None,
) -> RouteSplit:
    """Split the original path at an explicit segment/t; return two ordered paths.

    DU lengths reuse the existing geometry length semantics. Decimal conversion
    does not round for display or billing, and is explicitly unconfirmed. Caller
    must retain the projection evidence separately if t was obtained by projection.
    """
    points = _route(record)
    i = _segment_index(segment_index, len(points) - 1)
    t = _number(t)
    _require(0 <= t <= 1, 'SEGMENT_PARAMETER_INVALID')
    factor = None
    if metres_per_drawing_unit is not None:
        _require(isinstance(metres_per_drawing_unit, str)
                 and len(metres_per_drawing_unit) <= 64
                 and re.fullmatch(r'(?:0|[1-9]\d*)(?:\.\d+)?', metres_per_drawing_unit)
                 and Decimal(metres_per_drawing_unit) > 0, 'SCALE_INVALID')
        factor = Decimal(metres_per_drawing_unit)
    a, b = points[i:i + 2]
    point = a.copy() if t == 0 else b.copy() if t == 1 else [
        a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), a[2]
    ]
    before = points[:i + 1]
    if before[-1] != point:
        before.append(point.copy())
    after = points[i + 1:]
    if after[0] != point:
        after.insert(0, point.copy())

    def metres(length):
        if factor is None:
            return None
        with localcontext() as context:
            context.prec = 100
            return _decimal_string(Decimal(str(length)) * factor)

    def piece(vertices):
        length = math.fsum(math.dist(a, b) for a, b in zip(vertices, vertices[1:]))
        return {'vertices': vertices, 'lengthDU': _decimal_string(Decimal(str(length))),
                'lengthM': metres(length)}

    original = _length(record)
    left, right = piece(before), piece(after)
    summed = math.fsum([float(left['lengthDU']), float(right['lengthDU'])])
    _require(math.isfinite(original) and math.isclose(original, summed, rel_tol=1e-12, abs_tol=1e-9),
             'LENGTH_CONSERVATION_FAILED')
    # This tolerance checks floating arithmetic conservation, not CAD precision,
    # a permitted geometric snap distance or an engineering dimension tolerance.
    return {'entityId': record['id'], 'source': _source(record['source']),
            'segmentIndex': i, 't': t, 'point': point,
            'originalLengthDU': _decimal_string(Decimal(str(original))),
            'originalLengthM': metres(original), 'before': left, 'after': right,
            'conservationResidualDU': _decimal_string(Decimal(str(summed - original))),
            'metresPerDrawingUnit': metres_per_drawing_unit,
            'scaleStatus': 'drawing_units_only' if factor is None else 'unconfirmed_conversion'}
