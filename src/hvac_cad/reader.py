"""Read original CAD only; conversion and audits operate on private scratch copies."""

from pathlib import Path
import hashlib
import re
import shutil
import subprocess
import tempfile
import time
import ezdxf
from . import TOOL_VERSION, REPLAY_READER_VERSIONS
from .normalize import normalize, NormalizationLimitError
from .preflight import preflight_document
from .diagnostics import capture_warnings


class CadReadError(Exception):
    def __init__(self, code, message="", detail=None):
        self.code = code
        self.detail = detail or {}
        super().__init__(f"{code}: {message}")


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def convert_dwg(
    source, scratch, converter=None, timeout=180, max_bytes=512 * 1024 * 1024
):
    executable = Path(
        converter or shutil.which("dwg2dxf") or "/missing/dwg2dxf"
    ).resolve()
    if not executable.is_file():
        raise CadReadError("CONVERTER_UNAVAILABLE", "未找到本地dwg2dxf")
    try:
        probe = subprocess.run(
            [str(executable), "--version"], capture_output=True, timeout=5, check=False
        )
        if probe.returncode != 0:
            raise CadReadError("CONVERTER_UNAVAILABLE", "版本探测失败")
        version = probe.stdout.decode(errors="replace").strip()
    except subprocess.TimeoutExpired as exc:
        raise CadReadError("CONVERSION_TIMEOUT", "版本探测超时") from exc
    except OSError as exc:
        raise CadReadError("CONVERTER_UNAVAILABLE", type(exc).__name__) from exc
    if version != "dwg2dxf 0.14":
        raise CadReadError("CONVERTER_VERSION_UNSUPPORTED", version[:80])
    output = scratch / "converted.dxf"
    with tempfile.TemporaryFile() as log:
        try:
            process = subprocess.Popen(
                [str(executable), "-o", str(output), str(source.resolve())],
                cwd=scratch,
                stdout=log,
                stderr=log,
                shell=False,
            )
        except OSError as exc:
            raise CadReadError("CONVERTER_UNAVAILABLE", type(exc).__name__) from exc
        deadline = time.monotonic() + timeout
        # 日志上限 8MiB：dwg2dxf 对复杂图会按图元刷警告，实测 4.5MB 示例图即有
        # 64KiB 以上输出；客户 34MB 项目图在 1MiB 旧限下被误拦（CONVERSION_LIMIT_EXCEEDED）。
        log_limit = 8 * 1024 * 1024
        try:
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise CadReadError(
                        "CONVERSION_TIMEOUT", f"超过 {timeout} 秒转换时限"
                    )
                if log.tell() > log_limit or (
                    output.exists() and output.stat().st_size > max_bytes
                ):
                    raise CadReadError(
                        "CONVERSION_LIMIT_EXCEEDED",
                        f"转换日志 {log.tell()} 字节（上限 {log_limit}）或中间文件 "
                        f"{output.stat().st_size if output.exists() else 0} 字节"
                        f"（上限 {max_bytes}）",
                    )
                time.sleep(0.02)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        log.seek(0)
        diagnostic = log.read(65536).decode(errors="replace")
    if process.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        raise CadReadError(
            "CONVERSION_FAILED",
            f"退出码{process.returncode}; {diagnostic[-2000:]}",
        )
    if output.stat().st_size > max_bytes:
        raise CadReadError(
            "CONVERSION_LIMIT_EXCEEDED",
            f"中间文件 {output.stat().st_size} 字节超过上限 {max_bytes} 字节"
            "；请按楼栋/楼层拆分图纸后分批上传",
        )
    return output, {
        "converterVersion": version,
        "returnCode": process.returncode,
        "diagnostic": diagnostic,
    }


