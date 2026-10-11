import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dscode.agent import Agent
from dscode.cli import DemoLLM
from dscode.config import APP, Config
from dscode.llm import DeepSeek
from dscode.mcp import StdioMCP
from dscode.runtime import Runtime
from dscode.scheduler import Scheduler, cron_fields, cron_matches
from dscode.state import Store, WorkspaceLock
from dscode.tools import safe_path


class SequenceModel:
    tokens = 0
    calls = 0

    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def chat(self, messages, tools=None, max_tokens=None):
        self.calls += 1
        self.requests.append(json.loads(json.dumps(messages)))
        return next(self.replies)


def answer(text):
    return {"role": "assistant", "content": text}, "stop"


def tool(name, args, call_id="x"):
    return {"role": "assistant", "content": None, "reasoning_content": "preserve this field",
            "tool_calls": [{"id": call_id, "type": "function", "function": {
                "name": name, "arguments": json.dumps(args)}}]}, "tool_calls"


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.rt = Runtime(Config(self.root, permission="auto-edit"), DemoLLM(), interactive=False)

    def tearDown(self):
        self.rt.close()
        self.temp.cleanup()

    def test_real_loop_and_resume(self):
        agent = Agent(self.rt)
        result = agent.run("write then read")
        self.assertEqual(result["status"], "done")
        self.assertTrue((self.root / "hello.py").exists())
        restored = Agent(self.rt, session_id=agent.session_id)
        self.assertEqual(agent.messages, restored.messages)
        self.assertEqual([m["role"] for m in agent.messages],
                         ["user", "assistant", "tool", "assistant", "tool", "assistant"])

    def test_permission_denies_shell_noninteractive(self):
        model = SequenceModel([tool("shell", {"command": "echo SHOULD_NOT_RUN"}), answer("denied")])
        self.rt.llm = model
        agent = Agent(self.rt)
        agent.run("run command")
        self.assertIn("Permission denied", agent.messages[2]["content"])
        self.assertEqual(model.requests[1][2]["reasoning_content"], "preserve this field")

    def test_invalid_arguments_dont_crash_loop(self):
        self.rt.llm = SequenceModel([tool("read_file", {"path": 42}), answer("invalid")])
        agent = Agent(self.rt)
        self.assertEqual(agent.run("read")["status"], "done")
        self.assertIn("Error", agent.messages[2]["content"])

    def test_path_escape_and_env_protected(self):
        for path in ("../secret.txt", ".env", ".git/config", ".deepseek-agent/sessions/x.json"):
            with self.assertRaises(ValueError):
                safe_path(self.root, path)

    def test_write_backup_exact_edit(self):
        agent = Agent(self.rt)
        agent.local.write_file("a.txt", "old\n")
        agent.local.edit_file("a.txt", "old", "new")
        self.assertEqual((self.root / "a.txt").read_text(), "new\n")
        backups = list((self.rt.store.root / "backups").glob("*"))
        self.assertEqual(backups[0].read_text(), "old\n")
        with self.assertRaises(ValueError):
            agent.local.edit_file("a.txt", "absent", "x")

    def test_dependencies_cycles_and_owner(self):
        store = self.rt.store
        a = store.task("create", subject="A")["id"]
        b = store.task("create", subject="B")["id"]
        store.task("dependencies", task_id=b, depends_on=[a])
        with self.assertRaises(ValueError):
            store.task("dependencies", task_id=a, depends_on=[b])
        with self.assertRaises(ValueError):
            store.task("claim", task_id=b)
        store.task("claim", task_id=a, owner="alice")
        with self.assertRaises(ValueError):
            store.task("complete", task_id=a, owner="bob")
        store.task("complete", task_id=a, owner="alice")
        self.assertEqual(store.task("claim", task_id=b)["status"], "in_progress")

    def test_incomplete_tool_batch_repaired_without_replay(self):
        agent = Agent(self.rt)
        agent.messages = [{"role": "user", "content": "write"}, tool("write_file", {"path": "x", "content": "a"})[0]]
        agent.save()
        resumed = Agent(self.rt, session_id=agent.session_id)
        self.assertEqual(resumed.messages[-1]["tool_call_id"], "x")
        self.assertIn("unknown", resumed.messages[-1]["content"])
        self.assertFalse((self.root / "x").exists())

    def test_subagent_can_complete_task_and_releases_unfinished_claim(self):
        task = self.rt.store.task("create", subject="child task")
        self.rt.llm = SequenceModel([
            tool("tasks", {"action": "claim", "task_id": task["id"]}),
            tool("tasks", {"action": "complete", "task_id": task["id"]}), answer("done")])
        self.assertEqual(self.rt.subagent("do child task")["status"], "done")
        self.assertEqual(self.rt.store.task("get", task_id=task["id"])["status"], "completed")
        other = self.rt.store.task("create", subject="unfinished")
        self.rt.llm = SequenceModel([
            tool("tasks", {"action": "claim", "task_id": other["id"]}), answer("need more information")])
        self.rt.subagent("investigate")
        self.assertEqual(self.rt.store.task("get", task_id=other["id"])["status"], "pending")

    def test_restart_releases_stale_task_claim(self):
        task = self.rt.store.task("create", subject="interrupted")
        self.rt.store.task("claim", task_id=task["id"], owner="old_worker")
        self.rt.close()
        self.rt = Runtime(Config(self.root), DemoLLM(), interactive=False)
        restored = self.rt.store.task("get", task_id=task["id"])
        self.assertEqual(restored["status"], "pending")
        self.assertIsNone(restored["owner"])

    def test_goal_continue_then_achieved(self):
        self.rt.llm = SequenceModel([
            answer("not yet"), answer('{"ok":false,"reason":"read evidence","impossible":false}'),
            tool("glob", {}), answer("found files"),
            answer('{"ok":true,"reason":"listing evidence present","impossible":false}')])
        agent = Agent(self.rt)
        result = agent.run("list files", goal="files listed")
        self.assertEqual(result["status"], "achieved")
        self.assertEqual(agent.goal, "")

    def test_compaction_no_orphan_tool_messages(self):
        self.rt.llm = SequenceModel([answer("File was read. Need to edit it.")])
        agent = Agent(self.rt)
        agent.current_request = "edit file"
        agent.messages = [{"role": "user", "content": "edit file"},
                          tool("read_file", {"path": "a.txt"})[0],
                          {"role": "tool", "tool_call_id": "x", "content": "old content"}]
        agent.compact(force=True)
        self.assertEqual(len(agent.messages), 1)
        self.assertEqual(agent.messages[0]["role"], "user")
        self.assertIn("edit file", agent.messages[0]["content"])
        self.assertTrue(list((self.rt.store.root / "transcripts").glob("*.json")))

    def test_truncated_tool_call_never_executed(self):
        response = tool("write_file", {"path": "danger.txt", "content": "x"})[0]
        self.rt.llm = SequenceModel([(response, "length")])
        self.assertEqual(Agent(self.rt).run("test")["status"], "limit")
        self.assertFalse((self.root / "danger.txt").exists())

    def test_shell_real_output_and_timeout(self):
        result = self.rt.shell.run("echo hello", self.root, timeout=10)
        self.assertIn("exit_code=0", result)
        self.assertIn("hello", result)
        command = "Start-Sleep -Seconds 5" if sys.platform == "win32" else "sleep 5"
        result = self.rt.shell.run(command, self.root, timeout=1)
        self.assertIn("timeout", result)

    @unittest.skipUnless(sys.platform == "win32", "Windows PowerShell regression")
    def test_powershell_stderr_and_real_failures(self):
        from unittest.mock import patch
        import shutil
        from dscode.tools import Shell
        windows_ps = shutil.which("powershell.exe")
        native = "& '" + sys.executable.replace("'", "''") + "' '" + str(APP / "tests/stderr_fixture.py").replace("'", "''") + "'"
        commands = [
            (native + " 0 2>&1 | Out-String", 0),
            (native + " 7 2>&1 | Out-String", 7),
            ("Write-Output 'normal report'; [Console]::Error.WriteLine('normal report')", 0),
            ("cmd /c exit 7", 7),
            ("Get-Item definitely_missing_file_for_agent_test", 1),
            ("throw 'real terminating error'", 1),
            ("Write-Error 'real error'; Write-Output 'later output'", 1),
        ]
        # Force the older Windows PowerShell, even on machines with pwsh installed.
        with patch("dscode.tools.shutil.which", side_effect=lambda name: windows_ps if name == "powershell.exe" else None):
            for command, code in commands:
                with self.subTest(command=command):
                    result = self.rt.shell.run(command, self.root, timeout=10)
                    self.assertTrue(result.startswith(f"exit_code={code};"), result)

    def test_repeated_failed_shell_stops_before_third_approval(self):
        from unittest.mock import Mock
        self.rt.approve = Mock(return_value=True)
        self.rt.shell.run = Mock(return_value="exit_code=1;\nfailed")
        self.rt.llm = SequenceModel([tool("shell", {"command": "bad"}, str(i)) for i in range(3)])
        agent = Agent(self.rt)
        self.assertEqual(agent.run("try")["status"], "blocked")
        self.assertEqual(self.rt.approve.call_count, 2)
        self.assertEqual(self.rt.shell.run.call_count, 2)
        self.assertEqual(sum(m["role"] == "tool" for m in agent.messages), 3)

    def test_successful_shell_may_be_repeated(self):
        from unittest.mock import Mock
        self.rt.approve = Mock(return_value=True)
        self.rt.shell.run = Mock(return_value="exit_code=0;\nok")
        self.rt.llm = SequenceModel([tool("shell", {"command": "ok"}, str(i)) for i in range(3)] + [answer("done")])
        self.assertEqual(Agent(self.rt).run("check")["status"], "done")
        self.assertEqual(self.rt.shell.run.call_count, 3)

    def test_background_result_wakes_lead(self):
        agent = Agent(self.rt)
        self.rt.background("echo bg-ok", agent, 10)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            kind, value = self.rt.events.get(timeout=12)
            if kind == "team":
                self.assertIn("bg-ok", value)
                return
        self.fail("No background delivery")

    def test_team_final_delivery_and_idle_shutdown(self):
        self.rt.llm = SequenceModel([answer("Code reviewed")])
        task = self.rt.store.task("create", subject="review")
        self.rt.spawn("alice", "reviewer", "Review", task["id"])
        deadline = time.monotonic() + 5
        delivered = False
        while time.monotonic() < deadline:
            kind, value = self.rt.events.get(timeout=5)
            if kind == "team":
                self.assertIn("Code reviewed", value)
                delivered = True
                break
        self.assertTrue(delivered)
        self.rt.shutdown("alice")
        for t in self.rt.threads:
            t.join(timeout=3)
        self.assertEqual(self.rt.teams["alice"]["status"], "stopped")
        self.assertEqual(self.rt.store.task("get", task_id=task["id"])["status"], "pending")

    def test_plan_gate_prevents_edit_before_approval(self):
        agent = Agent(self.rt, name="alice")
        self.rt.teams["alice"] = {"plan_gate": "required", "agent": agent, "queue": queue.Queue(), "status": "idle"}
        self.assertIn("Blocked", agent.permission("write_file", {"path":"x", "content":"y"}, "write"))
        self.rt.teams["alice"]["plan_gate"] = "approved"
        self.assertIsNone(agent.permission("write_file", {}, "write"))

    def test_workflow_journal_resume(self):
        class ReviewModel:
            model_calls = 0
            def chat(self, messages, tools=None, max_tokens=None):
                self.model_calls += 1
                if "Finding:" in messages[-1]["content"]:
                    return answer('{"isReal":true,"reason":"supported"}')
                return answer('{"findings":[{"title":"issue","severity":"high"}]}')
        model = ReviewModel()
        self.rt.llm = model
        first = self.rt.workflow.run("some code")
        self.assertEqual(first["status"], "completed")
        calls = model.model_calls
        second = self.rt.workflow.run("some code", first["run_id"])
        self.assertEqual(second["findings"], first["findings"])
        self.assertEqual(calls, model.model_calls)
        with self.assertRaises(ValueError):
            self.rt.workflow.run("different code", first["run_id"])

    def test_exclusive_workspace_lock(self):
        # subprocess is required: flock locks are process-specific on some platforms.
        result = subprocess.run([sys.executable, "-c",
            "from pathlib import Path; from dscode.state import WorkspaceLock; "
            "WorkspaceLock(Path(__import__('sys').argv[1]))", str(self.rt.store.root / "runtime.lock")],
            cwd=APP, capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_real_git_worktree_routes_teammate_edits(self):
        def git(*args):
            return subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                                  text=True, check=True).stdout
        git("init")
        (self.root / "tracked.txt").write_text("initial", encoding="utf-8")
        git("add", "tracked.txt")
        git("-c", "user.name=Agent Test", "-c", "user.email=test@example.invalid",
            "commit", "-m", "fixture", "--no-gpg-sign")
        task = self.rt.store.task("create", subject="isolated edit")
        self.rt.worktree("isolated", task["id"])
        with self.assertRaises(ValueError):
            self.rt.task_tool(Agent(self.rt), action="claim", task_id=task["id"])
        self.rt.llm = SequenceModel([
            tool("edit_file", {"path": "tracked.txt", "old_text": "initial", "new_text": "changed"}),
            tool("tasks", {"action": "complete", "task_id": task["id"]}), answer("done")])
        self.rt.spawn("alice", "editor", "edit in worktree", task["id"])
        while True:
            kind, value = self.rt.events.get(timeout=10)
            if kind == "team":
                break
        self.rt.shutdown("alice")
        for thread in self.rt.threads:
            thread.join(timeout=3)
        self.assertEqual((self.root / "tracked.txt").read_text(), "initial")
        self.assertEqual((self.rt.store.root / "worktrees/isolated/tracked.txt").read_text(), "changed")
        self.assertEqual(self.rt.store.task("get", task_id=task["id"])["status"], "completed")


