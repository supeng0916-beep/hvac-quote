"""Occurrence-aware DXF normalization; definitions alone never become quantities."""

from dataclasses import dataclass, field
import hashlib
import math
import re
from collections import Counter
from ezdxf import bbox, transform
from ezdxf.math import Matrix44, OCS
from .hatch_bounds import hatch_bounds, lwpolyline_bounds
from . import TOOL_VERSION, REPLAY_READER_VERSIONS
from . import hatch_bounds_v014
from .preflight import (
    OPAQUE_MIN_CHILDREN,
    _layer_is_strong,
    _text_is_hvac,
    analyze_blocks,
)


class NormalizationLimitError(Exception):
    def __init__(self, code: str, detail: dict | None = None) -> None:
        self.code = code
        self.detail = detail or {}
        super().__init__(code)


SUPPORTED = {
    "LINE",
    "LWPOLYLINE",
    "POLYLINE",
    "ARC",
    "CIRCLE",
    "ELLIPSE",
    "SPLINE",
    "TEXT",
    "MTEXT",
    "ATTRIB",
    "POINT",
    "INSERT",
    "DIMENSION",
}
UNIT_MAP = {
    1: ("in", "0.0254"),
    2: ("ft", "0.3048"),
    4: ("mm", "0.001"),
    5: ("cm", "0.01"),
    6: ("m", "1"),
}


def dimension_measurement_kind(dimension_type):
    """Semantic type, never inferred from a positive value or display text."""
    if type(dimension_type) is not int:
        return "unknown"
    if dimension_type in (0, 1):
        return "linear"
    if dimension_type in (2, 5):
        return "angular"
    if dimension_type in (3, 4, 6):
        return "other"
    return "unknown"


def point(value):
    values = [float(x) for x in value]
    if len(values) == 2:
        values.append(0.0)
    if not all(math.isfinite(x) for x in values):
        raise ValueError("non-finite coordinate")
    return values


def extent(entity):
    box = bbox.extents([entity], fast=False)
    if not box.has_data:
        return None
    values = [box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y]
    return values if all(math.isfinite(x) for x in values) else None


def _render_color(entity, document, effective_layer):
    """Keep native color evidence; unresolved BYBLOCK never becomes an ACI.

    True color overrides ACI, so consumers cannot classify RGB entities from
    their legacy ACI fallback. Layer 0 has already inherited the INSERT layer.
    BYBLOCK requires the complete reference-color chain, not a layer lookup.
    """
    aci = entity.dxf.get("color", 256)
    true_color = entity.dxf.get("true_color")
    result = {
        "aciColor": aci,
        "aciSource": "byblock" if aci == 0 else "bylayer" if aci == 256 else "explicit",
        "trueColor": true_color,
        "effectiveAciColor": None,
        "effectiveTrueColor": None,
        "effectiveSource": "unresolved_byblock",
        "layer": effective_layer,
    }
    if true_color is not None:
        if type(true_color) is int and 0 <= true_color <= 0xFFFFFF:
            result.update(effectiveTrueColor=true_color, effectiveSource="entity_truecolor")
        else:
            result["effectiveSource"] = "invalid_color"
        return result
    if aci == 0:
        return result
    if aci == 256:
        if effective_layer not in document.layers:
            result["effectiveSource"] = "unresolved_layer"
            return result
        layer = document.layers.get(effective_layer)
        layer_true_color = layer.dxf.get("true_color")
        if layer_true_color is not None:
            if type(layer_true_color) is int and 0 <= layer_true_color <= 0xFFFFFF:
                result.update(effectiveTrueColor=layer_true_color, effectiveSource="layer_truecolor")
            else:
                result["effectiveSource"] = "invalid_color"
            return result
        layer_aci = layer.dxf.get("color", 7)
        # A negative layer ACI means the layer is off, not another palette color.
        if type(layer_aci) is int and 1 <= abs(layer_aci) <= 255:
            result.update(effectiveAciColor=abs(layer_aci), effectiveSource="layer_aci")
        else:
            result["effectiveSource"] = "invalid_color"
        return result
    if type(aci) is int and 1 <= aci <= 255:
        result.update(effectiveAciColor=aci, effectiveSource="entity_aci")
    else:
        result["effectiveSource"] = "invalid_color"
    return result


