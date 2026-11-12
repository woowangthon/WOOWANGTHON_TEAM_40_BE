"""줄 단위 diff. 직접 구현하지 않고 표준 라이브러리 difflib를 쓴다."""

from __future__ import annotations

import difflib
from typing import TypedDict


class DiffLine(TypedDict):
    type: str  # ctx | add | del
    no: int  # add, ctx 는 새 줄 번호, del 은 옛 줄 번호
    text: str


def line_diff(before: str, after: str, context: int = 2) -> tuple[list[DiffLine], int, int]:
    """(diff 줄 목록, 추가 줄 수, 삭제 줄 수)를 돌려준다."""
    a = before.splitlines()
    b = after.splitlines()
    out: list[DiffLine] = []
    plus = minus = 0
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for group in matcher.get_grouped_opcodes(context):
        if out:
            out.append({"type": "ctx", "no": 0, "text": "…"})
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                out.extend({"type": "ctx", "no": j1 + k + 1, "text": b[j1 + k]} for k in range(j2 - j1))
                continue
            if tag in ("replace", "delete"):
                out.extend({"type": "del", "no": i1 + k + 1, "text": a[i1 + k]} for k in range(i2 - i1))
                minus += i2 - i1
            if tag in ("replace", "insert"):
                out.extend({"type": "add", "no": j1 + k + 1, "text": b[j1 + k]} for k in range(j2 - j1))
                plus += j2 - j1
    return out, plus, minus


def replace_lines(content: str, start_line: int, end_line: int, new_text: str) -> str:
    """start_line..end_line(1부터, 포함)을 new_text로 바꾼다. end_line = start_line - 1 이면 start_line 앞에 넣는다."""
    lines = content.split("\n")
    new_lines = new_text.split("\n") if new_text != "" else []
    return "\n".join(lines[: start_line - 1] + new_lines + lines[end_line:])


def slice_lines(content: str, start_line: int, end_line: int) -> str:
    lines = content.split("\n")
    return "\n".join(lines[start_line - 1 : end_line])
