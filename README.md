# DeepSeek Code：可阅读、可修改的终端编程 Agent

一个面向本地代码开发的终端 Agent。它把 DeepSeek 的模型决策能力与文件、Shell、测试、MCP 和任务协作工具连接起来，支持在用户确认下完成“理解代码 → 修改文件 → 运行验证 → 汇报结果”的闭环。

## 项目亮点

- **Agent Loop**：实现模型决策、工具调用、结果回传和多轮迭代。
- **安全工具执行**：提供路径校验、PreToolUse/PostToolUse Hooks、审批模式、修改 Diff、备份和重复失败命令拦截。
- **任务与协作**：支持持久化任务依赖、子 Agent、队友 Agent、后台命令、Cron 定时任务和 Git Worktree 隔离。
- **上下文与记忆**：支持长输出落盘、上下文压缩、项目记忆、Skills 按需加载和会话恢复。
- **MCP 与工作流**：通过 stdio JSON-RPC 动态发现并调用 MCP 工具；使用 Journal 和 SHA-256 步骤标识缓存代码审查结果。
- **可验证性**：包含自动测试、手动验收清单和离线演示模式，覆盖权限、Windows PowerShell、MCP、任务、协作和恢复流程。

## 技术栈

Python 3.11+、DeepSeek Chat Completions API、Windows PowerShell、SQLite/JSON 持久化、线程与消息队列、MCP stdio JSON-RPC、Git Worktree、Python `unittest`。

## 项目结构

```text
agent.py                 启动入口
dscode/                  Agent、工具、运行时、MCP、工作流等核心模块
examples/mcp_server.py   可运行的示例 MCP Server
skills/                  可按需加载的技能说明
tests/                   自动化测试
playground/              安全练习目录
MANUAL_TESTS.md          手动验收清单
```

## 快速运行

无需 API Key 的离线演示：

```powershell
python agent.py --demo
```

正式模式：

```powershell
Copy-Item .env.example .env
notepad .env
python agent.py --doctor
python agent.py --workspace .\playground
```

在 `.env` 中填写自己的 `DEEPSEEK_API_KEY`。真实 `.env` 已被 Git 忽略，提交时不会上传。

## 典型用法

```text
先阅读当前目录，创建 calculator.py，实现加减法，添加 unittest 测试并运行，最后汇报测试结果。
```

Agent 会先向模型请求下一步动作；涉及写文件、Shell、MCP 或外部副作用时显示审批提示。输入 `y` 批准，其他输入拒绝。也可以使用 `--permission read-only` 进行只读代码审查。

## 验证

```powershell
python -m unittest discover -s tests -v
python agent.py --demo
```

自动测试不需要真实 API Key；真实 API 的连通性和模型效果需要在配置 Key 后单独验证。详细操作和边界场景见 [MANUAL_TESTS.md](MANUAL_TESTS.md)。

## 安全说明

- 不要提交 `.env`、API Key、`.deepseek-agent/` 运行数据或个人项目文件。
- 文件工具默认限制在工作目录；Shell 和 MCP 仍使用当前 Windows 用户权限，不是操作系统级沙箱。
- 这是学习和本地开发项目，不是 Anthropic Claude Code 官方产品，也不保证消除模型幻觉。

下面保留完整的功能说明、配置细节、架构解释和排错记录。

重要功能的操作步骤与验收标准见 [手动测试清单](MANUAL_TESTS.md)。更新代码后请退出旧进程再启动。
空闲时输入 y/yes/n/no 不会发送给模型；早于确认提示的输入不用于审批。
同一轮同目录同 Shell 命令失败或被拒绝两次后，第三次会停止本轮；不包含异步后台结果。
Windows Shell 不再把正常原生命令 stderr 当作终止错误；真实非零退出码和 PowerShell 错误仍会报告失败。
请优先每次调用只运行一个外部命令，复杂脚本需自行检查各步骤退出码。

