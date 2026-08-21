#!/usr/bin/env python3
"""Cross-platform Codex -> OpenCode CLI orchestrator for Linux and macOS."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


MIN_OPENCODE_VERSION = (1, 1, 1)
ALLOWED_WORKER_TOOLS = {
    "read",
    "glob",
    "grep",
    "list",
    "lsp",
    "edit",
    "write",
    "patch",
    "apply_patch",
    "todowrite",
    "todoread",
}


class OrchestratorError(RuntimeError):
    """An expected configuration or execution failure."""


@dataclass
class ProcessResult:
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    duration_seconds: float


def run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    env_overrides: dict[str, str] | None = None,
) -> ProcessResult:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)

    popen_options: dict[str, Any] = {
        "cwd": str(cwd),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "posix":
        popen_options["start_new_session"] = True
    elif os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    started_at = time.monotonic()
    process = subprocess.Popen(list(command), **popen_options)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        exit_code = process.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        stdout, stderr = process.communicate()
        exit_code = 124

    return ProcessResult(
        exit_code=exit_code,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=round(time.monotonic() - started_at, 3),
    )


def write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")


def write_json(path: Path, value: Any) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise OrchestratorError(f"'{field_name}' must be a JSON array of strings.")
    if not all(isinstance(item, str) for item in value):
        raise OrchestratorError(f"'{field_name}' must contain only strings.")
    return value


def resolve_executable(value: str | None) -> str:
    requested = value or "opencode"
    if "/" in requested or "\\" in requested:
        candidate = Path(requested).expanduser().resolve()
        if not candidate.is_file():
            raise OrchestratorError(f"OpenCode executable not found: {candidate}")
        return str(candidate)

    candidate = shutil.which(requested)
    if not candidate:
        raise OrchestratorError(
            "OpenCode is not on PATH. Install it or pass --opencode /absolute/path/to/opencode."
        )
    return candidate


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def is_wsl_windows_launcher(executable: str) -> bool:
    """Detect a Windows .exe npm launcher exposed to Linux through WSL interop."""
    if os.name != "posix" or not Path("/proc/sys/kernel/osrelease").is_file():
        return False
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").lower()
        launcher = Path(executable).read_bytes()[:4096].lower()
    except OSError:
        return False
    return "microsoft" in release and b".exe" in launcher


def path_for_opencode(project: Path, executable: str) -> tuple[str, bool]:
    """Translate a WSL path only when OpenCode itself is a Windows launcher."""
    if not is_wsl_windows_launcher(executable):
        return str(project), False

    wslpath = shutil.which("wslpath")
    if not wslpath:
        raise OrchestratorError(
            "Windows OpenCode was detected inside WSL, but wslpath is unavailable. "
            "Install OpenCode natively inside Linux or pass a native binary with --opencode."
        )
    translated = run_process(
        [wslpath, "-w", str(project)], cwd=project, timeout_seconds=30
    )
    if translated.exit_code != 0 or not translated.stdout.strip():
        raise OrchestratorError(
            "Could not translate the WSL project path for Windows OpenCode: "
            f"{translated.stderr.strip()}"
        )
    return translated.stdout.strip(), True


def git_result(project: Path, *arguments: str) -> ProcessResult | None:
    git = shutil.which("git")
    if not git:
        return None
    return run_process(
        [git, "-C", str(project), *arguments],
        cwd=project,
        timeout_seconds=30,
    )


def format_list_section(heading: str, items: list[str], empty_text: str) -> str:
    lines = [f"- {item}" for item in items] if items else [empty_text]
    return f"{heading}\n" + "\n".join(lines)


def parse_events(text: str) -> tuple[str | None, str, list[dict[str, str]], list[str]]:
    session_id: str | None = None
    messages: list[str] = []
    tools: list[dict[str, str]] = []
    malformed: list[str] = []

    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed.append(line)
            continue

        if event.get("sessionID"):
            session_id = str(event["sessionID"])
        part = event.get("part") or {}
        if part.get("type") == "text" and "text" in part:
            messages.append(str(part["text"]))
        if part.get("type") == "tool" and part.get("tool"):
            state = part.get("state") or {}
            tools.append(
                {
                    "tool": str(part["tool"]),
                    "status": str(state.get("status") or "unknown"),
                }
            )

    return session_id, "\n".join(messages), tools, malformed


def limit_feedback(text: str, maximum_length: int = 12000) -> str:
    if len(text) <= maximum_length:
        return text
    return text[:maximum_length] + "\n... [output truncated by orchestrator]"


def verification_command(command: str) -> list[str]:
    if os.name == "nt":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            raise OrchestratorError("PowerShell is required to run verification on Windows.")
        return [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]

    shell = "/bin/sh"
    if not Path(shell).is_file():
        resolved = shutil.which("sh")
        if not resolved:
            raise OrchestratorError("A POSIX sh executable is required for verification commands.")
        shell = resolved
    return [shell, "-lc", command]


def load_task(args: argparse.Namespace) -> dict[str, Any]:
    spec: dict[str, Any] = {}
    if args.task_file:
        task_path = Path(args.task_file).expanduser().resolve()
        if not task_path.is_file():
            raise OrchestratorError(f"Task file not found: {task_path}")
        try:
            loaded = json.loads(task_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise OrchestratorError(f"Invalid task JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise OrchestratorError("Task JSON must contain an object at its root.")
        spec = loaded

    task = args.task if args.task is not None else spec.get("task")
    if not isinstance(task, str) or not task.strip():
        raise OrchestratorError("A non-empty task is required.")

    scope = args.scope if args.scope is not None else string_list(spec.get("scope"), "scope")
    constraints = (
        args.constraint
        if args.constraint is not None
        else string_list(spec.get("constraints"), "constraints")
    )
    acceptance = (
        args.acceptance
        if args.acceptance is not None
        else string_list(spec.get("acceptance"), "acceptance")
    )
    verify_commands = (
        args.verify
        if args.verify is not None
        else string_list(spec.get("verifyCommands"), "verifyCommands")
    )
    model = args.model if args.model is not None else spec.get("model")
    if model is not None and not isinstance(model, str):
        raise OrchestratorError("'model' must be a string.")

    max_rounds = args.max_rounds if args.max_rounds is not None else spec.get("maxRounds", 2)
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int):
        raise OrchestratorError("'maxRounds' must be an integer.")
    if not 1 <= max_rounds <= 10:
        raise OrchestratorError("maxRounds must be between 1 and 10.")

    return {
        "task": task.strip(),
        "scope": scope,
        "constraints": constraints,
        "acceptance": acceptance,
        "verify_commands": verify_commands,
        "model": model or "",
        "max_rounds": max_rounds,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Let Codex delegate a bounded implementation task to OpenCode and verify it independently."
    )
    parser.add_argument("--project", required=True, help="Target project directory")
    task_group = parser.add_mutually_exclusive_group(required=True)
    task_group.add_argument("--task", help="Inline task goal")
    task_group.add_argument("--task-file", help="JSON task specification")
    parser.add_argument("--scope", action="append", help="Allowed/expected scope; repeatable")
    parser.add_argument("--constraint", action="append", help="Constraint; repeatable")
    parser.add_argument("--acceptance", action="append", help="Acceptance criterion; repeatable")
    parser.add_argument("--verify", action="append", help="Independent verification command; repeatable")
    parser.add_argument("--model", help="OpenCode model in provider/model format")
    parser.add_argument("--max-rounds", type=int, help="Maximum implementation rounds (1-10)")
    parser.add_argument("--worker-timeout", type=int, default=900, help="Seconds per OpenCode round")
    parser.add_argument("--verify-timeout", type=int, default=300, help="Seconds per verification command")
    parser.add_argument("--allow-dirty", action="store_true", help="Allow pre-existing Git changes")
    parser.add_argument("--dry-run", action="store_true", help="Create artifacts without starting OpenCode")
    parser.add_argument("--artifact-root", help="Artifact directory; defaults to runs/ beside this script")
    parser.add_argument("--opencode", help="OpenCode executable name or absolute path")
    return parser


def run(args: argparse.Namespace) -> int:
    if not 30 <= args.worker_timeout <= 7200:
        raise OrchestratorError("worker-timeout must be between 30 and 7200 seconds.")
    if not 1 <= args.verify_timeout <= 3600:
        raise OrchestratorError("verify-timeout must be between 1 and 3600 seconds.")

    task = load_task(args)
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        raise OrchestratorError(f"Project is not a directory: {project}")

    opencode = resolve_executable(args.opencode)
    opencode_project, wsl_windows_bridge = path_for_opencode(project, opencode)
    version_result = run_process(
        [opencode, "--version"], cwd=project, timeout_seconds=30
    )
    if version_result.exit_code != 0:
        raise OrchestratorError(f"OpenCode preflight failed: {version_result.stderr.strip()}")
    opencode_version = version_result.stdout.strip()
    parsed_version = parse_version(opencode_version)
    if parsed_version and parsed_version < MIN_OPENCODE_VERSION:
        raise OrchestratorError(
            f"OpenCode 1.1.1 or newer is required for the permission model; found {opencode_version}."
        )
    if not parsed_version:
        print(f"WARNING: could not parse OpenCode version: {opencode_version}", file=sys.stderr)

    git_probe = git_result(project, "rev-parse", "--is-inside-work-tree")
    is_git_repository = bool(
        git_probe
        and git_probe.exit_code == 0
        and git_probe.stdout.strip() == "true"
    )
    git_status_before = ""
    if is_git_repository:
        status_result = git_result(project, "status", "--porcelain=v1")
        if not status_result or status_result.exit_code != 0:
            detail = status_result.stderr.strip() if status_result else "git not available"
            raise OrchestratorError(f"Could not read git status: {detail}")
        git_status_before = status_result.stdout.rstrip()
        if git_status_before and not args.allow_dirty:
            raise OrchestratorError(
                "Project has existing changes. Re-run with --allow-dirty only after Codex "
                f"has reviewed them.\n{git_status_before}"
            )

    artifact_root = (
        Path(args.artifact_root).expanduser().resolve()
        if args.artifact_root
        else Path(__file__).resolve().parent / "runs"
    )
    run_id = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    run_directory = artifact_root / run_id
    run_directory.mkdir(parents=True, exist_ok=False)

    permission: dict[str, Any] = {
        "*": "deny",
        "read": {
            "*": "allow",
            "*.env": "deny",
            "*.env.*": "deny",
            "*.env.example": "allow",
        },
        "edit": "allow",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "lsp": "allow",
        "todowrite": "allow",
        "bash": "deny",
        "task": "deny",
        "skill": "deny",
        "webfetch": "deny",
        "websearch": "deny",
        "question": "deny",
        "external_directory": "deny",
        "doom_loop": "deny",
    }
    agent_name = f"codex-worker-{run_id.lower()}"
    worker_prompt = """You are an implementation worker controlled by a Codex orchestrator.

