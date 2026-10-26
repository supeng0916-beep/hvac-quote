"""Conservative complete HATCH bounds, not length/area or pricing evidence.

Only closed polyline loops are supported. Every bulged edge is contained in
its full circle; every loop (including islands) contributes to the envelope.
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


def _loop_bounds(loops, elevation, ocs, matrix):
    points = []
    vertex_count = 0
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
                center, _, _, radius = bulge_to_arc(start[:2], end[:2], bulge)
                if radius <= 0 or not all(math.isfinite(n) for n in (center.x, center.y, radius)):
                    return None
                points.extend([(center.x-radius, center.y-radius),
                               (center.x+radius, center.y+radius)])
    if not points or not all(math.isfinite(n) for p in points for n in p):
        return None
    # Outward allowance covers finite arithmetic in the arc construction and
    # affine transform. It only enlarges containment, never measured geometry.
    padding = 64 * math.ulp(max(1.0, *(abs(n) for p in points for n in p)))
    left = min(p[0] for p in points) - padding
    bottom = min(p[1] for p in points) - padding
    right = max(p[0] for p in points) + padding
    top = max(p[1] for p in points) + padding
    corners = [matrix.transform(ocs.to_wcs((x, y, elevation)))
               for x, y in [(left, bottom), (left, top), (right, bottom), (right, top)]]
    if not all(math.isfinite(n) for p in corners for n in p):
        return None
    pad = 64 * math.ulp(max(1.0, *(abs(n) for p in corners for n in p)))
    box = [min(p.x for p in corners)-pad, min(p.y for p in corners)-pad,
           max(p.x for p in corners)+pad, max(p.y for p in corners)+pad]
    return box if all(math.isfinite(n) for n in box) else None