面向 Python 3.11+ 和 Windows PowerShell，根据旁边 learn-cc 的 s01–s17 概念重新组织实现。
默认对接 DeepSeek Chat Completions API。它是一个学习和本地项目开发用的 Agent，
不是 Anthropic Claude Code 官方产品，也没有声称完整复刻其全部功能。

核心和示例 MCP Server **只使用 Python 标准库**，不需要 pip 安装依赖。
原 s01–s17 文件不参与导入，可以把这个文件夹单独复制到其他位置使用。

## 1. 先体验，不需要 API Key

在 PowerShell 中：

```powershell
cd E:\code_learning\learn-cc\deepseek-code
python agent.py --demo
```

演示器模拟模型回复，但真实走工具调用、创建文件、读取文件和保存会话的代码。
演示使用一次性临时目录，结束后自动清理，不消耗 API 额度。
它不是对真实 DeepSeek API 的连通性验证。

## 2. 配置 DeepSeek

仅首次执行（已有 `.env` 时不要覆盖）：

```powershell
Copy-Item .env.example .env
notepad .env
```

把文件中的这一行替换为你在 DeepSeek 开放平台申请的 API Key：

```ini
DEEPSEEK_API_KEY=你的实际密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_THINKING=disabled
```

默认模型名来自实现时核对的 DeepSeek 官方文档。若你的账号或第三方平台提供的模型名不同，
修改 `DEEPSEEK_MODEL`；兼容网关可修改 `DEEPSEEK_BASE_URL`。填写服务根地址，不要重复添加
`/chat/completions`。旧教程里的 `deepseek-chat` 等名称是否仍可用，以你的服务商为准。
`DEEPSEEK_THINKING=enabled` 开启思考模式；不支持该参数的兼容平台可设置为 `omit`。

`.env` 固定从本应用目录加载，不会误用待编辑项目里的 `.env`；已有进程环境变量优先。
Key 不写进会话和日志。HTTP 使用系统证书校验，不会关闭 TLS 验证。

```powershell
python agent.py --doctor
```

这个命令检查 Python、Git、配置及 Key 是否存在，只显示“是否配置”，不打印密钥，也不发 API 请求。

## 3. 开始使用

先用专门的练习目录：

```powershell
python agent.py --workspace .\playground
```

也可以双击 `start.cmd`，它自动进入这个应用目录并以 playground 为工作目录。

然后输入：

```text
先看看当前目录。创建 calculator.py，包含 add 和 subtract；添加 unittest 测试并运行，最后告诉我执行结果。
```

默认 `ask` 模式：文件写入、Shell 命令、MCP 调用、保存记忆、创建团队和定时任务会显示具体参数；
输入 `y` 批准，其他输入拒绝。正在等待批准时不要输入下一条任务，它会被作为拒绝回答处理。
未批准的操作会作为拒绝结果反馈给模型。

允许工作目录内自动改文件，但仍逐次确认 Shell/MCP：

```powershell
python agent.py --workspace .\playground --permission auto-edit
```

只读学习/审查：

```powershell
python agent.py --workspace E:\你的项目 --permission read-only
```

`read-only` 禁止文件修改、Shell、MCP、保存记忆、组队和创建定时任务；会话和进度等运行记录仍会写入
`.deepseek-agent`。退出 API 请求后不会自动回滚已完成的修改。

## 4. 常用命令

| 命令 | 用途 |
| --- | --- |
| `/help` | 帮助 |
| `/status` | 当前模型、会话 ID、API 调用次数、Token、目标 |
| `/tasks` | 持久化任务表 |
| `/team` | 队友状态 |
| `/jobs` | 后台命令与定时任务 |
| `/skills` | 当前技能目录 |
| `/memory` | 项目长期记忆 |
| `/mcp` | 已配置及已连接 MCP Server 名称 |
| `/sessions` | 已保存会话 |
| `/resume session_xxx` | 恢复一个会话 |
| `/new` | 开启新会话，保留项目任务、记忆和定时任务 |
| `/compact` | 主动归档/摘要上下文，会调用模型 |
| `/goal 完成条件` | 设置目标并执行，停止前让判断器验收 |
| `/goal clear` | 清除目标 |
| `/stop` | 取消当前工作与队友，终止登记的命令进程 |
| `/quit` | 退出 |