def _render_style(entity, document, effective_layer):
    """Keep native/effective line style; never use it as medium evidence alone."""
    native = entity.dxf.get("linetype", "BYLAYER")
    scale = _text_number(entity, "ltscale", 1, positive=True)
    effective = None
    label = str(native or "")
    if label.upper() == "BYLAYER":
        try:
            layer = document.layers.get(effective_layer)
            effective = layer.dxf.get("linetype")
        except Exception:  # noqa: BLE001
            effective = None
    elif label.upper() != "BYBLOCK":
        effective = native
    return {
        "lineTypeName": label,
        "effectiveLineTypeName": str(effective) if effective else None,
        "ltypeScale": scale,
    }


def _text_number(entity, name, default=None, positive=False):
    value = entity.dxf.get(name, default)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or (positive and value <= 0)):
        return None
    return float(value)


def _text_parameters(entity):
    # Height is mandatory source evidence. Do not expose ezdxf's default when
    # the original DXF omitted it; optional fields retain their DXF defaults.
    return {
        "height": _text_number(entity, "height", positive=True),
        "rotation": _text_number(entity, "rotation", 0),
        "style": entity.dxf.get("style", "Standard"),
        "widthFactor": _text_number(entity, "width", 1, positive=True),
        "halign": entity.dxf.get("halign", 0),
        "valign": entity.dxf.get("valign", 0),
    }


def _text_layout(entity, clone):
    if entity.dxftype() == "MTEXT":
        source_height = _text_number(entity, "char_height", positive=True)
        return {
            "charHeight": _text_number(clone, "char_height", positive=True)
            if source_height is not None else None,
            "width": _text_number(clone, "width", 0),
            "attachmentPoint": clone.dxf.get("attachment_point", 1),
            "textDirection": point(clone.get_text_direction()),
            "style": clone.dxf.get("style", "Standard"),
            "sourceTextParameters": {
                "charHeight": source_height,
                "width": _text_number(entity, "width", 0),
                "attachmentPoint": entity.dxf.get("attachment_point", 1),
                "textDirection": point(entity.get_text_direction()),
                "style": entity.dxf.get("style", "Standard"),
            },
        }
    source_parameters = _text_parameters(entity)
    parameters = _text_parameters(clone)
    if source_parameters["height"] is None:
        parameters["height"] = None
    return {**parameters, "sourceTextParameters": source_parameters}


