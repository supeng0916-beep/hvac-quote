"""上传文件的内容校验：扩展名只作线索，必须核对实际格式。

auto 与显式类型共用同一校验；显式类型只表达业务用途，不能绕过格式检查。
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

MAX_UNCOMPRESSED_XLSX = 512 * 1024 * 1024
DWG_SUPPORTED = {
    "AC1015": "AutoCAD 2000",
    "AC1018": "AutoCAD 2004",
    "AC1021": "AutoCAD 2007",
    "AC1024": "AutoCAD 2010",
    "AC1027": "AutoCAD 2013",
    "AC1032": "AutoCAD 2018",
}
CAD_SUFFIXES = {".dwg", ".dxf"}
TEMPLATE_SUFFIXES = {".xlsx", ".xlsm"}


class UploadValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _read_head(path: Path, size: int = 4096) -> bytes:
    with Path(path).open("rb") as stream:
        return stream.read(size)


def _dwg_version(path: Path) -> str:
    head = _read_head(path, 8)
    match = re.match(rb"AC\d{4}", head)
    if not match:
        raise UploadValidationError(
            "FORMAT_MISMATCH", "扩展名是 DWG，但文件内容不是 DWG"
        )
    version = match.group(0).decode("ascii")
    if version not in DWG_SUPPORTED:
        raise UploadValidationError(
            "CAD_VERSION_UNSUPPORTED",
            f"不支持此 DWG 版本（{version}）；请另存为 2000–2018 版本",
        )
    return DWG_SUPPORTED[version]


def _dxf_format(path: Path) -> str:
    head = _read_head(path, 128)
    if b"AutoCAD Binary DXF" in head[:32]:
        return "binary DXF"
    if head.lstrip().startswith(b"0") or b"SECTION" in head:
        return "text DXF"
    raise UploadValidationError(
        "FORMAT_MISMATCH", "扩展名是 DXF，但文件内容不是 DXF"
    )


def _xlsx_format(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or not any(
                name.startswith("xl/") for name in names
            ):
                raise UploadValidationError(
                    "FORMAT_MISMATCH",
                    "扩展名是 XLSX，但文件不是 Excel 工作簿",
                )
            total = sum(item.file_size for item in archive.infolist())
            if total > MAX_UNCOMPRESSED_XLSX:
                raise UploadValidationError(
                    "ARCHIVE_TOO_LARGE",
                    f"工作簿解压后约 {total / 1024 / 1024:.0f} MB，超过上限",
                )
            return "OOXML"
    except zipfile.BadZipFile as exc:
        raise UploadValidationError(
            "FORMAT_MISMATCH", "扩展名是 XLSX，但文件已损坏或不是压缩包"
        ) from exc


def _pdf_format(path: Path) -> str:
    if not _read_head(path, 5).startswith(b"%PDF-"):
        raise UploadValidationError(
            "FORMAT_MISMATCH", "扩展名是 PDF，但文件内容不是 PDF"
        )
    return "PDF"


def _detect(path: Path, suffix: str) -> tuple[str, str, str]:
    """返回（内容格式 kind, 格式说明, 版本）。"""
    if suffix in CAD_SUFFIXES:
        if suffix == ".dwg":
            return "drawing", "DWG", _dwg_version(path)
        return "drawing", "DXF", _dxf_format(path)
    if suffix in TEMPLATE_SUFFIXES:
        return "template", "XLSX/XLSM", _xlsx_format(path)
    if suffix == ".pdf":
        return "reference", "PDF", _pdf_format(path)
    raise UploadValidationError(
        "FILE_TYPE_UNSUPPORTED", f"不支持的扩展名：{suffix or '（无）'}"
    )


def validate_upload(
    path: Path,
    *,
    filename: str,
    declared_kind: str,
    size: int,
) -> dict:
    """校验并返回上传元数据；失败抛 UploadValidationError（带错误码）。"""
    suffix = Path(filename).suffix.lower()
    if size <= 0:
        raise UploadValidationError("EMPTY_FILE", "文件为空，请重新选择。")
    if not suffix:
        raise UploadValidationError(
            "FILE_TYPE_UNSUPPORTED", "文件没有扩展名，无法确认格式"
        )
    detected_kind, content_format, version = _detect(Path(path), suffix)
    if declared_kind == "auto":
        business_kind = detected_kind
    else:
        business_kind = declared_kind
        if declared_kind == "drawing" and detected_kind != "drawing":
            raise UploadValidationError(
                "FORMAT_MISMATCH",
                f"选择了“设计图纸”，但文件内容是 {content_format}；"
                "请选择正确用途或更换文件",
            )
        if declared_kind == "template" and detected_kind != "template":
            raise UploadValidationError(
                "FORMAT_MISMATCH",
                f"选择了“报价模板”，但文件内容是 {content_format}；"
                "请选择正确用途或更换文件",
            )
        if declared_kind == "reference" and detected_kind != "reference":
            raise UploadValidationError(
                "FORMAT_MISMATCH",
                f"选择了“参考资料”，但文件内容是 {content_format}",
            )
    return {
        "businessKind": business_kind,
        "detectedKind": detected_kind,
        "contentFormat": content_format,
        "formatVersion": version,
        "suffix": suffix,
    }


__all__ = ["UploadValidationError", "validate_upload"]
