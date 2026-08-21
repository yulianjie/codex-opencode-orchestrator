---
name: codex-opencode-orchestrator
description: 将范围明确的本地代码修改委派给受限 OpenCode CLI，由 Codex 保留计划、权限控制、Git 差异审查和独立验收。用于用户明确要求 Codex 指挥或调用 OpenCode 在指定仓库实现、修复或重构代码；不要用于只读分析、开放式自治任务，或要求 OpenCode 执行 Shell、联网、提交或推送的任务。
---

# Codex 指挥 OpenCode

使用本 Skill 自带的编排器，不要把权限控制、事件审计或重试逻辑重新写成临时提示词。把包含本文件的目录视为 `<skill-dir>`，把用户指定的代码仓库视为 `<project>`。

## 保持职责边界

- Codex 负责理解需求、限定任务、检查现有改动、选择验证命令、审查最终差异并决定是否接受。
- OpenCode 只负责在 `<project>` 内检查和编辑代码。编排器禁止它运行 Shell、访问网络、启动子 Agent、加载 Skill、读取 `.env` 类密钥文件或触碰外部目录。
- 仅在用户已授权修改 `<project>` 时正式执行。只读分析、排查或方案讨论不构成修改授权。
- 不让 OpenCode 创建提交或推送。后续提交或发布必须由 Codex 在用户另行授权后执行。
- 不把 `runs/`、会话标识、本机绝对路径或原始事件日志提交到目标仓库。

## 建立任务合同

正式委派前确定以下信息：

- 一个可交付的任务目标。
- 明确的目标仓库绝对路径。
- 允许或预期修改的文件范围。
- 必须保留的兼容性、安全性和禁止事项。
- 可以逐项判断的验收条件。
- 至少一条由编排器独立运行的有效验证命令。

验证命令会在 `<project>` 的系统 Shell 中执行。只使用 Codex 已从仓库脚本或项目说明中确认过的测试、构建、格式或类型检查命令；不要把未经审查的用户文本直接拼接成验证命令。

可以根据仓库说明、现有测试脚本和用户目标补全非关键细节。若目标仓库、修改授权或任务边界仍不明确，先询问用户。若无法形成有意义的验证命令，只执行干跑并说明阻塞，不把 `needs-review` 当作完成。

复杂任务优先使用基于 `task.example.json` 创建的临时任务文件，以避免跨 Shell 转义改变参数含义。不要修改随 Skill 提供的示例文件。

## 执行工作流

1. 读取 `<project>` 中适用的仓库说明，检查 `git status --short` 和相关实现、测试。
2. 形成任务合同。已有未提交改动时先确认其归属和与本任务的重叠范围。
3. 若 `codex-opencode` MCP 可用，调用 `prepare_opencode_task` 执行干跑；审核结果后再调用 `run_opencode_task`。正式执行工具需要用户审批，且不提供脏工作区放行参数。
4. 若 MCP 不可用，使用对应平台入口执行干跑：
   - Linux、macOS、WSL：`python3 "<skill-dir>/codex_opencode.py" --project "<project>" --task-file "<task.json>" --dry-run`
   - Windows：`pwsh -NoProfile -File "<skill-dir>/Invoke-OpenCodeWorker.ps1" -Project "<project>" -TaskFile "<task.json>" -DryRun`
5. 读取干跑输出目录中的 `task-packet.md`、`restricted-opencode-config.json` 和 `metadata.json`，确认目标、范围、验证命令和权限边界正确。
6. 使用相同参数移除干跑选项后正式执行。不要添加 `--allow-dirty` 或 `-AllowDirty`，除非 Codex 已审查现有改动并能可靠区分其归属；MCP 路径始终拒绝脏工作区。
7. 读取 `run_opencode_task` 返回值或命令打印的 `summary.json` 路径，并检查实际成功执行的工具。需要重新读取时调用 `get_opencode_run`，不要依赖模型文字总结。
8. 独立检查 `<project>` 的 `git status --short`、`git diff --stat` 和完整差异，确认修改未越出任务合同。

编排器会在验证失败时把真实错误返回同一个 OpenCode 会话，并在合同允许的轮数内重试。编排器返回非零后，检查证据并报告具体失败；不要无上限地重新运行。

## 接受条件

只有同时满足以下条件才宣布完成：

- `summary.json.status` 为 `passed`。
- 至少一条验证命令成功，且全部验证退出码为 0。
- 没有 `permission-violation`，成功工具事件仅属于允许的项目内读写工具。
- 最终 Git 差异只包含任务合同允许的修改，没有覆盖用户原有工作。
- Codex 已检查实际差异，而不是仅依赖 OpenCode 的文字总结。

交付时说明修改文件、独立验证结果、尚未覆盖的运行环境和需要用户决定的风险。macOS 只有在真实 macOS 环境运行后才能声称完成真实平台验证。

## 按需读取

- 需要完整参数、任务 JSON 字段、状态含义或平台故障排查时，读取 `<skill-dir>/README.md`。
- 需要判断当前实现已经在哪些环境得到验证时，读取 `<skill-dir>/VERIFICATION.md`。