def _union_bounds(boxes):
    if not boxes or any(box is None for box in boxes):
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _containment_bounds(entity):
    """Conservative WCS bounds for INSERT membership, never sampled extents.

    Full circles/ellipses contain their arcs. Positive-weight B-splines lie
    within their control hull; fit points alone do not bound a fitted curve.
    Text glyphs, complex wide polylines, thickness and proxies fail closed.
    """
    if entity.proxy_graphic or (entity.dxf.hasattr("thickness") and entity.dxf.thickness):
        return None
    kind = entity.dxftype()
    padding = 0.0
    if kind == "LINE":
        points = [entity.dxf.start, entity.dxf.end]
    elif kind == "POINT":
        points = [entity.dxf.location]
    elif kind == "LWPOLYLINE":
        if entity.has_arc:
            return None
        points = list(entity.vertices_in_wcs())
        if entity.has_width:
            width = float(entity.dxf.get("const_width", 0))
            if (len(points) != 2 or not math.isfinite(width) or width <= 0
                    or any(start or end for start, end in entity.get_points("se"))):
                return None
            # Two-point straight stroke, no miter joins. WCS padding by half
            # width contains the cross-section for every OCS orientation.
            padding = width / 2
    elif kind in {"CIRCLE", "ARC"}:
        center = entity.ocs().to_wcs(entity.dxf.center)
        radius = float(entity.dxf.radius)
        if not math.isfinite(radius) or radius <= 0:
            return None
        # A sphere bounds the full circle for any OCS orientation.
        points = [(center.x-radius, center.y-radius, center.z),
                  (center.x+radius, center.y+radius, center.z)]
    elif kind == "ELLIPSE":
        ellipse = entity.construction_tool()
        center, major, minor = ellipse.center, ellipse.major_axis, ellipse.minor_axis
        # Componentwise triangle inequality; also contains partial ellipses.
        dx, dy = abs(major.x)+abs(minor.x), abs(major.y)+abs(minor.y)
        points = [(center.x-dx, center.y-dy, center.z),
                  (center.x+dx, center.y+dy, center.z)]
    elif kind == "SPLINE":
        spline = entity.construction_tool()
        weights = list(spline.weights())
        knots = list(spline.knots())
        if (any(not math.isfinite(w) or w <= 0 for w in weights)
                or any(not math.isfinite(k) for k in knots)
                or any(a > b for a, b in zip(knots, knots[1:]))):
            return None
        points = list(spline.control_points)
    else:
        return None
    coords = [point(p) for p in points]
    if not coords:
        return None
    values = [min(p[0] for p in coords)-padding, min(p[1] for p in coords)-padding,
              max(p[0] for p in coords)+padding, max(p[1] for p in coords)+padding]
    return values if all(math.isfinite(v) for v in values) else None


@dataclass
class DrawingRead:
    document: object
    file_version_id: str
    source_hash: str
    units: dict
    entities: list = field(default_factory=list)
    views: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    dimensions: list = field(default_factory=list)
    floor_candidates: list = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)
    blocked_root_handles: set = field(default_factory=set)
    reader_version: str = TOOL_VERSION

    def issue(self, code, message, source=None, severity="blocks_pricing"):
        # Bound repeated issues while preserving the exact total in internal diagnostics.
        counts = self.diagnostics.setdefault("issueCounts", {})
        counts[code] = counts.get(code, 0) + 1
        prior = next((i for i in self.issues if i["code"] == code), None)
        if prior:
            if source and len(prior["sources"]) < 20 and source not in prior["sources"]:
                prior["sources"].append(source)
            return
        self.issues.append(
            {
                "id": f"cad-{self.file_version_id[:80]}-{len(self.issues) + 1}",
                "code": code,
                "message": message,
                "affectedItemIds": [],
                "severity": severity,
                "sources": [source] if source else [],
            }
        )

    def report(self):
        bad = []

        def clean(value, location=""):
            if isinstance(value, str):
                if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
                    if len(bad) < 20:
                        bad.append(
                            {"location": location, "escaped": ascii(value)[:500]}
                        )
                    return "".join(
                        "�" if 0xD800 <= ord(c) <= 0xDFFF else c for c in value
                    )
                return value
            if isinstance(value, list):
                return [clean(v, f"{location}[{i}]") for i, v in enumerate(value)]
            if isinstance(value, dict):
                return {
                    clean(k, location): clean(v, f"{location}.{k}")
                    for k, v in value.items()
                }
            return value

        for field_name in [
            "entities",
            "views",
            "dimensions",
            "floor_candidates",
            "issues",
            "diagnostics",
        ]:
            setattr(self, field_name, clean(getattr(self, field_name), field_name))
        if bad:
            self.diagnostics["invalidTextSequences"] = bad
            self.issue(
                "TEXT_ENCODING_REPLACED",
                "部分原始文字编码无法解码，已标记替代字符，不能作为型号依据",
                {"fileVersionId": self.file_version_id},
            )
        return {
            "schemaVersion": "cad-read-2",
            "toolVersion": self.reader_version,
            "fileVersionId": self.file_version_id,
            "sourceHash": self.source_hash,
            "units": self.units,
            "views": self.views,
            "entities": self.entities,
            "dimensions": self.dimensions,
            "floorCandidates": self.floor_candidates,
            "issues": self.issues,
            "diagnostics": self.diagnostics,
        }


