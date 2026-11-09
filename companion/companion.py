"""자리 3 보조 프로그램.

학생 컴퓨터에서 돌며 세 가지를 한다.
1. 지정한 프로그램 창을 주기적으로 캡처해 서버에 스냅샷 버전으로 올린다.
2. 지정한 저장 폴더를 감시해 결과물 파일이 저장될 때마다 버전으로 올린다.
3. 우리 웹의 AI 패널을 pywebview 창으로 항상 위에 띄운다.

실행: python companion.py --server http://localhost:8000
PyInstaller: pyinstaller companion.spec
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests

from capture import capture_window_png, find_window
from watcher import FolderWatcher

CONFIG_PATH = Path.home() / ".woowang_companion.json"


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


class Companion:
    def __init__(self, server: str, token: str) -> None:
        self.server = server.rstrip("/")
        self.token = token
        self.session = requests.Session()
        self.session.headers["X-Companion-Token"] = token
        self.me: dict[str, Any] = {}
        self.stop_event = threading.Event()
        self.status_lines: list[str] = []

    def log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {text}"
        print(line, flush=True)
        self.status_lines.append(line)
        del self.status_lines[:-200]

    def fetch_me(self) -> dict[str, Any]:
        r = self.session.get(f"{self.server}/api/companion/me", timeout=15)
        r.raise_for_status()
        self.me = r.json()
        return self.me

    def upload_file(self, path: Path, reason: str) -> bool:
        try:
            with path.open("rb") as fh:
                r = self.session.post(
                    f"{self.server}/api/companion/versions",
                    files={"file": (path.name, fh)},
                    data={"reason": reason},
                    timeout=120,
                )
        except (OSError, requests.RequestException) as exc:
            self.log(f"파일 업로드 실패 {path.name}: {exc}")
            return False
        if r.status_code >= 400:
            self.log(f"파일 업로드 거부 {path.name}: {r.status_code} {r.text[:200]}")
            return False
        self.log(f"버전 저장됨: {path.name}")
        return True

    def upload_snapshot(self, png: bytes, title: str) -> bool:
        try:
            r = self.session.post(
                f"{self.server}/api/companion/snapshots",
                files={"file": ("snapshot.png", png, "image/png")},
                data={"window_title": title},
                timeout=120,
            )
        except requests.RequestException as exc:
            self.log(f"캡처 업로드 실패: {exc}")
            return False
        if r.status_code >= 400:
            self.log(f"캡처 업로드 거부: {r.status_code} {r.text[:200]}")
            return False
        self.log(f"창 캡처 저장됨: {title}")
        return True

    def snapshot_loop(self, keyword: str, interval: int) -> None:
        while not self.stop_event.wait(interval):
            hwnd, title = find_window(keyword)
            if hwnd is None:
                self.log(f"'{keyword}' 창을 찾지 못했다. 프로그램이 열려 있는지 확인한다")
                continue
            png = capture_window_png(hwnd)
            if png is None:
                self.log("창이 최소화되어 있거나 캡처가 비어 있어 이번 캡처를 건너뛴다")
                continue
            self.upload_snapshot(png, title)

    def run(self, folder: Path, keyword: str, extensions: list[str], interval: int) -> None:
        watcher = FolderWatcher(folder, extensions, lambda p: self.upload_file(p, "저장 폴더에서 감지한 저장"))
        watcher.start()
        self.log(f"폴더 감시 시작: {folder} ({', '.join(extensions)})")
        threading.Thread(target=self.snapshot_loop, args=(keyword, interval), daemon=True).start()
        self.log(f"창 캡처 시작: '{keyword}' 창을 {interval}초마다")
        try:
            while not self.stop_event.is_set():
                time.sleep(0.5)
        finally:
            watcher.stop()


def open_panel(companion: Companion, url: str) -> None:
    """pywebview로 AI 패널을 항상 위에 띄운다. 창을 닫으면 프로그램도 끝난다."""
    import webview

    window = webview.create_window(
        "학습 도우미",
        url,
        width=420,
        height=640,
        on_top=True,
        min_size=(320, 400),
    )

    def on_closed() -> None:
        companion.stop_event.set()

    window.events.closed += on_closed
    webview.start()


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def main() -> int:
    parser = argparse.ArgumentParser(description="학습 도우미 보조 프로그램")
    parser.add_argument("--server", default=None, help="서버 주소, 예: http://localhost:8000")
    parser.add_argument("--token", default=None, help="웹에서 발급한 연결 토큰")
    parser.add_argument("--folder", default=None, help="감시할 저장 폴더")
    parser.add_argument("--no-panel", action="store_true", help="AI 패널 창을 띄우지 않는다")
    args = parser.parse_args()

    cfg = load_config()
    server = args.server or ask("서버 주소", cfg.get("server", "http://localhost:8000"))
    token = args.token or ask("연결 토큰", cfg.get("token", ""))
    if not token:
        print("연결 토큰이 필요하다. 웹의 과제 단계에서 발급한다.")
        return 1

    companion = Companion(server, token)
    try:
        me = companion.fetch_me()
    except requests.RequestException as exc:
        print(f"서버에 연결하지 못했다: {exc}")
        return 1

    print()
    print(f"과제: {me['assignment_title']} / 학생: {me['student_name']}")
    print()
    print("기록 범위 안내")
    print(me["consent_text"])
    if ask("동의하면 y를 입력", "n").lower() != "y":
        print("동의하지 않아 종료한다.")
        return 0
    if me.get("deadline_passed"):
        print("마감이 지나 기록할 수 없다.")
        return 0
    if not me.get("attempt_no"):
        print("웹에서 과제 단계를 먼저 열어 시도를 시작해야 한다.")
        return 0

    tool_cfg = me.get("tool_config") or {}
    folder = Path(args.folder or ask("감시할 저장 폴더", cfg.get("folder", str(Path.home() / "Documents"))))
    if not folder.is_dir():
        print(f"폴더가 없다: {folder}")
        return 1
    keyword = str(tool_cfg.get("window_keyword") or tool_cfg.get("program") or "Excel")
    extensions = [e.strip().lower() for e in str(tool_cfg.get("extensions") or ".xlsx").split(",") if e.strip()]
    interval = int(me.get("snapshot_seconds") or 60)

    save_config({"server": server, "token": token, "folder": str(folder)})

    worker = threading.Thread(target=companion.run, args=(folder, keyword, extensions, interval), daemon=True)
    worker.start()

    if args.no_panel:
        try:
            while worker.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            companion.stop_event.set()
        return 0

    panel_url = f"{server}{me['ai_panel_path']}?token={token}"
    open_panel(companion, panel_url)
    companion.stop_event.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
