#!/usr/bin/env python3
"""Install the Codex Skill and optionally register its local MCP server."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Mapping, Sequence


SKILL_NAME = "codex-opencode-orchestrator"
MCP_SERVER_NAME = "codex-opencode"
SOURCE_ROOT = Path(__file__).resolve().parent
INSTALL_MANIFEST = (
    "SKILL.md",
    "agents/openai.yaml",
    "README.md",
    "VERIFICATION.md",
    "task.example.json",
    "codex_opencode.py",
    "codex-opencode",
    "Invoke-OpenCodeWorker.ps1",
    "mcp_backend.py",
    "mcp_server.py",
    "requirements-mcp.txt",
)
MCP_POLICY = (
    ("startup_timeout_sec", "30"),
    ("tool_timeout_sec", "7200"),
    ("default_tools_approval_mode", '"writes"'),
)
MCP_TOOL_APPROVALS = {
    "prepare_opencode_task": "approve",
    "run_opencode_task": "prompt",
    "get_opencode_run": "approve",
    "list_opencode_runs": "approve",
}


class InstallError(RuntimeError):
    """A user-facing installation failure."""


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def skill_destination(
    project: Path | str | None = None,
    *,
    home: Path | str | None = None,
) -> Path:
    """Return the current Codex discovery path for this Skill."""

    if project is not None:
        project_root = Path(project).expanduser().resolve()
        if not project_root.is_dir():
            raise InstallError(f"project does not exist: {project_root}")
        return project_root / ".agents" / "skills" / SKILL_NAME
    user_home = Path(home).expanduser() if home is not None else Path.home()
    return user_home / ".agents" / "skills" / SKILL_NAME


def codex_config_path(
    *,
    home: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the Codex config used by `codex mcp`."""

    environment = os.environ if environ is None else environ
    configured_home = environment.get("CODEX_HOME")
    if configured_home:
        return Path(configured_home).expanduser() / "config.toml"
    user_home = Path(home).expanduser() if home is not None else Path.home()
    return user_home / ".codex" / "config.toml"


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _validated_destination(source: Path, destination: Path) -> tuple[Path, bool]:
    source = source.resolve()
    if not (source / "SKILL.md").is_file():
        raise InstallError(f"source is not a Codex Skill: {source}")
    for relative in INSTALL_MANIFEST:
        if not (source / relative).is_file():
            raise InstallError(f"installation source is missing: {relative}")

    # Resolve the parent for containment checks, but preserve the leaf itself so
    # --force replaces a destination symlink rather than its target.
    destination = destination.expanduser().absolute()
    safety_destination = destination.parent.resolve() / destination.name
    if safety_destination == source or source in safety_destination.parents:
        raise InstallError(
            f"destination cannot be inside the source repository: {destination}"
        )
    return destination, destination.exists() or destination.is_symlink()


