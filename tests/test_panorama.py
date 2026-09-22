import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from pypdf import PdfReader

import linux_recorder as recorder
from panorama_export import validate_tiles, write_long_png, write_pdf
from terminal_history import HistoryError, Snapshot, capture_panorama, history_extends


class FakeRange:
    def __init__(self, history, row):
        self.history, self.row = history, row

    def GetText(self, _):
        return self.history.rows[self.row] + "\r\n"

    def GetBoundingRectangles(self):
        return [0, (self.row - self.history.top) * 4, 12, 4]


class FakeHistory:
    def __init__(self, rows):
        self.rows, self.top = rows, 0
        self.restored = False

    def focus(self):
        pass

    def require_focus(self):
        pass

    def snapshot(self):
        return Snapshot("\r\n".join(self.rows) + "\r\n", self.rows)

    def capture_snapshot(self):
        return self.snapshot()

    def geometry(self):
        return (0, 0, 12, 24)  # six visible rows

    def line_range(self, row):
        return FakeRange(self, row)

    def show(self, row):
        self.top = min(row, max(0, len(self.rows) - 6))
        block = self.line_range(row)
        return block, block.GetBoundingRectangles()

    def bottom(self):
        self.restored = True

    def grab(self, box):
        image = Image.new("RGB", (box[2] - box[0], box[3] - box[1]))
        # Each physical row has an ID color, including identical text rows.
        for y in range(image.height):
            row = self.top + (box[1] + y) // 4
            image.paste((row, 10, 20), (0, y, image.width, y + 1))
        return image


class PanoramaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)
        self.temp.cleanup()

    def capture(self, rows):
        history = FakeHistory(rows)
        state = {"directory": str(self.root)}
        manifest = capture_panorama(state, history, history.grab, recorder.write_json)
        return state, manifest, history

    def test_panorama_preserves_every_row_once_including_repeats(self):
        state, manifest, history = self.capture(["same"] * 17)
        paths = validate_tiles(manifest, Path(state["panorama_manifest"]).parent)
        png = self.root / "long.png"
        write_long_png(png, paths, 12, 17 * 4)
        with Image.open(png) as image:
            self.assertEqual(image.size, (12, 68))
            self.assertEqual([image.getpixel((0, row * 4))[0] for row in range(17)], list(range(17)))
        self.assertTrue(history.restored)
        self.assertEqual(sum(tile["rows"] for tile in manifest["tiles"]), 17)

    def test_short_terminal_has_no_blank_screen_tail(self):
        state, manifest, _ = self.capture(["prompt", "answer", "prompt"])
        paths = validate_tiles(manifest, Path(state["panorama_manifest"]).parent)
        self.assertEqual(len(paths), 1)
        with Image.open(paths[0]) as image:
            self.assertEqual(image.height, 12)

    def test_missing_or_overlapping_rows_are_rejected(self):
        state, manifest, _ = self.capture(["row"] * 17)
        directory = Path(state["panorama_manifest"]).parent
        manifest["tiles"][1]["first_row"] -= 1
        with self.assertRaisesRegex(ValueError, "중복"):
            validate_tiles(manifest, directory)

    def test_erased_history_does_not_produce_false_success(self):
        history = FakeHistory(["prompt"])
        with self.assertRaises(HistoryError):
            capture_panorama({"directory": str(self.root), "history_changed": True},
                             history, history.grab, recorder.write_json)
        self.assertFalse((self.root / "captures").exists())

    def test_archived_history_allows_recovery_after_temporary_alternate_screen(self):
        (self.root / "history-before-change-test.txt").write_text(
            "original\r\nprompt\r\n", encoding="utf-8", newline="")
        history = FakeHistory(["original", "prompt man ls", "prompt"])
        state = {"directory": str(self.root), "history_changed": True,
                 "history_anchor": "original"}
        capture_panorama(state, history, history.grab, recorder.write_json)
        self.assertFalse(state["history_changed"])
        self.assertTrue(state["transient_history_change_ignored"])

    def test_snapshot_can_grow_but_not_lose_completed_rows(self):
        self.assertTrue(history_extends(["pwd  ", "prompt"], ["pwd", "prompt ls", "result"]))
        self.assertFalse(history_extends(["first", "second", "prompt"], ["second", "prompt"]))

    def test_start_anchor_detects_a_single_fast_overflow(self):
        history = FakeHistory(["ROW-1000", "ROW-1001", "prompt"])
        with self.assertRaisesRegex(HistoryError, "맨 처음"):
            capture_panorama({"directory": str(self.root), "history_anchor": "original-prompt$"},
                             history, history.grab, recorder.write_json)
        self.assertFalse((self.root / "captures").exists())

    def test_new_output_during_capture_preserves_incomplete_attempt(self):
        state, manifest, history = self.capture(["old"] * 17)
        calls = []
        def changed_grab(box):
            image = history.grab(box)
            history.rows[-1] = "new output"
            calls.append(1)
            return image
        with self.assertRaises(HistoryError):
            capture_panorama({"directory": str(self.root)}, history, changed_grab, recorder.write_json)
        attempts = list((self.root / "captures").glob("*/manifest.json"))
        saved = [json.loads(path.read_text(encoding="utf-8")) for path in attempts]
        self.assertIn("incomplete", [item["status"] for item in saved])

    def test_bad_pixel_seam_is_rejected(self):
        history = FakeHistory(["row"] * 17)
        calls = []
        def bad_grab(box):
            image = history.grab(box)
            calls.append(1)
            if len(calls) > 1:
                image.putpixel((0, 0), (255, 255, 255))
            return image
        with patch("terminal_history.time.sleep"), self.assertRaisesRegex(HistoryError, "연결 줄"):
            capture_panorama({"directory": str(self.root)}, history, bad_grab, recorder.write_json)

    def test_long_pdf_page_split_keeps_pixel_order(self):
        from verify_export import verify_panorama
        paths = [self.root / "one.png", self.root / "two.png"]
        for path, color in zip(paths, ("red", "blue")):
            with Image.new("RGB", (8, 8000), color) as image:
                image.save(path)
        manifest = dict(status="complete", width=8, row_height=1, total_rows=16000,
                        tiles=[dict(file="one.png", first_row=0, rows=8000),
                               dict(file="two.png", first_row=8000, rows=8000)])
        manifest_path = self.root / "manifest.json"
        recorder.write_json(manifest_path, manifest)
        png, pdf = self.root / "all.png", self.root / "all.pdf"
        write_long_png(png, paths, 8, 16000)
        self.assertEqual(write_pdf(pdf, paths, manifest, "long"), 2)
        verify_panorama({"panorama_manifest": str(manifest_path)}, PdfReader(pdf), png)

    def test_pdf_is_one_continuous_page_for_normal_length(self):
        state, manifest, _ = self.capture(["row"] * 17)
        paths = validate_tiles(manifest, Path(state["panorama_manifest"]).parent)
        pdf = self.root / "long.pdf"
        count = write_pdf(pdf, paths, manifest, "example")
        reader = PdfReader(pdf)
        self.assertEqual(count, 1)
        self.assertEqual(tuple(reader.pages[0].mediabox), (0, 0, 12, 68))

    def test_text_backup_preserves_crlf_exactly(self):
        text = "prompt\r\n테스트\r\n"
        recorder.save_history_backup(self.root, text)
        self.assertEqual((self.root / "history.txt").read_bytes(), text.encode("utf-8"))

    def test_separate_desktop_outputs_and_recovery_keep_intermediates_in_session(self):
        state, manifest, _ = self.capture(["row"] * 17)
        desktop = self.root / "desktop"
        state.update(name="test", mode="panorama-v1", output_directory=str(desktop))
        with patch.object(recorder, "publish_outputs", side_effect=PermissionError("read only")):
            with self.assertRaises(recorder.RecorderError):
                recorder.finish(self.root, state)
        self.assertEqual(state["status"], "export-error")
        self.assertTrue(Path(state["panorama_manifest"]).exists())
        self.assertTrue(recorder.finish(self.root, state))
        outputs = [Path(path) for path in state["outputs"]]
        self.assertEqual({path.suffix for path in outputs}, {".pdf", ".png"})
        self.assertTrue(all(path.parent == desktop for path in outputs))
        self.assertEqual(set(desktop.iterdir()), set(outputs))
        self.assertFalse(list(self.root.glob("*.png")))
        self.assertFalse(list(self.root.glob("*.pdf")))
        self.assertTrue((self.root / "session.json").exists())
        from verify_export import verify_panorama
        verify_panorama(state, PdfReader(next(p for p in outputs if p.suffix == ".pdf")),
                        next(p for p in outputs if p.suffix == ".png"))


if __name__ == "__main__":
    unittest.main()