工作时可输入下一条普通消息排队；状态命令随时可用。会话切换要求没有活跃队友或后台任务。
Ctrl+C 请求停止。已发出的 API 请求不能即时撤回，会等响应/超时后停止，不执行随后返回的工具请求。

恢复进程退出前的会话：

```powershell
python agent.py --workspace .\playground --session session_实际编号
```

非交互单次任务：

```powershell
python agent.py --workspace .\playground --prompt "读取 README.md 并解释这个项目"
```

非交互模式不会代替用户同意操作；需要确认的工具会被拒绝。可显式指定 `--permission auto-edit`
允许文件编辑，但 Shell 和 MCP 仍需交互确认。`--goal "条件"` 必须与 `--prompt` 一起使用。

## 5. 对应 s01–s17 的功能与例子

### 基础循环、工具、权限、Hooks（s01–s04）

核心在 `dscode/agent.py`：

```text
用户消息 → DeepSeek → tool_calls → PreToolUse → handler
          ↑                                 ↓
          └──────── role=tool 结果 ← PostToolUse
```

有文件读取、精确替换、完整写入、glob、文本搜索和 Shell。
Windows Shell 明确调用 PowerShell，不会把 PowerShell 命令交给 CMD。
写入前备份已有文件，结果包含修改 diff。文件工具限制工作目录，默认不读取 `.env`；
Shell、外部 MCP 是获准后执行的普通进程，不是操作系统沙箱，仍有用户账户的权限。

### Todo、子 Agent、Skills（s05–s07）

```text
先列计划，再分步骤实现。找一个子 Agent 只调查项目结构，返回结论后你再处理。
```

`subagent` 是单次委托，子 Agent 有独立消息，不再委托其他 Agent；最终文本作为工具结果返回。
`spawn_teammate` 是持续存在的队友，见后面的团队说明。

Skills 从应用的 `skills/*/SKILL.md` 和项目的 `skills/*/SKILL.md` 发现，同名时项目版本优先。
先给模型目录，再通过 `load_skill` 读取正文。附带 `code-review` 示例：

```text
使用 code-review 技能检查 calculator.py，只给发现，不修改。
```

### 上下文、记忆（s08–s09）

长工具输出保存到 `.deepseek-agent/outputs`，发给模型的是预览和读取路径。
上下文达到配置的字符阈值后，归档完整对话并调用模型生成摘要；在完整工具批次边界压缩，
避免留下没有对应结果的 tool_calls。API 报上下文超限时只尝试一次重新压缩。
字符阈值是估计，不是模型准确 Token 计数；system/tools 特别大时仍可能需要减小输入。

记忆通过明确的 `memory` 工具保存，不偷偷把所有对话自动提取为长期偏好：

```text
记住这个项目使用 unittest，不使用 pytest。把它保存为 project_testing 记忆。
```

### 任务依赖、后台执行（s10–s11）

```text
创建三个任务：实现加法、实现减法、整体验证。验证依赖前两个完成。
```

任务工具 `tasks` 支持创建、依赖、领取、完成、释放；检查依赖环和任务归属。
程序不把“标记完成”视为真实验证通过，仍需工具证据。

```text
把测试命令放到后台运行；你同时阅读 README，测试完成后汇总结果。
```

后台命令最多 4 个。完成结果自动唤醒主 Agent，无需持续调用模型查询状态。
`/jobs` 查看结果文件位置，命令退出码会返回给模型。

### 定时任务（s12）

```text
每天上午 9 点检查 README 中是否有 TODO，创建定时任务。
```

