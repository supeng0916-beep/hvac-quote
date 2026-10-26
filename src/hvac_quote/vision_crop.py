"""把 CAD 局部范围渲染成 PNG，并给出像素↔CAD 坐标映射。

这是供视觉模型使用的近似渲染（线、弧、圆、文字、候选框），不是 AutoCAD
原生保真截图；渲染结果只作视觉建议的输入，长度和数量仍由确定性程序计算。
"""

from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass, field
from pathlib import Path

from ezdxf.colors import aci2rgb
from PIL import Image, ImageDraw, ImageFont

from hvac_cad.reader import read_drawing, read_preflight

from .vision import canonical_json

TEXT_TYPES = {"TEXT", "MTEXT", "ATTRIB"}
LINE_TYPES = {"LINE"}
POLY_TYPES = {"LWPOLYLINE", "POLYLINE"}
CURVE_TYPES = {"ARC", "CIRCLE"}
FONT_CANDIDATES = (
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
)


class CropError(ValueError):
    pass


@dataclass
class CropResult:
    png: bytes
    mapping: dict
    entityCount: int
    bbox: list[float]
    truncated: bool = False
    skipped: int = 0
    extra: dict = field(default_factory=dict)


def _bbox(entity: dict) -> list[float] | None:
    source = entity.get("source") or {}
    bbox = source.get("bbox")
    if bbox and len(bbox) == 4:
        return [float(value) for value in bbox]
    geometry = entity.get("geometry") or {}
    points = []
    if geometry.get("start") and geometry.get("end"):
        points = [geometry["start"], geometry["end"]]
    elif geometry.get("vertices"):
        points = geometry["vertices"]
    if not points:
        return None
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def _intersects(first: list[float], second: list[float]) -> bool:
    return not (
        first[2] < second[0]
        or first[0] > second[2]
        or first[3] < second[1]
        or first[1] > second[3]
    )


def _xy(value) -> tuple[float, float]:
    if isinstance(value, dict):
        return float(value.get("x", 0)), float(value.get("y", 0))
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return float(value[0]), float(value[1])
    return 0.0, 0.0


UNKNOWN_COLOR = (96, 96, 96)


def _valid_true_color(value) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 0 <= value <= 0xFFFFFF
    )


def _valid_aci(value) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 1 <= value <= 255
    )


def _color_source(entity: dict) -> str:
    render = (entity.get("geometry") or {}).get("renderColor") or {}
    if not isinstance(render, dict):
        return "unknown"
    if _valid_true_color(render.get("effectiveTrueColor")):
        return "effective_true_color"
    if _valid_aci(render.get("effectiveAciColor")):
        return "effective_aci"
    if _valid_true_color(render.get("trueColor")):
        return "raw_true_color"
    if _valid_aci(render.get("aciColor")):
        return "raw_aci"
    return "unknown"


def _color(entity: dict, index: int = 0) -> tuple[int, int, int]:
    """Use inherited true color/ACI; unknown stays neutral, never index-rotated."""
    render = (entity.get("geometry") or {}).get("renderColor") or {}
    if not isinstance(render, dict):
        return UNKNOWN_COLOR
    if _valid_true_color(render.get("effectiveTrueColor")):
        value = render["effectiveTrueColor"]
        return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)
    if _valid_aci(render.get("effectiveAciColor")):
        value = int(render["effectiveAciColor"])
        if value == 7:
            return (0, 0, 0)
        rgb = aci2rgb(value)
        if rgb is not None:
            return tuple(int(channel) for channel in rgb)
    if _valid_true_color(render.get("trueColor")):
        value = render["trueColor"]
        return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)
    if _valid_aci(render.get("aciColor")):
        value = int(render["aciColor"])
        if value == 7:
            return (0, 0, 0)
        rgb = aci2rgb(value)
        if rgb is not None:
            return tuple(int(channel) for channel in rgb)
    return UNKNOWN_COLOR


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_CANDIDATES:
        try:
            if Path(path).is_file():
                return ImageFont.truetype(path, max(8, size))
        except OSError:
            continue
    return ImageFont.load_default()


