"""Local tools and hooks. Shell access is permission-controlled, not a sandbox."""
import difflib
import fnmatch
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid

from .config import APP
from .state import identifier

SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".deepseek-agent"}


def safe_path(root, raw, internal=False):
    path = (root / raw).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("路径越出了当前工作目录")
    parts = path.relative_to(root.resolve()).parts
    if not internal and any(p in (".git", ".deepseek-agent") for p in parts):
        raise ValueError("请通过专用工具访问 Agent 状态或 Git 元数据")
    if any(p == ".env" or p.startswith(".env.") and p != ".env.example" for p in parts):
        raise ValueError("不向模型暴露 .env 密钥文件")
    return path


def walk_files(root, pattern="*", limit=300):
    found = []
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not (Path(parent) / d).is_symlink())
        for name in sorted(files):
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            if not (fnmatch.fnmatch(relative, pattern) or
                    pattern.startswith("**/") and fnmatch.fnmatch(relative, pattern[3:])):
                continue
            try:
                safe_path(root, relative)
            except ValueError:
                continue
            found.append(relative)
            if len(found) >= limit:
                return found
    return found


class Hooks:
    def __init__(self):
        self.callbacks = {}

    def register(self, event, callback):
        self.callbacks.setdefault(event, []).append(callback)

    def trigger(self, event, *args):
        for callback in self.callbacks.get(event, []):
            result = callback(*args)
            if result is not None:
                return result
        return None


class ToolRegistry:
    def __init__(self):
        self.entries = {}

    def add(self, name, description, handler, properties, required=(), effect="read"):
        if name in self.entries:
            raise ValueError("Duplicate tool: " + name)
        definition = {"type": "function", "function": {
            "name": name, "description": description, "parameters": {
                "type": "object", "properties": properties,
                "required": list(required), "additionalProperties": False}}}
        self.entries[name] = (definition, handler, effect)

    def definitions(self):
        return [entry[0] for entry in self.entries.values()]

    def call(self, name, arguments):
        if name not in self.entries:
            raise ValueError("Unknown tool: " + name)
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        definition, handler, _ = self.entries[name]
        schema = definition["function"]["parameters"]
        for required in schema.get("required", []):
            if required not in arguments:
                raise ValueError("Missing argument: " + required)
        types = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict}
        for key, value in arguments.items():
            if key not in schema["properties"] and schema.get("additionalProperties") is False:
                raise ValueError("Unknown argument: " + key)
            kind = schema["properties"].get(key, {}).get("type")
            if kind in types and (not isinstance(value, types[kind]) or kind == "integer" and isinstance(value, bool)):
                raise ValueError("Wrong argument type: " + key)
        return handler(**arguments)


def S(description=""):
    return {"type": "string", "description": description}


def I(description=""):
    return {"type": "integer", "description": description}


B = {"type": "boolean"}
A = {"type": "array", "items": {"type": "string"}}


class Shell:
    def __init__(self, store, stop):
        self.store, self.stop = store, stop
        self.processes = set()
        self.lock = threading.Lock()

    @staticmethod
    def terminate(process):
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            finally:
                # taskkill can fail under restricted permissions; reap direct child too.
                if process.poll() is None:
                    process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def close(self):
        with self.lock:
            for process in list(self.processes):
                self.terminate(process)

    def run(self, command, cwd, timeout=120, cancel=None):
        timeout = max(1, min(int(timeout), 600))
        if self.stop.is_set() or cancel is not None and cancel.is_set():
            return "Cancelled before execution"
        if os.name == "nt":
            shell = shutil.which("pwsh") or shutil.which("powershell.exe")
            if not shell:
                raise RuntimeError("PowerShell not found")
            wrapped = ("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
                       "$ErrorActionPreference = 'Continue'; "
                       "$PSNativeCommandUseErrorActionPreference = $false; "
                       "$Error.Clear(); $global:LASTEXITCODE = 0;\n"
                       "try {\n" + command + "\n} catch { Write-Output $_; exit 1 }\n"
                       "$agentNativeExit = $LASTEXITCODE;\n"
                       "$agentRealErrors = @($Error | Where-Object { "
                       "$_.FullyQualifiedErrorId -notmatch '^NativeCommandError' });\n"
                       "if ($agentNativeExit -ne 0) { exit $agentNativeExit }\n"
                       "if ($agentRealErrors.Count -gt 0) { exit 1 }\nexit 0\n")
            argv = [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", wrapped]
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW}
        else:
            argv = ["/bin/sh", "-c", command]
            kwargs = {"start_new_session": True}
        # File-backed capture prevents verbose commands filling RAM/pipes.
        path = self.store.path("outputs", "shell_" + uuid.uuid4().hex + ".txt")
        with path.open("wb") as output:
            process = subprocess.Popen(argv, cwd=cwd, stdout=output,
                                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kwargs)
            with self.lock:
                self.processes.add(process)
            start = time.monotonic()
            reason = ""
            try:
                while process.poll() is None:
                    if self.stop.is_set() or cancel is not None and cancel.is_set():
                        reason = "cancelled"
                    elif time.monotonic() - start > timeout:
                        reason = "timeout"
                    if reason:
                        self.terminate(process)
                        break
                    time.sleep(0.1)
                process.wait(timeout=10)
            finally:
                self.terminate(process)
                with self.lock:
                    self.processes.discard(process)
        with path.open("rb") as saved:
            preview = saved.read(16000).decode("utf-8", errors="replace")
        return f"exit_code={process.returncode}; {reason}\nFull output: {path}\n{preview}"