def _load_document(path, file_version_id, workdir, converter=None):
    """转换 DWG 并读取 DXF；preflight 与正式检查共用。"""
    path = Path(path)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", file_version_id):
        raise CadReadError("INVALID_FILE_ID")
    if path.suffix.lower() not in {".dwg", ".dxf"}:
        raise CadReadError("FILE_TYPE_UNSUPPORTED")
    if not path.is_file() or path.stat().st_size > 256 * 1024 * 1024:
        raise CadReadError("FILE_UNAVAILABLE_OR_TOO_LARGE")
    original_hash = sha256(path)
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="cad-", dir=workdir))
    diagnostics = {}
    read_path = path
    if path.suffix.lower() == ".dwg":
        read_path, diagnostics = convert_dwg(path, scratch, converter)
    try:
        with capture_warnings() as warnings:
            document = ezdxf.readfile(read_path)
            audit = document.audit()
        if warnings.count:
            diagnostics["readerWarnings"] = {
                "count": warnings.count,
                "examples": warnings.messages,
            }
        diagnostics.update(
            readerVersion=f"ezdxf {ezdxf.__version__}",
            dxfVersion=document.dxfversion,
            auditErrors=len(audit.errors),
            auditFixes=len(audit.fixes),
        )
        return document, diagnostics, original_hash, path
    except CadReadError:
        raise
    except Exception as exc:
        raise CadReadError(
            "CAD_READ_FAILED",
            f"{type(exc).__name__}: {str(exc)[:200]}",
            {"exception": type(exc).__name__},
        ) from exc


def read_preflight(
    path,
    file_version_id,
    workdir,
    converter=None,
    max_entities=200000,
):
    """轻量预检：统计布局与图块规模，不做递归展开。"""
    document, diagnostics, original_hash, source = _load_document(
        path, file_version_id, workdir, converter
    )
    report = preflight_document(document, max_entities=max_entities)
    report["diagnostics"] = diagnostics
    report["sourceHash"] = original_hash
    report["fileName"] = source.name
    if sha256(source) != original_hash:
        raise CadReadError("SOURCE_CHANGED_DURING_READ")
    return report


def read_drawing(path, file_version_id, workdir, converter=None, max_entities=200000, *, audit_observer=None, reader_version=TOOL_VERSION, scope=None):
    if reader_version not in REPLAY_READER_VERSIONS:
        raise CadReadError('READER_VERSION_UNSUPPORTED')
    document, diagnostics, original_hash, path = _load_document(
        path, file_version_id, workdir, converter
    )
    try:
        if audit_observer is not None:
            audit_observer("before", document, None)
        audit = document.audit()
        if audit_observer is not None:
            audit_observer("after", document, audit)
        result = normalize(
            document,
            file_version_id,
            original_hash,
            max_entities,
            reader_version=reader_version,
            scope=scope,
        )
        if diagnostics.get("readerWarnings"):
            result.issue(
                "READER_DIAGNOSTIC",
                "读取器报告了对象复制/变换损失，不能视为无损读取",
                {"fileVersionId": file_version_id},
            )
        result.diagnostics.update(diagnostics)
        result.diagnostics.update(
            readerVersion=f"ezdxf {ezdxf.__version__}",
            dxfVersion=document.dxfversion,
            auditErrors=len(audit.errors),
            auditFixes=len(audit.fixes),
        )
        if audit.errors or audit.fixes:
            result.issue(
                "CAD_AUDIT_CHANGED",
                "读取器发现或修复了图元结构，需核对转换保真度",
                {"fileVersionId": file_version_id},
            )
        if path.suffix.lower() == ".dwg":
            result.issue(
                "DWG_CONVERSION_UNVERIFIED",
                "DWG转换已完成，但未通过AutoCAD原生显示一致性验证",
                {"fileVersionId": file_version_id},
                "review",
            )
        if sha256(path) != original_hash:
            raise CadReadError("SOURCE_CHANGED_DURING_READ")
        return result
    except NormalizationLimitError as exc:
        raise CadReadError(
            "ENTITY_LIMIT_EXCEEDED",
            str(exc) or "图块递归展开超过实体预算",
            getattr(exc, "detail", {}),
        ) from exc
    except CadReadError:
        raise
    except Exception as exc:
        raise CadReadError(
            "CAD_READ_FAILED",
            f"{type(exc).__name__}: {str(exc)[:200]}",
            {"exception": type(exc).__name__},
        ) from exc
