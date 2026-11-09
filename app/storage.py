"""업로드 파일 저장. 파일 이름을 정리하고 크기 제한을 건다."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from .config import settings

_SAFE = re.compile(r"[^0-9A-Za-z가-힣._ -]+")


def safe_name(name: str) -> str:
    base = Path(name or "file").name
    cleaned = _SAFE.sub("_", base).strip(" .") or "file"
    return cleaned[:120]


async def save_upload(upload: UploadFile, subdir: str, max_mb: int) -> tuple[Path, str, int]:
    """(저장 경로, 원래 이름, 크기)를 돌려준다."""
    original = safe_name(upload.filename or "file")
    folder = settings.media_dir / subdir
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{uuid.uuid4().hex}_{original}"
    limit = max_mb * 1024 * 1024
    size = 0
    with target.open("wb") as fh:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                fh.close()
                target.unlink(missing_ok=True)
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"파일은 {max_mb}MB 이하여야 한다"
                )
            fh.write(chunk)
    return target, original, size


def remove_file(path: str) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
