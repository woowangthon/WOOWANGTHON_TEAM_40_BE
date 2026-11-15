"""보고서(docx) 파일 보관과 텍스트 추출.

docx 원본은 media 폴더에 두고, 문단 단위 텍스트를 WorkspaceFile.content 에 넣는다.
XML 을 비교하지 않고 텍스트를 비교하므로 diff, 이력 화면, AI 문맥을 코드 파일과 똑같이 쓴다.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

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
                # 칸 안에 문단이 여럿이면 한 줄로 잇는다. 한 행이 꼭 한 줄이어야 줄 번호가 맞는다
                lines.append(" | ".join(" ".join(p.text for p in c.paragraphs).strip() for c in row.cells))
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


# ---------- AI 수정안을 docx 에 적용 ----------
# extract_text 의 한 줄은 본문의 문단 하나, 또는 표의 행 하나다. 같은 순서로 요소 목록을 만들어 줄 번호와 맞춘다.
# 문단은 글자를 바꾸고, 표 행은 '칸 | 칸' 을 나눠 칸마다 글자를 바꾼다. 문단 모양과 칸 모양(테두리, 배경)은 그대로 둔다.
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# 글자만 바꾸면 망가지는 것들: 그림, 도형, 수식, 필드(목차, 쪽 번호), 개체, 각주 참조
_UNSAFE = ("drawing", "pict", "object", "fldChar", "fldSimple", "instrText", "footnoteReference", "endnoteReference", "txbxContent", "oMath", "oMathPara")


class DocEditError(ValueError):
    pass


class _Item:
    """본문 한 줄. kind 가 'p' 면 el 은 문단, 'row' 면 el 은 표 행(w:tr)이고 tbl 은 그 표다."""

    def __init__(self, kind: str, el: Any, tbl: Any = None) -> None:
        self.kind, self.el, self.tbl = kind, el, tbl


def _items(d: Any) -> list[_Item]:
    out: list[_Item] = []
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            out.append(_Item("p", child))
        elif tag == "tbl":
            for row in Table(child, d).rows:
                out.append(_Item("row", row._tr, child))
    return out


def _local(el: Any) -> str:
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def _unsafe(el: Any) -> bool:
    return any(_local(x) in _UNSAFE for x in el.iter())


def _cells(tr: Any) -> list[Any]:
    return tr.findall(f"{W}tc")


def _cell_text(tc: Any) -> str:
    return " ".join("".join(t.text or "" for t in p.iter(f"{W}t")) for p in tc.findall(f"{W}p")).strip()


def _row_locked(it: _Item) -> bool:
    """병합된 칸, 칸 안의 표, 여러 문단으로 된 칸, '|' 가 든 칸이 있으면 칸을 안전하게 나눌 수 없다."""
    if _unsafe(it.el):
        return True
    for tc in _cells(it.el):
        pr = tc.find(f"{W}tcPr")
        if pr is not None and (pr.find(f"{W}gridSpan") is not None or pr.find(f"{W}vMerge") is not None or pr.find(f"{W}hMerge") is not None):
            return True
        if tc.find(f"{W}tbl") is not None:
            return True
        paras = tc.findall(f"{W}p")
        if sum(1 for p in paras if "".join(t.text or "" for t in p.iter(f"{W}t")).strip()) > 1:
            return True
        if "|" in _cell_text(tc):
            return True
    return False


def _locked(it: _Item) -> bool:
    return _row_locked(it) if it.kind == "row" else _unsafe(it.el)


def locked_lines(data: bytes) -> list[int]:
    """AI 가 고칠 수 없는 줄 번호(1부터). 그림, 수식, 필드가 든 문단과 병합된 칸이 있는 표 행이다."""
    d = Document(io.BytesIO(data))
    return [i + 1 for i, it in enumerate(_items(d)) if _locked(it)]


def table_lines(data: bytes) -> list[tuple[int, int, int]]:
    """표마다 (첫 줄, 마지막 줄, 칸 수). AI 에게 표 행이 어디인지 알려 줄 때 쓴다."""
    d = Document(io.BytesIO(data))
    out: list[tuple[int, int, int]] = []
    last_tbl: Any = None
    for i, it in enumerate(_items(d)):
        if it.kind != "row":
            last_tbl = None
            continue
        n = len(_cells(it.el))
        if out and it.tbl is last_tbl:
            first, _, cols = out[-1]
            out[-1] = (first, i + 1, max(cols, n))
        else:
            out.append((i + 1, i + 1, n))
        last_tbl = it.tbl
    return out


_HEAD = re.compile(r"^(#{1,3}) +(.*)$")


class _Ctx:
    """문서의 스타일 이름표와 본문 문단 본보기. 제목 줄('# ')과 본문 줄에 맞는 문단 모양을 고를 때 쓴다."""

    def __init__(self, d: Any) -> None:
        self.names: dict[str, str] = {}
        for st in d.styles:
            if st.style_id and st.name:
                self.names[st.style_id] = st.name.lower()
        # 본보기는 글자가 있는 첫 본문 문단, 없으면 첫 본문 문단이다. 고치는 도중 모양이 바뀌지 않게 복사해 둔다
        from copy import deepcopy

        plain = [it.el for it in _items(d) if it.kind == "p" and not self.level(it.el) and not _unsafe(it.el)]
        texted = [p for p in plain if "".join(t.text or "" for t in p.iter(f"{W}t")).strip()]
        pick = (texted or plain or [None])[0]
        self.body: Any = deepcopy(pick) if pick is not None else None

    def level(self, p: Any) -> int:
        style = p.find(f"{W}pPr/{W}pStyle")
        sid = (style.get(f"{W}val") if style is not None else "") or ""
        name = self.names.get(sid, sid).lower()
        if name == "title":
            return 1
        m = re.match(r"(?:heading|제목)\s*(\d)", name)
        return int(m.group(1)) if m else 0

    def heading_id(self, level: int) -> str | None:
        return next((sid for sid, name in self.names.items() if name == f"heading {level}"), None)


def heading_levels(data: bytes) -> dict[int, int]:
    """제목 문단의 줄 번호(1부터)와 수준. AI 에게 보고서를 보여 줄 때 제목 줄에 '# ' 를 붙인다."""
    d = Document(io.BytesIO(data))
    ctx = _Ctx(d)
    return {i + 1: lv for i, it in enumerate(_items(d)) if it.kind == "p" and (lv := ctx.level(it.el))}


def _reset_ppr(p: Any, template: Any | None, style_id: str | None = None) -> None:
    """문단 모양을 본보기 문단 것으로 바꾸고, 글자 모양도 새로 시작하게 기존 글자를 비운다."""
    from copy import deepcopy

    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    for child in list(p):
        p.remove(child)
    ppr = None
    if template is not None and template.find(f"{W}pPr") is not None:
        ppr = deepcopy(template.find(f"{W}pPr"))
    if style_id:
        ppr = OxmlElement("w:pPr")
        ps = OxmlElement("w:pStyle")
        ps.set(qn("w:val"), style_id)
        ppr.append(ps)
    if ppr is not None:
        p.append(ppr)


def _place(ctx: _Ctx, p: Any, line: str) -> None:
    """한 줄을 문단 p 에 넣는다. '# ' 로 시작하면 제목, 아니면 본문 모양이 되게 맞춘다."""
    m = _HEAD.match(line)
    level, text = (len(m.group(1)), m.group(2)) if m else (0, line)
    cur = ctx.level(p)
    if level and cur != level:
        sid = ctx.heading_id(level)
        if sid:
            _reset_ppr(p, None, sid)
    elif not level and cur:
        _reset_ppr(p, ctx.body)
    _set_text(p, text)


def _set_text(p: Any, text: str) -> None:
    """문단 모양(pPr)과 첫 글자 모양(rPr)은 두고 글자만 바꾼다."""
    from copy import deepcopy

    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    first_rpr = None
    for r in p.iter(f"{W}r"):
        rpr = r.find(f"{W}rPr")
        if rpr is not None:
            first_rpr = deepcopy(rpr)
        break
    for child in list(p):
        if child.tag != f"{W}pPr":
            p.remove(child)
    if not text:
        return
    r = OxmlElement("w:r")
    if first_rpr is not None:
        r.append(first_rpr)
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    r.append(t)
    p.append(r)


def _new_paragraph(template: Any | None) -> Any:
    from copy import deepcopy

    from docx.oxml import OxmlElement

    p = OxmlElement("w:p")
    if template is not None:
        ppr = template.find(f"{W}pPr")
        if ppr is not None:
            p.append(deepcopy(ppr))
        # 첫 글자 모양도 이어받도록 빈 글자 하나를 둔다. _set_text 가 이걸 본다
        r = template.find(f"{W}r")
        rpr = r.find(f"{W}rPr") if r is not None else None
        if rpr is not None:
            nr = OxmlElement("w:r")
            nr.append(deepcopy(rpr))
            p.append(nr)
    return p


def _split_row(line: str, n: int) -> list[str]:
    cells = [c.strip() for c in line.split("|")]
    if len(cells) != n:
        raise DocEditError(f"표 행은 칸이 {n}개여야 한다. '칸 | 칸' 모양으로 칸 수를 맞춰 다시 요청해 주세요")
    return cells


def _set_row(tr: Any, cells: list[str]) -> None:
    for tc, text in zip(_cells(tr), cells, strict=True):
        paras = tc.findall(f"{W}p")
        if not paras:
            from docx.oxml import OxmlElement

            paras = [OxmlElement("w:p")]
            tc.append(paras[0])
        _set_text(paras[0], text)
        for extra in paras[1:]:  # 빈 문단만 남아 있다(_row_locked 가 걸렀다)
            tc.remove(extra)


def _new_row(template: Any, cells: list[str]) -> Any:
    from copy import deepcopy

    tr = deepcopy(template)
    trpr = tr.find(f"{W}trPr")
    if trpr is not None:
        for h in trpr.findall(f"{W}tblHeader"):  # 머리글 행을 복사해도 새 행은 머리글이 아니다
            trpr.remove(h)
    _set_row(tr, cells)
    return tr


def apply_lines(data: bytes, start: int, end: int, new_text: str) -> bytes:
    """start..end 줄을 new_text 의 줄들로 바꾼다. end = start - 1 이면 start 앞에 넣기만 한다.

    문단 범위는 줄마다 문단 하나, 표 행 범위는 줄마다 행 하나다. 한 수정안에 문단과 표 행을 섞지 않는다.
    """
    d = Document(io.BytesIO(data))
    items = _items(d)
    n = len(items)
    if not (1 <= start <= n + 1 and start - 1 <= end <= n):
        raise DocEditError("수정할 위치가 보고서와 맞지 않는다")
    targets = items[start - 1 : end]
    lines = new_text.split("\n") if new_text != "" else []
    if targets and not lines:
        # 지우기만 할 때는 문단과 표가 섞여도, 표가 여럿이어도 된다. 통째로 들어내는 것이라 깨질 것이 없다
        _delete(targets)
        return _save(d)
    if any(_locked(it) for it in targets):
        raise DocEditError("그림, 수식, 목차가 든 문단이나 병합된 칸이 있는 표는 글자를 바꿀 수 없다. 통째로 지우는 것은 된다")
    kinds = {it.kind for it in targets}
    if len(kinds) > 1 or len({id(it.tbl) for it in targets if it.kind == "row"}) > 1:
        raise DocEditError("문단과 표, 또는 서로 다른 표를 한 수정안에서 함께 바꿀 수 없다")

    ctx = _Ctx(d)
    if not targets:
        return _insert(d, ctx, items, start, lines)
    if kinds == {"row"}:
        _replace_rows(targets, lines)
    else:
        _replace_paragraphs(ctx, [it.el for it in targets], lines)
    return _save(d)


def _delete(targets: list[_Item]) -> None:
    by_table: dict[int, list[_Item]] = {}
    for it in targets:
        if it.kind == "p":
            it.el.getparent().remove(it.el)
        else:
            by_table.setdefault(id(it.tbl), []).append(it)
    for rows in by_table.values():
        tbl = rows[0].tbl
        if len(rows) == len(tbl.findall(f"{W}tr")):
            tbl.getparent().remove(tbl)  # 표의 모든 행이면 표를 지운다
            continue
        if any(_row_locked(it) for it in rows):
            raise DocEditError("병합된 칸이 있는 표는 일부 행만 지울 수 없다. 표 전체를 지우거나 직접 고쳐 주세요")
        for it in rows:
            tbl.remove(it.el)


def _replace_paragraphs(ctx: _Ctx, paras: list[Any], lines: list[str]) -> None:
    anchor = paras[-1]
    for i, text in enumerate(lines):
        if i < len(paras):
            _place(ctx, paras[i], text)
            anchor = paras[i]
        else:
            # 새 문단은 본문 본보기 모양으로 만들고, 제목 줄이면 _place 가 제목으로 바꾼다
            p = _new_paragraph(paras[-1] if not ctx.level(paras[-1]) else ctx.body)
            _place(ctx, p, text)
            anchor.addnext(p)
            anchor = p
    for extra in paras[len(lines) :]:
        extra.getparent().remove(extra)


def _replace_rows(rows: list[_Item], lines: list[str]) -> None:
    tbl = rows[0].tbl
    ncols = len(_cells(rows[0].el))
    parsed = [_split_row(line, ncols) for line in lines]
    anchor = rows[-1].el
    for i, cells in enumerate(parsed):
        if i < len(rows):
            if len(_cells(rows[i].el)) != ncols:
                raise DocEditError("칸 수가 다른 행이 섞인 표는 AI 수정안으로 바꿀 수 없다")
            _set_row(rows[i].el, cells)
            anchor = rows[i].el
        else:
            tr = _new_row(rows[-1].el, cells)
            anchor.addnext(tr)
            anchor = tr
    for extra in rows[len(parsed) :]:
        tbl.remove(extra.el)
    if not tbl.findall(f"{W}tr"):  # 행을 다 지웠으면 표도 지운다
        tbl.getparent().remove(tbl)


def _insert(d: Any, ctx: _Ctx, items: list[_Item], start: int, lines: list[str]) -> bytes:
    prev = items[start - 2] if start >= 2 else None
    nxt = items[start - 1] if start - 1 < len(items) else None
    # 이웃한 표와 칸 수가 같은 '칸 | 칸' 줄이면 그 표에 행을 넣는다
    for side in (prev, nxt):
        if side is None or side.kind != "row" or _row_locked(side):
            continue
        ncols = len(_cells(side.el))
        if lines and all(line.count("|") == ncols - 1 for line in lines):
            anchor = side.el
            rows = [_new_row(side.el, _split_row(line, ncols)) for line in lines]
            if side is prev:
                for tr in rows:
                    anchor.addnext(tr)
                    anchor = tr
            else:
                for tr in rows:
                    anchor.addprevious(tr)
            return _save(d)

    # 문단으로 넣는다. 앞이 제목이면 본문 모양을 쓰도록 근처의 일반 문단을 고른다
    near = [it.el for it in items[max(0, start - 4) : start + 2] if it.kind == "p" and not ctx.level(it.el) and not _unsafe(it.el)]
    template = near[0] if near else ctx.body
    paras = []
    for text in lines:
        p = _new_paragraph(template)
        _place(ctx, p, text)
        paras.append(p)
    if prev is not None:
        # 앞이 표 행이면 표 안이 아니라 표 바로 뒤에 넣는다. 표 중간이면 그 행 뒤에서 표를 나눌 수 없으니 막는다
        if prev.kind == "row":
            if nxt is not None and nxt.kind == "row" and nxt.tbl is prev.tbl:
                raise DocEditError("표 중간에는 문단을 넣을 수 없다. 표 행이면 '칸 | 칸' 모양으로 칸 수를 맞춰 주세요")
            anchor = prev.tbl
        else:
            anchor = prev.el
        for p in paras:
            anchor.addnext(p)
            anchor = p
    elif nxt is not None:
        target = nxt.tbl if nxt.kind == "row" else nxt.el
        for p in paras:
            target.addprevious(p)
    else:
        for p in paras:
            _body_append(d, p)
    return _save(d)


def _body_append(d: Any, p: Any) -> None:
    body = d.element.body
    sect = body.find(f"{W}sectPr")
    if sect is not None:
        sect.addprevious(p)
    else:
        body.append(p)


def _save(d: Any) -> bytes:
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()
