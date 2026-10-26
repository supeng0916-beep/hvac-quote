"""Conservative complete HATCH bounds, not length/area or pricing evidence.

Only closed polyline loops are supported. Every bulged edge contributes its
endpoints and all coordinate extrema on the arc, not the unused full circle.
Every loop (including islands) contributes to the envelope.
Unknown paths invalidate the whole result. No sampled bbox or partial child
union is used. Compute before INSERT transformation so nonuniform scaling
cannot silently approximate circular boundaries as splines.
"""
import math
from ezdxf.entities.boundary_paths import PolylinePath
from ezdxf.math import bulge_to_arc


def hatch_bounds(entity, matrix):
    try:
        return _bounds(entity, matrix)
    except (ValueError, TypeError, AttributeError, ArithmeticError):
        return None


def _bounds(entity, matrix):
    if entity.dxftype() != 'HATCH' or entity.proxy_graphic or not entity.paths:
        return None
    if entity.dxf.hasattr('thickness') and entity.dxf.thickness:
        return None
    # Bounds use an affine image of a planar box, not perspective projection.
    if (not all(math.isfinite(v) for v in matrix)
            or any(matrix[row, 3] != 0 for row in range(3)) or matrix[3, 3] != 1):
        return None
    elevation = list(entity.dxf.elevation)
    extrusion = list(entity.dxf.extrusion)
    if (not all(math.isfinite(v) for v in elevation + extrusion)
            or elevation[0] != 0 or elevation[1] != 0 or math.hypot(*extrusion) == 0):
        return None
    loops = []
    if len(entity.paths) > 10000:
        return None
    for path in entity.paths:
        if not isinstance(path, PolylinePath) or not path.is_closed:
            return None
        loops.append((path.vertices, True))
    return _loop_bounds(loops, elevation[2], entity.ocs(), matrix)


def lwpolyline_bounds(entity, matrix):
    """Same complete-arc envelope for zero-width polylines; never a length."""
    try:
        if (entity.dxftype() != 'LWPOLYLINE' or entity.has_width or entity.proxy_graphic
                or entity.dxf.get('thickness', 0)):
            return None
        extrusion = list(entity.dxf.extrusion)
        if (not all(math.isfinite(v) for v in extrusion) or math.hypot(*extrusion) == 0
                or not math.isfinite(entity.dxf.elevation)):
            return None
        if (not all(math.isfinite(v) for v in matrix)
                or any(matrix[row, 3] != 0 for row in range(3)) or matrix[3, 3] != 1):
            return None
        return _loop_bounds([(list(entity.get_points('xyb')), entity.closed)],
                            entity.dxf.elevation, entity.ocs(), matrix)
    except (ValueError, TypeError, AttributeError, ArithmeticError):
        return None


def _outward(low, high):
    pair = math.nextafter(low, -math.inf), math.nextafter(high, math.inf)
    if not all(math.isfinite(v) for v in pair):
        raise ArithmeticError('unbounded affine interval')
    return pair


def _interval_add(a, b):
    if a == (0.0, 0.0):
        return b
    if b == (0.0, 0.0):
        return a
    return _outward(a[0] + b[0], a[1] + b[1])


def _interval_mul(a, b):
    if a == (0.0, 0.0) or b == (0.0, 0.0):
        return (0.0, 0.0)
    products = [x * y for x in a for y in b]
    if not all(math.isfinite(v) for v in products):
        raise ArithmeticError('non-finite affine interval')
    return _outward(min(products), max(products))


def _sum_products(values, coefficients):
    result = (0.0, 0.0)
    for value, coefficient in zip(values, coefficients):
        result = _interval_add(result, _interval_mul(value, coefficient))
    return result


def _affine_xy_bounds(left, bottom, right, top, elevation, ocs, matrix):
    """Enclose the affine image in XY only.

    Each float multiply/add is widened by one ULP, so a large Z ordinate cannot
    contribute rounding padding to X/Y. Nonzero Z->XY coefficients are kept.
    """
    result = [[], []]
    basis = [ocs.ux, ocs.uy, ocs.uz]
    for x, y in ((left, bottom), (left, top), (right, bottom), (right, top)):
        local = [(v, v) for v in (x, y, elevation)]
        wcs = [
            _sum_products(local, [(axis[j], axis[j]) for axis in basis])
            for j in range(3)
        ]
        for k in range(2):
            result[k].append(_interval_add(
                _sum_products(wcs, [(matrix[j, k], matrix[j, k]) for j in range(3)]),
                (matrix[3, k], matrix[3, k]),
            ))
    return [
        min(p[0] for p in result[0]),
        min(p[0] for p in result[1]),
        max(p[1] for p in result[0]),
        max(p[1] for p in result[1]),
    ]


def _loop_bounds(loops, elevation, ocs, matrix):
    points = []
    vertex_count = 0
    construction_scale = 1.0
    for vertices, closed in loops:
        if len(vertices) < 2:
            return None
        vertex_count += len(vertices)
        if vertex_count > 200000:
            return None
        if any(len(v) != 3 or not all(math.isfinite(n) for n in v) for v in vertices):
            return None
        points.extend((v[0], v[1]) for v in vertices)
        for start, end in zip(vertices, vertices[1:] + (vertices[:1] if closed else [])):
            x, y, bulge = start
            points.append((x, y))
            if bulge:
                if start[:2] == end[:2]:
                    return None
                center, arc_start, arc_end, radius = bulge_to_arc(start[:2], end[:2], bulge)
                if radius <= 0 or not all(math.isfinite(n) for n in
                        (center.x, center.y, radius, arc_start, arc_end)):
                    return None
                # ezdxf returns a counterclockwise arc, swapping endpoints for
                # negative bulges. A huge bulge may round to a complete turn;
                # using its limiting full circle then remains conservative.
                sweep = 4 * math.atan(abs(bulge))
                angular_padding = 64 * math.ulp(math.tau)
                for angle, dx, dy in [(0, 1, 0), (math.pi/2, 0, 1),
                                      (math.pi, -1, 0), (3*math.pi/2, 0, -1)]:
                    if (angle-arc_start) % math.tau <= sweep + angular_padding:
                        points.append((center.x + dx*radius, center.y + dy*radius))
                # Short arcs can have distant centres; keep their construction
                # scale in the outward rounding allowance even when that centre
                # is not part of the arc's much smaller geometric envelope.
                construction_scale = max(construction_scale, abs(center.x),
                                         abs(center.y), radius)
    if not points or not all(math.isfinite(n) for p in points for n in p):
        return None
    # Outward allowance covers finite arithmetic in the arc construction and
    # affine transform. It only enlarges containment, never measured geometry.
    padding = 64 * math.ulp(max(construction_scale,
                              *(abs(n) for p in points for n in p)))
    left = min(p[0] for p in points) - padding
    bottom = min(p[1] for p in points) - padding
    right = max(p[0] for p in points) + padding
    top = max(p[1] for p in points) + padding
    box = _affine_xy_bounds(left, bottom, right, top, elevation, ocs, matrix)
    return box if all(math.isfinite(n) for n in box) else None
