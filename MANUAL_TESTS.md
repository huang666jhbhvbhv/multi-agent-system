# 重要功能手动测试

先退出旧 Agent，重新启动以加载修复。真实模型测试会消耗 API 额度；不要在重要项目上试破坏性命令。

## 1. 自动回归（不需要 API）

在 PowerShell 中运行：

```powershell
cd E:\code_learning\learn-cc\deepseek-code
python -m unittest discover -s tests -v
python agent.py --demo
```

预期：测试最后显示 OK；离线演示创建并读取文件。它们不等于真实 DeepSeek 已验证。

## 2. 准备独立测试目录

```powershell
New-Item -ItemType Directory .\manual-test
python agent.py --workspace .\manual-test
```

如果目录已经存在，可选另一个新名字。以下中文任务是在 Agent 界面输入，不是在 PowerShell 输入。
每项结束后可以 /new 开启干净会话；文件、记忆、任务表仍保留。不要用 /new 清除测试文件。

## 3. 审批和误输入（优先测）

1. 空闲时输入 y，再输入 n。
2. 预期：显示“当前没有待确认操作”，不调用模型、不问你选择什么功能。可用 /status 对比 API 次数。
3. 输入：`使用 write_file 创建 denied.txt，内容 hello。若我拒绝，不要换工具创建。`
4. 等看到确认提示后输入 n；预期显示“已拒绝”，文件不存在。
5. 再明确要求创建 allowed.txt，看到提示后输入 y；预期只批准当前操作，文件存在且内容正确。
6. 不要预先连续输入多个 y；早于当前提示的输入会被丢弃，不会用于下一项审批。

## 4. 文件编辑、测试执行和真正失败（优先测）

输入：`创建 calculator.py，实现加减乘除，除零抛 ValueError。创建 unittest 测试并运行。最后简述文件、测试数量、退出码，不要新增其他功能。`

预期：文件写入需批准；测试运行需批准；全部测试通过时报告 OK 和退出码 0。
再输入：`仅运行 python -m unittest test_calculator -v 2>&1 | Out-String，不改文件。`
预期：旧版 Windows PowerShell 可能仍把 stderr 显示为 NativeCommandError 样式，但不应中断成功测试，最终退出码应为 0。

真正失败对照：`只执行 python -m unittest definitely_nonexistent_test_module -v，不修改文件，不重复运行。`
预期：退出码非 0；Agent 明确报告失败，不能因为出现局部 ok 就声称整体验证通过。

编辑测试：`用 edit_file 给 calculator.py 的模块说明增加“手动测试版本”，保持计算逻辑不变。`
预期：仅目标内容改变，.deepseek-agent/backups 中有旧文件；重新运行原测试仍通过。

## 5. 重复失败保护

输入：`为了检查重试保护，用 shell 连续执行三次完全相同的命令 python -m unittest definitely_nonexistent_test_module -v，不修改其他文件。`

前两次审批可批准，第三次应在审批/执行前被拦截，显示 blocked，不会继续刷命令。
如果模型提前停止重试，也合理；自动回归会确定性测试第三次拦截。
限制是同一轮、同工作目录、同命令文本连续失败两次（首尾空白忽略）；不是任意等价命令检测，也不统计异步后台失败。

## 6. 停止和后台通知（优先测）

输入：`前台执行 Start-Sleep -Seconds 30; Write-Output finished，不做其他事。`
批准后立刻输入 /stop。预期：命令停止、界面仍可响应；可能要等 API 返回才结束本轮，不应接着执行新的工具。
停止不撤销已经完成的文件修改。

新任务：`后台执行 Start-Sleep -Seconds 5; Write-Output BG_OK，同时读取 calculator.py；后台结束后告诉我 BG_OK 和退出码。`
批准后预期：可以先读文件，后台完成会自动通知主 Agent；用 /jobs 可检查结果，不应不断发 API 查询状态。

## 7. 会话恢复、记忆、压缩

输入：`本轮测试标记是 ORANGE-42，只回复收到，不写记忆文件。` 用 /status 记下会话编号。
/quit 退出，用下面命令恢复（替换实际编号）：

```powershell
python agent.py --workspace .\manual-test --session session_实际编号
```

询问测试标记，预期回答 ORANGE-42。输入 /compact，再问一次，应仍保留这个关键事实。
输入：`用 memory 保存 project_testing：本项目使用 unittest。` 批准后 /new，再问项目测试框架，预期能读取长期记忆。
摘要是模型生成，若丢失事实应记为质量问题，不能只看“压缩完成”。

## 8. 任务依赖和队友

输入：`创建 A 代码审查、B 文档审查、C 汇总三个任务。C 依赖 A 和 B。先尝试领取 C，确认依赖阻止领取。然后安排 alice 和 bob 分别只读审查 A/B，完成后标记任务完成并发消息，你最后汇总 C。不修改项目文件。`

用 /tasks、/team 检查：依赖没完成不能领取 C；队友结果会回来；主 Agent 汇总后任务状态合理。
出现创建队友审批时逐次确认。模型可能没按测试流程调用工具，需区分“没走到测试点”和“程序没拦住”。

## 9. MCP

输入：`连接 demo MCP，用 add 工具计算 123 加 456，不要直接心算代替工具。`
预期：启动 Server 和调用工具分别审批，结果 579；/mcp 显示 demo 已连接。
若拒绝工具调用，不应声称已从 MCP 获得结果。

## 10. 定时任务

输入：`创建 cron 为 * * * * * 的非重复定时任务（recurring=false），到期只回复 CRON_OK。`
批准后 /jobs 查看；保持程序运行至下一分钟，预期自动触发一次。成功完成后任务不再待执行。
测试完用 /jobs 检查没有遗留循环任务；有则让 Agent 取消并确认。
它不是 Windows 系统任务计划，关闭程序期间不会执行。

## 11. 目标验收、Skills、工作流

- `/goal 运行现有 calculator 的 unittest 测试，提供全部通过且退出码为 0 的证据，不新增功能`：批准测试后应由判断器判断；不能仅靠模型口头说成功。
- `加载 code-review 技能，只审查 calculator.py`：预期调用 load_skill，按技能内容审查，不修改文件。
- `读取 calculator.py，调用 Workflow 做 review-changes 审查，告诉我 run_id`：预期有审查结果与 journal；相同输入和 run_id 再调用应复用步骤缓存。这个流程是文本审查，不等于执行测试。

## 12. 只读权限

退出当前进程再启动：

```powershell
python agent.py --workspace .\manual-test --permission read-only
```

要求创建 forbidden.txt 或执行 shell。预期被拒绝，文件不存在。读取 calculator.py 应正常。
read-only 仍保存会话等 Agent 状态；Shell/MCP 在普通模式获准后不是操作系统沙箱。

## 如何报告 bug

记录：启动命令、输入任务、批准/拒绝时机、完整工具命令和退出码、预期与实际结果。
用 /status 记会话编号；.deepseek-agent/sessions 和 outputs 可辅助排查，分享前去掉密钥和私人代码。
[done] 只表示一轮结束；测试是否通过要看完整测试报告和退出码。不同模型的完成率可重复测 3 次比较。
