"""Check publication rules in a disposable Git repository, never the real index."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import manual_smoke


PROJECT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git")


@unittest.skipUnless(GIT, "Git is required to verify publication rules")
class RepositoryRulesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(os.environ)
        for key in list(self.env):
            if key.startswith("GIT_"):
                del self.env[key]
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        self.git("init", "--quiet", "--template=")
        for name in (".gitignore", ".gitattributes"):
            shutil.copyfile(PROJECT / name, self.root / name)

    def git(self, *args, input=None, check=True):
        return subprocess.run(
            [GIT, "-c", f"core.excludesFile={os.devnull}", "-c", "core.autocrlf=true",
             "-C", str(self.root), *args],
            input=input, capture_output=True, text=True, encoding="utf-8",
            env=self.env, check=check, timeout=15,
        )

    def test_private_notes_and_generated_records_are_ignored(self):
        paths = [
            "LOCAL_USAGE.md", ".env", ".env.production", ".venv/pyvenv.cfg",
            "__pycache__/example.pyc", "tmp/preview.png", ".coverage", ".coverage.review",
            "recordings/lesson/data.txt", "test-recordings/lesson/data.txt",
            "linux-recordings/lesson/data.txt", "lesson/captures/tile-000001.png",
            "lesson/session.json", "lesson/manifest.json", "lesson/history.txt",
            "lesson/history-before-change-example.txt", ".active-session.json",
            ".stop-request", ".control.lock", ".recorder.lock", "lesson.log",
            "lesson.png", "lesson.pdf",
        ]
        result = self.git("check-ignore", "--no-index", "--stdin", "-z", input="\0".join(paths) + "\0")
        self.assertEqual(result.stdout.split("\0")[:-1], paths)

    def test_public_sources_docs_and_tests_are_not_ignored(self):
        paths = [
            ".gitignore", ".gitattributes", "README.md", "TEST_REPORT.md", "notes.md",
            "linux_recorder.py", "terminal_history.py", "panorama_export.py", "recording.bashrc",
            "requirements.txt", "requirements-dev.txt", "tests/test_repository.py",
            "tests/manual_smoke.py", "tests/verify_export.py",
        ]
        result = self.git("check-ignore", "--no-index", "--stdin", "-z",
                          input="\0".join(paths) + "\0", check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_bash_checkout_stays_lf_with_windows_autocrlf(self):
        name = "recording.bashrc"
        shutil.copyfile(PROJECT / name, self.root / name)
        self.git("add", "--", ".gitattributes", name)
        checkout = self.root / "checkout"
        self.git("checkout-index", "--all", f"--prefix={checkout.as_posix()}/")
        content = (checkout / name).read_bytes()
        self.assertNotIn(b"\r", content)
        self.assertFalse(content.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(content.decode("utf-8"), (PROJECT / name).read_text(encoding="utf-8"))


class SmokeOptionsTests(unittest.TestCase):
    def test_smoke_uses_default_wsl_distribution_unless_selected(self):
        self.assertIsNone(manual_smoke.parse_args([]).distro)
        self.assertIsNone(manual_smoke.parse_args([]).profile)

    def test_smoke_accepts_an_explicit_distribution(self):
        self.assertEqual(manual_smoke.parse_args(["--distro", "My-Ubuntu"]).distro, "My-Ubuntu")

    def test_smoke_accepts_a_separate_terminal_profile(self):
        args = manual_smoke.parse_args(["--distro", "My-Ubuntu", "--profile", "My Ubuntu Profile"])
        self.assertEqual(args.distro, "My-Ubuntu")
        self.assertEqual(args.profile, "My Ubuntu Profile")


if __name__ == "__main__":
    unittest.main()
