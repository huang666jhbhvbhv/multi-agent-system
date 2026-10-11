"""Shared services: approvals, background jobs, team mail, tasks, MCP and cron."""
from dataclasses import dataclass, field
import json
from pathlib import Path
import queue
import subprocess
import threading
import uuid

from .agent import Agent
from .llm import DeepSeek
from .mcp import MCPManager
from .scheduler import Scheduler
from .state import Store, WorkspaceLock, atomic_json, identifier, read_json
from .tools import ToolRegistry, Shell, S, I, B, A
from .workflow import Workflow


@dataclass
class Approval:
    detail: str
    cancel: threading.Event
    ready: threading.Event = field(default_factory=threading.Event)
    allowed: bool = False


class Runtime:
    def __init__(self, config, llm=None, interactive=True):
        self.config = config
        if not config.workspace.is_dir():
            raise ValueError("Workspace must be an existing directory")
        self.store = Store(config.workspace)
        self.workspace_lock = WorkspaceLock(self.store.root / "runtime.lock")
        self.llm = llm or DeepSeek(config)
        self.stop = threading.Event()
        self.events = queue.Queue()
        self.approvals = queue.Queue()
        self.interactive = interactive
        self.shell = Shell(self.store, self.stop)
        self.mcp = MCPManager(config)
        self.workflow = Workflow(self)
        self.scheduler = Scheduler(self.store, self.emit, self.stop)
        self.teams = {}
        self.backgrounds = {}
        self.lock = threading.RLock()
        self.threads = []
        self.closing = False
        # No worker from a previous process survives this exclusive workspace lock.
        for task in self.store.task():
            if task["status"] == "in_progress":
                self.store.task("release", task_id=task["id"], owner=task["owner"])

    def emit(self, kind, value):
        self.events.put((kind, value))

    def approve(self, detail, cancel):
        if not self.interactive or self.stop.is_set() or cancel.is_set():
            return False
        request = Approval(detail, cancel)
        self.approvals.put(request)
        while not request.ready.wait(0.1):
            if self.stop.is_set() or cancel.is_set():
                return False
        return request.allowed and not cancel.is_set() and not self.stop.is_set()

    def start_thread(self, fn, name):
        thread = threading.Thread(target=fn, name=name, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def start(self):
        self.start_thread(self.scheduler.run, "cron")

    def pending_work(self, owner):
        with self.lock:
            return (any(b["owner"] == owner and b["status"] == "running" for b in self.backgrounds.values()) or
                    owner == "lead" and any(t["status"] in ("working", "waiting_plan") for t in self.teams.values()))

    def background(self, command, agent, timeout):
        with self.lock:
            if sum(j["status"] == "running" for j in self.backgrounds.values()) >= 4:
                raise ValueError("At most 4 background commands")
            job_id = "bg_" + uuid.uuid4().hex[:8]
            self.backgrounds[job_id] = dict(id=job_id, owner=agent.name, status="running", command=command)
        def work():
            try:
                result = self.shell.run(command, agent.workspace, timeout, agent.cancel)
            except Exception as exc:
                result = f"Error: {exc}"
            with self.lock:
                self.backgrounds[job_id].update(status="finished", result=result)
            self.deliver("background", agent.name, f"[{job_id}] {result}")
        self.start_thread(work, job_id)
        return f"Started {job_id}; completion will be delivered automatically."

    def deliver(self, sender, to, text):
        # Append-only audit mailbox. Live delivery uses queues, not repeated model polling.
        identifier(to)
        record = dict(sender=sender, to=to, content=text)
        with self.lock:
            with self.store.path("mailboxes", to + ".jsonl").open("a", encoding="utf-8") as out:
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
            if to == "lead":
                self.emit("team", f"[Message from {sender}]\n{text}")
            elif to in self.teams and self.teams[to]["status"] != "stopped":
                self.teams[to]["queue"].put(f"[Message from {sender}]\n{text}")
            else:
                raise ValueError("Unknown or stopped teammate")
        return "Message delivered"

    def spawn(self, name, role, prompt, task_id, require_plan=False):
        identifier(name)
        with self.lock:
            if name in ("lead", "agent") or name in self.teams:
                raise ValueError("Name already used or reserved")
            if sum(t["status"] != "stopped" for t in self.teams.values()) >= 3:
                raise ValueError("At most 3 persistent teammates")
            task = self.store.task("claim", task_id=task_id, owner=name)
            try:
                bindings = read_json(self.store.root / "worktrees.json", {})
                workspace = Path(bindings.get(task_id, str(self.config.workspace)))
                if not workspace.is_dir():
                    raise ValueError("Task worktree missing")
                agent = Agent(self, name=name, role=role, workspace=workspace)
            except Exception:
                self.store.task("release", task_id=task_id, owner=name)
                raise
            inbox = queue.Queue()
            team = dict(agent=agent, queue=inbox, status="working", task_id=task_id,
                        plan_gate="required" if require_plan else "none", plan="")
            self.teams[name] = team
            inbox.put(f"{prompt}\nAssigned task (already claimed): {json.dumps(task, ensure_ascii=False)}. "
                      "Call tasks(action='complete', task_id=...) when finished, then report evidence. " +
                      ("Submit a plan before edits/shell; wait for lead approval." if require_plan else ""))
        def work():
            try:
                while not self.stop.is_set() and not agent.cancel.is_set():
                    try:
                        text = inbox.get(timeout=0.3)
                    except queue.Empty:
                        continue
                    if text is None:
                        break
                    team["status"] = "working"
                    result = agent.run(text)
                    if agent.cancel.is_set() or self.stop.is_set():
                        break
                    team["status"] = "waiting_plan" if team["plan_gate"] == "pending" else "idle"
                    if team["plan_gate"] != "pending":
                        self.deliver(name, "lead", f"Status: {result['status']}\n{result['text']}")
            finally:
                for task in self.store.task():
                    if task["status"] == "in_progress" and task["owner"] == name:
                        self.store.task("release", task_id=task["id"], owner=name)
                team["status"] = "stopped"
                self.emit("notice", f"[{name}] stopped")
        self.start_thread(work, "team-" + name)
        return f"Started teammate {name}; end your turn, results will wake you."

    def team_list(self):
        with self.lock:
            return {name: {k:t[k] for k in ("status", "task_id", "plan_gate")} for name,t in self.teams.items()}

    def shutdown(self, name):
        team = self.teams[name]
        team["agent"].cancel.set()
        team["queue"].put(None)
        return "Shutdown requested; in-flight API may finish before thread exits."

    def plan(self, agent, plan):
        team = self.teams[agent.name]
        request_id = "plan_" + uuid.uuid4().hex[:12]
        team.update(plan=plan, plan_gate="pending", plan_id=request_id)
        self.deliver(agent.name, "lead", f"Plan approval requested: {request_id}\n{plan}")
        return "Plan submitted. End your turn and wait for review."

    def review_plan(self, name, request_id, approve, feedback=""):
        team = self.teams[name]
        if team.get("plan_id") != request_id or team["plan_gate"] != "pending":
            raise ValueError("Stale or unknown plan request")
        team["plan_gate"] = "approved" if approve else "required"
        return self.deliver("lead", name, f"[Plan {'approved' if approve else 'rejected'}] {feedback}")

    def task_tool(self, agent, **args):
        action = args.get("action", "list")
        team = self.teams.get(agent.name)
        if agent.name != "lead":
            if action not in ("list", "get", "claim", "complete", "release"):
                raise ValueError("Only lead creates tasks or changes dependencies")
            if action == "complete" and team and team["plan_gate"] in ("required", "pending"):
                raise ValueError("Plan is not approved")
        if action == "claim":
            bindings = read_json(self.store.root / "worktrees.json", {})
            task_id = args.get("task_id")
            if task_id in bindings and team is None:
                raise ValueError("Worktree tasks must be assigned to a persistent teammate")
            workspace = Path(bindings.get(task_id, str(self.config.workspace))).resolve()
            if not workspace.is_dir():
                raise ValueError("Task worktree missing")
        task = self.store.task(owner=agent.name, **args)
        if action == "claim":
            if team is not None:
                agent.workspace = workspace
                agent.local.root = workspace
                team.update(task_id=task["id"])
        return task

    def worktree(self, name, task_id):
        identifier(name)
        task = self.store.task("get", task_id=task_id)
        if task["status"] != "pending":
            raise ValueError("Create worktree before claiming/spawning")
        bindings = read_json(self.store.root / "worktrees.json", {})
        if task_id in bindings:
            raise ValueError("Task already bound")
        path = self.store.path("worktrees", name)
        if path.exists():
            raise ValueError("Worktree directory already exists")
        result = subprocess.run(["git", "worktree", "add", "-b", "agent/" + name, str(path), "HEAD"],
            cwd=self.config.workspace, capture_output=True, text=True, errors="replace", timeout=30)
        if result.returncode:
            raise ValueError(result.stderr)
        bindings[task_id] = str(path)
        atomic_json(self.store.root / "worktrees.json", bindings)
        return f"Created {path} on agent/{name}. Commit and merge explicitly; no auto-merge."

    def subagent(self, prompt, role="researcher", parent=None):
        agent = Agent(self, name="sub_" + uuid.uuid4().hex[:8], role=role)
        if parent is not None:
            agent.cancel = parent.cancel
            agent.workspace = parent.workspace
            agent.local.root = parent.workspace
        try:
            return agent.run(prompt)
        finally:
            for task in self.store.task():
                if task["status"] == "in_progress" and task["owner"] == agent.name:
                    self.store.task("release", task_id=task["id"], owner=agent.name)

    def registry(self, agent):
        reg = ToolRegistry()
        agent.local.register(reg)
        def tasks(**args):
            return self.task_tool(agent, **args)
        reg.add("tasks", "Persistent dependency task board. actions: list/get/create/dependencies/claim/complete/release. dependencies replaces the dependency list.", tasks,
                {"action": S(), "task_id": S(), "subject": S(), "description": S(), "depends_on": A}, effect="state")
        if agent.name in self.teams:
            reg.add("send_message", "Send intermediate coordination to lead or a teammate.",
                lambda to, content: self.deliver(agent.name, to, content), {"to": S(), "content": S()}, ["to", "content"], "state")
            reg.add("submit_plan", "Submit a plan and wait for lead review.", lambda plan: self.plan(agent, plan), {"plan": S()}, ["plan"], "state")
        if agent.name == "lead":
            reg.add("subagent", "Run a focused child agent with fresh context; returns final result. No further delegation.",
                    lambda prompt, role="researcher": self.subagent(prompt, role, agent),
                    {"prompt": S(), "role": S()}, ["prompt"], "delegate")
            reg.add("spawn_teammate", "Spawn a persistent teammate for a ready task; max 3. Results wake lead automatically.", self.spawn,
                    {"name": S(), "role": S(), "prompt": S(), "task_id": S(), "require_plan": B},
                    ["name", "role", "prompt", "task_id"], "delegate")
            reg.add("list_teammates", "Get teammate status.", self.team_list, {})
            reg.add("send_message", "Send follow-up work or coordination to a teammate.",
                    lambda to, content: self.deliver("lead", to, content), {"to": S(), "content": S()}, ["to", "content"], "state")
            reg.add("shutdown_teammate", "Stop a teammate; unfinished owned tasks are released.", self.shutdown, {"name": S()}, ["name"], "state")
            reg.add("review_plan", "Review the current teammate plan by exact request_id.", self.review_plan,
                {"name": S(), "request_id": S(), "approve": B, "feedback": S()}, ["name", "request_id", "approve"], "state")
            reg.add("create_worktree", "Create a task-bound Git worktree BEFORE claiming/spawning. No automatic merge.", self.worktree,
                {"name": S(), "task_id": S()}, ["name", "task_id"], "write")
            reg.add("schedule", "Persistent five-field local-time cron; runs only while this application is alive. list/create/cancel.", self.scheduler.manage,
                {"action": S(), "cron": S(), "prompt": S(), "job_id": S(), "recurring": B}, effect="schedule")
            reg.add("connect_mcp", "Start a configured local MCP server and discover its tools.", self.mcp.connect, {"name": S()}, ["name"], "mcp")
            reg.add("Workflow", "Run review-changes: four parallel textual code reviews and verification. Supply code/diff as text; optional run_id resumes cached steps.",
                    lambda text, run_id="": self.workflow.run(text, run_id, agent.cancel),
                    {"text": S(), "run_id": S()}, ["text"], "delegate")
            self.mcp.register(reg)
        return reg

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.stop.set()
        for team in list(self.teams.values()):
            team["agent"].cancel.set()
            team["queue"].put(None)
        self.shell.close()
        self.mcp.close()
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=0.3)
        alive = [t for t in self.threads if t.is_alive()]
        if alive:
            # Keep ownership until late API responses/worker cleanup stop writing state.
            def release_when_finished():
                for thread in alive:
                    thread.join()
                self.workspace_lock.close()
            threading.Thread(target=release_when_finished, daemon=True).start()
        else:
            self.workspace_lock.close()
