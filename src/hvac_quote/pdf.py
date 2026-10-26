"""客户报价 PDF 渲染。

Excel 由 LibreOffice 重算；PDF 用 ReportLab 从同一份已审核数据渲染，
避免 LibreOffice 在 macOS 上把中文回退成无中文字形的字体。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

_FONT_NAME = "QuoteCJK"
_FONT_PATH: str | None = None

_FONT_CANDIDATES = (
    ("/System/Library/Fonts/Supplemental/Songti.ttc", 0),
    ("/Library/Fonts/Arial Unicode.ttf", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc", 0),
    ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", 0),
    ("C:/Windows/Fonts/simsun.ttc", 0),
    ("C:/Windows/Fonts/msyh.ttc", 0),
)


@dataclass(frozen=True)
class QuotePdfRow:
    index: int | str
    name: str
    spec: str
    unit: str
    quantity: str
    material_unit_price: str
    installation_unit_price: str
    material_amount: str
    installation_amount: str
    total_amount: str


@dataclass(frozen=True)
class QuotePdfSection:
    title: str
    rows: tuple[QuotePdfRow, ...]


@dataclass(frozen=True)
class QuotePdfData:
    title: str
    project: str
    stage_note: str
    sections: tuple[QuotePdfSection, ...]
    subtotal: str
    tax: str
    total: str
    omitted: tuple[str, ...]
    notes: tuple[str, ...]
    caveats: tuple[str, ...] = ()
    tax_label: str = "税额"
    uppercase: str | None = None
    tax_basis_note: str = ""


def _register_font() -> str:
    global _FONT_PATH
    if _FONT_PATH is not None:
        return _FONT_NAME
    errors: list[str] = []
    for path, index in _FONT_CANDIDATES:
        if not Path(path).exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont(_FONT_NAME, path, subfontIndex=index))
            _FONT_PATH = path
            return _FONT_NAME
        except Exception as exc:  # noqa: BLE001 - 逐个候选字体尝试
            errors.append(f"{path}: {type(exc).__name__}")
    raise RuntimeError("未找到可用于 PDF 的中文字体；尝试过：" + "; ".join(errors))


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(str(text)).replace("\n", "<br/>"), style)


def _styles(font: str) -> dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle(
            "title", fontName=font, fontSize=16, leading=21,
            alignment=TA_CENTER, spaceAfter=3,
        ),
        "project": ParagraphStyle(
            "project", fontName=font, fontSize=9, leading=12,
            alignment=TA_CENTER, textColor=colors.HexColor("#444444"),
        ),
        "stage": ParagraphStyle(
            "stage", fontName=font, fontSize=8, leading=11,
            alignment=TA_CENTER, textColor=colors.HexColor("#b00020"),
            spaceBefore=3, spaceAfter=5,
        ),
        "header": ParagraphStyle(
            "header", fontName=font, fontSize=7.5, leading=9,
            alignment=TA_CENTER, textColor=colors.white,
        ),
        "cell": ParagraphStyle(
            "cell", fontName=font, fontSize=7.2, leading=8.6,
            alignment=TA_LEFT,
        ),
        "center": ParagraphStyle(
            "center", fontName=font, fontSize=7.2, leading=8.6,
            alignment=TA_CENTER,
        ),
        "right": ParagraphStyle(
            "right", fontName=font, fontSize=7.2, leading=8.6,
            alignment=TA_RIGHT,
        ),
        "section": ParagraphStyle(
            "section", fontName=font, fontSize=8.2, leading=10,
            alignment=TA_LEFT, textColor=colors.HexColor("#1f3864"),
        ),
        "note": ParagraphStyle(
            "note", fontName=font, fontSize=7, leading=9.5,
            alignment=TA_LEFT, textColor=colors.HexColor("#333333"),
            wordWrap="CJK",
        ),
    }


HEADERS = (
    "序号", "名称", "规格及型号", "单位", "数量",
    "材料单价", "安装单价", "材料合价", "安装合价", "综合合价",
)
WIDTHS = (30, 68, 98, 28, 44, 44, 44, 55, 55, 55)


def _table(data: QuotePdfData, styles: dict[str, ParagraphStyle]) -> Table:
    rows: list[list] = [
        [_p(header, styles["header"]) for header in HEADERS]
    ]
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3864")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#7f7f7f")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    for section in data.sections:
        row_index = len(rows)
        rows.append([_p(section.title, styles["section"])] + [""] * 9)
        commands.extend([
            ("SPAN", (0, row_index), (-1, row_index)),
            ("BACKGROUND", (0, row_index), (-1, row_index), colors.HexColor("#d9e2f3")),
        ])
        for item in section.rows:
            rows.append([
                _p(item.index, styles["center"]),
                _p(item.name, styles["cell"]),
                _p(item.spec, styles["cell"]),
                _p(item.unit, styles["center"]),
                _p(item.quantity, styles["right"]),
                _p(item.material_unit_price, styles["right"]),
                _p(item.installation_unit_price, styles["right"]),
                _p(item.material_amount, styles["right"]),
                _p(item.installation_amount, styles["right"]),
                _p(item.total_amount, styles["right"]),
            ])
    table = Table(rows, colWidths=WIDTHS, repeatRows=1)
    table.setStyle(TableStyle(commands))
    return table


def _totals(data: QuotePdfData, styles: dict[str, ParagraphStyle]) -> Table:
    rows = [
        (_p("未税合计", styles["right"]), _p(data.subtotal, styles["right"])),
        (_p(data.tax_label, styles["right"]), _p(data.tax, styles["right"])),
        (_p("含税合计", styles["right"]), _p(data.total, styles["right"])),
    ]
    widths = (72, 78)
    if data.tax_basis_note:
        rows.append(
            (
                _p("计税基数", styles["right"]),
                _p(data.tax_basis_note, styles["cell"]),
            )
        )
        widths = (72, 150)
    table = Table(rows, colWidths=widths, hAlign="RIGHT")
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#7f7f7f")),
        ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#fce4d6")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return table


def _footer(canvas, doc) -> None:
    font = _register_font()
    canvas.saveState()
    canvas.setFont(font, 7)
    canvas.setFillColor(colors.HexColor("#666666"))
    canvas.drawString(22, 14, "HVAC Quote自动报价")
    canvas.drawRightString(A4[0] - 22, 14, f"第 {doc.page} 页")
    canvas.restoreState()


def render_quote_pdf(out_path: Path, data: QuotePdfData) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    font = _register_font()
    styles = _styles(font)

    story = [
        _p(data.title, styles["title"]),
        _p(data.project, styles["project"]),
        _p(data.stage_note, styles["stage"]),
        Spacer(1, 3),
        _table(data, styles),
        Spacer(1, 6),
        _totals(data, styles),
        Spacer(1, 6),
    ]
    if data.uppercase:
        story.append(
            _p(f"含税合计（大写）：{data.uppercase}", styles["note"])
        )
    story.extend([
        Spacer(1, 10),
        _p("未计入与说明", styles["section"]),
    ])
    if data.omitted:
        for text in data.omitted:
            story.append(_p(f"• {text}", styles["note"]))
    else:
        story.append(_p("无", styles["note"]))

    if data.caveats:
        story.append(Spacer(1, 8))
        story.append(_p("阻断处置与剩余风险", styles["section"]))
        for text in data.caveats:
            story.append(_p(f"• {text}", styles["note"]))

    if data.notes:
        story.append(Spacer(1, 8))
        story.append(_p("备注", styles["section"]))
        for text in data.notes:
            story.append(_p(text, styles["note"]))

    document = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=22,
        rightMargin=22,
        topMargin=20,
        bottomMargin=30,
        title=data.title,
        author="HVAC Quote自动报价",
    )
    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return out_path
