"""보고서(docx) 파일 보관과 텍스트 추출.

docx 원본은 media 폴더에 두고, 문단 단위 텍스트를 WorkspaceFile.content 에 넣는다.
XML 을 비교하지 않고 텍스트를 비교하므로 diff, 이력 화면, AI 문맥을 코드 파일과 똑같이 쓴다.
"""

from __future__ import annotations

import io
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from ..config import settings
from ..models import WorkspaceFile


def doc_path(f: WorkspaceFile) -> Path:
    return settings.media_dir / "workspace" / f"attempt{f.attempt_id}" / f"{f.id}.docx"


def blank_docx(outline: str = "") -> bytes:
    """빈 보고서. outline 의 각 줄이 문단이 되고 '# ' 로 시작하면 제목이 된다."""
    d = Document()
    for line in outline.splitlines():
        if line.startswith("# "):
            d.add_heading(line[2:].strip(), level=1)
        elif line.startswith("## "):
            d.add_heading(line[3:].strip(), level=2)
        else:
            d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def extract_text(data: bytes) -> str:
    """본문 순서대로 문단과 표를 한 줄씩 뽑는다. 표는 칸을 ' | ' 로 잇는다."""
    d = Document(io.BytesIO(data))
    lines: list[str] = []
    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            lines.append(Paragraph(child, d).text)
        elif tag == "tbl":
            for row in Table(child, d).rows:
                lines.append(" | ".join(c.text.strip() for c in row.cells))
    # 끝의 빈 문단은 비교에 의미가 없다
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def ensure_doc(f: WorkspaceFile, outline: str = "") -> Path:
    """docx 원본이 없으면 만든다. 만든 경우 content 도 채운다."""
    path = doc_path(f)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        data = blank_docx(outline)
        path.write_bytes(data)
        f.content = extract_text(data)
    return path


def save_doc(f: WorkspaceFile, data: bytes) -> str:
    path = doc_path(f)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return extract_text(data)