class ProtocolTests(unittest.TestCase):
    def test_interactive_cli_approval_via_local_http(self):
        replies = iter([tool("write_file", {"path": "approved.txt", "content": "approved"}),
                        answer("CLI finished")])
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                message, reason = next(replies)
                data = json.dumps({"choices": [{"message": message, "finish_reason": reason}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                env = dict(os.environ, DEEPSEEK_API_KEY="local-test-key",
                           DEEPSEEK_BASE_URL=f"http://127.0.0.1:{server.server_port}")
                process = subprocess.Popen([sys.executable, "-u", str(APP / "agent.py"), "--workspace", temp],
                    cwd=APP, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8")
                lines = queue.Queue()
                def read():
                    for line in process.stdout:
                        lines.put(line)
                reader = threading.Thread(target=read, daemon=True)
                reader.start()
                transcript = []
                def until(marker):
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        line = lines.get(timeout=max(0.1, deadline-time.monotonic()))
                        transcript.append(line)
                        if marker in line:
                            return
                    self.fail("Missing marker " + marker + "\n" + "".join(transcript))
                try:
                    until("请输入任务 >")
                    process.stdin.write("y\n")
                    process.stdin.flush()
                    until("当前没有待确认操作")
                    process.stdin.write("create approved.txt\n")
                    process.stdin.flush()
                    until("允许？")
                    self.assertFalse((Path(temp) / "approved.txt").exists())
                    process.stdin.write("y\n")
                    process.stdin.flush()
                    until("[done]")
                    self.assertEqual((Path(temp) / "approved.txt").read_text(), "approved")
                    process.stdin.write("/quit\n")
                    process.stdin.flush()
                    self.assertEqual(process.wait(timeout=10), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    reader.join(timeout=2)
                    process.stdin.close()
                    process.stdout.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_real_mcp_stdio_discovery_and_call(self):
        with tempfile.TemporaryDirectory() as temp:
            client = StdioMCP({"command": "{python}", "args": ["{app}/examples/mcp_server.py"]}, Path(temp), APP)
            try:
                self.assertIn("add", [t["name"] for t in client.tools])
                self.assertEqual(client.call("add", a=2, b=5), "7")
                self.assertIn("MCP error", client.call("add", a="x", b=5))
            finally:
                client.close()
            self.assertIsNotNone(client.process.poll())

    def test_http_payload_preserves_reasoning_and_tool_pairing(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append(body)
                response = tool("glob", {})[0] if len(received) == 1 else answer("ok")[0]
                data = json.dumps({"choices": [{"message": response, "finish_reason": "stop"}], "usage":{"total_tokens": 10}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = Config(Path.cwd(), key="test-key", base_url=f"http://127.0.0.1:{server.server_port}", thinking="enabled")
            llm = DeepSeek(config)
            messages = [{"role": "user", "content": "list"}]
            first, _ = llm.chat(messages, tools=[{"type":"function"}])
            messages.extend([first, {"role":"tool", "tool_call_id":"x", "content":"a.py"}])
            llm.chat(messages, tools=[{"type":"function"}])
            self.assertEqual(received[1]["messages"][1]["reasoning_content"], "preserve this field")
            self.assertEqual(received[1]["messages"][2]["role"], "tool")
            self.assertEqual(received[1]["thinking"], {"type":"enabled"})
            self.assertEqual(llm.tokens, 20)
        finally:
            server.shutdown()
            server.server_close()

    def test_cron_dedup_durable_and_ack(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp))
            events = []
            scheduler = Scheduler(store, lambda kind, value: events.append((kind,value)), threading.Event())
            job = scheduler.manage("create", cron="0 9 * * *", prompt="review", recurring=False)
            moment = datetime(2026,10,9,9,0)
            scheduler.poll(moment)
            scheduler.poll(moment)
            self.assertEqual(len(events), 1)
            restored_events = []
            restored = Scheduler(store, lambda *args: restored_events.append(args), threading.Event())
            restored.poll(datetime(2026,10,9,10,0))
            self.assertEqual(len(restored_events), 1)
            restored.ack(job, True)
            self.assertEqual(restored.manage(), [])
            self.assertTrue(cron_matches("*/5 * * * *", datetime(2026,10,9,9,10)))
            with self.assertRaises(ValueError):
                cron_fields("99 9 * * *")


if __name__ == "__main__":
    unittest.main()