def normalize(
    document,
    file_version_id,
    source_hash,
    max_entities=200000,
    *,
    reader_version=TOOL_VERSION,
    scope=None,
):
    if reader_version not in REPLAY_READER_VERSIONS:
        raise ValueError('READER_VERSION_UNSUPPORTED')
    scope = scope or {}
    selected_views = {
        str(item) for item in (scope.get("views") or []) if str(item).strip()
    }
    collapse_context = scope.get("collapseContextBlocks", True) is not False
    # Provenance is needed even for a full expansion: callers must not be able
    # to bypass source-semantic gates by turning folding off.
    block_analysis = analyze_blocks(document)
    code = int(document.header.get("$INSUNITS", 0))
    unit, factor = UNIT_MAP.get(code, (None, None))
    result = DrawingRead(
        document,
        file_version_id,
        source_hash,
        {
            "headerCode": code,
            "measurementUnit": unit,
            "metresPerDrawingUnit": factor,
            "verifiedByDimensions": False,
        },
        reader_version=reader_version,
    )
    result.issue(
        "SCALE_UNCONFIRMED" if unit is None else "SCALE_NOT_VERIFIED",
        "图纸单位未知，不能换算工程量"
        if unit is None
        else "单位仅来自图纸头信息，尚未完成尺度独立核对",
        {"fileVersionId": file_version_id},
        "blocks_pricing" if unit is None else "review",
    )
    for space in document.layouts:
        if not len(space):
            continue
        view = "model" if space.name == "Model" else space.name
        if selected_views and view not in selected_views:
            continue
        result.views.append({"view": view, "floors": []})
        result.issue(
            "FLOOR_SCOPE_UNRESOLVED",
            "尚无已确认的楼层与图面区域绑定；可预览，不可按楼层核量",
            {"fileVersionId": file_version_id, "view": view},
        )
        result.diagnostics.setdefault("rootTypes", {})[view] = dict(
            Counter(e.dxftype() for e in space)
        )
        for entity in space:
            _walk(
                entity,
                Matrix44(),
                entity.dxf.handle,
                [],
                [],
                view,
                result,
                max_entities,
                "0",
                block_analysis=block_analysis,
                collapse_context=collapse_context,
            )
    result.diagnostics["scope"] = {
        "views": sorted(selected_views) if selected_views else "all",
        "collapseContextBlocks": collapse_context,
        "opaqueThreshold": OPAQUE_MIN_CHILDREN,
    }
    if not result.views:
        result.issue(
            "SCOPE_EMPTY",
            "所选范围没有可读取的布局，请重新选择范围。",
            {"fileVersionId": file_version_id},
            "blocks_pricing",
        )
    return result


