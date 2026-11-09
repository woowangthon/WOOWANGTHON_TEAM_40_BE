"""파일에서 텍스트를 뽑는다. 학습 자료와 결과물 버전에 쓴다."""

from __future__ import annotations

from pathlib import Path

TEXT_SUFFIXES = {
    ".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp",
    ".h", ".sql", ".html", ".css", ".xml", ".yaml", ".yml", ".tex", ".r", ".ipynb",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            return _pdf(path)
        if suffix == ".docx":
            return _docx(path)
        if suffix in {".xlsx", ".xlsm"}:
            return extract_xlsx_cells(path)
        if suffix in TEXT_SUFFIXES:
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001  추출 실패는 빈 텍스트로 둔다
        return ""
    return ""


def _pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(p.strip() for p in pages if p.strip())


def _docx(path: Path) -> str:
    try:
        import docx  # type: ignore[import-not-found]
    except ImportError:
        return ""
    document = docx.Document(str(path))
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            parts.append("\t".join(cell.text.strip() for cell in row.cells))
    return "\n".join(parts)


def extract_xlsx_cells(path: Path, max_cells: int = 5000) -> str:
    """Excel 결과물을 텍스트 비교에 넣기 위해 시트별 셀 값과 수식을 한 줄씩 적는다."""
    from openpyxl import load_workbook

    lines: list[str] = []
    count = 0
    wb = load_workbook(str(path), data_only=False)
    for ws in wb.worksheets:
        lines.append(f"## 시트 {ws.title}")
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                lines.append(f"{cell.coordinate}: {cell.value}")
                count += 1
                if count >= max_cells:
                    lines.append("... 셀이 많아 이하 생략")
                    return "\n".join(lines)
    return "\n".join(lines)


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_SUFFIXES
