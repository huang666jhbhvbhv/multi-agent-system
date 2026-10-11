"""Workspace persistence, dependency tasks, and a single-process workspace lock."""
import json
import os
from pathlib import Path
import re
import threading
import uuid


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("Invalid identifier")
    return value


class WorkspaceLock:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        self.file.seek(0)
        if not self.file.read(1):
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("该工作目录已有 Agent 进程运行，请先退出另一个进程。") from None

    def close(self):
        if self.file.closed:
            return
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()


class Store:
    def __init__(self, workspace):
        self.root = workspace / ".deepseek-agent"
        if self.root.exists() and self.root.is_symlink():
            raise ValueError("State directory cannot be a symlink")
        if not self.root.resolve().is_relative_to(workspace.resolve()):
            raise ValueError("State directory escapes workspace")
        self.root.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.tasks_path = self.root / "tasks.json"

    def path(self, folder, name):
        path = self.root / folder / name
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("State path escapes workspace")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def output(self, text):
        path = self.path("outputs", uuid.uuid4().hex + ".txt")
        path.write_text(text, encoding="utf-8")
        return path

    def audit(self, event, **data):
        import datetime
        with self.lock:
            with self.path("logs", "events.jsonl").open("a", encoding="utf-8") as out:
                out.write(json.dumps({"time": datetime.datetime.now().isoformat(),
                                      "event": event, **data}, ensure_ascii=False) + "\n")

    def task(self, action="list", task_id=None, subject="", description="",
             depends_on=None, owner="lead"):
        with self.lock:
            tasks = read_json(self.tasks_path, {})
            if action == "list":
                return list(tasks.values())
            if action == "create":
                if not subject.strip():
                    raise ValueError("subject cannot be empty")
                task_id = "task_" + uuid.uuid4().hex[:12]
                tasks[task_id] = dict(id=task_id, subject=subject, description=description,
                                      status="pending", owner=None, depends_on=[])
            elif task_id not in tasks:
                raise ValueError("Unknown task_id")
            elif action == "get":
                return tasks[task_id]
            elif action == "dependencies":
                task = tasks[task_id]
                if task["status"] != "pending":
                    raise ValueError("Only pending tasks can change dependencies")
                deps = list(dict.fromkeys(depends_on or []))
                def reaches(current, target, seen):
                    if current == target:
                        return True
                    if current in seen:
                        return False
                    seen.add(current)
                    return any(reaches(d, target, seen) for d in tasks[current]["depends_on"])
                for dep in deps:
                    if dep not in tasks or reaches(dep, task_id, set()):
                        raise ValueError("Missing dependency or dependency cycle")
                task["depends_on"] = deps
            elif action == "claim":
                task = tasks[task_id]
                if task["status"] != "pending":
                    raise ValueError("Task is not pending")
                if any(t["status"] == "in_progress" and t["owner"] == owner for t in tasks.values()):
                    raise ValueError("Finish your current task first")
                if any(tasks[d]["status"] != "completed" for d in task["depends_on"]):
                    raise ValueError("Dependencies are incomplete")
                task.update(status="in_progress", owner=owner)
            elif action in ("complete", "release"):
                task = tasks[task_id]
                if task["status"] != "in_progress" or task["owner"] != owner:
                    raise ValueError("Caller must own an in-progress task")
                task.update(status="completed" if action == "complete" else "pending")
                if action == "release":
                    task["owner"] = None
            else:
                raise ValueError("Unknown task action")
            atomic_json(self.tasks_path, tasks)
            return tasks[task_id]
