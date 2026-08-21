"""Standard-library backend used by the Codex OpenCode MCP server."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent
ORCHESTRATOR = ROOT / "codex_opencode.py"
RUN_ID_PATTERN = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")
RUN_LINE_PATTERN = re.compile(r"^Run:\s+(\d{8}-\d{6}-[0-9a-f]{8})\s*$", re.MULTILINE)
MAX_LIST_ITEMS = 50
MAX_TEXT_LENGTH = 10_000
_RUN_LOCK = threading.Lock()


class BackendError(RuntimeError):
    """A safe error that can be returned through MCP."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def artifact_root() -> Path:
    configured = os.environ.get("CODEX_OPENCODE_ARTIFACT_ROOT")
    return Path(configured).expanduser().resolve() if configured else ROOT / "runs"


def _validate_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BackendError(f"{field} must be a non-empty string")
    if "\x00" in value or len(value) > MAX_TEXT_LENGTH:
        raise BackendError(f"{field} is invalid or too long")
    return value.strip()


def _validate_list(
    values: list[str],
    field: str,
    *,
    required: bool = False,
    maximum: int = MAX_LIST_ITEMS,
    item_maximum_length: int = MAX_TEXT_LENGTH,
) -> list[str]:
    if not isinstance(values, list):
        raise BackendError(f"{field} must be a list of strings")
    cleaned = [_validate_text(value, f"{field} item") for value in values]
    if required and not cleaned:
        raise BackendError(f"{field} must contain at least one item")
    if len(cleaned) > maximum:
        raise BackendError(f"{field} cannot contain more than {maximum} items")
    if any(len(value) > item_maximum_length for value in cleaned):
        raise BackendError(
            f"{field} items cannot exceed {item_maximum_length} characters"
        )
    return cleaned


def build_task_spec(
    *,
    task: str,
    scope: list[str],
    constraints: list[str],
    acceptance: list[str],
    verify_commands: list[str],
    model: str = "",
    max_rounds: int = 2,
) -> dict[str, Any]:
    """Validate the MCP arguments and build the core task file."""

    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int):
        raise BackendError("max_rounds must be an integer")
    if not 1 <= max_rounds <= 2:
        raise BackendError("max_rounds must be between 1 and 2 for MCP runs")
    return {
        "task": _validate_text(task, "task"),
        "scope": _validate_list(scope, "scope", required=True),
        "constraints": _validate_list(constraints, "constraints"),
        "acceptance": _validate_list(acceptance, "acceptance", required=True),
        "verifyCommands": _validate_list(
            verify_commands,
            "verify_commands",
            required=True,
            maximum=4,
            item_maximum_length=2_000,
        ),
        "model": _validate_text(model, "model") if model else "",
        "maxRounds": max_rounds,
    }


def _public_summary(summary: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "status",
        "runId",
        "completedAt",
        "project",
        "artifactPath",
        "verification",
        "gitStatusBefore",
        "gitStatusAfter",
        "gitDiffStat",
    }
    public = {key: value for key, value in summary.items() if key in allowed}
    public["rounds"] = [
        {
            key: value
            for key, value in round_record.items()
            if key
            in {
                "round",
                "exitCode",
                "timedOut",
                "durationSeconds",
                "tools",
                "permissionViolations",
            }
        }
        for round_record in summary.get("rounds", [])
        if isinstance(round_record, dict)
    ]
    return public


def _run_directory(run_id: str, root: Path | None = None) -> Path:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise BackendError("invalid run_id")
    base = (root or artifact_root()).resolve()
    candidate = base / run_id
    if candidate.parent != base:
        raise BackendError("run_id escapes the artifact directory")
    return candidate


