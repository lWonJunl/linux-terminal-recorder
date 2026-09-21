"""Windows WSL screenshot recorder. Run from an interactive Windows terminal."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageGrab


class RecorderError(RuntimeError):
    """An actionable error, displayed without a traceback by the CLI."""


def desktop_path() -> Path:
    if os.name == "nt":
        # Resolve redirected / OneDrive desktops too; do not assume ~/Desktop.
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer) == 0:
            return Path(buffer.value)
    return Path.home() / "Desktop"


ROOT = Path(os.environ["LINUX_RECORDER_HOME"]).expanduser().resolve() if os.environ.get(
    "LINUX_RECORDER_HOME"
) else desktop_path() / "linux-recordings"
STATE_FILE = ROOT / ".active-session.json"
SCRIPT = Path(__file__).resolve()
OUTPUT_ROOT = Path(os.environ["LINUX_RECORDER_OUTPUT"]).expanduser().resolve() if os.environ.get(
    "LINUX_RECORDER_OUTPUT"
) else desktop_path()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_name(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:80].rstrip(" .")
    if not value:
        raise RecorderError("과제명에 한 글자 이상 입력하세요.")
    if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)
    }:
        value = "_" + value
    return value


def session_dir(name: str) -> Path:
    return ROOT / safe_name(name)


def replace_file(source: Path, destination: Path) -> None:
    # Windows readers / virus scanners can briefly deny rename/delete sharing.
    for attempt in range(20):
        try:
            os.replace(source, destination)
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) not in (5, 32, 33) or attempt == 19:
                raise
            time.sleep(0.05)


def write_json(path: Path, data: dict) -> None:
    """Readers see either the old complete JSON or the new complete JSON."""
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def save_history_backup(directory: Path, raw: str, archive: bool = False) -> None:
    name = "history-before-change-" + uuid.uuid4().hex[:8] + ".txt" if archive else "history.txt"
    path = directory / name
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(raw.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON의 최상위 값은 객체여야 합니다.")
        return data
    except (OSError, ValueError) as error:
        raise RecorderError(f"상태 파일 읽기 실패: {path}: {error}") from error


def absolute_path(value, label: str) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise RecorderError(f"상태 파일의 {label} 경로가 없거나 잘못됐습니다.")
    path = Path(value)
    if not path.is_absolute():
        raise RecorderError(f"상태 파일의 {label}에는 절대 경로가 필요합니다.")
    return path


def confined_path(path: Path, boundary: Path, label: str, *, direct_child=False) -> Path:
    """Check the resolved target too, including existing symlinks/junctions."""
    try:
        base, target = boundary.resolve(), path.resolve()
        if target == base or not target.is_relative_to(base):
            raise ValueError("허용된 폴더 밖의 경로")
        if direct_child and target.parent != base:
            raise ValueError("직접 하위 경로가 아님")
        return target
    except (OSError, ValueError, RuntimeError) as error:
        raise RecorderError(f"{label} 경로가 허용된 폴더를 벗어났거나 잘못됐습니다: {path}") from error


def read_session(directory: Path) -> dict:
    directory = confined_path(directory, ROOT, "세션", direct_child=True)
    source = confined_path(directory / "session.json", directory, "상태 파일", direct_child=True)
    state = read_json(source)
    try:
        for key in ("name", "status"):
            if not isinstance(state.get(key), str) or not state[key].strip():
                raise ValueError(f"{key} 항목은 비어 있지 않은 문자열이어야 합니다.")
        stored = confined_path(absolute_path(state.get("directory"), "directory"), ROOT,
                               "세션", direct_child=True)
        if stored != directory:
            raise ValueError("directory 항목이 현재 세션 폴더와 다릅니다.")
        captures = confined_path(absolute_path(state.get("captures"), "captures"), directory,
                                 "캡처", direct_child=True)
        if captures != directory / "captures":
            raise ValueError("captures 항목이 현재 세션의 captures 폴더와 다릅니다.")
        count, interval = state.get("capture_count"), state.get("interval")
        if type(count) is not int or count < 0:
            raise ValueError("capture_count 항목은 0 이상의 정수여야 합니다.")
        if type(interval) not in (int, float) or not math.isfinite(interval) or interval < 0.1:
            raise ValueError("interval 항목은 0.1 이상의 유한한 숫자여야 합니다.")
        if state.get("mode") not in (None, "panorama-v1"):
            raise ValueError("지원하지 않는 기록 형식입니다.")
        if state.get("mode") == "panorama-v1":
            if not isinstance(state.get("terminal_title"), str) or not state["terminal_title"]:
                raise ValueError("terminal_title 항목이 없거나 잘못됐습니다.")
            if "terminal_hwnd" in state and (type(state["terminal_hwnd"]) is not int or state["terminal_hwnd"] <= 0):
                raise ValueError("terminal_hwnd 항목은 양의 정수여야 합니다.")
        if "panorama_manifest" in state:
            manifest = confined_path(absolute_path(state["panorama_manifest"], "panorama_manifest"),
                                     captures, "캡처 목록")
            if manifest.name != "manifest.json":
                raise ValueError("캡처 목록 파일 이름이 잘못됐습니다.")
        if "output_directory" in state:
            absolute_path(state["output_directory"], "output_directory")
        if "history_changed" in state and type(state["history_changed"]) is not bool:
            raise ValueError("history_changed 항목은 참/거짓 값이어야 합니다.")
        for key in ("history_anchor", "history_digest"):
            if key in state and not isinstance(state[key], str):
                raise ValueError(f"{key} 항목은 문자열이어야 합니다.")
        # These files may be read later during final capture, not just recovery.
        confined_path(directory / "history.txt", directory, "텍스트 백업", direct_child=True)
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise RecorderError(f"세션 상태가 손상됐습니다: {source}\n{error}\n원본 파일을 보존한 뒤 확인하세요.") from error
    return state


def read_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    pointer = read_json(confined_path(STATE_FILE, ROOT, "활성 상태 파일", direct_child=True))
    return read_session(absolute_path(pointer.get("directory"), "directory"))


class FileLock:
    """OS lock: automatically released even if the recording process crashes."""

    def __init__(self, path: Path, timeout: float = 0):
        self.path, self.timeout, self.stream = path, timeout, None

    def __enter__(self):
        self.stream = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.stream.write(b"0")
            self.stream.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.stream.close()
                    raise RecorderError(f"작업이 아직 실행 중입니다: {self.path.name}") from None
                time.sleep(0.05)

    def __exit__(self, *_):
        self.stream.close()


def grab_screen(box: tuple[int, int, int, int] | None = None) -> Image.Image:
    """No backend can bypass a restricted/locked desktop; preserve both errors."""
    errors = []
    try:
        import mss
        with getattr(mss, "MSS", mss.mss)() as screen:
            region = screen.monitors[0] if box is None else {
                "left": box[0], "top": box[1],
                "width": box[2] - box[0], "height": box[3] - box[1],
            }
            raw = screen.grab(region)
            return Image.frombytes("RGB", raw.size, raw.rgb)
    except Exception as error:
        errors.append(f"mss: {type(error).__name__}: {error}")
    try:
        return ImageGrab.grab(bbox=box, all_screens=True).convert("RGB")
    except Exception as error:
        errors.append(f"Pillow: {type(error).__name__}: {error}")
    raise RecorderError(
        "Windows 화면에 접근하지 못했습니다. 제한된 실행 환경, 화면 잠금 또는 "
        "끊어진 원격 세션인지 확인하세요. Windows의 일반 PowerShell에서 실행할 수 있습니다. "
        "관리자 권한은 필요하지 않습니다.\n" + "\n".join(errors)
    )


def guarded_wsl_command(distro: str | None = None) -> list[str]:
    """Use a session-only Bash rcfile; never modify the user's shell settings."""
    command = ["wsl.exe"]
    if distro:
        command += ["--distribution", distro]
    guard = SCRIPT.with_name("recording.bashrc")
    if not guard.is_file():
        raise RecorderError(f"기록 보호 설정 파일이 없습니다: {guard}")
    result = subprocess.run(
        command + ["--exec", "wslpath", "-u", str(guard)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    path = result.stdout.strip()
    if result.returncode or not path.startswith("/") or "\n" in path:
        raise RecorderError("WSL 기록 보호 경로를 확인하지 못했습니다: " + result.stderr.strip())
    return command + ["--cd", "~", "--exec", "bash", "--rcfile", path, "-i"]


def launch_terminal(title: str, distro: str | None = None):
    import pygetwindow as windows

    command = ["wt.exe", "--window", "new", "new-tab", "--title", title,
               "--suppressApplicationTitle"] + guarded_wsl_command(distro)
    # This is the visible, interactive terminal requested by the user.
    process = subprocess.Popen(command)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        matches = [window for window in windows.getWindowsWithTitle(title)
                   if window.title == title]
        if matches:
            # The HWND/title exist during the opening animation, before the
            # terminal has painted. Let composition settle before any capture.
            time.sleep(1.0)
            return matches[0]
        code = process.poll()
        if code not in (None, 0):
            raise RecorderError(f"Windows Terminal 실행 실패 (종료 코드 {code}).")
        time.sleep(0.1)
    raise RecorderError("20초 안에 기록할 WSL 창을 찾지 못했습니다.")


def capture(state: dict, image: Image.Image) -> bool:
    digest = hashlib.sha256(str(image.size).encode() + image.tobytes()).hexdigest()
    if digest == state.get("last_frame_digest"):
        return False
    count = int(state["capture_count"]) + 1
    path = Path(state["captures"]) / f"capture-{count:06d}.png"
    temp = path.with_suffix(".png.tmp")
    created = False
    try:
        with temp.open("xb") as stream:
            created = True
            image.save(stream, format="PNG")
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(temp, path)
    finally:
        if created:
            temp.unlink(missing_ok=True)
    state.update(capture_count=count, last_capture_at=now(), last_frame_digest=digest)
    write_json(Path(state["directory"]) / "session.json", state)
    return True


def record_loop(state: dict, frame_source) -> None:
    directory = Path(state["directory"])
    stop_file = directory / ".stop-request"
    try:
        while not stop_file.exists():
            image = frame_source()
            next_status = "recording" if image is not None else "paused"
            if next_status != state["status"]:
                state["status"] = next_status
                write_json(directory / "session.json", state)
                if image is None:
                    print("캡처 대기: 기록할 WSL 창을 맨 앞으로 가져오세요.", flush=True)
            if image is not None:
                try:
                    capture(state, image)
                finally:
                    image.close()
            deadline = time.monotonic() + state["interval"]
            while not stop_file.exists() and time.monotonic() < deadline:
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        state["status"] = "stopped"
    except KeyboardInterrupt:
        state["status"] = "interrupted"
        print("기록을 멈췄습니다. stop 명령으로 원본을 PDF로 저장하세요.", flush=True)
    except Exception as error:
        state.update(status="capture-error", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        state["stopped_at"] = now()
        write_json(directory / "session.json", state)


def start(name: str, interval: float, distro: str | None = None) -> None:
    if not math.isfinite(interval) or interval < 0.1:
        raise RecorderError("캡처 간격은 0.1초 이상의 유한한 숫자여야 합니다.")
    safe_name(name)
    if os.name != "nt":
        raise RecorderError("Windows Python에서 실행하세요. WSL 내부 Python은 지원하지 않습니다.")
    for executable in ("wt.exe", "wsl.exe"):
        if not shutil.which(executable):
            raise RecorderError(f"{executable}를 찾을 수 없습니다.")
    # Check before creating a session or launching a terminal.
    with grab_screen():
        pass
    ROOT.mkdir(parents=True, exist_ok=True)
    recorder_lock = FileLock(ROOT / ".recorder.lock")
    with FileLock(ROOT / ".control.lock"):
        if read_state() is not None:
            raise RecorderError("이전 세션이 남아 있습니다. 먼저 stop으로 저장하세요.")
        recorder_lock.__enter__()
        try:
            directory = session_dir(name)
            if directory.exists():
                directory = ROOT / f"{safe_name(name)}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
            captures = directory / "captures"
            captures.mkdir(parents=True, exist_ok=False)
            state = dict(name=name, directory=str(directory), captures=str(captures), mode="panorama-v1",
                         started_at=now(), interval=interval, status="starting", capture_count=0,
                         pid=os.getpid(), terminal_title="Linux Recorder " + uuid.uuid4().hex[:12],
                         output_directory=str(OUTPUT_ROOT), clear_guard="bash-rc-v1")
            write_json(directory / "session.json", state)
            write_json(STATE_FILE, {"directory": str(directory)})
        except BaseException:
            recorder_lock.__exit__()
            raise
    try:
        window = launch_terminal(state["terminal_title"], distro)
        state["terminal_hwnd"] = window._hWnd
        write_json(directory / "session.json", state)
        from terminal_history import TerminalHistory, monitor
        history = TerminalHistory(window._hWnd, state["terminal_title"])
        print(f"파노라마 기록 시작\n중간 기록: {directory}\nPDF/PNG 저장 위치: {state['output_directory']}\n"
              f"clear/reset/Ctrl+L 보호가 적용된 Bash입니다.\n평소처럼 실습한 뒤 다른 PowerShell에서 "
              f"python \"{SCRIPT}\" stop\n캡처가 끝날 때까지 WSL 창을 닫지 마세요.", flush=True)
        monitor(state, history, lambda value: write_json(directory / "session.json", value),
                lambda raw, archive=False: save_history_backup(directory, raw, archive))
    except Exception as error:
        state.update(status="capture-error", error=f"{type(error).__name__}: {error}", stopped_at=now())
        write_json(directory / "session.json", state)
        raise RecorderError(f"기록 실패: {error}\n원본 보존: {directory}\nstop으로 정리하거나 복구하세요.") from error
    finally:
        recorder_lock.__exit__()


def publish_outputs(sources: list[Path], destination: Path, basename: str) -> list[str]:
    """Copy across drives, never overwrite existing files or expose sidecars."""
    destination.mkdir(parents=True, exist_ok=True)
    basename = safe_name(basename)
    for attempt in range(20):
        stem = basename if attempt == 0 else f"{basename}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        targets = [destination / (stem + source.suffix) for source in sources]
        if any(path.exists() for path in targets):
            continue
        created = []
        try:
            for source, target in zip(sources, targets):
                with target.open("xb") as output:
                    created.append(target)
                    with source.open("rb") as original:
                        shutil.copyfileobj(original, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
        except BaseException as error:
            # Only remove files exclusively created by this attempt. Originals
            # and all capture tiles stay available if publishing fails.
            for path in created:
                path.unlink(missing_ok=True)
            if isinstance(error, FileExistsError):
                continue
            raise
        return [str(path) for path in targets]
    raise RecorderError("결과 파일 이름이 계속 충돌합니다. 저장 위치를 확인하세요.")


def export(directory: Path, state: dict) -> list[str]:
    if state.get("mode") == "panorama-v1":
        from panorama_export import export_panorama
        return export_panorama(directory, state, read_json, replace_file, publish_outputs)
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    files = [confined_path(path, directory / "captures", "기존 캡처", direct_child=True)
             for path in sorted((directory / "captures").glob("capture-*.png"))]
    if not files:
        raise RecorderError("캡처된 화면이 없습니다.")
    sizes = []
    for path in files:
        with Image.open(path) as image:
            sizes.append(image.size)
    width = max(size[0] for size in sizes)
    total_height = sum(size[1] for size in sizes)
    # Bound each canvas rather than allocating an entire lecture in RAM.
    max_height = max(1, min(16000, 20_000_000 // width))
    parts = math.ceil(total_height / max_height)
    max_height = math.ceil(total_height / parts)  # avoid a nearly empty final page
    basename = safe_name(directory.name)
    outputs = []
    with tempfile.TemporaryDirectory(prefix=".export-", dir=directory) as scratch:
        staging = Path(scratch)
        pdf_name = basename + ".pdf"
        pdf = canvas.Canvas(str(staging / pdf_name))
        pdf.setTitle(state["name"])
        file_index = offset = 0
        remaining = total_height
        for part in range(parts):
            height = min(max_height, remaining)
            with Image.new("RGB", (width, height), "black") as merged:
                y = 0
                while y < height:
                    with Image.open(files[file_index]) as original:
                        amount = min(height - y, original.height - offset)
                        with original.crop((0, offset, original.width, offset + amount)) as strip:
                            merged.paste(strip, (0, y))
                        offset += amount
                        y += amount
                        if offset == original.height:
                            file_index += 1
                            offset = 0
                png_name = basename + (f"-{part + 1:03d}" if parts > 1 else "") + ".png"
                merged.save(staging / png_name)
                outputs.append(png_name)
                scale = min(1.0, 720 / width, 14000 / height)
                pdf.setPageSize((width * scale, height * scale))
                pdf.drawImage(ImageReader(merged), 0, 0, width=width * scale, height=height * scale)
                pdf.showPage()
            remaining -= height
        pdf.save()
        outputs.append(pdf_name)
        for filename in outputs:
            replace_file(staging / filename, directory / filename)
    return [str(directory / filename) for filename in outputs]


def finish(directory: Path, state: dict) -> bool:
    is_panorama = state.get("mode") == "panorama-v1"
    if not is_panorama and not any((directory / "captures").glob("capture-*.png")):
        state.update(status="empty", finished_at=now())
        write_json(directory / "session.json", state)
        print(f"캡처가 없어 PDF를 만들지 않았습니다. 오류 기록: {directory}")
        return False
    try:
        if is_panorama and not state.get("panorama_manifest"):
            if "terminal_hwnd" not in state:
                state.update(status="empty", finished_at=now())
                write_json(directory / "session.json", state)
                print("WSL 창 시작에 실패한 빈 세션입니다. 원인: " + state.get("error", "unknown"))
                return False
            from terminal_history import TerminalHistory, capture_panorama
            with grab_screen():
                pass  # DPI initialization and interactive-desktop preflight
            history = TerminalHistory(state["terminal_hwnd"], state["terminal_title"])
            capture_panorama(state, history, grab_screen, write_json)
            write_json(directory / "session.json", state)
        outputs = export(directory, state)
    except Exception as error:
        state.update(status="export-error", error=str(error))
        write_json(directory / "session.json", state)
        raise RecorderError(f"PDF 생성 실패. 원본이 보존되어 있습니다: {directory}\n{error}") from error
    state.update(status="completed", finished_at=now(), outputs=outputs)
    if is_panorama and state.get("error"):
        state["previous_error"] = state.pop("error")
    write_json(directory / "session.json", state)
    for path in outputs:
        print(path)
    if state.get("error"):
        print("기록 도중 오류가 있었습니다. 저장된 화면까지만 변환했습니다: " + state["error"])
    return True


def stop(timeout: float = 30) -> None:
    if not ROOT.exists():
        raise RecorderError("진행 중인 기록이 없습니다.")
    with FileLock(ROOT / ".control.lock"):
        state = read_state()
        if state is None:
            raise RecorderError("진행 중인 기록이 없습니다.")
        directory = Path(state["directory"])
        write_json(directory / ".stop-request", {"requested_at": now()})
        # Export only after the writer has released its OS lock.
        with FileLock(ROOT / ".recorder.lock", timeout=timeout):
            state = read_session(directory)
            completed = finish(directory, state)
            STATE_FILE.unlink(missing_ok=True)
        if not completed:
            raise RecorderError("빈 세션을 종료했습니다. 다시 start할 수 있습니다.")


def status() -> None:
    state = read_state()
    if state is None:
        print("기록 중인 세션이 없습니다.")
    else:
        running = True
        try:
            with FileLock(ROOT / ".recorder.lock"):
                running = False
        except RecorderError:
            pass
        print(json.dumps({**state, "recorder_running": running}, ensure_ascii=False, indent=2))


def recover(name: str) -> None:
    if not name or name in (".", "..") or re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        raise RecorderError("recover에는 저장된 세션 폴더 이름만 입력하세요.")
    directory = confined_path(ROOT / name, ROOT, "세션", direct_child=True)
    if not (directory / "session.json").exists():
        raise RecorderError(f"세션을 찾을 수 없습니다: {directory}")
    with FileLock(ROOT / ".control.lock"), FileLock(ROOT / ".recorder.lock"):
        state = read_session(directory)
        active = read_state()  # Validate before exporting or changing any session.
        if not finish(directory, state):
            raise RecorderError("복구할 캡처가 없습니다.")
        if active and Path(active["directory"]).resolve() == directory:
            STATE_FILE.unlink(missing_ok=True)


def doctor() -> None:
    print(f"Python: {sys.executable}\n중간 기록 위치: {ROOT}\nPDF/PNG 위치: {OUTPUT_ROOT}")
    print(f"Windows Terminal: {shutil.which('wt.exe')}\nWSL: {shutil.which('wsl.exe')}")
    with grab_screen() as image:
        print(f"화면 캡처 가능: {image.width} x {image.height} (이미지 저장 없음)")


def main() -> int:
    parser = argparse.ArgumentParser(description="WSL 터미널 파노라마 기록기 (Windows Python)")
    sub = parser.add_subparsers(dest="command", required=True)
    start_parser = sub.add_parser("start")
    start_parser.add_argument("name")
    start_parser.add_argument("--interval", type=float, default=1.0, help="텍스트 복구 백업 간격 (초)")
    start_parser.add_argument("--distro", help="예: Ubuntu-26.04 (생략하면 기본 WSL 배포판)")
    sub.add_parser("stop")
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("recover").add_argument("name")
    args = parser.parse_args()
    try:
        if args.command == "start":
            start(args.name, args.interval, args.distro)
        elif args.command == "stop":
            stop()
        elif args.command == "status":
            status()
        elif args.command == "doctor":
            doctor()
        else:
            recover(args.name)
    except (RecorderError, OSError, ImportError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
