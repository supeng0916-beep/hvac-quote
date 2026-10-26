from dataclasses import replace
from pathlib import Path

import pymupdf

from hvac_quote.pdf import (
    QuotePdfData,
    QuotePdfRow,
    QuotePdfSection,
    render_quote_pdf,
)


def _data():
    return QuotePdfData(
        title="暖通演示报价表", project="虚构办公空间", stage_note="软件演示，非工程报价。",
        sections=(QuotePdfSection("设备与安装", (QuotePdfRow(1, "演示设备", "DEMO-01", "台", "2", "100.00", "10.00", "200.00", "20.00", "220.00"),)),),
        subtotal="220.00", tax="0.00", total="220.00", omitted=("未画出的管线未计入",), notes=("仅用于合成测试。",),
    )


def test_pdf_renders_chinese_and_totals(tmp_path):
    out = tmp_path / "quote.pdf"
    render_quote_pdf(out, _data())

    assert out.exists()
    doc = pymupdf.open(out)
    text = "\n".join(page.get_text() for page in doc)

    assert "暖通演示报价表" in text
    assert "虚构办公空间" in text
    assert "DEMO-01" in text
    assert "220.00" in text
    assert "未计入" in text

    fonts = [font[3] for page in doc for font in page.get_fonts(full=True)]
    assert fonts  # A supported embedded CJK font is selected for the current OS.


def test_pdf_notes_keep_their_own_numbering(tmp_path):
    out = tmp_path / "notes.pdf"
    data = replace(_data(), notes=("1· 第一条", "2· 第二条"))

    render_quote_pdf(out, data)

    text = "\n".join(page.get_text() for page in pymupdf.open(out))
    assert "1· 第一条" in text
    assert "1. 1·" not in text
