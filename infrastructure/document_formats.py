"""Bounded document decoding and text chunking; no model or storage operations."""
import io
import re
from datetime import date, datetime
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from docx import Document
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.utils.datetime import WINDOWS_EPOCH, from_excel

CHUNK_SIZE = 1800
CHUNK_OVERLAP = 200
MAX_ARCHIVE_MEMBERS = 2_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_DOCUMENT_TEXT_CHARS = 500_000
MAX_SPREADSHEET_ROWS = 10_000
MAX_SPREADSHEET_CELLS = 100_000
SUPPORTED_EXTENSIONS = {".docx", ".txt", ".md", ".xlsx"}
LEGACY_EXCEL_EXTENSION = ".xls"


def validate_office_archive(data: bytes) -> None:
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("Office 文件包含过多内部条目")
            total_size = sum(member.file_size for member in members)
            if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise ValueError("Office 文件解压后超过 50 MB 限制")
            if any(member.flag_bits & 0x1 for member in members):
                raise ValueError("Office 文件已加密，无法读取")
    except BadZipFile as exc:
        raise ValueError("Office 文件损坏、加密或无法读取") from exc

def _is_date_header(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = re.sub(r"\s+", "", value).lower()
    return normalized == "date" or normalized.endswith("日期")

def _excel_date_serial(value: object, epoch: datetime) -> object:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    try:
        converted = from_excel(value, epoch)
    except (OverflowError, TypeError, ValueError):
        return value
    if isinstance(converted, datetime) and 1900 <= converted.year <= 2200:
        return converted
    return value

def _excel_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        if not any((
                value.hour,
                value.minute,
                value.second,
                value.microsecond,
        )):
            return value.date().isoformat()
        return value.isoformat(sep=" ", timespec="minutes")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value).strip()

def _extract_xlsx_text(data: bytes) -> str:
    try:
        validate_office_archive(data)
    except ValueError as exc:
        if str(exc).startswith("Office 文件损坏"):
            raise ValueError("Excel 文件损坏、加密或无法读取") from exc
        raise
    try:
        workbook = load_workbook(
            io.BytesIO(data),
            read_only=True,
            data_only=True,
        )
        try:
            parts = []
            row_count = 0
            cell_count = 0
            text_length = 0
            for worksheet in workbook.worksheets:
                if worksheet.sheet_state != "visible":
                    continue
                rows = []
                date_columns = set()
                epoch = getattr(workbook, "epoch", WINDOWS_EPOCH)
                for raw_row in worksheet.iter_rows(values_only=True):
                    row_count += 1
                    cell_count += len(raw_row)
                    if (
                            row_count > MAX_SPREADSHEET_ROWS
                            or cell_count > MAX_SPREADSHEET_CELLS):
                        raise ValueError("Excel 文件有效数据范围过大")
                    date_columns.update(
                        index
                        for index, value in enumerate(raw_row)
                        if _is_date_header(value)
                    )
                    values = [
                        _excel_value(
                            _excel_date_serial(
                                value,
                                epoch,
                            )
                            if index in date_columns
                            else value
                        )
                        for index, value in enumerate(raw_row)
                    ]
                    while values and not values[-1]:
                        values.pop()
                    if any(values):
                        rendered = " | ".join(values)
                        text_length += len(rendered)
                        if text_length > MAX_DOCUMENT_TEXT_CHARS:
                            raise ValueError("文档文本超过 50 万字符限制")
                        rows.append(rendered)
                if rows:
                    parts.append(f"[工作表：{worksheet.title}]")
                    parts.extend(rows)
            return "\n".join(parts)
        finally:
            workbook.close()
    except (
            BadZipFile,
            InvalidFileException,
            KeyError,
            OSError,
            ValueError,
    ) as exc:
        raise ValueError("Excel 文件损坏、加密或无法读取") from exc

def extract_text(filename: str, data: bytes) -> str:
    extension = Path(filename).suffix.lower()
    if extension == ".docx":
        validate_office_archive(data)
        try:
            document = Document(io.BytesIO(data))
        except (BadZipFile, KeyError, OSError, ValueError) as exc:
            raise ValueError("Word 文件损坏、加密或无法读取") from exc
        parts = [
            paragraph.text.strip()
            for paragraph in document.paragraphs
            if paragraph.text.strip()
        ]
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                if any(cells):
                    parts.append(" | ".join(cells))
        text = "\n".join(parts)
    elif extension == ".xlsx":
        text = _extract_xlsx_text(data)
    else:
        text = _decode_text(data)

    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_DOCUMENT_TEXT_CHARS:
        raise ValueError("文档文本超过 50 万字符限制")
    if len(text) < 10:
        raise ValueError("文档中没有提取到足够的文本")
    return text

def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("文本文件编码无法识别")

def chunk_text(text: str) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + CHUNK_SIZE)
        if end < len(text):
            boundary = max(
                text.rfind("\n", start, end),
                text.rfind("。", start, end),
            )
            if boundary > start + CHUNK_SIZE // 2:
                end = boundary + 1

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= len(text):
            break
        start = max(start + 1, end - CHUNK_OVERLAP)

    return chunks