def _walk(
    entity,
    matrix,
    root_handle,
    trail,
    blocks,
    view,
    result,
    limit,
    parent_layer,
    *,
    block_analysis=None,
    parent_owner=None,
    expansion_contexts=None,
    collapse_context=True,
):
    if len(result.entities) >= limit:
        raise NormalizationLimitError(
            "ENTITY_LIMIT_EXCEEDED",
            {
                "limit": limit,
                "entities": len(result.entities),
                "view": view,
                "scope": result.diagnostics.get("scope"),
            },
        )
    kind = entity.dxftype()
    handle = entity.dxf.get("handle") or "virtual"
    original_layer = str(entity.dxf.get("layer", "0"))
    layer = parent_layer if original_layer == "0" else original_layer
    path = [*trail, handle]
    layer_owner_path = (
        list(path) if original_layer != "0" else list(parent_owner or [])
    )
    inherited_contexts = [
        {**item, "instancePath": list(item.get("instancePath") or [])}
        for item in (expansion_contexts or [])
    ]
    source = {
        "fileVersionId": result.file_version_id,
        "entityHandle": root_handle,
        "view": view,
        "originalLayer": original_layer,
        "layerInherited": original_layer == "0" and bool(trail),
        "layerOwnerPath": layer_owner_path,
        "expansionContexts": inherited_contexts,
    }
    record = {
        "id": hashlib.sha256(
            f"{view}:{root_handle}:{'/'.join(path)}".encode()
        ).hexdigest()[:24],
        "type": kind,
        "definitionHandle": handle,
        "instancePath": path,
        "layer": layer,
        "source": source,
        "supported": kind in SUPPORTED,
        "geometry": {"renderColor": _render_color(entity, result.document, layer)},
    }
    if kind == "INSERT":
        name = entity.dxf.name
        record["geometry"] = {
            "blockName": name,
            "insert": point(matrix.transform(entity.dxf.insert)),
            "renderColor": record["geometry"]["renderColor"],
        }
        record["attributes"] = [
            {"tag": a.dxf.tag, "text": a.dxf.text} for a in entity.attribs
        ]
        record["xdata"] = {
            app: [
                {"code": t.code, "value": str(t.value)} for t in tags if t.code == 1000
            ]
            for app, tags in (entity.xdata.data.items() if entity.xdata else [])
        }
        result.entities.append(record)
        block = entity.block()
        if block is None:
            result.blocked_root_handles.add(root_handle)
            result.issue("BLOCK_MISSING", "图块定义缺失", source)
            return
        if block.block.is_xref or block.block.is_xref_overlay:
            result.blocked_root_handles.add(root_handle)
            result.issue("XREF_UNRESOLVED", "存在外部参照；未自动读取外部路径", source)
            return
        if name in blocks or len(blocks) >= 32:
            result.blocked_root_handles.add(root_handle)
            result.issue(
                "BLOCK_RECURSION", "图块循环引用或嵌套过深，已停止展开", source
            )
            return
        stats = (block_analysis or {}).get(name)
        would_collapse = bool(
            stats is not None
            and stats.count >= OPAQUE_MIN_CHILDREN
            and not stats.hvac
        )
        protected_by = []
        if _layer_is_strong(layer):
            protected_by.append("layer")
        if _text_is_hvac(name):
            protected_by.append("block_name")
        protected_instance = bool(protected_by)
        child_contexts = list(inherited_contexts)
        if would_collapse and protected_instance:
            child_contexts.append(
                {
                    "instancePath": list(path),
                    "blockName": name,
                    "childCount": stats.count,
                    "protectedBy": list(protected_by),
                    "reason": "opaque_definition_protected_for_expansion",
                }
            )
        if collapse_context and would_collapse and not protected_instance:
            record["geometry"]["opaque"] = True
            record["geometry"]["contextOnly"] = True
            record["geometry"]["childCount"] = stats.count
            opaque = result.diagnostics.setdefault("opaqueBlocks", [])
            opaque.append(
                {
                    "eventId": record["id"],
                    "fileVersionId": result.file_version_id,
                    "definitionHandle": handle,
                    "instancePath": list(path),
                    "reason": "opaque_definition_no_hvac",
                    "representedInsertCount": entity.mcount,
                    "blockName": name,
                    "childCount": stats.count,
                    "view": view,
                    "rootHandle": root_handle,
                }
            )
            result.issue(
                "CONTEXT_BLOCK_COLLAPSED",
                f"图块“{name}”展开约 {stats.count} 个子图元，未发现暖通/管线证据，"
                "已按上下文字块折叠；如需逐图元检查请缩小范围或联系开发。",
                {
                    "fileVersionId": result.file_version_id,
                    "view": view,
                    "eventId": record["id"],
                    "instancePath": list(path),
                },
                "review",
            )
            # 中小型折叠块仍计算外框：匿名设备候选/楼层区域需要位置证据。
            # 超大建筑块跳过，避免为了 bbox 再遍历数万子图元。
            if stats.count <= 6000:
                try:
                    box = bbox.extents([entity])
                    if box.has_data:
                        record["source"]["bbox"] = [
                            box.extmin.x,
                            box.extmin.y,
                            box.extmax.x,
                            box.extmax.y,
                        ]
                except Exception:  # noqa: BLE001
                    pass
            return None
        if entity.mcount > limit - len(result.entities):
            raise NormalizationLimitError(
                "ENTITY_LIMIT_EXCEEDED",
                {
                    "limit": limit,
                    "entities": len(result.entities),
                    "view": view,
                    "blockName": name,
                    "scope": result.diagnostics.get("scope"),
                },
            )
        inserts = entity.multi_insert() if entity.mcount > 1 else [entity]
        child_bounds = []
        for idx, insert in enumerate(inserts):
            child_matrix = insert.matrix44() @ matrix
            instance_trail = [*path, f"instance-{idx}"]
            for child in block:
                if child.dxftype() != "ATTDEF":
                    child_bounds.append(_walk(
                        child,
                        child_matrix,
                        root_handle,
                        instance_trail,
                        [*blocks, name],
                        view,
                        result,
                        limit,
                        layer,
                        block_analysis=block_analysis,
                        parent_owner=layer_owner_path,
                        expansion_contexts=child_contexts,
                        collapse_context=collapse_context,
                    ))
                else:
                    # A skipped ATTDEF may contain visible default text.
                    child_bounds.append(None)
            # ATTRIB coordinates already live in the INSERT's parent space.
            for attr in insert.attribs:
                child_bounds.append(_walk(
                    attr,
                    matrix,
                    root_handle,
                    instance_trail,
                    blocks,
                    view,
                    result,
                    limit,
                    layer,
                    block_analysis=block_analysis,
                    parent_owner=layer_owner_path,
                    expansion_contexts=child_contexts,
                    collapse_context=collapse_context,
                ))
        box = None if entity.proxy_graphic else _union_bounds(child_bounds)
        if box is not None:
            record["source"]["bbox"] = box
        return box
    if kind == "IMAGE":
        result.issue(
            "IMAGE_NOT_LOADED", "存在外部图片；只读预检不加载外部图片内容", source
        )
    if kind == "ACAD_PROXY_ENTITY" or entity.proxy_graphic:
        result.issue("PROXY_OBJECT", "存在代理图形，不能证明已读取其业务含义", source)
    if kind not in SUPPORTED and kind not in {"HATCH", "IMAGE", "ATTDEF", "VIEWPORT"}:
        result.issue("UNSUPPORTED_ENTITY", f"未支持的实体类型：{kind}", source)
    try:
        # ezdxf Dimension.transform can swallow geometry-block transform errors.
        # Reject non-uniform scaling in the dimension plane before it can yield
        # a plausible number from incompletely transformed geometry.
        if kind == "DIMENSION":
            ocs = OCS(entity.dxf.extrusion)
            x_axis, y_axis = matrix.transform_directions((ocs.ux, ocs.uy))
            x_length, y_length = math.hypot(*x_axis), math.hypot(*y_axis)
            valid_lengths = all(
                math.isfinite(length) and length > 0
                for length in (x_length, y_length)
            )
            # Relative comparison only: absolute tolerance on squared lengths
            # can accept a 2:1 distortion when both axes are very small.
            uniform = valid_lengths and math.isclose(
                x_length, y_length, rel_tol=1e-9, abs_tol=0.0
            )
            # Equal axis lengths alone miss shear from nested scaled INSERTs.
            # These are numerical transform checks, not scale adoption tolerances.
            orthogonal = uniform and math.isclose(
                (x_axis / x_length).dot(y_axis / y_length), 0.0, abs_tol=1e-9
            )
            if not uniform or not orthogonal:
                record["supported"] = False
                result.entities.append(record)
                result.issue("TRANSFORM_INCOMPLETE", "尺寸标注非均匀变换无法完整核对", source)
                return
        # Complete original HATCH loops are bounded before affine transforms.
        # Transformed bulges may become EdgePaths; never bound only their vertices.
        curved_polyline = kind == 'LWPOLYLINE' and entity.has_arc
        bound_hatch = hatch_bounds_v014.hatch_bounds if result.reader_version == 'hvac-cad-0.1.4' else hatch_bounds
        bound_polyline = hatch_bounds_v014.lwpolyline_bounds if result.reader_version == 'hvac-cad-0.1.4' else lwpolyline_bounds
        boundary_box = (bound_hatch(entity, matrix) if kind == 'HATCH' else
                        bound_polyline(entity, matrix) if curved_polyline else None)
        log, clones = transform.copies([entity], matrix)
        if len(log) or not clones:
            result.issue("TRANSFORM_INCOMPLETE", "实体变换不完整，请核对源图", source)
        # A failed DIMENSION transform cannot establish WCS length semantics.
        if kind == "DIMENSION" and (len(log) or not clones):
            record["supported"] = False
            result.entities.append(record)
            return
        if not clones:
            result.entities.append(record)
            return
        trusted_boxes = []
        for idx, clone in enumerate(clones):
            if len(result.entities) >= limit:
                raise NormalizationLimitError("ENTITY_LIMIT_EXCEEDED")
            data = {
                **record,
                "source": dict(source),
                "geometry": {},
                "type": clone.dxftype(),
            }
            if idx:
                data["id"] += f"-{idx}"
            if boundary_box is not None:
                box = boundary_box
                if kind == "HATCH" or curved_polyline:
                    data["source"]["bboxMethod"] = "affine_xy_interval"
            elif kind == "HATCH" or curved_polyline:
                # A failed conservative bound must not be replaced by ezdxf's
                # sampled extent and then presented as trusted geometry.
                box = None
                result.issue(
                    "GEOMETRY_BOUNDS_UNAVAILABLE",
                    "填充或曲线多段线的二维外框无法可靠计算，未写入可信外框；"
                    "几何仍保留供人工核对。",
                    {
                        "fileVersionId": result.file_version_id,
                        "view": view,
                        "entityId": data["id"],
                        "instancePath": list(data["instancePath"]),
                    },
                    "review",
                )
            else:
                box = extent(clone)
            if box:
                data["source"]["bbox"] = box
            t = clone.dxftype()
            if t == "LINE":
                data["geometry"] = {
                    "start": point(clone.dxf.start),
                    "end": point(clone.dxf.end),
                }
            elif t in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
                text = clone.plain_text() if t == "MTEXT" else clone.dxf.text
                data["geometry"] = {"text": text, "insert": point(clone.dxf.insert),
                                    **_text_layout(entity, clone)}
                if re.search(
                    r"(?:\d+\s*[F层]|[一二三四五六七八九十首地下负]+层).*(?:平面|空调|管路|风管)",
                    text,
                    re.I,
                ):
                    result.floor_candidates.append(
                        {"text": text, "source": dict(data["source"])}
                    )
            elif t in {"CIRCLE", "ARC"}:
                data["geometry"] = {
                    "center": point(clone.dxf.center),
                    "radius": clone.dxf.radius,
                    "coordinateSystem": "OCS",
                    "extrusion": point(clone.dxf.extrusion),
                }
                if t == "ARC":
                    data["geometry"].update(
                        startAngle=clone.dxf.start_angle, endAngle=clone.dxf.end_angle
                    )
            elif t == "ELLIPSE":
                data["geometry"] = {
                    "center": point(clone.dxf.center),
                    "majorAxis": point(clone.dxf.major_axis),
                    "ratio": clone.dxf.ratio,
                    "startParam": clone.dxf.start_param,
                    "endParam": clone.dxf.end_param,
                }
            elif t == "LWPOLYLINE":
                data["geometry"] = {
                    "coordinateSystem": "OCS",
                    "vertices": [list(p) for p in clone.get_points("xyb")],
                    "closed": clone.closed,
                    "elevation": clone.dxf.elevation,
                    "extrusion": point(clone.dxf.extrusion),
                }
            elif t == "POLYLINE":
                data["geometry"] = {
                    "vertices": [
                        {
                            "position": point(v.dxf.location),
                            "bulge": v.dxf.get("bulge", 0),
                            "startWidth": v.dxf.get("start_width", 0),
                            "endWidth": v.dxf.get("end_width", 0),
                        }
                        for v in clone.vertices
                    ],
                    "closed": clone.is_closed,
                    "is3d": clone.is_3d_polyline,
                    "coordinateSystem": "WCS" if clone.is_3d_polyline else "OCS",
                    "extrusion": point(clone.dxf.extrusion),
                    "flags": clone.dxf.flags,
                }
                if clone.is_polygon_mesh or clone.is_poly_face_mesh:
                    data["supported"] = False
                    result.issue(
                        "UNSUPPORTED_ENTITY", "POLYLINE网格拓扑尚未规范化", source
                    )
            elif t == "SPLINE":
                data["geometry"] = {
                    "degree": clone.dxf.degree,
                    "controlPoints": [point(p) for p in clone.control_points],
                    "fitPoints": [point(p) for p in clone.fit_points],
                    "knots": list(clone.knots),
                    "weights": list(clone.weights),
                    "flags": clone.dxf.flags,
                }
                for attr in ["start_tangent", "end_tangent"]:
                    if clone.dxf.hasattr(attr):
                        data["geometry"][attr] = point(clone.dxf.get(attr))
            elif t == "POINT":
                data["geometry"] = {"location": point(clone.dxf.location)}
            elif t == "DIMENSION":
                measurement = clone.get_measurement()
                if isinstance(measurement, (int, float)) and math.isfinite(measurement):
                    label = clone.dxf.get("text", "<>")
                    evidence = {
                        "source": dict(source),
                        "measuredDrawingUnits": float(measurement),
                        "overrideText": label,
                        "dimensionType": clone.dimtype,
                        "measurementKind": dimension_measurement_kind(clone.dimtype),
                    }
                    result.dimensions.append(evidence)
                    if re.fullmatch(r"\d+(?:\.\d+)?", label) and not math.isclose(
                        float(label), measurement, rel_tol=0.001, abs_tol=0.001
                    ):
                        result.issue(
                            "DIMENSION_SCALE_CONFLICT",
                            "尺寸覆写文字与图面测量不一致，不能据此认定比例",
                            source,
                        )
            data["geometry"]["renderColor"] = record["geometry"]["renderColor"]
            data["geometry"]["renderStyle"] = _render_style(
                entity, result.document, layer
            )
            result.entities.append(data)
            try:
                trusted_boxes.append(_containment_bounds(clone) if trail else None)
            except (ValueError, TypeError, AttributeError, ArithmeticError):
                # Bounds failure must not alter the existing leaf record.
                trusted_boxes.append(None)
        if (not len(log) and not entity.proxy_graphic
                and not (entity.dxf.hasattr("thickness") and entity.dxf.thickness)):
            return boundary_box if kind == 'HATCH' or curved_polyline else _union_bounds(trusted_boxes)
    except (
        ValueError,
        TypeError,
        AttributeError,
        ZeroDivisionError,
        ArithmeticError,
    ) as exc:
        record["supported"] = False
        result.entities.append(record)
        result.issue(
            "ENTITY_READ_INCOMPLETE",
            f"实体{kind}读取不完整：{type(exc).__name__}",
            source,
        )