def get_run(run_id: str, *, root: Path | None = None) -> dict[str, Any]:
    """Read safe high-level evidence for one orchestrator run."""

    directory = _run_directory(run_id, root)
    if not directory.is_dir():
        raise BackendError(f"run not found: {run_id}")

    summary_path = directory / "summary.json"
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BackendError(f"summary is unreadable for run: {run_id}") from exc
        if not isinstance(summary, dict):
            raise BackendError(f"summary is invalid for run: {run_id}")
        return {"ok": summary.get("status") == "passed", **_public_summary(summary)}

    metadata_path = directory / "metadata.json"
    if not metadata_path.is_file():
        raise BackendError(f"run has no readable metadata: {run_id}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackendError(f"metadata is unreadable for run: {run_id}") from exc
    if not isinstance(metadata, dict):
        raise BackendError(f"metadata is invalid for run: {run_id}")
    return {
        "ok": True,
        "status": "dry-run",
        "runId": run_id,
        "project": metadata.get("project"),
        "artifactPath": str(directory),
        "taskPacket": str(directory / "task-packet.md"),
        "permissionConfig": str(directory / "restricted-opencode-config.json"),
    }


def list_runs(limit: int = 20, *, root: Path | None = None) -> dict[str, Any]:
    """List recent runs without returning raw event logs or session IDs."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise BackendError("limit must be between 1 and 50")
    base = (root or artifact_root()).resolve()
    if not base.exists():
        return {"runs": []}
    directories = sorted(
        (
            path
            for path in base.iterdir()
            if path.is_dir() and RUN_ID_PATTERN.fullmatch(path.name)
        ),
        key=lambda path: path.name,
        reverse=True,
    )
    records: list[dict[str, Any]] = []
    for directory in directories[:limit]:
        try:
            run = get_run(directory.name, root=base)
        except (BackendError, json.JSONDecodeError):
            records.append({"runId": directory.name, "status": "unreadable"})
            continue
        records.append(
            {
                key: run.get(key)
                for key in ("runId", "status", "project", "artifactPath")
            }
        )
    return {"runs": records}


def invoke_orchestrator(
    *,
    project: str,
    task: str,
    scope: list[str],
    constraints: list[str],
    acceptance: list[str],
    verify_commands: list[str],
    model: str = "",
    max_rounds: int = 2,
    worker_timeout: int = 900,
    verify_timeout: int = 300,
    dry_run: bool,
    runner: Runner = subprocess.run,
    root: Path | None = None,
) -> dict[str, Any]:
    """Run the existing core in an isolated child process."""

    project_path = Path(_validate_text(project, "project")).expanduser().resolve()
    if not project_path.is_dir():
        raise BackendError(f"project is not a directory: {project_path}")
    if not 30 <= worker_timeout <= 900:
        raise BackendError("worker_timeout must be between 30 and 900 seconds")
    if not 1 <= verify_timeout <= 300:
        raise BackendError("verify_timeout must be between 1 and 300 seconds")

    spec = build_task_spec(
        task=task,
        scope=scope,
        constraints=constraints,
        acceptance=acceptance,
        verify_commands=verify_commands,
        model=model,
        max_rounds=max_rounds,
    )
    runs = (root or artifact_root()).resolve()
    runs.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"

    task_file: Path | None = None
    with _RUN_LOCK:
        try:
            handle, temporary_name = tempfile.mkstemp(
                prefix="codex-opencode-mcp-", suffix=".json"
            )
            os.close(handle)
            task_file = Path(temporary_name)
            task_file.write_text(
                json.dumps(spec, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(ORCHESTRATOR),
                "--project",
                str(project_path),
                "--task-file",
                str(task_file),
                "--artifact-root",
                str(runs),
                "--worker-timeout",
                str(worker_timeout),
                "--verify-timeout",
                str(verify_timeout),
            ]
            configured_opencode = os.environ.get("CODEX_OPENCODE_BIN")
            if configured_opencode:
                command.extend(["--opencode", configured_opencode])
            if dry_run:
                command.append("--dry-run")

            total_timeout = (
                max_rounds
                * (worker_timeout + len(verify_commands) * verify_timeout)
                + 120
            )
            result = runner(
                command,
                cwd=str(project_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=total_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"orchestrator exceeded {exc.timeout} seconds") from exc
        finally:
            if task_file is not None and task_file.exists():
                task_file.unlink()

    match = RUN_LINE_PATTERN.search(result.stdout)
    if not match:
        detail = (result.stderr or result.stdout).strip()[-4000:]
        raise BackendError(f"orchestrator did not create a run: {detail}")
    run_id = match.group(1)
    response = get_run(run_id, root=runs)
    response["exitCode"] = result.returncode
    if result.returncode != 0 and response.get("status") == "passed":
        response["ok"] = False
        response["status"] = "inconsistent-exit"
    return response
