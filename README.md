# Codex 指挥 OpenCode CLI：Windows / Linux / macOS

这套实现把 Codex 设为“总控与验收者”，把 OpenCode 设为“受限实现工人”。Codex 负责拆任务、执行编排器、独立运行测试、检查 Git 差异并决定是否接受；OpenCode 只在指定项目里读写文件。测试失败时，编排器会把真实输出送回同一个 OpenCode 会话继续修复。

交付包提供两个入口：

- Linux、macOS、WSL：`codex_opencode.py`，以及便捷启动脚本 `codex-opencode`。
- Windows：`Invoke-OpenCodeWorker.ps1`。

二者使用相同的任务 JSON、权限边界、状态含义和证据文件。

## 安全模型

直接运行 `opencode run --auto` 会自动批准所有未显式拒绝的权限，不适合作为默认编排方式。本实现为每次运行动态创建专用 Agent：

- 允许：当前项目内的读取、搜索、编辑和 LSP 查询。
- 拒绝：Shell、联网、子 Agent、Skills、外部目录、`.env`/密钥文件和交互式提问。
- 禁止自动分享、自动提交和自动推送。
- OpenCode 不负责宣布测试通过；验证命令由编排器在独立进程中执行。
- 默认拒绝脏 Git 工作区；只有 Codex 已审核原有改动时才允许显式放行。
- 编排器审计 OpenCode 的 JSON 工具事件；若发现成功执行的未授权工具，状态立即变为 `permission-violation`，不会接受验证结果。

## Linux / macOS

要求：Python 3.10+、已登录并可正常使用的 OpenCode CLI；Git 项目还需要 Git。

首次使用：

```sh
cd /path/to/codex-opencode-orchestrator
chmod +x ./codex-opencode
./codex-opencode \
  --project "$HOME/src/my-project" \
  --task-file ./task.example.json \
  --dry-run
```

审核生成的任务包与权限文件后，移除 `--dry-run` 正式执行：

```sh
./codex-opencode \
  --project "$HOME/src/my-project" \
  --task-file ./task.example.json
```

如果复制文件后没有保留可执行位，可直接运行：

```sh
sh ./codex-opencode --project /path/to/repo --task-file ./task.example.json
```

也可以跳过启动脚本：

```sh
python3 ./codex_opencode.py \
  --project /path/to/repo \
  --task "修复用户列表的空状态渲染" \
  --scope src/pages/users.tsx \
  --scope src/pages/users.test.tsx \
  --constraint "保留现有 API" \
  --acceptance "无用户时显示空状态" \
  --verify "npm test -- users" \
  --verify "npm run typecheck"
```

每个 `--scope`、`--constraint`、`--acceptance` 和 `--verify` 都可以重复传入。

### macOS 注意事项

- 如果 `opencode` 不在非交互 Shell 的 `PATH` 中，添加 `--opencode /absolute/path/to/opencode`。
- 验证命令通过系统 `/bin/sh -lc` 执行；需要 Bash 或 Zsh 专属语法时，在任务文件中明确写成 `bash -lc '...'` 或 `zsh -lc '...'`。
- Python 由 Homebrew、pyenv 或官方安装均可，脚本不依赖第三方 Python 包。

### WSL 注意事项

- 推荐在 WSL 内原生安装 OpenCode，并把 Linux 项目放在 WSL 文件系统中，以获得更好的 I/O 性能。
- 如果 WSL 的 `PATH` 找到的是 Windows npm 安装的 OpenCode，编排器会自动用 `wslpath` 转换项目路径，并通过 `WSLENV` 转发该次运行的权限配置。
- 事件级安全闸门仍会检查是否实际执行了 Shell 等禁用工具。

## Windows

要求：PowerShell 7、已登录并可正常使用的 OpenCode CLI；Git 项目还需要 Git。

```powershell
& .\Invoke-OpenCodeWorker.ps1 `
  -Project C:\path\to\repo `
  -Task "修复用户列表的空状态渲染" `
  -Scope @("src/pages/users.tsx", "src/pages/users.test.tsx") `
  -Constraint @("保留现有 API", "不要改动无关样式") `
  -Acceptance @("无用户时显示空状态", "有用户时行为不变") `
  -VerifyCommand @("npm test -- users", "npm run typecheck")
```