def _arc_points(geometry: dict, steps: int = 24) -> list[tuple[float, float]]:
    cx, cy = _xy(geometry.get("center"))
    radius = float(geometry.get("radius") or 0)
    start = float(geometry.get("startAngle") or 0)
    end = float(geometry.get("endAngle") or 0)
    sweep = (end - start) % 360 or 360
    return [
        (
            cx + radius * math.cos(math.radians(start + sweep * step / steps)),
            cy + radius * math.sin(math.radians(start + sweep * step / steps)),
        )
        for step in range(steps + 1)
    ]


def render_crop(
    drawing_path: Path,
    file_id: str,
    workdir: Path,
    *,
    bbox: list[float] | None = None,
    handles: set[str] | None = None,
    candidates: list[dict] | None = None,
    padding: float = 0.1,
    max_size: int = 1200,
    max_entities: int = 30000,
    expand_blocks: bool = True,
) -> CropResult:
    read = None
    block_expansion = "collapsed"
    if expand_blocks:
        try:
            preflight = read_preflight(
                Path(drawing_path),
                file_id,
                Path(workdir) / "preflight",
                max_entities=300000,
            )
            if int(preflight.get("totalEstimated") or 0) <= 300000:
                read = read_drawing(
                    Path(drawing_path),
                    file_id,
                    Path(workdir) / "expanded",
                    max_entities=500000,
                    scope={"collapseContextBlocks": False},
                )
                block_expansion = "full"
        except Exception:  # noqa: BLE001 - 大图/转换失败时回退折叠读取
            read = None
    if read is None:
        read = read_drawing(Path(drawing_path), file_id, Path(workdir))
        block_expansion = "collapsed"
    report = read.report()
    entities = report.get("entities") or []
    selected = []
    for entity in entities:
        handle = str((entity.get("source") or {}).get("entityHandle") or "")
        if handles and handle not in handles:
            continue
        box = _bbox(entity)
        if box is None:
            continue
        if bbox is not None and not _intersects(box, bbox):
            continue
        if bbox is None and handles is None:
            # 无范围时取全部，但仍按实体预算截断并明确标记。
            pass
        selected.append((entity, box))
    if not selected:
        raise CropError("所选范围内没有可渲染图元")
    truncated = len(selected) > max_entities
    skipped = max(0, len(selected) - max_entities)
    if truncated:
        selected = selected[:max_entities]
    x0 = min(box[0] for _, box in selected)
    y0 = min(box[1] for _, box in selected)
    x1 = max(box[2] for _, box in selected)
    y1 = max(box[3] for _, box in selected)
    if bbox is not None:
        x0, y0, x1, y1 = bbox
    width = max(x1 - x0, 1.0)
    height = max(y1 - y0, 1.0)
    pad = max(width, height) * max(0.0, padding)
    x0 -= pad
    y0 -= pad
    x1 += pad
    y1 += pad
    width = x1 - x0
    height = y1 - y0
    scale = min(max_size / width, max_size / height)
    image_width = max(320, int(width * scale) + 1)
    image_height = max(240, int(height * scale) + 1)
    image = Image.new("RGB", (image_width, image_height), "white")
    draw = ImageDraw.Draw(image)

    def to_pixel(x: float, y: float) -> tuple[float, float]:
        return ((x - x0) * scale, (y1 - y) * scale)

    font = _font(max(9, int(10 * max(0.6, min(scale * 12, 2.0)))))
    drawn = 0
    unknown_color_entities = 0
    for index, (entity, box) in enumerate(selected):
        kind = entity.get("type")
        geometry = entity.get("geometry") or {}
        color = _color(entity, index)
        if _color_source(entity) == "unknown":
            unknown_color_entities += 1
        if kind in LINE_TYPES:
            start = _xy(geometry.get("start"))
            end = _xy(geometry.get("end"))
            draw.line(
                [
                    to_pixel(*start),
                    to_pixel(*end),
                ],
                fill=color,
                width=1,
            )
            drawn += 1
        elif kind in POLY_TYPES:
            points = [
                to_pixel(*_xy(point))
                for point in geometry.get("vertices") or []
            ]
            if len(points) >= 2:
                draw.line(points, fill=color, width=1)
                drawn += 1
        elif kind in CURVE_TYPES:
            points = [to_pixel(x, y) for x, y in _arc_points(geometry)]
            if len(points) >= 2:
                draw.line(points, fill=color, width=1)
                drawn += 1
        elif kind == "INSERT":
            left, top = to_pixel(float(box[0]), float(box[3]))
            right, bottom = to_pixel(float(box[2]), float(box[1]))
            draw.rectangle(
                [left, top, right, bottom],
                outline=(70, 70, 70),
                width=1,
            )
            drawn += 1
        elif kind in TEXT_TYPES:
            raw = geometry.get("text") or geometry.get("characters") or ""
            if raw:
                if geometry.get("insert"):
                    px, py = to_pixel(*_xy(geometry.get("insert")))
                else:
                    center = (
                        (box[0] + box[2]) / 2,
                        (box[1] + box[3]) / 2,
                    )
                    px, py = to_pixel(*center)
                draw.text((px, py), str(raw)[:60], fill=(30, 30, 30), font=font)
                drawn += 1
    ordered_candidates = sorted(
        candidates or [],
        key=lambda item: str(item.get("candidateId") or ""),
    )
    seen_candidate_ids: set[str] = set()
    for item in ordered_candidates:
        candidate_id = str(item.get("candidateId") or "")
        box = item.get("bbox")
        if (
            not candidate_id
            or candidate_id in seen_candidate_ids
            or not isinstance(box, (list, tuple))
            or len(box) != 4
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in box
            )
        ):
            raise CropError("候选框或候选 ID 无效")
        if float(box[0]) > float(box[2]) or float(box[1]) > float(box[3]):
            raise CropError("候选框边界倒置")
        seen_candidate_ids.add(candidate_id)
        left, top = to_pixel(float(box[0]), float(box[3]))
        right, bottom = to_pixel(float(box[2]), float(box[1]))
        draw.rectangle(
            [left, top, right, bottom],
            outline=(200, 40, 40),
            width=2,
        )
        label = str(item.get("candidateId") or "")
        if label:
            draw.text((left, max(0, top - 14)), label[:40], fill=(160, 20, 20), font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    png = buffer.getvalue()
    png_sha256 = hashlib.sha256(png).hexdigest()
    source_hash = str(report.get("sourceHash") or "")
    if not source_hash:
        raise CropError("缺少原图内容哈希，不能建立裁图身份")
    manifest = {
        "schemaVersion": "vision-crop-2",
        "renderer": "approximate-linework-v2",
        "sourceHash": source_hash,
        "fileId": file_id,
        "cadBBox": [x0, y0, x1, y1],
        "width": image_width,
        "height": image_height,
        "scale": scale,
        "handles": sorted(str(value) for value in (handles or set())),
        "maxSize": max_size,
        "maxEntities": max_entities,
        "padding": padding,
        "requestedExpandBlocks": bool(expand_blocks),
        "blockExpansion": block_expansion,
        "candidateOverlays": [
            {
                "candidateId": str(item.get("candidateId") or ""),
                "bbox": list(item.get("bbox") or []),
            }
            for item in ordered_candidates
        ],
        "truncated": truncated,
        "skippedEntities": skipped,
        "drawnEntities": drawn,
        "colorPolicy": "ezdxf-aci-white-background-v1",
        "pngSha256": png_sha256,
    }
    crop_id = hashlib.sha256(
        canonical_json(manifest).encode("utf-8")
    ).hexdigest()
    mapping = {
        "cropId": crop_id,
        "fileId": file_id,
        "sourceHash": source_hash,
        "cadBBox": [x0, y0, x1, y1],
        "width": image_width,
        "height": image_height,
        "scale": scale,
        "pixelToCad": (
            f"x = {x0} + px / {scale}; y = {y1} - py / {scale}"
        ),
        "drawnEntities": drawn,
        "selectedEntities": len(selected),
        "skippedEntities": skipped,
        "truncated": truncated,
        "renderer": "approximate-linework-v2",
        "blockExpansion": block_expansion,
        "collapsedBlockCount": len(
            (report.get("diagnostics") or {}).get("opaqueBlocks") or []
        ),
        "manifest": manifest,
        "pngSha256": png_sha256,
        "colorPolicy": "ezdxf-aci-white-background-v1",
        "unknownColorEntities": unknown_color_entities,
    }
    return CropResult(
        png=png,
        mapping=mapping,
        entityCount=drawn,
        bbox=[x0, y0, x1, y1],
        truncated=truncated,
        skipped=skipped,
    )


__all__ = ["CropError", "CropResult", "render_crop"]
