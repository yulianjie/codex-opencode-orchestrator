# 验证记录

验证时间：2026-08-21（Asia/Singapore）

环境：PowerShell 7.6.4、Python 3.12、Codex CLI 0.146.0、OpenCode 1.18.14、MCP Python SDK 2.0.0、Node.js 24.14.1、Ubuntu WSL2（Linux 6.6.87.2）。

## 已验证

- PowerShell 脚本语法解析通过，示例任务 JSON 可正常解析。
- 受限 Agent 探针返回 `CODEX_OPENCODE_READY`。
- Windows 端到端运行在一个干净的隔离 Git 项目中完成：
  - OpenCode 实际工具调用仅有 `read`、`glob`、`edit`。
  - 仅修改 `src/greet.js`，Git 摘要为 1 个文件、2 行新增、1 行删除。
  - Codex 侧独立运行 `node test.js`，退出码 0，输出 `All greet checks passed.`。
  - 最终 `summary.json` 状态为 `passed`。
- 在目标项目已有未提交改动时，默认执行被拒绝并返回非零退出码；错误中列出已有改动。
- 使用 `-TaskFile`、`-AllowDirty` 和 `-DryRun` 能正确加载数组字段、生成任务包且不启动 OpenCode。

### Linux / WSL 跨平台验证

- `codex_opencode.py` 在 Ubuntu WSL2 的 Python 3.12.3 下通过语法检查；`codex-opencode` 通过 `/bin/sh` 语法检查。
- Linux 干跑正确识别 POSIX 项目路径、Git 状态、任务 JSON 和证据目录。
- 使用受控原生 POSIX 假 CLI 验证了非 WSL 桥接路径：`openCodeProject` 保持 Linux 路径、`wslWindowsBridge=false`，并由 `/bin/sh -lc` 完成独立验证。
- 自动识别到 WSL 中实际使用的是 Windows npm OpenCode，使用 `wslpath` 转换项目路径，并通过 `WSLENV` 转发动态权限配置。
- Linux / WSL 端到端运行从干净 Git 项目开始：
  - 工具调用仅为 `read` 和 `edit`，未执行 Shell、网络或子 Agent。
  - 仅修改 `slugify.py`。
  - Linux 侧通过 `/bin/sh -lc` 独立运行 `python3 -B test_slugify.py`，退出码 0。
  - 验证后 Git 状态仅有目标源文件，无 `__pycache__` 等附加产物。
  - 最终 `summary.json` 状态为 `passed`。
- 脏 Git 项目在 Linux 入口下默认被拒绝并返回非零退出码。
- 使用受控假事件模拟成功执行 `bash` 后，安全闸门返回 `permission-violation`，未运行验证命令。

### Codex Skill 验证

- 使用 `skill-creator` 的 `quick_validate.py` 完成结构校验，结果为 `Skill is valid!`。PyYAML 安装在一次性隔离环境中，没有修改全局 Python。
- 使用 Codex CLI 0.146.0 从临时项目的 `.agents/skills/codex-opencode-orchestrator/` 加载安装副本；解析 `codex debug prompt-input` 的 JSON 后，确认可用 Skill 列表包含名称、描述和路径。
- `codex debug prompt-input` 只证明发现结果，不执行模型侧的显式或隐式选择，因此没有把它当作 Skill 触发质量的运行证据。
- 安装副本分别通过 Windows PowerShell 和 Ubuntu WSL2 干跑，均生成任务包、受限配置和元数据；权限断言确认 `edit=allow`，Shell、网络和外部目录保持 `deny`。
- 这两次 Skill 包装验证均为干跑，没有启动 OpenCode 或修改目标项目；OpenCode 的真实端到端执行证据仍以前述 Windows 与 Linux / WSL 验证为准。

### 安装器与 MCP 验证

- Windows 与 Ubuntu WSL2 下 14 个安装器和 MCP 后端单元测试全部通过，覆盖发现路径、白名单复制、干跑、覆盖保护、源内目标拒绝、任务合同、路径穿越拒绝、损坏摘要处理、摘要脱敏、MCP 审批策略，以及 MCP 注册失败时恢复原 Skill 和原 `config.toml`。
- Ubuntu WSL2 下同一套 Python 测试、`compileall` 和 `/bin/sh` 语法检查通过；项目级安装副本成功写入 `.agents/skills/codex-opencode-orchestrator/`，包含 Skill、编排核心和 MCP 服务文件。
- 在隔离的用户目录和 `CODEX_HOME` 中完成真实 `--with-mcp` 安装与 `--force` 更新：虚拟环境安装 MCP Python SDK 2.0.0，`codex mcp get codex-opencode --json` 确认 STDIO 命令、30 秒启动超时和 7200 秒工具超时有效；配置文件中的正式执行工具为 `prompt`，准备与只读查询工具为 `approve`。
- 使用 MCP SDK 客户端实际初始化 `codex-opencode` 服务，发现 `prepare_opencode_task`、`run_opencode_task`、`get_opencode_run`、`list_opencode_runs` 四个工具；真实调用准备工具后生成 `dry-run` 任务包、受限权限配置和元数据。
- 使用受控假 OpenCode 事件验证正式 MCP 调用路径：核心独立运行 `git diff --exit-code` 并返回 `passed`，目标 Git 工作区保持干净，MCP 返回结构中不含假会话 ID。这个探针证明 MCP 包装和核心闭环，不替代前述真实 OpenCode 端到端证据。
- 从真实项目的 `.agents/skills/` 安装副本运行 `codex debug prompt-input`，确认 Codex 的可用 Skill 信息中包含 `codex-opencode-orchestrator`。该命令仍只证明发现，不证明模型一定会隐式选择 Skill。

macOS 与 Linux 共用 Python 标准库实现及 POSIX `/bin/sh` 启动路径；当前机器没有 macOS 运行环境，因此 macOS 已完成代码级兼容适配和 Shell 语法验证，但未声称有真实 macOS 运行证据。

## 证据文件

成功运行的原始任务包、受限配置、NDJSON 事件、独立验证日志和最终汇总保留在本地，没有提交到仓库。`runs/` 默认被 Git 忽略，因为其中可能包含机器路径、代码片段、测试输出和 OpenCode 会话标识。

这是一轮本地示例验证，证明了编排、权限、会话、测试和证据链能够闭环；具体业务仓库仍需提供与其技术栈相匹配的验证命令。
