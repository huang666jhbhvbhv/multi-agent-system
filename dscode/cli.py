import argparse
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time

from .agent import Agent
from .config import APP, Config
from .runtime import Runtime

HELP = """
直接输入中文任务；模型可读写代码、搜索、执行命令、创建任务/队友等。
/help                帮助
/status              模型、会话、Token 用量、目标
/tasks /team /jobs   任务表 / 队友 / 后台与定时任务
/skills /memory /mcp 查看技能 / 长期记忆 / MCP 配置
/sessions            列出保存的会话
/resume SESSION_ID   恢复会话（工作空闲且无活跃队友/后台任务时）
/new                 新会话（保留任务、记忆、计划任务）
/compact             归档并摘要当前对话（调用 API）
/goal 完成条件        设置目标并开始工作，结束前由独立 LLM 验收
/goal clear          清除当前目标
/stop                停止本轮及队友；已完成的文件修改仍然保留
/quit                退出并清理子进程
确认操作时输入 y 批准，其他输入拒绝。Shell 每次都确认。
"""


class DemoLLM:
    """Explicitly offline fake model; real filesystem/tool loop is exercised."""
    tokens = 0
    calls = 0

    def chat(self, messages, tools=None, max_tokens=None):
        self.calls += 1
        if self.calls == 1:
            name, arguments = "write_file", {"path": "hello.py", "content": "print('Hello from DeepSeek Code')\n"}
        elif self.calls == 2:
            name, arguments = "read_file", {"path": "hello.py"}
        else:
            return {"role": "assistant", "content": "离线演示完成：实际创建并读取了 hello.py，消息经过了完整工具循环。模型回复由演示器生成，未调用 DeepSeek。"}, "stop"
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": "demo_" + str(self.calls), "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments)}}]}, "tool_calls"


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def doctor(config):
    import shutil
    print("Python:", sys.version.split()[0])
    print("App:", APP)
    print("Workspace:", config.workspace)
    print("Model:", config.model, "thinking=" + config.thinking)
    print("API endpoint:", config.base_url + "/chat/completions")
    print("API key:", "已配置（未发送网络请求）" if config.key else "未配置")
    print("Git:", shutil.which("git") or "未安装（仅 worktree 功能需要）")
    print("PowerShell:", shutil.which("pwsh") or shutil.which("powershell.exe") or "未检测到")
    print("MCP config:", config.mcp_file)


