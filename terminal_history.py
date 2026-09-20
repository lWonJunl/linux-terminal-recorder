"""Read/scroll the real Windows Terminal buffer; never render text into images."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import time
import uuid

from PIL import Image


class HistoryError(RuntimeError):
    pass


@dataclass
class Snapshot:
    raw: str
    rows: list[str]

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.raw.encode("utf-8")).hexdigest()


def rows_from_text(text: str) -> list[str]:
    rows = text.split("\r\n")
    while len(rows) > 1 and not rows[-1].strip():
        rows.pop()
    return rows


def history_extends(before: list[str], after: list[str]) -> bool:
    # The final line is still being typed; completed rows must remain intact.
    fixed = [row.rstrip() for row in before[:-1]]
    return len(after) >= len(fixed) and fixed == [row.rstrip() for row in after[:len(fixed)]]


class TerminalHistory:
    def __init__(self, hwnd: int, title: str):
        import comtypes.client
        import pygetwindow as windows

        self.window = windows.Win32Window(hwnd)
        self.title = title
        self.check_identity()
        self.uia = comtypes.client.GetModule("UIAutomationCore.dll")
        self.automation = comtypes.client.CreateObject(self.uia.CUIAutomation,
                                                      interface=self.uia.IUIAutomation)
        root = self.automation.ElementFromHandle(hwnd)
        condition = self.automation.CreatePropertyCondition(
            self.uia.UIA_IsTextPatternAvailablePropertyId, True)
        elements = root.FindAll(self.uia.TreeScope_Subtree, condition)
        self.element = None
        for index in range(elements.Length):
            candidate = elements.GetElement(index)
            if candidate.CurrentClassName == "TermControl":
                self.element = candidate
                break
        if self.element is None:
            raise HistoryError("WSL 터미널의 스크롤 기록을 읽을 수 없습니다.")
        self.pattern = self.element.GetCurrentPattern(self.uia.UIA_TextPatternId).QueryInterface(
            self.uia.IUIAutomationTextPattern)

    def check_identity(self):
        if self.window.title != self.title:
            raise HistoryError("기록한 WSL 창을 찾을 수 없습니다. 캡처가 끝날 때까지 창을 닫지 마세요.")

    def snapshot(self) -> Snapshot:
        self.check_identity()
        raw = self.pattern.DocumentRange.GetText(-1)
        return Snapshot(raw, rows_from_text(raw))

    def capture_snapshot(self) -> Snapshot:
        # GetText joins soft-wrapped rows. Enumerate visual Line units to preserve
        # their actual screen positions (including wide Korean characters).
        raw = self.snapshot().raw
        doc = self.pattern.DocumentRange
        cursor = doc.Clone()
        cursor.MoveEndpointByRange(1, cursor, 0)
        cursor.ExpandToEnclosingUnit(self.uia.TextUnit_Line)
        rows = []
        while cursor.CompareEndpoints(0, doc, 1) < 0:
            rows.append(cursor.GetText(-1).removesuffix("\r\n"))
            if len(rows) > 40000:
                raise HistoryError("터미널 줄 이동이 정상적으로 끝나지 않았습니다.")
            if cursor.Move(self.uia.TextUnit_Line, 1) != 1:
                break
        while len(rows) > 1 and not rows[-1].strip():
            rows.pop()
        if self.snapshot().raw != raw:
            raise HistoryError("스크롤 기록을 읽는 중 출력이 바뀌었습니다. 출력이 끝난 뒤 다시 시도하세요.")
        return Snapshot(raw, rows)

    def focus(self):
        import pygetwindow as windows

        self.check_identity()
        if self.window.isMinimized:
            self.window.restore()
        active = windows.getActiveWindow()
        if active is None or active._hWnd != self.window._hWnd:
            try:
                self.window.activate()
            except Exception:
                pass
        time.sleep(0.25)
        try:
            self.require_focus()
        except HistoryError:
            print("Windows가 자동 창 전환을 제한했습니다. 10초 안에 기록 중인 WSL 창을 클릭하세요.", flush=True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                time.sleep(0.1)
                active = windows.getActiveWindow()
                if active is not None and active._hWnd == self.window._hWnd and not self.window.isMinimized:
                    return
            self.require_focus()

    def require_focus(self):
        import pygetwindow as windows

        self.check_identity()
        active = windows.getActiveWindow()
        if active is None or active._hWnd != self.window._hWnd or self.window.isMinimized:
            raise HistoryError("캡처 중에는 WSL 창을 맨 앞에 유지하세요. 창을 선택한 후 stop을 다시 실행할 수 있습니다.")

    def line_range(self, row: int):
        block = self.pattern.DocumentRange.Clone()
        block.MoveEndpointByRange(1, block, 0)
        moved = block.Move(self.uia.TextUnit_Line, row)
        if moved != row:
            raise HistoryError(f"스크롤 기록의 {row + 1}번째 줄로 이동하지 못했습니다.")
        block.ExpandToEnclosingUnit(self.uia.TextUnit_Line)
        return block

    def show(self, row: int):
        block = self.line_range(row)
        for _ in range(8):
            block.ScrollIntoView(True)
            time.sleep(0.2)
            bounds = list(block.GetBoundingRectangles())
            if len(bounds) == 4 and bounds[2] > 0 and bounds[3] > 0:
                return block, bounds
        raise HistoryError(f"{row + 1}번째 줄의 실제 화면 위치를 읽지 못했습니다.")

    def geometry(self):
        rect = self.element.CurrentBoundingRectangle
        return (rect.left, rect.top, rect.right, rect.bottom)

    def bottom(self):
        doc = self.pattern.DocumentRange.Clone()
        doc.MoveEndpointByRange(0, doc, 1)
        doc.ScrollIntoView(False)


def monitor(state: dict, history, save_state, save_backup) -> None:
    directory = Path(state["directory"])
    previous = None
    try:
        while not (directory / ".stop-request").exists():
            current = history.snapshot()
            anchor = state.get("history_anchor")
            changed = previous is not None and not history_extends(previous.rows, current.rows)
            if anchor and not current.rows[0].startswith(anchor):
                changed = True
            if not anchor and current.rows[0].strip():
                state["history_anchor"] = current.rows[0].rstrip()
            if changed and not state.get("history_changed"):
                # Includes erased scrollback, buffer overflow, reflow, and TUI rewrites.
                # Preserve the last intact buffer once. A missing anchor remains
                # missing on every poll; do not archive the same damaged buffer.
                state["history_changed"] = True
                if previous is not None:
                    save_backup(previous.raw, archive=True)
            if previous is None or current.digest != previous.digest:
                save_backup(current.raw)
                state.update(status="recording", history_rows=len(current.rows),
                             history_digest=current.digest, last_history_at=time.time())
                save_state(state)
            previous = current
            deadline = time.monotonic() + state["interval"]
            while time.monotonic() < deadline and not (directory / ".stop-request").exists():
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        state["status"] = "stopped"
    except KeyboardInterrupt:
        state["status"] = "interrupted"
    except Exception as error:
        state.update(status="history-error", error=str(error))
        raise
    finally:
        save_state(state)


def capture_panorama(state: dict, history, grab, save_json) -> dict:
    """Capture disjoint rows by their actual UIA coordinates, even identical rows."""
    if state.get("history_changed"):
        raise HistoryError("기록 중 스크롤 내용 변경이 감지됐습니다(clear/버퍼 초과/창 크기 변경/전체화면 앱 등). "
                           "전체 기록으로 잘못 저장하지 않도록 중단했습니다. history.txt 백업을 확인하세요.")
    history.focus()
    baseline = history.snapshot()
    if state.get("history_anchor") and not baseline.rows[0].startswith(state["history_anchor"]):
        raise HistoryError("실습의 맨 처음 기록이 더 이상 스크롤 버퍼에 없습니다. history.txt 백업을 확인하세요.")
    if state.get("history_digest") and baseline.digest != state["history_digest"]:
        backup = Path(state["directory"]) / "history.txt"
        if backup.exists():
            with backup.open(encoding="utf-8", newline="") as stream:
                previous = rows_from_text(stream.read())
            if not history_extends(previous, baseline.rows):
                raise HistoryError("마지막 자동 저장 이후 스크롤 기록이 지워졌습니다.")
    snapshot = history.capture_snapshot()
    if snapshot.digest != baseline.digest:
        raise HistoryError("스크롤 내용을 읽는 도중 출력이 변경됐습니다.")
    total = len(snapshot.rows)
    directory = Path(state["directory"])
    attempt = directory / "captures" / ("panorama-" + uuid.uuid4().hex[:12])
    attempt.mkdir(parents=True, exist_ok=False)
    manifest = dict(version=1, status="capturing", total_rows=total, digest=snapshot.digest,
                    tiles=[], directory=str(attempt), width=0, row_height=0)
    save_json(attempt / "manifest.json", manifest)
    try:
        geometry = history.geometry()
        row = 0
        previous_tail = None
        while row < total:
            history.require_focus()
            if history.snapshot().digest != snapshot.digest or history.geometry() != geometry:
                raise HistoryError("캡처 중 출력 또는 창 크기가 바뀌었습니다. 명령이 끝난 뒤 stop을 다시 실행하세요.")
            display_row = max(0, row - 1)
            overlap = row - display_row
            block, bounds = history.show(display_row)
            x, y, width, row_height = [int(round(value)) for value in bounds]
            if rows_from_text(block.GetText(-1))[0].rstrip() != snapshot.rows[display_row].rstrip():
                raise HistoryError("스크롤 줄 위치와 출력이 일치하지 않습니다.")
            if manifest["width"] and (width, row_height) != (manifest["width"], manifest["row_height"]):
                raise HistoryError("캡처 도중 글꼴 크기가 바뀌었습니다.")
            manifest.update(width=width, row_height=row_height)
            # Leave one row of safety at the bottom for padding/partial rows.
            count = min(total - row, max(1, math.floor((geometry[3] - y) / row_height) - 1 - overlap))
            last = history.line_range(row + count - 1)
            end_bounds = list(last.GetBoundingRectangles())
            expected = [x, y + (overlap + count - 1) * row_height, width, row_height]
            if len(end_bounds) != 4 or any(abs(a - b) > 1 for a, b in zip(end_bounds, expected)):
                raise HistoryError("화면 밖의 줄을 캡처하려고 했습니다. 창 크기를 유지하고 다시 시도하세요.")
            history.require_focus()
            box = (x, y, x + width, y + (count + overlap) * row_height)
            image = None
            for retry in range(8):
                history.require_focus()
                candidate = grab(box)
                if candidate.size != (width, (count + overlap) * row_height):
                    candidate.close()
                    raise HistoryError("스크린샷 크기가 줄 위치와 다릅니다.")
                if previous_tail is not None:
                    with candidate.crop((0, 0, width, row_height)) as shared:
                        matches = shared.tobytes() == previous_tail
                    if not matches:
                        candidate.close()
                        time.sleep(0.15)
                        continue
                image = candidate.crop((0, overlap * row_height, width, candidate.height))
                candidate.close()
                break
            if image is None:
                raise HistoryError("앞뒤 캡처의 연결 줄이 일치하지 않습니다. 화면 갱신이 멈춘 뒤 다시 시도하세요.")
            with image:
                with image.crop((0, image.height - row_height, width, image.height)) as tail:
                    previous_tail = tail.tobytes()
                filename = f"tile-{len(manifest['tiles']) + 1:06d}.png"
                temporary = attempt / (filename + ".tmp")
                with temporary.open("xb") as stream:
                    image.save(stream, format="PNG")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, attempt / filename)
            history.require_focus()
            if history.snapshot().digest != snapshot.digest:
                raise HistoryError("캡처 중 새로운 출력이 발생했습니다. 출력 종료 후 다시 시도하세요.")
            manifest["tiles"].append(dict(file=filename, first_row=row, rows=count))
            row += count
            save_json(attempt / "manifest.json", manifest)
            print(f"파노라마 캡처: {row}/{total}줄", flush=True)
        manifest["status"] = "complete"
        save_json(attempt / "manifest.json", manifest)
        state.update(panorama_manifest=str(attempt / "manifest.json"),
                     capture_count=len(manifest["tiles"]), panorama_rows=total)
        return manifest
    except Exception as error:
        manifest.update(status="incomplete", error=str(error))
        save_json(attempt / "manifest.json", manifest)
        raise
    finally:
        try:
            history.bottom()
        except Exception:
            pass
