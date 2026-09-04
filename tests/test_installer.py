from __future__ import annotations

import hashlib
import os
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
        self.skills = root / "skills"
        self.codex_skills = root / "codex-skills"
        self.orca_managed_skills = root / "orca-runtime-home" / "home" / "skills"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "adaptive_coordinator").mkdir()
        shutil.copy2(INSTALLER, self.repo / "scripts" / INSTALLER.name)
        shutil.copytree(ROOT / "skills", self.repo / "skills")
        (self.repo / "adaptive_coordinator" / "__init__.py").write_text('VALUE = "committed"\n')
        (self.repo / "adaptive_coordinator" / "cli.py").write_text(
            "def main():\n"
            "    from pathlib import Path\n"
            "    from adaptive_coordinator import VALUE\n"
            "    print(VALUE)\n"
            "    print((Path(__file__).resolve().parent.parent / 'INSTALL_COMMIT').read_text().strip())\n"
            "    return 0\n"
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
            str(self.skills),
            str(self.codex_skills),
            str(self.orca_managed_skills),
            cwd=self.repo,
            check=False,
        )

    def test_i1_clean_tree_installs(self) -> None:
        result = self.install_result()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.bin / "orca-adaptive").is_symlink())
        skill = self.skills / "orca-adaptive-routing"
        self.assertTrue(skill.is_symlink())
        self.assertIn("allow_implicit_invocation: true", (skill / "agents" / "openai.yaml").read_text())
        codex_skill = self.codex_skills / "orca-adaptive-routing"
        self.assertTrue(codex_skill.is_symlink())
        self.assertEqual(skill.resolve(), codex_skill.resolve())
        orca_skill = self.orca_managed_skills / "orca-adaptive-routing"
        self.assertTrue(orca_skill.is_symlink())
        self.assertEqual(skill.resolve(), orca_skill.resolve())

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

    def test_i6_launcher_ignores_dirty_source_package_from_source_cwd(self) -> None:
        result = self.install_result()
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.repo / "adaptive_coordinator" / "__init__.py").write_text('VALUE = "dirty-source"\n')
        invoked = run(str(self.bin / "orca-adaptive"), cwd=self.repo)
        self.assertEqual(invoked.stdout.splitlines(), ["committed", self.commit])

    def test_i7_existing_unmanaged_skill_is_not_replaced(self) -> None:
        unmanaged = self.skills / "orca-adaptive-routing"
        unmanaged.mkdir(parents=True)
        marker = unmanaged / "KEEP"
        marker.write_text("user-owned\n")
        result = self.install_result()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unmanaged or already-backed-up skill installation", result.stderr)
        self.assertEqual(marker.read_text(), "user-owned\n")
        self.assertFalse((self.install / self.commit).exists())

    def test_i8_existing_named_skill_is_preserved_then_replaced_by_snapshot_link(self) -> None:
        existing = self.skills / "orca-adaptive-routing"
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("---\nname: orca-adaptive-routing\n---\nold\n")
        result = self.install_result()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(existing.is_symlink())
        backup = self.skills / "orca-adaptive-routing.pre-snapshot"
        self.assertEqual((backup / "SKILL.md").read_text(), "---\nname: orca-adaptive-routing\n---\nold\n")
        self.assertIn("Delegate multi-step project", (existing / "SKILL.md").read_text())

    def test_i9_unmanaged_codex_discovery_skill_is_preserved_and_install_fails_closed(self) -> None:
        unmanaged = self.codex_skills / "orca-adaptive-routing"
        unmanaged.mkdir(parents=True)
        marker = unmanaged / "KEEP"
        marker.write_text("codex-owned\n")
        result = self.install_result()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unmanaged or already-backed-up skill installation", result.stderr)
        self.assertEqual(marker.read_text(), "codex-owned\n")
        self.assertFalse((self.install / self.commit).exists())

    def test_i10_unmanaged_orca_runtime_skill_is_preserved_and_install_fails_closed(self) -> None:
        unmanaged = self.orca_managed_skills / "orca-adaptive-routing"
        unmanaged.mkdir(parents=True)
        marker = unmanaged / "KEEP"
        marker.write_text("orca-runtime-owned\n")
        result = self.install_result()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unmanaged or already-backed-up skill installation", result.stderr)
        self.assertEqual(marker.read_text(), "orca-runtime-owned\n")
        self.assertFalse((self.install / self.commit).exists())

    def test_i11_existing_orca_runtime_home_is_auto_detected(self) -> None:
        fake_home = Path(self.temp.name) / "home"
        runtime_home = fake_home / ".local" / "share" / "orca" / "codex-runtime-home" / "home"
        runtime_home.mkdir(parents=True)
        environment = dict(os.environ, HOME=str(fake_home))
        result = subprocess.run(
            [
                "bash", "scripts/install-production.sh", str(self.install), str(self.bin),
                str(self.skills), str(self.codex_skills),
            ],
            cwd=self.repo, check=False, capture_output=True, text=True, env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        auto_skill = runtime_home / "skills" / "orca-adaptive-routing"
        self.assertTrue(auto_skill.is_symlink())
        self.assertEqual(auto_skill.resolve(), (self.skills / "orca-adaptive-routing").resolve())

    def _assert_unmanaged_symlink_rejected(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        foreign = Path(self.temp.name) / f"foreign-{root.name}"
        foreign.mkdir()
        (foreign / "SKILL.md").write_text("---\nname: foreign\n---\n")
        (root / "orca-adaptive-routing").symlink_to(foreign, target_is_directory=True)
        result = self.install_result()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unmanaged skill symlink", result.stderr)
        self.assertEqual((foreign / "SKILL.md").read_text(), "---\nname: foreign\n---\n")
        self.assertFalse((self.install / self.commit).exists())

    def test_i12_unmanaged_shared_skill_symlink_is_rejected(self) -> None:
        self._assert_unmanaged_symlink_rejected(self.skills)

    def test_i13_unmanaged_codex_skill_symlink_is_rejected(self) -> None:
        self._assert_unmanaged_symlink_rejected(self.codex_skills)

    def test_i14_unmanaged_orca_skill_symlink_is_rejected(self) -> None:
        self._assert_unmanaged_symlink_rejected(self.orca_managed_skills)

    def test_i15_exact_snapshot_skill_links_are_idempotent(self) -> None:
        first = self.install_result()
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.install_result()
        self.assertEqual(second.returncode, 0, second.stderr)
        expected = self.install / self.commit / "orca-adaptive-routing-skill"
        for root in (self.skills, self.codex_skills, self.orca_managed_skills):
            self.assertEqual((root / "orca-adaptive-routing").resolve(), expected)


if __name__ == "__main__":
    unittest.main()
