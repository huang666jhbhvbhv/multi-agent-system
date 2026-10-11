"""Code-defined review -> verification pipeline with resumable semantic journal."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import uuid

from .llm import json_answer
from .state import atomic_json, read_json, identifier


class Workflow:
    def __init__(self, runtime):
        self.rt = runtime
        self.lock = threading.Lock()

    def run(self, text, run_id="", cancel=None):
        if not text.strip() or len(text) > 50000:
            raise ValueError("Supply 1..50000 characters of code/diff")
        run_id = identifier(run_id) if run_id else "wf_" + uuid.uuid4().hex[:12]
        # One workflow at a time; its four branches execute concurrently.
        if not self.lock.acquire(blocking=False):
            raise ValueError("Another workflow is already running")
        try:
            return self._run(text, run_id, cancel)
        finally:
            self.lock.release()

    def _run(self, text, run_id, cancel=None):
        snapshot = self.rt.store.path("workflows", run_id + ".json")
        journal = self.rt.store.path("workflows", run_id + ".journal.jsonl")
        digest = hashlib.sha256(text.encode()).hexdigest()
        previous = read_json(snapshot, {})
        if previous and previous["input_hash"] != digest:
            raise ValueError("Resume requires the original code/diff")
        cache = {}
        if journal.exists():
            for line in journal.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                cache[record["key"]] = record["value"]
        lock = threading.Lock()
        state = dict(run_id=run_id, input_hash=digest, status="running")
        atomic_json(snapshot, state)

        def step(label, prompt):
            basis = json.dumps(["review-v1", self.rt.config.model,
                                self.rt.config.thinking, label, prompt], ensure_ascii=False)
            key = hashlib.sha256(basis.encode()).hexdigest()
            with lock:
                if key in cache:
                    return cache[key]
            if self.rt.stop.is_set() or cancel is not None and cancel.is_set():
                raise RuntimeError("Workflow stopped")
            msg, finish = self.rt.llm.chat([
                {"role": "system", "content": "Review only supplied code. No tools or execution access. Do not invent test results. Return only a JSON object."},
                {"role": "user", "content": prompt}], max_tokens=2500)
            if finish != "stop":
                raise ValueError("Workflow response incomplete")
            result = json_answer(msg["content"])
            if label.startswith("audit"):
                findings = result.get("findings")
                if not isinstance(findings, list) or any(not isinstance(f, dict) or
                    not isinstance(f.get("title"), str) or f.get("severity") not in ("high", "medium", "low") for f in findings):
                    raise ValueError("Invalid findings JSON")
            elif not isinstance(result.get("isReal"), bool) or not isinstance(result.get("reason"), str):
                raise ValueError("Invalid verification JSON")
            with lock:
                with journal.open("a", encoding="utf-8") as out:
                    out.write(json.dumps({"key": key, "value": result}, ensure_ascii=False) + "\n")
                    out.flush()
                cache[key] = result
            return result

        def branch(dimension):
            audit = step("audit:" + dimension,
                f"Review {dimension}. Return {{\"findings\":[{{\"title\":\"...\",\"severity\":\"high|medium|low\"}}]}}. "
                f"Report at most 5 concrete issues. Input:\n{text}")
            confirmed = []
            for finding in audit["findings"][:5]:
                verdict = step("verify:" + dimension + ":" + finding["title"],
                    f"Check whether this finding is supported by the code. Return {{\"isReal\":true/false,\"reason\":\"...\"}}. "
                    f"Finding: {json.dumps(finding)}\nCode:\n{text}")
                if verdict["isReal"]:
                    confirmed.append(dict(dimension=dimension, **finding, reason=verdict["reason"]))
            return confirmed
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(branch, ["correctness", "security", "performance", "style"]))
            findings = sorted([f for batch in results for f in batch],
                              key=lambda f: ["high", "medium", "low"].index(f["severity"]))
            state.update(status="completed", findings=findings)
        except Exception as exc:
            state.update(status="failed", error=str(exc))
        atomic_json(snapshot, state)
        return state
