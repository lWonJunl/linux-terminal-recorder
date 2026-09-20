"""Regression tests use generated images, never the user's desktop."""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from pypdf import PdfReader

import linux_recorder as recorder


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root_patch = patch.object(recorder, "ROOT", self.root)
        self.state_patch = patch.object(recorder, "STATE_FILE", self.root / ".active-session.json")
        self.root_patch.start()
        self.state_patch.start()
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)
        self.state_patch.stop()
        self.root_patch.stop()
        self.temp.cleanup()

    def session(self, name="test"):
        directory = self.root / name
        (directory / "captures").mkdir(parents=True)
        state = dict(name=name, directory=str(directory), captures=str(directory / "captures"),
                     capture_count=0, interval=0.1, status="recording")
        recorder.write_json(directory / "session.json", state)
        recorder.write_json(recorder.STATE_FILE, {"directory": str(directory)})
        return directory, state

    def frame(self, state, color="red", size=(30, 20)):
        with Image.new("RGB", size, color) as image:
            return recorder.capture(state, image)

    def test_duplicate_frames_are_not_saved(self):
        directory, state = self.session()
        self.assertTrue(self.frame(state))
        self.assertFalse(self.frame(state))
        self.assertTrue(self.frame(state, "blue"))
        self.assertEqual(len(list((directory / "captures").glob("*.png"))), 2)

    def test_capture_temporary_collision_preserves_existing_file(self):
        directory, state = self.session()
        captures = directory / "captures"
        existing = captures / "capture-000001.png.tmp"
        original = b"unfinished capture from an earlier attempt"
        existing.write_bytes(original)
        before = state.copy()
        with self.assertRaises(FileExistsError):
            self.frame(state)
        self.assertTrue(existing.is_file(), "A pre-existing temporary file must not be deleted")
        self.assertEqual(existing.read_bytes(), original)
        self.assertFalse((captures / "capture-000001.png").exists())
        self.assertEqual(state, before)
        self.assertEqual(recorder.read_json(directory / "session.json"), before)

    def test_capture_temporary_cleanup_only_removes_its_own_file_on_failure(self):
        for stage in ("save", "replace_file"):
            with self.subTest(stage=stage):
                directory, state = self.session(stage)
                captures = directory / "captures"
                unrelated = captures / "capture-000002.png.tmp"
                unrelated.write_bytes(b"keep")
                before = state.copy()
                owner = Image.Image if stage == "save" else recorder
                with patch.object(owner, stage, side_effect=OSError("fixture failure")):
                    with self.assertRaisesRegex(OSError, "fixture failure"):
                        self.frame(state)
                self.assertFalse((captures / "capture-000001.png.tmp").exists())
                self.assertFalse((captures / "capture-000001.png").exists())
                self.assertEqual(unrelated.read_bytes(), b"keep")
                self.assertEqual(state, before)
                self.assertEqual(recorder.read_json(directory / "session.json"), before)

    def test_temporary_windows_sharing_error_retries_atomic_save(self):
        directory, state = self.session()
        real_replace = os.replace
        error = PermissionError("file in use")
        error.winerror = 32
        attempts = []
        def busy_once(source, destination):
            attempts.append(source)
            if len(attempts) == 1:
                raise error
            real_replace(source, destination)
        state["status"] = "stopped"
        with patch.object(recorder.os, "replace", side_effect=busy_once):
            recorder.write_json(directory / "session.json", state)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(recorder.read_json(directory / "session.json")["status"], "stopped")

    def test_stop_produces_correct_pixels_and_pdf(self):
        directory, state = self.session()
        self.frame(state, "red")
        self.frame(state, "blue")
        recorder.stop()
        self.assertFalse(recorder.STATE_FILE.exists())
        self.assertEqual(recorder.read_json(directory / "session.json")["status"], "completed")
        with Image.open(directory / "test.png") as image:
            self.assertEqual(image.size, (30, 40))
            self.assertEqual(image.getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(image.getpixel((0, 39)), (0, 0, 255))
        pdf = PdfReader(directory / "test.pdf")
        self.assertEqual(len(pdf.pages), 1)
        self.assertEqual(len(pdf.pages[0].images), 1)
        self.assertEqual(pdf.pages[0].images[0].image.size, (30, 40))

    def test_empty_failed_session_can_be_cleared_without_losing_metadata(self):
        directory, state = self.session()
        state.update(status="capture-error", error="fixture failure")
        recorder.write_json(directory / "session.json", state)
        with self.assertRaisesRegex(recorder.RecorderError, "빈 세션"):
            recorder.stop()
        self.assertFalse(recorder.STATE_FILE.exists())
        self.assertEqual(recorder.read_json(directory / "session.json")["error"], "fixture failure")

    def test_failed_export_retains_raw_and_can_be_recovered(self):
        directory, state = self.session()
        self.frame(state)
        with patch.object(recorder, "export", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(recorder.RecorderError, "PDF 생성 실패"):
                recorder.stop()
        self.assertTrue(recorder.STATE_FILE.exists())
        self.assertEqual(len(list((directory / "captures").glob("*.png"))), 1)
        recorder.recover("test")
        self.assertTrue((directory / "test.pdf").exists())
        self.assertFalse(recorder.STATE_FILE.exists())

    def test_capture_failure_is_saved(self):
        directory, state = self.session()
        def fail():
            raise recorder.RecorderError("BitBlt test")
        with self.assertRaises(recorder.RecorderError):
            recorder.record_loop(state, fail)
        saved = recorder.read_json(directory / "session.json")
        self.assertEqual(saved["status"], "capture-error")
        self.assertIn("BitBlt test", saved["error"])

    def test_invalid_intervals_fail_before_launch(self):
        for interval in (0, -1, float("nan"), float("inf")):
            with self.subTest(interval=interval), self.assertRaises(recorder.RecorderError):
                recorder.start("test", interval)
        self.assertFalse(recorder.STATE_FILE.exists())

    def test_failed_preflight_does_not_create_session(self):
        with patch.object(recorder, "grab_screen", side_effect=recorder.RecorderError("no desktop")):
            with self.assertRaises(recorder.RecorderError):
                recorder.start("test", 1)
        self.assertFalse((self.root / "test").exists())

    def test_backend_fallback_and_original_failure_details(self):
        with patch("mss.MSS", side_effect=OSError("BitBlt denied")), \
             patch.object(recorder.ImageGrab, "grab", return_value=Image.new("RGB", (3, 4))):
            with recorder.grab_screen() as image:
                self.assertEqual(image.size, (3, 4))
        with patch("mss.MSS", side_effect=OSError("BitBlt denied")), \
             patch.object(recorder.ImageGrab, "grab", side_effect=OSError("screen grab failed")):
            with self.assertRaises(recorder.RecorderError) as raised:
                recorder.grab_screen()
            self.assertIn("BitBlt denied", str(raised.exception))
            self.assertIn("screen grab failed", str(raised.exception))

    def test_new_session_preserves_existing_name(self):
        directory, state = self.session()
        self.frame(state)
        recorder.STATE_FILE.unlink()
        before = (directory / "captures" / "capture-000001.png").read_bytes()
        with patch.object(recorder, "grab_screen", return_value=Image.new("RGB", (2, 2))), \
             patch.object(recorder, "launch_terminal", side_effect=OSError("fixture")):
            with self.assertRaises(recorder.RecorderError):
                recorder.start("test", 1)
        self.assertNotEqual(recorder.read_state()["directory"], str(directory))
        self.assertEqual((directory / "captures" / "capture-000001.png").read_bytes(), before)

    def test_capture_canvas_has_bounded_pixels(self):
        directory, state = self.session()
        self.frame(state, "red", (4000, 3000))
        self.frame(state, "blue", (4000, 3000))
        recorder.stop()
        pdf = PdfReader(directory / "test.pdf")
        self.assertEqual(len(pdf.pages), 2)
        sizes = [page.images[0].image.size for page in pdf.pages]
        self.assertEqual(sum(height for width, height in sizes), 6000)
        self.assertTrue(all(width * height <= 20_000_000 for width, height in sizes))

    def test_launch_failure_is_preserved_and_stop_clears(self):
        with patch.object(recorder, "grab_screen", return_value=Image.new("RGB", (2, 2))), \
             patch.object(recorder, "launch_terminal", side_effect=OSError("launch failed")):
            with self.assertRaisesRegex(recorder.RecorderError, "launch failed"):
                recorder.start("test", 1)
        self.assertEqual(recorder.read_state()["status"], "capture-error")
        with self.assertRaises(recorder.RecorderError):
            recorder.stop()
        self.assertFalse(recorder.STATE_FILE.exists())

    def test_paths_cannot_escape_root(self):
        self.assertEqual(recorder.session_dir("../bad:name").parent, self.root)
        self.assertEqual(recorder.safe_name("CON"), "_CON")
        for name in ("..", "../test", "test/sub"):
            with self.subTest(name=name), self.assertRaises(recorder.RecorderError):
                recorder.recover(name)

    def test_guarded_wsl_launch_uses_selected_distro_and_literal_rcfile_path(self):
        converted = "/mnt/c/Project with spaces/recording.bashrc"
        with patch.object(recorder.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, converted + "\n", "")) as run:
            command = recorder.guarded_wsl_command("Ubuntu-26.04")
        self.assertEqual(command, ["wsl.exe", "--distribution", "Ubuntu-26.04", "--cd", "~",
                                   "--exec", "bash", "--rcfile", converted, "-i"])
        self.assertEqual(run.call_args.args[0][:3], ["wsl.exe", "--distribution", "Ubuntu-26.04"])
        self.assertEqual(run.call_args.args[0][-1], str(recorder.SCRIPT.with_name("recording.bashrc")))

    def test_failed_wsl_path_conversion_does_not_launch_unguarded_shell(self):
        with patch.object(recorder.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 1, "", "fixture failure")):
            with self.assertRaisesRegex(recorder.RecorderError, "기록 보호 경로"):
                recorder.guarded_wsl_command()

    def test_terminal_profile_selection_keeps_guarded_wsl_command(self):
        title = "Linux Recorder test"
        window = SimpleNamespace(title=title)
        windows = SimpleNamespace(getWindowsWithTitle=lambda _: [window])
        guarded = ["wsl.exe", "--distribution", "Ubuntu-26.04", "--exec",
                   "bash", "--rcfile", "/mnt/c/Project with spaces/recording.bashrc", "-i"]
        for profile in (None, "Ubuntu-26.04", "My Ubuntu Profile"):
            with self.subTest(profile=profile), \
                 patch.dict(sys.modules, {"pygetwindow": windows}), \
                 patch.object(recorder, "guarded_wsl_command", return_value=guarded) as guard, \
                 patch.object(recorder.subprocess, "Popen") as popen, \
                 patch.object(recorder.time, "sleep"):
                self.assertIs(recorder.launch_terminal(title, "Ubuntu-26.04", profile), window)
                expected = ["wt.exe", "--window", "new", "new-tab", "--title", title,
                            "--suppressApplicationTitle"]
                if profile is not None:
                    expected += ["--profile", profile]
                popen.assert_called_once_with(expected + guarded)
                guard.assert_called_once_with("Ubuntu-26.04")

    def test_invalid_profile_is_rejected_before_preflight_or_session_creation(self):
        with patch.object(recorder, "grab_screen") as grab:
            for profile in ("", " ", "Ubuntu\nother", "Ubuntu\0", "Ubuntu;new-tab", 1):
                with self.subTest(profile=profile), self.assertRaisesRegex(recorder.RecorderError, "프로필"):
                    recorder.start("test", 1, "Ubuntu-26.04", profile)
            grab.assert_not_called()
        self.assertFalse(recorder.STATE_FILE.exists())

    def test_start_passes_and_saves_selected_profile(self):
        with patch.object(recorder, "grab_screen", return_value=Image.new("RGB", (2, 2))), \
             patch.object(recorder.shutil, "which", return_value="fixture.exe"), \
             patch.object(recorder, "launch_terminal", return_value=SimpleNamespace(_hWnd=123)) as launch, \
             patch("terminal_history.TerminalHistory"), patch("terminal_history.monitor"):
            recorder.start("test", 1, "Ubuntu-26.04", "My Ubuntu Profile")
        state = recorder.read_state()
        launch.assert_called_once_with(state["terminal_title"], "Ubuntu-26.04", "My Ubuntu Profile")
        self.assertEqual(state["terminal_profile"], "My Ubuntu Profile")
        self.assertEqual(state["clear_guard"], "bash-rc-v1")

    def test_cli_forwards_explicit_profile_and_distro(self):
        args = ["linux_recorder.py", "start", "lesson", "--distro", "Ubuntu-26.04",
                "--profile", "My Ubuntu Profile"]
        with patch.object(sys, "argv", args), patch.object(recorder, "start") as start:
            self.assertEqual(recorder.main(), 0)
        start.assert_called_once_with("lesson", 1.0, "Ubuntu-26.04", "My Ubuntu Profile")

    def test_cli_keeps_default_profile_when_option_is_omitted(self):
        with patch.object(sys, "argv", ["linux_recorder.py", "start", "lesson"]), \
             patch.object(recorder, "start") as start:
            self.assertEqual(recorder.main(), 0)
        start.assert_called_once_with("lesson", 1.0, None, None)

    def output_sources(self):
        directory = self.root / "private"
        directory.mkdir()
        sources = [directory / "test.png", directory / "test.pdf"]
        for index, path in enumerate(sources):
            path.write_bytes(f"fixture-{index}".encode())
        return sources

    def test_publish_only_two_final_files_in_separate_output_directory(self):
        sources = self.output_sources()
        desktop = self.root / "desktop"
        outputs = recorder.publish_outputs(sources, desktop, "test")
        self.assertEqual({p.name for p in desktop.iterdir()}, {"test.png", "test.pdf"})
        for source, output in zip(sources, outputs):
            self.assertEqual(source.read_bytes(), Path(output).read_bytes())

    def test_publish_collision_preserves_existing_pair(self):
        sources = self.output_sources()
        desktop = self.root / "desktop"
        desktop.mkdir()
        original = desktop / "test.pdf"
        original.write_bytes(b"user document")
        outputs = recorder.publish_outputs(sources, desktop, "test")
        self.assertEqual(original.read_bytes(), b"user document")
        self.assertNotEqual(Path(outputs[0]).stem, "test")
        self.assertEqual(Path(outputs[0]).stem, Path(outputs[1]).stem)
        self.assertFalse((desktop / "test.png").exists())

    def test_partial_publish_failure_removes_only_its_own_new_files(self):
        sources = self.output_sources()
        desktop = self.root / "desktop"
        desktop.mkdir()
        preserved = desktop / "notes.txt"
        preserved.write_bytes(b"keep")
        original_copy = recorder.shutil.copyfileobj
        calls = []
        def disk_full(source, destination, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                destination.write(b"incomplete")
                raise OSError("disk full")
            original_copy(source, destination, **kwargs)
        with patch.object(recorder.shutil, "copyfileobj", side_effect=disk_full):
            with self.assertRaisesRegex(OSError, "disk full"):
                recorder.publish_outputs(sources, desktop, "test")
        self.assertEqual(list(desktop.iterdir()), [preserved])
        self.assertTrue(all(path.exists() for path in sources))

    def test_pdf_and_png_are_split_without_missing_rows(self):
        directory, state = self.session()
        self.frame(state, "red", (5, 9000))
        self.frame(state, "blue", (5, 9000))
        recorder.stop()
        with Image.open(directory / "test-001.png") as first, \
             Image.open(directory / "test-002.png") as second:
            self.assertEqual(first.height + second.height, 18000)
            self.assertEqual(first.getpixel((0, 8999)), (255, 0, 0))
            self.assertEqual(first.height, 9000)
            self.assertEqual(second.getpixel((0, 0)), (0, 0, 255))
        self.assertEqual(len(PdfReader(directory / "test.pdf").pages), 2)

    def test_real_process_stop_exits_even_with_long_interval(self):
        directory, state = self.session()
        state["interval"] = 60
        recorder.write_json(directory / "session.json", state)
        code = (
            "import linux_recorder as r; from PIL import Image; "
            "state=r.read_state(); "
            "lock=r.FileLock(r.ROOT/'.recorder.lock'); lock.__enter__(); "
            "r.record_loop(state, lambda: Image.new('RGB',(30,20),'red')); "
            "lock.__exit__()"
        )
        env = {**os.environ, "LINUX_RECORDER_HOME": str(self.root), "PYTHONUTF8": "1"}
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen([sys.executable, "-c", code], env=env,
                                   cwd=recorder.SCRIPT.parent, creationflags=flags,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not list((directory / "captures").glob("*.png")):
                if time.monotonic() > deadline or process.poll() is not None:
                    self.fail("fixture writer did not start")
                time.sleep(0.05)
            with self.assertRaises(recorder.RecorderError):
                recorder.recover("test")  # must not export while a writer is active
            recorder.stop(timeout=3)
            self.assertEqual(process.wait(timeout=3), 0)
            saved = recorder.read_json(directory / "session.json")
            self.assertEqual(saved["capture_count"], 1)
            self.assertEqual(saved["status"], "completed")
            self.assertFalse(recorder.STATE_FILE.exists())
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
