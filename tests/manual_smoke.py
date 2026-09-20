"""Opt-in real WSL panorama test. Opens and controls only its own test window."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import linux_recorder as recorder


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run the opt-in Windows/WSL screenshot test.")
    parser.add_argument("--distro", help="WSL distribution name (default: WSL's default distribution)")
    parser.add_argument("--profile", help="Windows Terminal profile name (default: Terminal's default profile)")
    return parser.parse_args(argv)


def main():
    args = parse_args()
    import pyautogui
    import pygetwindow as windows
    from PIL import Image
    from pypdf import PdfReader
    from terminal_history import TerminalHistory

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = "Panorama-guard-" + stamp
    root = recorder.ROOT / ("panorama-test-" + stamp)
    output_directory = recorder.OUTPUT_ROOT
    desktop_files_before = {path for path in output_directory.iterdir() if path.is_file()}
    root.mkdir(parents=True, exist_ok=False)
    env = {**os.environ, "LINUX_RECORDER_HOME": str(root), "PYTHONUTF8": "1"}
    command = [sys.executable, "-X", "utf8", str(recorder.SCRIPT)]
    flags = subprocess.CREATE_NO_WINDOW
    window = None
    directory = root / name
    state_file = directory / "session.json"
    with (root / "start.log").open("w", encoding="utf-8") as log:
        start_command = command + ["start", name, "--interval", "0.5"]
        if args.distro:
            start_command += ["--distro", args.distro]
        if args.profile is not None:
            start_command += ["--profile", args.profile]
        process = subprocess.Popen(start_command,
                                   env=env, stdout=log, stderr=log, creationflags=flags)
        try:
            deadline = time.monotonic() + 60
            while True:
                if process.poll() is not None:
                    raise RuntimeError("start exited: " + (root / "start.log").read_text(encoding="utf-8"))
                if state_file.exists():
                    state = json.loads(state_file.read_text(encoding="utf-8"))
                    if state.get("terminal_hwnd"):
                        window = windows.Win32Window(state["terminal_hwnd"])
                    backup = directory / "history.txt"
                    if state.get("status") == "recording" and backup.exists():
                        prompt = backup.read_text(encoding="utf-8").rstrip()
                        if prompt.endswith(("$", "#")):
                            break
                if time.monotonic() > deadline:
                    raise RuntimeError("Timed out waiting for history recording: " + str(root))
                time.sleep(0.1)
            window = windows.Win32Window(state["terminal_hwnd"])
            history = TerminalHistory(window._hWnd, state["terminal_title"])
            history.focus()
            active = windows.getActiveWindow()
            if active is None or active._hWnd != window._hWnd:
                raise RuntimeError("Test WSL window is not focused; refusing to type")

            def focused():
                active = windows.getActiveWindow()
                if active is None or active._hWnd != window._hWnd:
                    raise RuntimeError("Focus changed; refusing to send more test keys")

            def type_command(script, ctrl_l=False, vi_command=False):
                before = history.snapshot().raw
                # Check each character, not only once per multi-second command.
                # Do not send keys to another application if focus changes.
                for char in script:
                    focused()
                    pyautogui.write(char, _pause=False)
                    time.sleep(0.04)
                focused()
                if ctrl_l:
                    pyautogui.hotkey("ctrl", "l")
                if vi_command:
                    focused()
                    pyautogui.press("escape")
                    focused()
                    pyautogui.hotkey("ctrl", "l")
                focused()
                pyautogui.press("enter")
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    focused()
                    after = history.snapshot().raw
                    if after != before and after.rstrip().endswith(("$", "#")):
                        time.sleep(0.2)
                        return
                    time.sleep(0.1)
                raise RuntimeError("Command did not return to primary prompt: " + script)

            # A fast 600-line burst, repeated identical output, color, wrapping, Korean.
            scripts = [
                "printf 'PANORAMA-BEGIN\\n'; seq -f 'ROW-%04g' 1 600",
                "clear",
                "clear -x",
                "reset",
                "printf 'REPEAT\\nREPEAT\\n'; printf '\\033[32mCOLOR\\033[0m\\n'",
                "printf '%240s\\n' '' | tr ' ' 'X'",
                "printf '\\uD14C\\uC2A4\\uD2B8\\n'; printf 'PANORAMA-END\\n'",
            ]
            for script in scripts:
                type_command(script)
            type_command("printf 'CTRL-L-OK\\n'", ctrl_l=True)
            # Verify the alternate Readline keymaps as well as the default.
            type_command("set -o vi")
            type_command("printf 'VI-CTRL-L-OK\\n'", ctrl_l=True, vi_command=True)
            deadline = time.monotonic() + 15
            while True:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                with (directory / "history.txt").open(encoding="utf-8", newline="") as stream:
                    rows = [line.rstrip() for line in stream.read().split("\r\n")]
                if "VI-CTRL-L-OK" in rows and state.get("history_rows", 0) > 600:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError("Test command did not finish")
                time.sleep(0.1)
            assert state["capture_count"] == 0, "Should not capture per command"
            expected = [f"ROW-{n:04d}" for n in range(1, 601)]
            assert [row for row in rows if re.fullmatch(r"ROW-\d{4}", row)] == expected
            assert rows.count("REPEAT") == 2
            assert "COLOR" in rows and "테스트" in rows
            assert "CTRL-L-OK" in rows
            assert not any("invalid keymap" in row or "bash: bind:" in row for row in rows)
            assert sum("clear blocked:" in row for row in rows) == 2
            assert sum("reset blocked:" in row for row in rows) == 1
            assert state["clear_guard"] == "bash-rc-v1"
            assert "X" * 240 in "".join(row for row in rows if row and set(row) == {"X"})
            assert not state.get("history_changed"), state
            result = subprocess.run(command + ["stop"], env=env, creationflags=flags,
                                    capture_output=True, text=True, encoding="utf-8", timeout=150)
            print(result.stdout, end="")
            if result.returncode:
                raise RuntimeError(result.stderr)
            assert process.wait(timeout=5) == 0
            final = json.loads(state_file.read_text(encoding="utf-8"))
            assert final["status"] == "completed", final
            assert not (root / ".active-session.json").exists()
            manifest_file = Path(final["panorama_manifest"])
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            png_file = next(Path(p) for p in final["outputs"] if p.endswith(".png"))
            pdf_file = next(Path(p) for p in final["outputs"] if p.endswith(".pdf"))
            assert png_file.parent == pdf_file.parent == output_directory
            added = {p for p in output_directory.iterdir() if p.is_file()} - desktop_files_before
            assert added == {png_file, pdf_file}, added
            assert not list(directory.glob("*.png")) and not list(directory.glob("*.pdf"))
            with Image.open(png_file) as panorama:
                assert panorama.height == manifest["total_rows"] * manifest["row_height"]
                for tile in manifest["tiles"]:
                    top = tile["first_row"] * manifest["row_height"]
                    bottom = top + tile["rows"] * manifest["row_height"]
                    with Image.open(manifest_file.parent / tile["file"]) as source:
                        assert panorama.crop((0, top, panorama.width, bottom)).tobytes() == source.convert("RGB").tobytes()
            pdf = PdfReader(pdf_file)
            assert len(pdf.pages) == 1, "600 lines should fit one long PDF page"
            print(json.dumps({"result": "PASS", "verified_output_rows": 600,
                              "terminal_profile": final.get("terminal_profile"),
                              "clear_reset_ctrl_l_blocked": True, "desktop_files_added": len(added),
                              "session_directory": str(directory),
                              "panorama_rows": manifest["total_rows"], "screen_tiles": len(manifest["tiles"]),
                              "pages": len(pdf.pages), "png": str(png_file), "pdf": str(pdf_file),
                              "recorder_exit_code": process.returncode}, ensure_ascii=False, indent=2))
        finally:
            if process.poll() is None:
                # Signal only this test recorder; preserve partial files on failure.
                if directory.exists():
                    recorder.write_json(directory / ".stop-request", {"test_cleanup": True})
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=5)
            if window is not None:
                window.close()


if __name__ == "__main__":
    main()
