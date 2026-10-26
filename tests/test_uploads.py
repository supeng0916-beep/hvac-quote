"""R05：上传内容校验与格式/用途分离。"""

from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import Workbook

from hvac_quote.uploads import UploadValidationError, validate_upload


def _write(tmp_path: Path, name: str, content: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _xlsx_bytes() -> bytes:
    workbook = Workbook()
    workbook.active["A1"] = "测试"
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_valid_dxf_binary_and_xlsx(tmp_path):
    dxf = _write(tmp_path, "a.dxf", b"0\nSECTION\n0\nENDSEC\n0\nEOF\n")
    info = validate_upload(dxf, filename="a.dxf", declared_kind="auto", size=dxf.stat().st_size)
    assert info["businessKind"] == "drawing"
    assert info["contentFormat"] == "DXF"

    binary = _write(tmp_path, "b.dxf", b"AutoCAD Binary DXF\r\n\x1a\x00")
    info = validate_upload(binary, filename="b.dxf", declared_kind="drawing", size=binary.stat().st_size)
    assert info["businessKind"] == "drawing"

    xlsx = _write(tmp_path, "c.xlsx", _xlsx_bytes())
    info = validate_upload(xlsx, filename="c.xlsx", declared_kind="auto", size=xlsx.stat().st_size)
    assert info["businessKind"] == "template"
    assert info["contentFormat"] == "XLSX/XLSM"


def test_fake_extension_is_rejected_even_when_declared(tmp_path):
    fake = _write(tmp_path, "fake.dwg", b"not a dwg")
    with pytest.raises(UploadValidationError) as exc:
        validate_upload(fake, filename="fake.dwg", declared_kind="drawing", size=fake.stat().st_size)
    assert exc.value.code == "FORMAT_MISMATCH"

    renamed = _write(tmp_path, "renamed.dwg", _xlsx_bytes())
    with pytest.raises(UploadValidationError) as exc:
        validate_upload(renamed, filename="renamed.dwg", declared_kind="drawing", size=renamed.stat().st_size)
    assert exc.value.code == "FORMAT_MISMATCH"


def test_declared_kind_cannot_bypass_content_check(tmp_path):
    xlsx = _write(tmp_path, "t.xlsx", _xlsx_bytes())
    with pytest.raises(UploadValidationError):
        validate_upload(xlsx, filename="t.xlsx", declared_kind="drawing", size=xlsx.stat().st_size)


def test_empty_and_unsupported_dwg_version(tmp_path):
    empty = _write(tmp_path, "empty.dxf", b"")
    with pytest.raises(UploadValidationError) as exc:
        validate_upload(empty, filename="empty.dxf", declared_kind="auto", size=0)
    assert exc.value.code == "EMPTY_FILE"

    old = _write(tmp_path, "old.dwg", b"AC1009" + b"\x00" * 32)
    with pytest.raises(UploadValidationError) as exc:
        validate_upload(old, filename="old.dwg", declared_kind="auto", size=old.stat().st_size)
    assert exc.value.code == "CAD_VERSION_UNSUPPORTED"


def test_pdf_and_corrupt_xlsx(tmp_path):
    pdf = _write(tmp_path, "a.pdf", b"%PDF-1.4\n...")
    info = validate_upload(pdf, filename="a.pdf", declared_kind="auto", size=pdf.stat().st_size)
    assert info["businessKind"] == "reference"

    corrupt = _write(tmp_path, "bad.xlsx", b"PK\x03\x04broken")
    with pytest.raises(UploadValidationError):
        validate_upload(corrupt, filename="bad.xlsx", declared_kind="template", size=corrupt.stat().st_size)