使用任务文件干跑：

```powershell
& .\Invoke-OpenCodeWorker.ps1 `
  -Project C:\path\to\repo `
  -TaskFile .\task.example.json `
  -DryRun
```

从 `cmd.exe` 或 CI 调用时，优先使用任务 JSON：

```powershell
pwsh -NoProfile -File .\Invoke-OpenCodeWorker.ps1 -Project C:\path\to\repo -TaskFile .\task.example.json
```

## 任务文件格式

`task.example.json` 展示了常用字段：

- `task`：必须，单一、可交付的目标。
- `scope`：允许或预期修改的文件范围。
- `constraints`：兼容性、安全和禁止事项。
- `acceptance`：可以逐条判定的完成条件。
- `verifyCommands`：由 Codex 侧独立执行的验证命令。
- `model`：可选的 OpenCode 模型 ID。
- `maxRounds`：验证失败后的最大实现轮数，默认 2，范围 1–10。

命令行参数会覆盖任务文件中的同名可选字段。Linux/macOS 使用 `--model provider/model-id`，Windows 使用 `-Model provider/model-id`。

## 状态与运行产物

每次运行都会在脚本旁的 `runs/<时间-随机ID>/` 下保存：

- `task-packet.md`：实际发送给 OpenCode 的任务包。
- `restricted-opencode-config.json`：该次运行的权限边界。
- `metadata.json`：平台、版本、项目和运行参数。
- `round-XX-events.ndjson`：OpenCode 原始 JSON 事件及工具调用证据。
- `round-XX-verify-XX.txt`：独立验收输出。
- `summary.json`：最终状态、会话 ID、轮次、测试结果和 Git 变化摘要。

状态含义：

- `passed`：至少提供一条验证命令，且全部返回 0，同时未发现权限违规。
- `needs-review`：OpenCode 完成编辑，但没有提供验证命令；返回非零退出码。
- `permission-violation`：OpenCode 成功执行了未授权工具；不会接受测试结果。
- `failed`：OpenCode、超时或最终验证失败。

运行产物可能包含代码片段和测试输出，不应直接提交到公开仓库，因此仓库默认忽略 `runs/`。验证命令也可能生成缓存、覆盖率等文件；Codex 必须把验证后的 `git status --short` 纳入验收。

## 推荐给 Codex 的固定指令

可把下面内容放进项目 `AGENTS.md`：

```text
需要把明确的实现单元交给 OpenCode 时，Linux/macOS 使用 codex-opencode，
Windows 使用 Invoke-OpenCodeWorker.ps1。先检查 Git 状态并形成包含 scope、
constraints、acceptance、verifyCommands 的任务包；先干跑审核任务与权限，再正式运行。
完成后读取 summary.json，独立检查 git diff、工具事件和验证日志。不得仅根据 OpenCode
的文字总结判定完成；不得让 OpenCode 提交或推送；工作区已有改动时必须先确认归属。
```

## Codex 验收清单

1. `git status --short` 与 `git diff` 是否只包含任务范围内修改。
2. `summary.json` 是否为 `passed`，工具事件是否只有允许的读写工具。
3. 验收条件是否覆盖真实用户路径，而不只是单条测试。
4. 是否还缺构建、类型检查或真实运行环境验证，并在交付中准确披露。

## 方案边界

当前实现针对本地、单项目、串行编码任务。高频任务可进一步升级为常驻 `opencode serve` 以减少冷启动；并行任务应使用相互隔离的 Git worktree，避免多个 Agent 同时修改同一工作区。

## 参考依据

- [OpenAI：Create a CLI Codex can use](https://learn.chatgpt.com/use-cases/agent-friendly-clis)
- [OpenCode CLI](https://opencode.ai/docs/cli/)
- [OpenCode Permissions](https://opencode.ai/docs/permissions/)
- [OpenCode Agents](https://opencode.ai/docs/agents/)
- [Microsoft：WSL interop](https://learn.microsoft.com/en-us/windows/dev-environment/wsl-interop)