五字段 Cron：分钟、小时、日、月、星期（0=周日）。支持 `*`、数字、逗号、范围、步长。
按电脑本地时间检查；Agent 忙时排队。配置保存在 `.deepseek-agent/schedules.json`。
程序必须保持运行；不补跑关机期间错过的时间点。
已到期但未确认完成的任务会在重启时恢复。失败不会无限重试消耗额度；可在 `/jobs` 查看，
必要时取消或重启重投递。投递可能在异常退出后重复，外部副作用需人工核对。

### 团队、计划审批、Worktree（s13）

```text
创建两个独立审查任务，安排 alice 看 Python 代码，bob 看文档；他们只读，你汇总结论。
```

每个队友有独立 messages 和线程，同一个 DeepSeek 客户端最多允许 4 个在途模型请求。
队友通过 `send_message` 发中间消息，最终结果自动送给 lead；Lead 空闲时会被唤醒。
队友完成一轮后保留会话等待新消息，不自动抢其他任务；后续任务需要明确消息指派并领取。
一个成员同时只能领取一个任务。最多 3 个队友，不支持递归创建团队。

传 `require_plan=true` 可要求先提交计划，由 Lead 用 `review_plan` 批准后才允许编辑或 Shell。
Lead 的计划批准不代替用户的 Shell/MCP 操作确认。

需要隔离修改时，先给 pending 任务调用 `create_worktree`，再创建并绑定队友。
Worktree 在 `.deepseek-agent/worktrees`，使用 `agent/名字` 分支；需要 Git 仓库至少有一次提交。
**不会自动提交、合并或删除 Worktree**，可以通过 Git 检查修改后手动整合。
Worktree 只隔离工作文件，不是安全沙箱。
绑定 Worktree 的任务必须交给持续队友；主 Agent 和单次子 Agent 不切换项目目录。

### 真正的 MCP（s14）

这版使用真实 stdio JSON-RPC：启动本地子进程，`initialize`、`tools/list`、`tools/call`。
不是教程里的进程内 mock。附带 `examples/mcp_server.py`，提供加法和文本统计：

```text
连接 demo MCP Server，使用里面的 add 工具计算 123 加 456。
```

会分别确认启动 Server 和调用外部工具。`mcp.json` 已配置 demo。
第三方 Server 配置格式：

```json
{
  "mcpServers": {
    "my_server": {
      "transport": "stdio",
      "command": "python",
      "args": ["E:/servers/my_server.py"],
      "envFrom": {"SERVICE_API_KEY": "MY_SERVICE_KEY"}
    }
  }
}
```

`envFrom` 的值是环境变量名，避免在配置里放密钥。
支持 `{python}` 当前解释器、`{app}` 应用目录、`{workspace}` 项目目录。
外部 Server 自行安装所需依赖。当前仅实现 stdio 工具能力，不支持远程 HTTP/OAuth、资源、提示模板、
工具列表变更通知热刷新；修改 Server 工具后重启本应用重新连接。

### 工作流、Journal（s16）

```text
读取 calculator.py，将代码作为 text 交给 Workflow 进行 review-changes 审查，并解释结果。
```

四个方向并发审查，每个方向内部依次复核发现。模型只有传入的文本，没有 Shell/文件工具。
每一步的结果写入 `.deepseek-agent/workflows/*.journal.jsonl`。
key 用模型配置、步骤名、完整提示词计算 SHA-256；相同 run_id 和输入可以复用成功步骤。
恢复时重新遍历代码，命中 Journal 就不再调用模型；它不是任意进程快照。
返回的 `run_id` 可用作下次 Workflow 调用的参数。流程从调用开始等待到最终结果返回，
不是启动后立即返回的后台工作流。

### 目标验收（s17）

```text
/goal 为 calculator.py 补充 unittest 测试，并提供 python -m unittest discover 退出码为 0 的执行证据
```

