"""Regression tests for local file safety and recording failure handling.

Every file is a disposable fixture. Never read or modify real session data.
"""

import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import linux_recorder as recorder
from panorama_export import validate_tiles, write_long_png
from terminal_history import HistoryError, capture_panorama, monitor
from test_panorama import FakeHistory


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "recordings"
        self.root.mkdir()
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(recorder, "ROOT", self.root))
        self.stack.enter_context(patch.object(recorder, "STATE_FILE", self.root / ".active-session.json"))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def manifest(self):
        with Image.new("RGB", (12, 4), "green") as image:
            image.save(self.root / "tile.png")
        return dict(status="complete", width=12, row_height=4, total_rows=1,
                    tiles=[dict(file="tile.png", first_row=0, rows=1)])

    def session(self):
        directory = self.root / "lesson"
        directory.mkdir(exist_ok=True)
        (directory / "captures").mkdir(exist_ok=True)
        state = dict(name="lesson", status="stopped", directory=str(directory),
                     captures=str(directory / "captures"), capture_count=0, interval=1)
        recorder.write_json(directory / "session.json", state)
        recorder.write_json(recorder.STATE_FILE, {"directory": str(directory)})
        return directory, state

    def test_hostile_session_names_stay_one_child_of_root(self):
        for name in ("../escape", r"..\escape", "C:\\outside", "file:stream", "CON", "NUL.txt",
                     "a\x00b\nc", 'x; echo nope', "리눅스 과제", "x" * 300):
            with self.subTest(name=name):
                path = recorder.session_dir(name)
                self.assertEqual(path.parent, self.root)
                self.assertLessEqual(len(path.name), 80)
                self.assertFalse(set('<>:"/\\|?*') & set(path.name))

    def test_empty_names_are_rejected(self):
        for name in ("", " ", ".", "..", " . "):
            with self.subTest(name=name), self.assertRaises(recorder.RecorderError):
                recorder.safe_name(name)

    def test_manifest_traversal_is_rejected_before_open(self):
        for name in ("../outside.png", r"..\outside.png", str(self.base / "outside.png")):
            manifest = self.manifest()
            manifest["tiles"][0]["file"] = name
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "파일 경로"):
                validate_tiles(manifest, self.root)

    def test_incomplete_manifest_cannot_export(self):
        manifest = self.manifest()
        manifest["status"] = "incomplete"
        with self.assertRaisesRegex(ValueError, "완료"):
            validate_tiles(manifest, self.root)

    def test_wrong_dimensions_and_corrupt_png_are_rejected(self):
        manifest = self.manifest()
        manifest["width"] = 13
        with self.assertRaisesRegex(ValueError, "일치"):
            validate_tiles(manifest, self.root)
        manifest["width"] = 12
        (self.root / "tile.png").write_bytes(b"not a PNG")
        with self.assertRaises(OSError):
            validate_tiles(manifest, self.root)

    def test_oversized_png_header_is_rejected_before_creating_file(self):
        output = self.root / "too-large.png"
        with self.assertRaisesRegex(ValueError, "초과"):
            write_long_png(output, [], 12, 2**31)
        self.assertFalse(output.exists())

    def test_publish_race_does_not_replace_a_file_created_by_someone_else(self):
        sources = [self.root / "source.png", self.root / "source.pdf"]
        for path in sources:
            path.write_bytes(b"new output")
        destination = self.base / "desktop"
        destination.mkdir()
        raced = destination / "lesson.pdf"
        original_open = Path.open
        triggered = []

        def racing_open(path, mode="r", *args, **kwargs):
            if path == raced and mode == "xb" and not triggered:
                triggered.append(True)
                with original_open(path, "wb") as stream:
                    stream.write(b"someone else's file")
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", racing_open):
            outputs = recorder.publish_outputs(sources, destination, "lesson")
        self.assertEqual(raced.read_bytes(), b"someone else's file")
        self.assertFalse((destination / "lesson.png").exists())
        self.assertEqual(Path(outputs[0]).stem, Path(outputs[1]).stem)
        self.assertEqual(len(list(destination.iterdir())), 3)

    def test_malformed_json_returns_actionable_error(self):
        recorder.STATE_FILE.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(recorder.RecorderError, "상태 파일 읽기 실패"):
            recorder.read_state()

    def test_status_without_session_does_not_create_files(self):
        recorder.status()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_focus_loss_aborts_without_capture(self):
        history = FakeHistory(["row"] * 17)
        with patch.object(history, "require_focus", side_effect=HistoryError("lost focus")):
            with self.assertRaisesRegex(HistoryError, "lost focus"):
                capture_panorama({"directory": str(self.root)}, history, history.grab, recorder.write_json)
        manifests = list((self.root / "captures").glob("*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        self.assertEqual(recorder.read_json(manifests[0])["status"], "incomplete")
        self.assertFalse(list((self.root / "captures").glob("*/*.png")))

    def test_monitor_marks_erasure_and_archives_previous_text(self):
        snapshots = [FakeHistory(["original", "prompt"]).snapshot(),
                     FakeHistory(["erased", "prompt"]).snapshot()]
        state = dict(directory=str(self.root), interval=0)
        backups = []

        def next_snapshot():
            snapshot = snapshots.pop(0)
            if not snapshots:
                (self.root / ".stop-request").touch()
            return snapshot

        history = FakeHistory([])
        with patch.object(history, "snapshot", side_effect=next_snapshot):
            monitor(state, history, lambda value: None,
                    lambda raw, archive=False: backups.append((raw, archive)))
        self.assertTrue(state["history_changed"])
        self.assertTrue(any("original" in raw and archive for raw, archive in backups))

    def test_monitor_ignores_temporary_alternate_screen_after_history_returns(self):
        snapshots = [FakeHistory(rows).snapshot() for rows in
                     (["original", "prompt"], ["MAN PAGE", "line 2"],
                      ["original", "prompt man ls", "prompt"])]
        state = dict(directory=str(self.root), interval=0)
        backups = []

        def next_snapshot():
            value = snapshots.pop(0)
            if not snapshots:
                (self.root / ".stop-request").touch()
            return value

        history = FakeHistory([])
        with patch.object(history, "snapshot", side_effect=next_snapshot):
            monitor(state, history, lambda value: None,
                    lambda raw, archive=False: backups.append((raw, archive)))
        self.assertFalse(state.get("history_changed", False))
        self.assertFalse(any(archived for _, archived in backups))
        self.assertIn(("original\r\nprompt man ls\r\nprompt\r\n", False), backups)

    def test_unchanged_erased_buffer_is_not_archived_every_poll(self):
        state = dict(directory=str(self.root), interval=0, history_anchor="original")
        history = FakeHistory(["erased", "prompt"])
        snapshot = history.snapshot()
        calls, archives = [], []

        def next_snapshot():
            calls.append(True)
            if len(calls) == 4:
                (self.root / ".stop-request").touch()
            return snapshot

        with patch.object(history, "snapshot", side_effect=next_snapshot):
            monitor(state, history, lambda value: None,
                    lambda raw, archive=False: archives.append(raw) if archive else None)
        self.assertLessEqual(len(archives), 1)

    def test_active_pointer_must_stay_inside_recordings(self):
        outside = self.base / "outside"
        outside.mkdir()
        recorder.write_json(outside / "session.json", {"name": "fixture"})
        recorder.write_json(recorder.STATE_FILE, {"directory": str(outside)})
        with patch.object(recorder, "read_session", wraps=recorder.read_session), \
             patch.object(recorder, "read_json", wraps=recorder.read_json) as read:
            with self.assertRaisesRegex(recorder.RecorderError, "허용된 폴더"):
                recorder.read_state()
        self.assertEqual(read.call_count, 1)  # Do not read the outside session.

    def test_active_pointer_schema_error_is_actionable(self):
        recorder.write_json(recorder.STATE_FILE, {})
        with self.assertRaises(recorder.RecorderError):
            recorder.read_state()

    def test_non_object_json_is_rejected(self):
        for text in ("[]", "null", '"text"', "0"):
            recorder.STATE_FILE.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(recorder.RecorderError):
                recorder.read_state()

    def test_bad_pointer_paths_are_rejected(self):
        for value in (None, 1, [], {}, "", "relative/path", "C:\\bad\0path"):
            recorder.write_json(recorder.STATE_FILE, {"directory": value})
            with self.subTest(value=value), self.assertRaises(recorder.RecorderError):
                recorder.read_state()

    def test_valid_session_is_still_readable(self):
        directory, state = self.session()
        self.assertEqual(recorder.read_state(), state)
        self.assertEqual(recorder.read_session(directory), state)

    def test_malformed_session_fields_are_rejected_without_changes(self):
        directory, state = self.session()
        for key, value in (("name", []), ("status", None), ("directory", str(self.root / "other")),
                           ("captures", str(self.base / "outside")), ("capture_count", True),
                           ("capture_count", -1), ("interval", "1"), ("interval", float("nan")),
                           ("interval", True), ("interval", 10**400), ("mode", "unknown"),
                           ("output_directory", None), ("history_changed", "false"),
                           ("history_anchor", []), ("history_digest", 1)):
            broken = {**state, key: value}
            recorder.write_json(directory / "session.json", broken)
            before = (directory / "session.json").read_bytes()
            with self.subTest(key=key, value=value), self.assertRaises(recorder.RecorderError):
                recorder.read_state()
            self.assertEqual((directory / "session.json").read_bytes(), before)
        for key in ("name", "status", "directory", "captures", "capture_count", "interval"):
            broken = {k: v for k, v in state.items() if k != key}
            recorder.write_json(directory / "session.json", broken)
            with self.subTest(missing=key), self.assertRaises(recorder.RecorderError):
                recorder.read_state()

    def test_manifest_path_cannot_point_to_another_session(self):
        directory, state = self.session()
        state["panorama_manifest"] = str(self.root / "other" / "manifest.json")
        recorder.write_json(directory / "session.json", state)
        with self.assertRaisesRegex(recorder.RecorderError, "허용된 폴더"):
            recorder.read_state()

    def test_stop_rejects_tampered_metadata_before_writing_stop_request(self):
        directory, state = self.session()
        state["directory"] = str(self.base / "outside")
        recorder.write_json(directory / "session.json", state)
        with self.assertRaises(recorder.RecorderError):
            recorder.stop()
        self.assertFalse((directory / ".stop-request").exists())
        self.assertTrue(recorder.STATE_FILE.exists())

    def test_cli_reports_broken_state_without_traceback(self):
        recorder.write_json(recorder.STATE_FILE, {})
        error = io.StringIO()
        with patch.object(sys, "argv", ["linux_recorder.py", "status"]), \
             contextlib.redirect_stderr(error):
            self.assertEqual(recorder.main(), 1)
        self.assertIn("directory", error.getvalue())
        self.assertNotIn("Traceback", error.getvalue())

    def test_resolved_session_escape_is_rejected(self):
        # Deterministic symlink/junction resolution simulation needs no special
        # Windows privilege; actual OS junctions are checked separately.
        alias = self.root / "lesson"
        outside = self.base / "outside"
        original = Path.resolve
        def resolve(path, *args, **kwargs):
            return outside if path == alias else original(path, *args, **kwargs)
        with patch.object(Path, "resolve", resolve):
            with self.assertRaisesRegex(recorder.RecorderError, "허용된 폴더"):
                recorder.read_session(alias)

    def test_resolved_capture_escape_is_rejected(self):
        manifest = self.manifest()
        tile = self.root / "tile.png"
        original = Path.resolve
        def resolve(path, *args, **kwargs):
            return self.base / "outside.png" if path == tile else original(path, *args, **kwargs)
        with patch.object(Path, "resolve", resolve), self.assertRaisesRegex(ValueError, "폴더"):
            validate_tiles(manifest, self.root)

    def test_malformed_manifest_schema_is_rejected(self):
        manifest = self.manifest()
        for key, value in (("width", "12"), ("row_height", True), ("total_rows", 0),
                           ("tiles", None), ("tiles", []), ("tiles", [{}]), ("tiles", [None])):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_tiles({**manifest, key: value}, self.root)
        for filename in (None, 1, "", ".", "..", "tile.png:stream"):
            manifest["tiles"][0]["file"] = filename
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                validate_tiles(manifest, self.root)

    def test_first_intact_backup_is_archived_only_once_after_erasure(self):
        snapshots = [FakeHistory(rows).snapshot() for rows in
                     (["original", "prompt"], ["erased", "prompt"],
                      ["erased", "prompt"], ["new output", "prompt"])]
        state = dict(directory=str(self.root), interval=0)
        backups = []
        def next_snapshot():
            value = snapshots.pop(0)
            if not snapshots:
                (self.root / ".stop-request").touch()
            return value
        history = FakeHistory([])
        with patch.object(history, "snapshot", side_effect=next_snapshot):
            monitor(state, history, lambda value: None,
                    lambda raw, archive=False: backups.append((raw, archive)))
        self.assertTrue(state["history_changed"])
        archives = [raw for raw, archived in backups if archived]
        self.assertEqual(archives, ["original\r\nprompt\r\n"])
        self.assertIn(("new output\r\nprompt\r\n", False), backups)

    def test_backup_does_not_reuse_a_preexisting_temporary_file(self):
        old_temp = self.root / "history.txt.tmp"
        old_temp.write_bytes(b"keep")
        recorder.save_history_backup(self.root, "new backup")
        self.assertEqual(old_temp.read_bytes(), b"keep")
        self.assertEqual((self.root / "history.txt").read_text(), "new backup")


if __name__ == "__main__":
    unittest.main()
