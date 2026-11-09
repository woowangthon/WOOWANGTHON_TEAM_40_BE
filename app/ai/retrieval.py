"""검색 증강 생성용 자료 검색.

자료를 조각으로 나눠 BM25로 색인하고, 질문과 관련된 조각만 골라 프롬프트에 넣는다.
자료 전체가 작으면 조각을 고르지 않고 전부 넣는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rank_bm25 import BM25Okapi

_TOKEN = re.compile(r"[A-Za-z0-9_]+|[가-힣]+")


def tokenize(text: str) -> list[str]:
    """영문과 숫자는 단어 단위, 한글은 단어와 글자 두 개짜리 조각을 함께 쓴다."""
    tokens: list[str] = []
    for tok in _TOKEN.findall(text.lower()):
        tokens.append(tok)
        if re.fullmatch(r"[가-힣]+", tok) and len(tok) > 2:
            tokens.extend(tok[i : i + 2] for i in range(len(tok) - 1))
    return tokens


def chunk_text(text: str, size: int = 700, overlap: int = 100) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind("\n", start + size // 2, end)
            if cut == -1:
                cut = text.rfind(". ", start + size // 2, end)
            if cut != -1:
                end = cut + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


@dataclass
class Chunk:
    source: str
    text: str


def build_context(
    materials: list[tuple[str, str]], query: str, max_chars: int = 12000, top_k: int = 6
) -> tuple[str, bool]:
    """(발췌 문자열, 자료 있음 여부)를 돌려준다. materials는 (파일 이름, 추출 텍스트) 목록이다."""
    docs = [(name, text) for name, text in materials if text and text.strip()]
    if not docs:
        return "", False
    total = sum(len(t) for _, t in docs)
    if total <= max_chars:
        parts = [f"[자료: {name}]\n{text.strip()}" for name, text in docs]
        return "\n\n".join(parts), True

    chunks: list[Chunk] = []
    for name, text in docs:
        for piece in chunk_text(text):
            chunks.append(Chunk(source=name, text=piece))
    corpus = [tokenize(c.text) for c in chunks]
    bm25 = BM25Okapi(corpus)
    q_tokens = tokenize(query) or ["자료"]
    scores = bm25.get_scores(q_tokens)
    ranked = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)
    picked: list[Chunk] = []
    used = 0
    for i in ranked:
        if len(picked) >= top_k:
            break
        c = chunks[i]
        if used + len(c.text) > max_chars:
            continue
        picked.append(c)
        used += len(c.text)
    picked.sort(key=lambda c: (c.source, c.text))
    parts = [f"[자료: {c.source}]\n{c.text}" for c in picked]
    return "\n\n".join(parts), True