Worker 准备停止时，独立的无工具模型请求根据目标和执行记录判断。
未完成会反馈缺少的证据并继续，最多续跑 5 次，另有总循环步数限制。
判断器无法替代确定性测试，也不能保证所有自然语言目标判断正确。
目标和会话一起保存；无法完成时保留目标并报告阻塞，可修改要求或 `/goal clear`。

## 6. 代码结构

```text
agent.py                 启动入口
dscode/
  cli.py                 命令行、审批 UI、事件唤醒
  config.py              .env 和运行配置
  llm.py                 DeepSeek HTTP、重试、用量与 reasoning_content
  agent.py               Agent Loop、会话、压缩、目标判断器
  tools.py               文件/Shell/技能/记忆、工具注册、Hooks
  state.py               跨平台工作目录锁、任务依赖和状态保存
  runtime.py             子 Agent、团队、消息、后台命令与服务整合
  scheduler.py           Cron 与持久化待投递记录
  mcp.py                 真实 MCP stdio 连接器
  workflow.py            审查流水线与 Journal
examples/mcp_server.py   可直接启动的 MCP Server
skills/code-review/     示例技能
playground/             练习项目
tests/test_agent.py     无真实 API 的功能/协议检查
```

项目内 `.deepseek-agent` 保存会话、输出、备份、日志、任务、记忆、定时配置、Worktree 和工作流。
同一项目一次仅允许运行一个应用进程，进程内部支持多线程。任务状态持久化，但运行中的线程/进程
不会跨重启恢复。中断会话的未知工具结果会标记为 unknown，模型应先检查实际文件，避免重复副作用。
重启取得项目独占锁后，未完成任务会释放回 pending，重新领取前应先核对已有修改。

建议在你的实际项目 `.gitignore` 加入 `.deepseek-agent/`。
已有文件的备份在 `.deepseek-agent/backups/`；新建文件没有旧版本备份，可通过 Git diff/status 查看。
终端输出工具名称与最终回答，当前不做 Token 流式展示。

## 7. 验证与排错

```powershell
python -m unittest discover -s tests -v
python agent.py --demo
python agent.py --doctor
```

测试覆盖真实本地文件读写/备份、工具消息闭环、权限、路径、会话恢复、目标续跑、上下文压缩、
任务依赖、Windows 命令和超时、后台通知、队友退出、工作流缓存、Cron、HTTP 格式及真实 MCP 子进程通信。
交付前在 Windows / Python 3.11.5 上通过 23 项测试，包含实际 Git Worktree 隔离修改，
以及本地 HTTP 模拟服务驱动的终端输入、审批、文件写入和退出全流程；离线演示也已通过。
测试不需要也不调用真实 DeepSeek；真实 API 需要你填写 Key 后运行任务验证。

- 401：检查 Key 和环境变量是否覆盖了 `.env`。
- 402 或余额错误：检查开放平台 API 余额；聊天网页账号不等于 API 额度。
- 404/模型不存在：核对服务商的模型名和 Base URL。
- 429：程序对限流/服务端暂时错误有限重试；减少团队/工作流并发。
- 400 工具消息错误：本实现保留 reasoning_content；兼容平台可能不支持 thinking 或工具调用格式。
- 中文终端：使用 PowerShell/Windows Terminal；`start.cmd` 会设置 UTF-8 代码页。
- Shell 需要额外包：在目标项目安装它的依赖，或在命令中明确使用该项目虚拟环境的 Python 路径。

Token 预算是进程级软限制，请求完成后累计输入+输出 Token，检查后才发下一次请求。
并发在途请求可能超出预算，网络重试也可能在服务端产生额外费用，不能当作精确账单上限。

## 8. 来源

设计学习来源：<https://github.com/shareAI-lab/learn-claude-code> 以及本地 s01–s17 章节。
本文件夹采用独立模块实现，不依赖原教程中 `fcntl` 或写死的课程子目录。

API 格式核对来源：

- <https://api-docs.deepseek.com/>
- <https://api-docs.deepseek.com/guides/tool_calls>
- <https://api-docs.deepseek.com/guides/thinking_mode>