Follow the task packet exactly. Inspect existing code before editing. Make the smallest complete change that satisfies the acceptance criteria, preserve unrelated work, and do not create commits. You are intentionally unable to run shell commands, access the network, launch subagents, load skills, or touch files outside the project. Codex runs verification independently and will return exact failures in a later round if needed.

Never claim that a command or test passed unless the task packet explicitly says Codex already ran it. End with these headings: STATUS, SUMMARY, FILES CHANGED, CHECKS NOT RUN, RISKS.
"""
    inline_config = {
        "$schema": "https://opencode.ai/config.json",
        "agent": {
            agent_name: {
                "description": "Restricted implementation worker controlled and verified by Codex",
                "mode": "primary",
                "steps": 40,
                "prompt": worker_prompt,
                "permission": permission,
            }
        },
    }
    inline_config_json = json.dumps(inline_config, ensure_ascii=False, separators=(",", ":"))

    task_packet = "\n\n".join(
        [
            f"# CODEX TASK PACKET\nRun ID: {run_id}\nProject: {opencode_project}",
            f"## Goal\n{task['task']}",
            format_list_section(
                "## Allowed scope",
                task["scope"],
                "- Current project only; infer the smallest relevant file set.",
            ),
            format_list_section(
                "## Constraints",
                task["constraints"],
                "- Preserve unrelated behavior and existing user changes.",
            ),
            format_list_section(
                "## Acceptance criteria",
                task["acceptance"],
                "- Implement the stated goal completely and report what changed.",
            ),
            format_list_section(
                "## Verification owned by Codex",
                task["verify_commands"],
                "- No command was supplied; Codex will inspect the resulting diff.",
            ),
            "Implement now. Do not merely propose a patch.",
        ]
    )

    write_text(run_directory / "task-packet.md", task_packet + "\n")
    write_json(run_directory / "restricted-opencode-config.json", inline_config)
    write_json(
        run_directory / "metadata.json",
        {
            "runId": run_id,
            "createdAt": datetime.now().astimezone().isoformat(),
            "project": str(project),
            "openCodeProject": opencode_project,
            "wslWindowsBridge": wsl_windows_bridge,
            "platform": platform.platform(),
            "pythonVersion": platform.python_version(),
            "openCodeVersion": opencode_version,
            "isGitRepository": is_git_repository,
            "allowDirty": args.allow_dirty,
            "gitStatusBefore": git_status_before,
            "model": task["model"],
            "maxRounds": task["max_rounds"],
            "workerTimeoutSec": args.worker_timeout,
            "verifyTimeoutSec": args.verify_timeout,
            "verificationShell": "PowerShell" if os.name == "nt" else "/bin/sh -lc",
        },
    )

    print(f"Run:       {run_id}")
    print(f"Project:   {project}")
    print(f"Artifacts: {run_directory}")
    print(
        "Boundary:  OpenCode may read/edit only inside the project; shell, web, "
        "subagents, skills, secrets, and external paths are denied."
    )

    if args.dry_run:
        result = {
            "status": "dry-run",
            "runId": run_id,
            "project": str(project),
            "artifactPath": str(run_directory),
            "taskPacket": str(run_directory / "task-packet.md"),
        }
        print("Dry run complete. OpenCode was not started.")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    open_code_environment = {
        "OPENCODE_CONFIG_CONTENT": inline_config_json,
        "OPENCODE_AUTO_SHARE": "false",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
        "OPENCODE_DISABLE_CLAUDE_CODE": "true",
    }
    if wsl_windows_bridge:
        shared_names = list(open_code_environment)
        existing_wslenv = [
            item for item in os.environ.get("WSLENV", "").split(":") if item
        ]
        open_code_environment["WSLENV"] = ":".join(
            dict.fromkeys([*shared_names, *existing_wslenv])
        )
    session_id: str | None = None
    round_summaries: list[dict[str, Any]] = []
    verification_history: list[dict[str, Any]] = []
    overall_status = "failed"
    next_prompt = task_packet

    for round_number in range(1, task["max_rounds"] + 1):
        print(f"\n[Round {round_number}/{task['max_rounds']}] OpenCode is working...", flush=True)
        open_code_arguments = [
            opencode,
            "run",
            "--pure",
            "--format",
            "json",
            "--dir",
            opencode_project,
            "--agent",
            agent_name,
            "--title",
            f"codex:{run_id}",
        ]
        if task["model"]:
            open_code_arguments.extend(["--model", task["model"]])
        if session_id:
            open_code_arguments.extend(["--session", session_id])
        open_code_arguments.append(next_prompt)

        worker_result = run_process(
            open_code_arguments,
            cwd=project,
            timeout_seconds=args.worker_timeout,
            env_overrides=open_code_environment,
        )
        events_path = run_directory / f"round-{round_number:02d}-events.ndjson"
        stderr_path = run_directory / f"round-{round_number:02d}-stderr.txt"
        write_text(events_path, worker_result.stdout)
        write_text(stderr_path, worker_result.stderr)

        parsed_session, worker_message, tools, malformed = parse_events(worker_result.stdout)
        if parsed_session:
            session_id = parsed_session
        if worker_message:
            print(worker_message)
        if malformed:
            print(
                f"WARNING: OpenCode emitted {len(malformed)} non-JSON line(s); inspect {events_path}.",
                file=sys.stderr,
            )

        round_summaries.append(
            {
                "round": round_number,
                "exitCode": worker_result.exit_code,
                "timedOut": worker_result.timed_out,
                "durationSeconds": worker_result.duration_seconds,
                "sessionId": session_id,
                "workerMessage": worker_message,
                "tools": tools,
                "eventsPath": str(events_path),
                "stderrPath": str(stderr_path),
            }
        )
        permission_violations = [
            record
            for record in tools
            if record["status"] == "completed"
            and record["tool"] not in ALLOWED_WORKER_TOOLS
        ]
        if permission_violations:
            round_summaries[-1]["permissionViolations"] = permission_violations
            overall_status = "permission-violation"
            names = ", ".join(record["tool"] for record in permission_violations)
            print(
                f"ERROR: OpenCode completed forbidden tool calls: {names}. "
                "Verification was not accepted.",
                file=sys.stderr,
            )
            break
        if worker_result.exit_code != 0:
            reason = (
                f"OpenCode timed out after {args.worker_timeout} seconds."
                if worker_result.timed_out
                else f"OpenCode exited with code {worker_result.exit_code}."
            )
            print(f"WARNING: {reason}", file=sys.stderr)
            if worker_result.stderr.strip():
                print(limit_feedback(worker_result.stderr, 4000), file=sys.stderr)
            break

        if not task["verify_commands"]:
            print(
                "WARNING: no verification command supplied; result is needs-review, not passed.",
                file=sys.stderr,
            )
            overall_status = "needs-review"
            break

        current_verification: list[dict[str, Any]] = []
        all_passed = True
        for verify_index, command in enumerate(task["verify_commands"], start=1):
            print(f"[Verify {verify_index}/{len(task['verify_commands'])}] {command}")
            verify_result = run_process(
                verification_command(command),
                cwd=project,
                timeout_seconds=args.verify_timeout,
            )
            verify_log_path = (
                run_directory / f"round-{round_number:02d}-verify-{verify_index:02d}.txt"
            )
            write_text(
                verify_log_path,
                "\n".join(
                    [
                        f"COMMAND: {command}",
                        f"EXIT CODE: {verify_result.exit_code}",
                        f"TIMED OUT: {verify_result.timed_out}",
                        f"DURATION SECONDS: {verify_result.duration_seconds}",
                        "",
                        "STDOUT:",
                        verify_result.stdout,
                        "",
                        "STDERR:",
                        verify_result.stderr,
                    ]
                ),
            )
            passed = verify_result.exit_code == 0
            all_passed = all_passed and passed
            record = {
                "round": round_number,
                "command": command,
                "exitCode": verify_result.exit_code,
                "timedOut": verify_result.timed_out,
                "durationSeconds": verify_result.duration_seconds,
                "passed": passed,
                "logPath": str(verify_log_path),
                "stdout": verify_result.stdout,
                "stderr": verify_result.stderr,
            }
            current_verification.append(record)
            verification_history.append(record)
            print("  PASS" if passed else "  FAIL")

        if all_passed:
            overall_status = "passed"
            break

        if round_number < task["max_rounds"]:
            failure_blocks = []
            for record in current_verification:
                if record["passed"]:
                    continue
                failure_blocks.append(
                    "\n".join(
                        [
                            "### Failed command",
                            record["command"],
                            f"Exit code: {record['exitCode']}; timed out: {record['timedOut']}",
                            "STDOUT:",
                            limit_feedback(record["stdout"]),
                            "STDERR:",
                            limit_feedback(record["stderr"]),
                        ]
                    )
                )
            next_prompt = "\n\n".join(
                [
                    "# CODEX VERIFICATION FEEDBACK",
                    f"Round {round_number} failed independent verification.",
                    *failure_blocks,
                    "Inspect the current files, fix the root cause within the original task scope, "
                    "and report the updated files. Do not merely explain the failure.",
                ]
            )

    git_status_after = ""
    git_diff_stat = ""
    if is_git_repository:
        status_after_result = git_result(project, "status", "--short")
        diff_stat_result = git_result(project, "diff", "--stat")
        if status_after_result and status_after_result.exit_code == 0:
            git_status_after = status_after_result.stdout.rstrip()
        if diff_stat_result and diff_stat_result.exit_code == 0:
            git_diff_stat = diff_stat_result.stdout.rstrip()

    public_verification = [
        {key: value for key, value in record.items() if key not in {"stdout", "stderr"}}
        for record in verification_history
    ]
    summary = {
        "status": overall_status,
        "runId": run_id,
        "completedAt": datetime.now().astimezone().isoformat(),
        "project": str(project),
        "artifactPath": str(run_directory),
        "openCodeSessionId": session_id,
        "rounds": round_summaries,
        "verification": public_verification,
        "gitStatusBefore": git_status_before,
        "gitStatusAfter": git_status_after,
        "gitDiffStat": git_diff_stat,
    }
    summary_path = run_directory / "summary.json"
    write_json(summary_path, summary)

    print(f"\nResult: {overall_status}")
    if git_status_after:
        print(f"Changed files:\n{git_status_after}")
    print(f"Summary: {summary_path}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if overall_status == "passed" else 1


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run(args)
    except OrchestratorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ERROR: operating system failure: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
