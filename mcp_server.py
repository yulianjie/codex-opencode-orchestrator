#!/usr/bin/env python3
"""STDIO MCP server exposing the bounded OpenCode orchestrator to Codex."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from mcp_backend import BackendError, get_run, invoke_orchestrator, list_runs


SERVER_INSTRUCTIONS = """Use prepare_opencode_task before run_opencode_task. The restricted OpenCode worker can edit only the requested clean Git project and cannot use shell, web tools, subagents, commits, or pushes. Verification commands run in the host shell, so pass only reviewed repository checks. Call run only after the user authorized changes and reviewed the dry-run contract. Codex must inspect the diff and accept only status=passed. Use get_opencode_run for sanitized evidence; raw events and session IDs stay local."""

mcp = MCPServer("codex-opencode", instructions=SERVER_INSTRUCTIONS)


def _error(exc: BackendError) -> dict[str, Any]:
    return {"ok": False, "status": "rejected", "error": str(exc)}


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
)
def prepare_opencode_task(
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
) -> dict[str, Any]:
    """Create and inspect a dry-run task packet without starting OpenCode."""

    try:
        return invoke_orchestrator(
            project=project,
            task=task,
            scope=scope,
            constraints=constraints,
            acceptance=acceptance,
            verify_commands=verify_commands,
            model=model,
            max_rounds=max_rounds,
            worker_timeout=worker_timeout,
            verify_timeout=verify_timeout,
            dry_run=True,
        )
    except BackendError as exc:
        return _error(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    )
)
def run_opencode_task(
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
) -> dict[str, Any]:
    """Run one authorized task; verify_commands execute in the project's host shell."""

    try:
        return invoke_orchestrator(
            project=project,
            task=task,
            scope=scope,
            constraints=constraints,
            acceptance=acceptance,
            verify_commands=verify_commands,
            model=model,
            max_rounds=max_rounds,
            worker_timeout=worker_timeout,
            verify_timeout=verify_timeout,
            dry_run=False,
        )
    except BackendError as exc:
        return _error(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def get_opencode_run(run_id: str) -> dict[str, Any]:
    """Return sanitized evidence for a run without raw events or session IDs."""

    try:
        return get_run(run_id)
    except BackendError as exc:
        return _error(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def list_opencode_runs(limit: int = 20) -> dict[str, Any]:
    """List recent run IDs, statuses, projects, and artifact directories."""

    try:
        return list_runs(limit)
    except BackendError as exc:
        return _error(exc)


if __name__ == "__main__":
    mcp.run(transport="stdio")