def interactive(rt, lead):
    inputs = queue.Queue()
    def read_input():
        while True:
            try:
                line = input()
            except EOFError:
                inputs.put(("/quit", time.monotonic()))
                return
            inputs.put((line, time.monotonic()))
            if line.strip() in ("/quit", "/exit"):
                return
    threading.Thread(target=read_input, daemon=True).start()
    rt.start()
    print(HELP)
    print(f"会话：{lead.session_id}\n工作目录：{lead.workspace}\n请输入任务 >", flush=True)
    active = False
    approval = None
    approval_shown = float("inf")
    pending = []
    paused = False

    def launch(text, goal=None, event=False, job_id=None, compact=False):
        nonlocal active
        active = True
        lead.cancel.clear()
        def work():
            try:
                if compact:
                    lead.compact(force=True)
                    result = {"status": "done", "text": "压缩完成"}
                else:
                    result = lead.run(text, goal=goal, event=event)
            except Exception as exc:
                result = {"status": "error", "text": str(exc)}
            rt.emit("turn_done", (result, job_id))
        rt.start_thread(work, "lead")

    while not rt.stop.is_set():
        try:
            # Approvals always remain in the foreground UI; workers never call input().
            if approval is not None and (approval.cancel.is_set() or approval.ready.is_set()):
                approval = None
            if approval is None:
                try:
                    request = rt.approvals.get_nowait()
                    if not request.cancel.is_set():
                        approval = request
                        approval_shown = time.monotonic()
                        print("\n[需要确认]\n" + approval.detail + "\n允许？输入 y 确认，其他输入拒绝：", flush=True)
                except queue.Empty:
                    pass
            for _ in range(30):
                try:
                    kind, value = rt.events.get_nowait()
                except queue.Empty:
                    break
                if kind == "notice":
                    print(value, flush=True)
                elif kind in ("team", "scheduled"):
                    if paused:
                        print("[已暂停自动续跑] 有新事件，重新输入任务后处理；定时任务失败可重启恢复。")
                    else:
                        pending.append((kind, value))
                elif kind == "turn_done":
                    active = False
                    result, job_id = value
                    print("\n" + result["text"] + f"\n[{result['status']}]\n请输入任务 >", flush=True)
                    if job_id:
                        rt.scheduler.ack(job_id, result["status"] in ("done", "achieved"))
            try:
                line, entered_at = inputs.get(timeout=0.1)
                line = line.strip()
            except queue.Empty:
                line = None
            if line in ("/quit", "/exit"):
                return
            if line == "/stop":
                paused = True
                pending.clear()
                lead.cancel.set()
                for name in list(rt.teams):
                    rt.shutdown(name)
                rt.shell.close()
                print("已请求停止；正在进行的 API 请求会在返回/超时后退出。")
                continue
            if approval is not None and line is not None:
                if entered_at < approval_shown:
                    print("此输入早于当前确认提示，未批准任何操作。请看清提示后重新输入 y 或 n。", flush=True)
                    continue
                approval.allowed = line.lower() in ("y", "yes")
                print("已批准当前操作。" if approval.allowed else "已拒绝当前操作。", flush=True)
                approval.ready.set()
                approval = None
                continue
            if line:
                if line.lower() in ("y", "yes", "n", "no"):
                    print("当前没有待确认操作；此输入不会发送给模型。请输入完整任务。", flush=True)
                elif line == "/help":
                    print(HELP)
                elif line == "/status":
                    print_json(dict(session=lead.session_id, model=rt.config.model,
                        calls=rt.llm.calls, tokens=rt.llm.tokens, active=active, goal=lead.goal,
                        permission=rt.config.permission))
                elif line == "/tasks":
                    print_json(rt.store.task())
                elif line == "/team":
                    print_json(rt.team_list())
                elif line == "/jobs":
                    print_json({"background": rt.backgrounds, "cron": rt.scheduler.manage()})
                elif line == "/skills":
                    print_json({k:v[1] for k,v in lead.local.skills().items()})
                elif line == "/memory":
                    print_json(lead.local.memory())
                elif line == "/mcp":
                    print_json({"configured": list(rt.mcp.specs()), "connected": list(rt.mcp.clients)})
                elif line == "/sessions":
                    print("\n".join(p.stem for p in sorted((rt.store.root / "sessions").glob("*.json"))))
                elif line.startswith("/") and active:
                    print("本轮运行中；请先 /stop 或等待完成。/status 可随时查看。")
                elif line == "/new" or line.startswith("/resume "):
                    if rt.pending_work("lead") or any(t["status"] != "stopped" for t in rt.teams.values()):
                        print("请先停止队友和后台任务，避免结果进入错误会话。")
                    else:
                        session_id = line.split(maxsplit=1)[1] if line.startswith("/resume ") else None
                        if session_id and not rt.store.path("sessions", session_id + ".json").is_file():
                            print("会话不存在")
                        else:
                            lead = Agent(rt, session_id=session_id)
                            lead.save()
                            print("当前会话：" + lead.session_id)
                elif line == "/compact":
                    launch("", compact=True)
                elif line == "/goal clear":
                    lead.goal = ""
                    lead.save()
                    print("目标已清除")
                elif line.startswith("/goal "):
                    paused = False
                    goal = line[6:].strip()
                    launch(goal, goal=goal)
                elif line.startswith("/"):
                    print("未知命令，输入 /help。")
                elif active:
                    pending.append(("user", line))
                    print("已排队，当前轮结束后处理。")
                else:
                    paused = False
                    launch(line)
            if not active and not paused and pending and approval is None:
                kind, value = pending.pop(0)
                if kind == "scheduled":
                    if value["id"] in rt.scheduler.jobs:
                        launch("[Scheduled task] " + value["prompt"], event=True, job_id=value["id"])
                else:
                    launch(value, event=kind == "team")
        except KeyboardInterrupt:
            lead.cancel.set()
            print("\n已请求停止，/quit 退出。")
        except Exception as exc:
            print(f"Error: {exc}")


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="DeepSeek Code: inspectable Windows-first coding agent")
    parser.add_argument("--workspace", default=str(Path.cwd()), help="Agent can read/edit this project")
    parser.add_argument("--permission", choices=["ask", "auto-edit", "read-only"], default="ask")
    parser.add_argument("--session", help="Resume saved session ID")
    parser.add_argument("--prompt", help="Run one task non-interactively; operations needing approval are denied")
    parser.add_argument("--goal", help="Completion condition for --prompt")
    parser.add_argument("--doctor", action="store_true", help="Check local configuration without API calls")
    parser.add_argument("--demo", action="store_true", help="Offline tool-loop demo in a disposable directory")
    args = parser.parse_args()
    if args.demo:
        with tempfile.TemporaryDirectory(prefix="deepseek-code-demo-") as tmp:
            config = Config(Path(tmp), permission="auto-edit")
            rt = Runtime(config, llm=DemoLLM(), interactive=False)
            try:
                print(Agent(rt).run("Create and read hello.py")["text"])
                print("实际文件内容：", (Path(tmp) / "hello.py").read_text(encoding="utf-8"))
            finally:
                rt.close()
        return
    config = Config.from_env(args.workspace, args.permission)
    if args.doctor:
        doctor(config)
        return
    if not config.key or config.key == "YOUR_DEEPSEEK_API_KEY":
        parser.error("请复制 .env.example 为 .env，填写 DEEPSEEK_API_KEY。先体验可运行 --demo。")
    if args.goal and not args.prompt:
        parser.error("--goal requires --prompt; interactive mode uses /goal")
    rt = Runtime(config, interactive=not bool(args.prompt))
    try:
        if args.session:
            from .state import identifier
            path = rt.store.path("sessions", identifier(args.session) + ".json")
            if not path.exists():
                raise ValueError("Session not found")
        lead = Agent(rt, session_id=args.session)
        lead.save()
        if args.prompt:
            result = lead.run(args.prompt, goal=args.goal)
            print(result["text"])
            print("Session:", lead.session_id)
            if result["status"] not in ("done", "achieved"):
                raise SystemExit(1)
        else:
            interactive(rt, lead)
    finally:
        rt.close()
