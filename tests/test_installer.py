from __future__ import annotations

import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

import install


class InstallerTests(unittest.TestCase):
    def test_user_and_project_destinations_match_codex_discovery(self):
        home = Path("C:/test-home")
        self.assertEqual(
            install.skill_destination(home=home),
            home / ".agents" / "skills" / install.SKILL_NAME,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            self.assertEqual(
                install.skill_destination(project),
                project / ".agents" / "skills" / install.SKILL_NAME,
            )

    def test_install_copies_only_complete_runtime_manifest(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "skill"
            install.install_package(install.SOURCE_ROOT, destination)
            for relative in install.INSTALL_MANIFEST:
                with self.subTest(relative=relative):
                    self.assertTrue((destination / relative).is_file())
            self.assertFalse((destination / ".git").exists())
            self.assertFalse((destination / "runs").exists())
            self.assertFalse((destination / "tests").exists())

    def test_existing_destination_requires_force_and_force_replaces_it(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "skill"
            destination.mkdir()
            old_file = destination / "old.txt"
            old_file.write_text("keep", encoding="utf-8")
            with self.assertRaises(install.InstallError):
                install.install_package(install.SOURCE_ROOT, destination)
            self.assertEqual(old_file.read_text(encoding="utf-8"), "keep")

            install.install_package(install.SOURCE_ROOT, destination, force=True)
            self.assertTrue((destination / "SKILL.md").is_file())
            self.assertFalse(old_file.exists())

    def test_dry_run_does_not_create_destination(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "skill"
            install.install_package(
                install.SOURCE_ROOT, destination, dry_run=True, with_mcp=True
            )
            self.assertFalse(destination.exists())

    def test_destination_inside_source_is_rejected(self):
        destination = install.SOURCE_ROOT / ".agents" / "skills" / install.SKILL_NAME
        with self.assertRaises(install.InstallError):
            install.install_package(install.SOURCE_ROOT, destination)

    def test_mcp_policy_has_safe_approvals_and_timeouts(self):
        original = """[mcp_servers.codex-opencode]\ncommand = \"python\"\nargs = [\"server.py\"]\n\n[mcp_servers.other]\nurl = \"https://example.test/mcp\"\n"""
        configured = install.add_mcp_policy(original)
        self.assertIn("tool_timeout_sec = 7200", configured)
        self.assertIn('default_tools_approval_mode = "writes"', configured)
        self.assertIn(
            "[mcp_servers.codex-opencode.tools.run_opencode_task]\n"
            'approval_mode = "prompt"',
            configured,
        )
        self.assertIn(
            "[mcp_servers.codex-opencode.tools.get_opencode_run]\n"
            'approval_mode = "approve"',
            configured,
        )
        self.assertLess(
            configured.index("[mcp_servers.codex-opencode.tools"),
            configured.index("[mcp_servers.other]"),
        )

    def test_codex_executable_uses_path_resolution(self):
        expected = "C:/tools/codex.CMD"
        with patch("install.shutil.which", return_value=expected):
            self.assertEqual(install._codex_executable(), expected)
        with patch("install.shutil.which", return_value=None):
            with self.assertRaises(install.InstallError):
                install._codex_executable()

    def test_mcp_registration_failure_restores_skill_and_config(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            destination = root / "home" / ".agents" / "skills" / install.SKILL_NAME
            destination.mkdir(parents=True)
            old_file = destination / "old.txt"
            old_file.write_text("preserve", encoding="utf-8")
            codex_home = root / "codex-home"
            codex_home.mkdir()
            config = codex_home / "config.toml"
            original_config = '[features]\nexample = true\n'
            config.write_text(original_config, encoding="utf-8")
            get_calls = 0

            def fake_runner(command, **_kwargs):
                nonlocal get_calls
                arguments = [str(item) for item in command]
                if "mcp" not in arguments:
                    return subprocess.CompletedProcess(arguments, 0, "", "")
                if "add" in arguments:
                    config.write_text(
                        original_config
                        + '\n[mcp_servers.codex-opencode]\ncommand = "python"\n'
                        + 'args = ["server.py"]\n',
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(arguments, 0, "", "")
                if "get" in arguments:
                    get_calls += 1
                    return subprocess.CompletedProcess(
                        arguments,
                        1,
                        "",
                        "missing" if get_calls == 1 else "invalid config",
                    )
                return subprocess.CompletedProcess(arguments, 0, "", "")

            with patch("install.shutil.which", return_value="codex"):
                with self.assertRaises(install.InstallError):
                    install.install_package(
                        install.SOURCE_ROOT,
                        destination,
                        force=True,
                        with_mcp=True,
                        home=root / "home",
                        environ={"CODEX_HOME": str(codex_home)},
                        runner=fake_runner,
                    )

            self.assertEqual(old_file.read_text(encoding="utf-8"), "preserve")
            self.assertEqual(config.read_text(encoding="utf-8"), original_config)


if __name__ == "__main__":
    unittest.main()