def _copy_manifest(source: Path, destination: Path) -> None:
    for relative in INSTALL_MANIFEST:
        source_file = source / relative
        destination_file = destination / relative
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination_file)


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def install_mcp_dependencies(
    skill_root: Path,
    *,
    runner: CommandRunner = subprocess.run,
) -> Path:
    """Create an isolated runtime for the MCP server."""

    venv = skill_root / ".venv"
    create = runner(
        [sys.executable, "-m", "venv", str(venv)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if create.returncode != 0:
        detail = (create.stderr or create.stdout).strip()
        raise InstallError(f"could not create MCP virtual environment: {detail}")

    python = _venv_python(venv)
    install = runner(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(skill_root / "requirements-mcp.txt"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if install.returncode != 0:
        detail = (install.stderr or install.stdout).strip()
        raise InstallError(f"could not install MCP dependencies: {detail}")
    return python


def add_mcp_policy(config_text: str, server_name: str = MCP_SERVER_NAME) -> str:
    """Add timeouts and per-tool approvals to a CLI-generated server table."""

    newline = "\r\n" if "\r\n" in config_text else "\n"
    lines = config_text.splitlines()
    header = f"[mcp_servers.{server_name}]"
    try:
        section_start = next(
            index for index, line in enumerate(lines) if line.strip() == header
        )
    except StopIteration as exc:
        raise InstallError(f"Codex did not create the expected MCP table: {header}") from exc

    section_end = len(lines)
    for index in range(section_start + 1, len(lines)):
        if lines[index].strip().startswith("["):
            section_end = index
            break

    present_keys = {
        line.split("=", 1)[0].strip()
        for line in lines[section_start + 1 : section_end]
        if "=" in line
    }
    policy_lines = [
        f"{key} = {value}" for key, value in MCP_POLICY if key not in present_keys
    ]
    tool_lines: list[str] = []
    for tool, mode in MCP_TOOL_APPROVALS.items():
        tool_lines.extend(
            [
                "",
                f"[mcp_servers.{server_name}.tools.{tool}]",
                f'approval_mode = "{mode}"',
            ]
        )
    updated = [
        *lines[:section_end],
        *policy_lines,
        *tool_lines,
        *lines[section_end:],
    ]
    return newline.join(updated).rstrip() + newline


def _run_codex(
    command: list[str],
    *,
    environment: Mapping[str, str],
    runner: CommandRunner,
) -> subprocess.CompletedProcess[str]:
    return runner(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(environment),
    )


def _codex_executable() -> str:
    # On Windows, CreateProcess may select the extensionless npm shim when a
    # bare name is used. shutil.which honors PATHEXT and returns codex.CMD.
    executable = shutil.which("codex")
    if not executable:
        raise InstallError("Codex CLI was not found in PATH")
    return executable


def register_mcp(
    skill_root: Path,
    python: Path,
    *,
    force: bool,
    home: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    runner: CommandRunner = subprocess.run,
) -> None:
    """Register the STDIO server with Codex and roll config back on failure."""

    environment = dict(os.environ if environ is None else environ)
    config = codex_config_path(home=home, environ=environment)
    config.parent.mkdir(parents=True, exist_ok=True)
    config_before = config.read_bytes() if config.is_file() else None
    codex = _codex_executable()

    probe = _run_codex(
        [codex, "mcp", "get", MCP_SERVER_NAME, "--json"],
        environment=environment,
        runner=runner,
    )
    if probe.returncode == 0 and not force:
        raise InstallError(
            f"MCP server already exists: {MCP_SERVER_NAME}; rerun with --force to replace it"
        )

    try:
        if probe.returncode == 0:
            removed = _run_codex(
                [codex, "mcp", "remove", MCP_SERVER_NAME],
                environment=environment,
                runner=runner,
            )
            if removed.returncode != 0:
                raise InstallError(
                    f"could not remove existing MCP server: {removed.stderr.strip()}"
                )

        added = _run_codex(
            [
                codex,
                "mcp",
                "add",
                MCP_SERVER_NAME,
                "--",
                str(python),
                str(skill_root / "mcp_server.py"),
            ],
            environment=environment,
            runner=runner,
        )
        if added.returncode != 0:
            raise InstallError(f"could not add MCP server: {added.stderr.strip()}")
        if not config.is_file():
            raise InstallError(f"Codex MCP config was not created: {config}")

        configured = add_mcp_policy(config.read_text(encoding="utf-8"))
        temporary = config.with_name(f".{config.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(configured, encoding="utf-8", newline="")
        temporary.replace(config)

        verified = _run_codex(
            [codex, "mcp", "get", MCP_SERVER_NAME, "--json"],
            environment=environment,
            runner=runner,
        )
        if verified.returncode != 0:
            raise InstallError(
                f"Codex rejected the MCP configuration: {verified.stderr.strip()}"
            )
    except Exception:
        config.parent.mkdir(parents=True, exist_ok=True)
        if config_before is None:
            if config.exists():
                config.unlink()
        else:
            config.write_bytes(config_before)
        raise


def install_package(
    source: Path,
    destination: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    with_mcp: bool = False,
    home: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    runner: CommandRunner = subprocess.run,
) -> None:
    """Stage and atomically install the Skill, optionally including MCP setup."""

    source = source.resolve()
    destination, destination_exists = _validated_destination(source, destination)
    if destination_exists and not force:
        raise InstallError(
            f"destination already exists: {destination}\n"
            "rerun with --force only after reviewing the installed copy"
        )
    if dry_run:
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    stage_parent = Path(
        tempfile.mkdtemp(prefix=f".{SKILL_NAME}-stage-", dir=destination.parent)
    )
    staged_skill = stage_parent / SKILL_NAME
    backup = destination.parent / f".{SKILL_NAME}-backup-{uuid.uuid4().hex}"
    moved_existing = False
    installed_new = False

    try:
        staged_skill.mkdir()
        _copy_manifest(source, staged_skill)
        mcp_python = (
            install_mcp_dependencies(staged_skill, runner=runner)
            if with_mcp
            else None
        )

        if destination_exists:
            destination.replace(backup)
            moved_existing = True
        staged_skill.replace(destination)
        installed_new = True

        if with_mcp:
            assert mcp_python is not None
            relative_python = mcp_python.relative_to(staged_skill)
            register_mcp(
                destination,
                destination / relative_python,
                force=force,
                home=home,
                environ=environ,
                runner=runner,
            )
    except Exception:
        if installed_new:
            _remove_path(destination)
        if moved_existing:
            backup.replace(destination)
        raise
    finally:
        _remove_path(stage_parent)

    if moved_existing:
        _remove_path(backup)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install codex-opencode-orchestrator for Codex."
    )
    parser.add_argument(
        "--project",
        type=Path,
        help="install as a project Skill instead of into $HOME/.agents/skills",
    )
    parser.add_argument(
        "--with-mcp",
        action="store_true",
        help="also create an isolated MCP runtime and register it with Codex",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing Skill and MCP registration",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the destination and planned actions without changing files",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.project is not None and args.with_mcp:
        parser.error("--with-mcp is only supported for the user-level Skill install")

    try:
        destination = skill_destination(args.project)
        install_package(
            SOURCE_ROOT,
            destination,
            force=args.force,
            dry_run=args.dry_run,
            with_mcp=args.with_mcp,
        )
    except (InstallError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    action = "Would install" if args.dry_run else "Installed"
    scope = "project" if args.project is not None else "user"
    print(f"{action} {SKILL_NAME} as a {scope} Codex Skill")
    print(f"Destination: {destination}")
    if args.with_mcp:
        mcp_action = "Would register" if args.dry_run else "Registered"
        print(f"{mcp_action} MCP server: {MCP_SERVER_NAME}")
    if not args.dry_run:
        print(f"Invoke with: ${SKILL_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