class LocalTools:
    def __init__(self, runtime, agent):
        self.rt, self.agent = runtime, agent
        self.root = agent.workspace

    def read_file(self, path, offset=1, limit=200):
        target = safe_path(self.root, path, internal=True)
        if target.stat().st_size > 8_000_000:
            raise ValueError("文件超过 8 MB，请使用范围明确的命令读取")
        lines = target.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        offset, limit = max(1, offset), max(1, min(limit, 500))
        return "\n".join(f"{i+1}: {lines[i]}" for i in range(offset-1, min(len(lines), offset-1+limit)))

    def glob(self, pattern="*"):
        return "\n".join(walk_files(self.root, pattern)) or "No files"

    def search(self, query, pattern="*", limit=80):
        found = []
        for relative in walk_files(self.root, pattern, 5000):
            path = self.root / relative
            if path.stat().st_size > 1_000_000:
                continue
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (UnicodeError, OSError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if query.casefold() in line.casefold():
                    found.append(f"{relative}:{i}: {line[:400]}")
                    if len(found) >= max(1, min(limit, 200)):
                        return "\n".join(found)
        return "\n".join(found) or "No matches"

    def _write(self, path, content, old):
        target = safe_path(self.root, path)
        # Preserve a recoverable copy before overwriting an existing file.
        if target.exists():
            backup = self.rt.store.path("backups", uuid.uuid4().hex + "_" + target.name)
            shutil.copy2(target, backup)
        diff = "\n".join(difflib.unified_diff(old.splitlines(), content.splitlines(),
                                           fromfile=path, tofile=path, lineterm=""))
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
            tmp = Path(temp.name)
            temp.write(content.encode("utf-8"))
        try:
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        return f"Wrote {len(content.encode('utf-8'))} bytes.\n{diff[:10000]}"

    def write_file(self, path, content):
        target = safe_path(self.root, path)
        old = target.read_text(encoding="utf-8") if target.exists() else ""
        return self._write(path, content, old)

    def edit_file(self, path, old_text, new_text):
        target = safe_path(self.root, path)
        old = target.read_text(encoding="utf-8")
        if not old_text or old.count(old_text) != 1:
            raise ValueError("old_text 必须非空且在文件中恰好出现一次")
        return self._write(path, old.replace(old_text, new_text, 1), old)

    def shell(self, command, timeout=120, background=False):
        if background:
            return self.rt.background(command, self.agent, timeout)
        return self.rt.shell.run(command, self.root, timeout, self.agent.cancel)

    def todo_write(self, todos):
        if len(todos) > 20:
            raise ValueError("At most 20 todos")
        if any(not isinstance(t, dict) or not t.get("content") or
               t.get("status") not in ("pending", "in_progress", "completed") for t in todos):
            raise ValueError("Each todo needs content and pending/in_progress/completed status")
        if sum(t["status"] == "in_progress" for t in todos) > 1:
            raise ValueError("Only one in_progress todo")
        self.agent.todos = todos
        return todos

    def memory(self, action="list", name="", content=""):
        folder = self.rt.store.path("memory", "placeholder").parent
        if action == "list":
            return {p.stem: p.read_text(encoding="utf-8")[:6000] for p in sorted(folder.glob("*.md"))}
        path = folder / (identifier(name) + ".md")
        if action == "get":
            return path.read_text(encoding="utf-8")
        if action == "save":
            if not content.strip() or len(content) > 6000:
                raise ValueError("Memory must contain 1..6000 characters")
            path.write_text(content, encoding="utf-8")
            return "Memory saved"
        raise ValueError("Use list/get/save")

    def skills(self):
        found = {}
        for root in (APP / "skills", self.root / "skills"):
            for path in sorted(root.glob("*/SKILL.md")):
                if path.resolve().is_relative_to(root.resolve()):
                    text = path.read_text(encoding="utf-8-sig")
                    name = path.parent.name
                    match = re.search(r"(?m)^description:\s*(.+)$", text)
                    found[name] = (path, match.group(1) if match else text.splitlines()[0])
        return found

    def load_skill(self, name):
        skills = self.skills()
        if name not in skills:
            raise ValueError("Unknown skill. Available: " + ", ".join(skills))
        return skills[name][0].read_text(encoding="utf-8")

    def register(self, reg):
        reg.add("read_file", "Read a UTF-8 file with line numbers. Can read saved tool outputs.", self.read_file,
                {"path": S(), "offset": I(), "limit": I()}, ["path"])
        reg.add("glob", "List workspace files matching a glob; excludes generated directories.", self.glob, {"pattern": S()})
        reg.add("search", "Search literal text in UTF-8 project files.", self.search,
                {"query": S(), "pattern": S(), "limit": I()}, ["query"])
        reg.add("write_file", "Create or overwrite UTF-8 file. Existing files are backed up.", self.write_file,
                {"path": S(), "content": S()}, ["path", "content"], "write")
        reg.add("edit_file", "Replace exactly one occurrence of old_text; backed up before edit.", self.edit_file,
                {"path": S(), "old_text": S(), "new_text": S()}, ["path", "old_text", "new_text"], "write")
        reg.add("shell", "Run PowerShell on Windows, sh on Unix. Output is already captured: prefer a plain command without 2>&1 or Out-String. Use one native command per call so its exit code is unambiguous. May run in background.", self.shell,
                {"command": S(), "timeout": I(), "background": B}, ["command"], "shell")
        reg.add("todo_write", "Replace your short task plan and progress.", self.todo_write,
                {"todos": {"type": "array", "items": {"type": "object", "properties": {
                    "content": S(), "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
                    "required": ["content", "status"]}}}, ["todos"])
        reg.add("load_skill", "Load full instructions of a skill in the system catalog.", self.load_skill, {"name": S()}, ["name"])
        reg.add("memory", "Persistent project preferences. Save only durable facts explicitly worth retaining.", self.memory,
                {"action": S("list/get/save"), "name": S("ASCII identifier"), "content": S()}, effect="memory")
