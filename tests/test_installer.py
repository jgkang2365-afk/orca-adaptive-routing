from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-production.sh"


def run(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


class ProductionInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="orca-installer-test.")
        root = Path(self.temp.name)
        self.repo = root / "source"
        self.install = root / "install"
        self.bin = root / "bin"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "adaptive_coordinator").mkdir()
        shutil.copy2(INSTALLER, self.repo / "scripts" / INSTALLER.name)
        (self.repo / "adaptive_coordinator" / "__init__.py").write_text('VALUE = "committed"\n')
        (self.repo / "adaptive_coordinator" / "cli.py").write_text(
            "def main():\n    return 0\n"
        )
        (self.repo / "pyproject.toml").write_text(
            '[project]\nname = "orca-adaptive-routing"\nversion = "9.9.9"\n'
        )
        run("git", "init", "-q", cwd=self.repo)
        run("git", "config", "user.name", "Installer Test", cwd=self.repo)
        run("git", "config", "user.email", "installer@example.invalid", cwd=self.repo)
        run("git", "add", ".", cwd=self.repo)
        run("git", "commit", "-qm", "fixture", cwd=self.repo)
        self.commit = run("git", "rev-parse", "HEAD", cwd=self.repo).stdout.strip()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def install_result(self) -> subprocess.CompletedProcess[str]:
        return run(
            "bash",
            "scripts/install-production.sh",
            str(self.install),
            str(self.bin),
            cwd=self.repo,
            check=False,
        )

    def test_i1_clean_tree_installs(self) -> None:
        result = self.install_result()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.bin / "orca-adaptive").is_symlink())

    def test_i2_tracked_modification_is_rejected(self) -> None:
        (self.repo / "adaptive_coordinator" / "cli.py").write_text("changed = True\n")
        result = self.install_result()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dirty source tree", result.stderr)

    def test_i3_staged_modification_is_rejected(self) -> None:
        (self.repo / "adaptive_coordinator" / "cli.py").write_text("changed = True\n")
        run("git", "add", "adaptive_coordinator/cli.py", cwd=self.repo)
        result = self.install_result()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dirty source tree", result.stderr)

    def test_i4_untracked_source_file_is_rejected(self) -> None:
        (self.repo / "adaptive_coordinator" / "untracked_probe.py").write_text("PROBE = True\n")
        result = self.install_result()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dirty source tree", result.stderr)
        self.assertFalse((self.install / self.commit).exists())

    def test_i5_install_matches_commit_object(self) -> None:
        result = self.install_result()
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.install / self.commit
        self.assertEqual((target / "INSTALL_COMMIT").read_text().strip(), self.commit)
        self.assertEqual((target / "INSTALL_VERSION").read_text().strip(), "9.9.9")

        committed_paths = run(
            "git",
            "ls-tree",
            "-r",
            "--name-only",
            self.commit,
            "adaptive_coordinator",
            cwd=self.repo,
        ).stdout.splitlines()
        installed_paths = sorted(
            path.relative_to(target).as_posix()
            for path in (target / "adaptive_coordinator").rglob("*")
            if path.is_file()
        )
        self.assertEqual(installed_paths, committed_paths)
        for path in committed_paths:
            committed = run("git", "show", f"{self.commit}:{path}", cwd=self.repo).stdout.encode()
            installed = (target / path).read_bytes()
            self.assertEqual(
                hashlib.sha256(installed).digest(),
                hashlib.sha256(committed).digest(),
                path,
            )
        self.assertFalse((target / "adaptive_coordinator" / "untracked_probe.py").exists())


if __name__ == "__main__":
    unittest.main()
