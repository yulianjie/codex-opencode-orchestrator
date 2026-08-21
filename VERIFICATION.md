# 验证记录

验证时间：2026-08-21（Asia/Singapore）

环境：PowerShell 7.6.4、Python 3.12、OpenCode 1.18.14、Node.js 24.14.1、Ubuntu WSL2（Linux 6.6.87.2）。

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

macOS 与 Linux 共用 Python 标准库实现及 POSIX `/bin/sh` 启动路径；当前机器没有 macOS 运行环境，因此 macOS 已完成代码级兼容适配和 Shell 语法验证，但未声称有真实 macOS 运行证据。

## 证据文件

成功运行的原始任务包、受限配置、NDJSON 事件、独立验证日志和最终汇总保留在本地，没有提交到仓库。`runs/` 默认被 Git 忽略，因为其中可能包含机器路径、代码片段、测试输出和 OpenCode 会话标识。

这是一轮本地示例验证，证明了编排、权限、会话、测试和证据链能够闭环；具体业务仓库仍需提供与其技术栈相匹配的验证命令。
