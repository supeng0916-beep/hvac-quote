"""Generic geometric evidence, never a complete pipe takeoff or billing quantity."""

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, localcontext
import math
import re

from ezdxf.math import Vec3, arc_angle_span_deg, bulge_to_arc

from .evidence import issue


@dataclass(frozen=True)
class ConfirmedScale:
    file_version_id: str
    view: str
    metres_per_drawing_unit: str
    evidence_id: str

    def __post_init__(self):
        value = self.metres_per_drawing_unit
        # Fixed decimal transport, bounded precision; no float or scientific notation.
        if (
            not isinstance(value, str)
            or len(value) > 64
            or not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d+)?", value)
            or Decimal(value) <= 0
        ):
            raise ValueError("positive finite decimal scale required")
        if not all(
            isinstance(v, str) and v.strip()
            for v in (self.file_version_id, self.view, self.evidence_id)
        ):
            raise ValueError("confirmed scale requires file, view and evidence id")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("finite geometric number required")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("finite geometric number required")
    return value


def _point(value):
    if len(value) != 3:
        raise ValueError("3D coordinate required")
    return Vec3(*(_number(v) for v in value))


def _segment(start, end, bulge):
    chord = (end - start).magnitude
    if not bulge:
        return chord
    if not chord or start.z != end.z:
        raise ValueError("ambiguous bulge geometry")
    _, _, _, radius = bulge_to_arc(start, end, bulge)
    # 4*atan(|b|) is the full sweep, including arcs greater than a semicircle.
    return radius * 4 * math.atan(abs(bulge))


def _length(record):
    if not record.get("supported"):
        raise ValueError("reader did not support entity")
    kind, geometry = record["type"], record["geometry"]
    if kind == "LINE":
        return (_point(geometry["end"]) - _point(geometry["start"])).magnitude
    if kind in {"CIRCLE", "ARC"}:
        radius = _number(geometry["radius"])
        if radius < 0:
            raise ValueError("negative radius")
        sweep = (
            360
            if kind == "CIRCLE"
            else arc_angle_span_deg(
                _number(geometry["startAngle"]), _number(geometry["endAngle"])
            )
        )
        return radius * math.radians(sweep)
    if kind not in {"LWPOLYLINE", "POLYLINE"}:
        raise NotImplementedError(kind)
    vertices = geometry["vertices"]
    if len(vertices) < 2 or not isinstance(geometry["closed"], bool):
        raise ValueError("polyline requires vertices and explicit closure")
    if kind == "LWPOLYLINE":
        if any(len(v) != 3 for v in vertices):
            raise ValueError("xyb vertices required")
        points = [Vec3(_number(v[0]), _number(v[1]), 0) for v in vertices]
        bulges = [_number(v[2]) for v in vertices]
    else:
        if geometry.get("flags", 0) & (16 | 64):
            raise ValueError("mesh topology is unsupported")
        points = [_point(v["position"]) for v in vertices]
        bulges = [_number(v["bulge"]) for v in vertices]
        if geometry["is3d"] and any(bulges):
            raise ValueError("3D polylines cannot carry bulges")
    pairs = list(zip(points, points[1:]))
    if geometry["closed"]:
        pairs.append((points[-1], points[0]))
    return math.fsum(_segment(a, b, bulges[i]) for i, (a, b) in enumerate(pairs))


def _matches(blocker, source):
    refs = blocker.get("sources", [])
    # cad-read-1 samples at most 20 refs per issue code. At that boundary the
    # displayed refs cannot prove another handle is unaffected: apply file-wide.
    if len(refs) >= 20:
        return any(
            ref.get("fileVersionId") in {None, source.get("fileVersionId")}
            for ref in refs
        )
    return not refs or any(
        all(
            ref.get(k) is None or ref[k] == source.get(k)
            for k in ("fileVersionId", "view", "entityHandle")
        )
        for ref in refs
    )


def _decimal_string(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def measure(record, *, issues, scale=None):
    """Caller must pass upstream issues. Scope/reader blockers keep values unknown.

    Float geometry follows ezdxf; transport and scale multiplication use decimals.
    No project rounding, loss, vertical supplement or billing rule is applied.
    """
    source = deepcopy(record["source"])
    result = {
        "entityId": record["id"],
        "source": source,
        "drawingLength": None,
        "measuredLengthM": None,
        "lossLengthM": None,
        "billableLengthM": None,
        "scaleEvidenceId": None,
        "issues": [],
    }
    matched = [i for i in issues if _matches(i, source)]
    result["issues"] = deepcopy(matched)
    if any(i["severity"] == "blocks_pricing" for i in matched):
        return result
    try:
        length = _length(record)
        if not math.isfinite(length) or length < 0:
            raise ValueError("invalid geometric result")
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        ZeroDivisionError,
        NotImplementedError,
    ) as exc:
        result["issues"].append(
            issue(
                "GEOMETRY_UNRESOLVED", f"几何长度未确定：{type(exc).__name__}", [source]
            )
        )
        return result
    drawing_length = Decimal(str(length))
    result["drawingLength"] = _decimal_string(drawing_length)
    if scale is None or (scale.file_version_id, scale.view) != (
        source["fileVersionId"],
        source.get("view"),
    ):
        result["issues"].append(
            issue("SCALE_UNCONFIRMED", "缺少匹配文件和视图的尺度确认记录", [source])
        )
        return result
    with localcontext() as context:
        context.prec = 100
        result["measuredLengthM"] = _decimal_string(
            drawing_length * Decimal(scale.metres_per_drawing_unit)
        )
    result["scaleEvidenceId"] = scale.evidence_id
    return result
