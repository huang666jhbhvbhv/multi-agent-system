"""Persistent cron queue. Delivery is acknowledged after an agent turn succeeds."""
from datetime import datetime
import threading
import uuid

from .state import atomic_json, read_json


def cron_fields(expression):
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("Cron needs minute hour day month weekday")
    sets = []
    for raw, (low, high) in zip(fields, [(0,59),(0,23),(1,31),(1,12),(0,6)]):
        values = set()
        for part in raw.split(","):
            base, sep, step = part.partition("/")
            step = int(step) if sep else 1
            if step <= 0:
                raise ValueError("Cron step must be positive")
            if base == "*":
                start, end = low, high
            elif "-" in base:
                start, end = map(int, base.split("-"))
            else:
                start = end = int(base)
            if not low <= start <= end <= high:
                raise ValueError("Cron value out of range")
            values.update(range(start, end + 1, step))
        sets.append(values)
    return fields, sets


def cron_matches(expression, now):
    fields, values = cron_fields(expression)
    minute, hour, day, month, weekday = values
    if now.minute not in minute or now.hour not in hour or now.month not in month:
        return False
    d, w = now.day in day, (now.weekday() + 1) % 7 in weekday
    if fields[2] == "*":
        return w
    if fields[4] == "*":
        return d
    return d or w


class Scheduler:
    def __init__(self, store, emit, stop):
        self.path = store.root / "schedules.json"
        self.jobs = read_json(self.path, {})
        self.lock = threading.RLock()
        self.inflight = set()
        self.emit, self.stop = emit, stop

    def manage(self, action="list", cron="", prompt="", job_id="", recurring=True):
        with self.lock:
            if action == "list":
                return list(self.jobs.values())
            if action == "create":
                cron_fields(cron)
                if not prompt.strip():
                    raise ValueError("Empty schedule prompt")
                job_id = "cron_" + uuid.uuid4().hex[:12]
                self.jobs[job_id] = dict(id=job_id, cron=cron, prompt=prompt,
                    recurring=recurring, pending=False, last_fired=None)
            elif action == "cancel":
                if job_id in self.inflight:
                    raise ValueError("Task already delivered; use /stop to stop current work")
                self.jobs.pop(job_id, None)
            else:
                raise ValueError("Use list/create/cancel")
            atomic_json(self.path, self.jobs)
            return job_id

    def poll(self, now=None):
        now = now or datetime.now()
        marker = now.strftime("%Y-%m-%d %H:%M")
        with self.lock:
            for job in list(self.jobs.values()):
                if not job["pending"] and job["last_fired"] != marker and cron_matches(job["cron"], now):
                    job.update(pending=True, last_fired=marker)
                    atomic_json(self.path, self.jobs)
                if job["pending"] and job["id"] not in self.inflight:
                    self.inflight.add(job["id"])
                    self.emit("scheduled", job)

    def ack(self, job_id, success):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            if success:
                if job["recurring"]:
                    job["pending"] = False
                else:
                    self.jobs.pop(job_id)
                self.inflight.discard(job_id)
            # On failure leave pending on disk, but don't spin/retry paid requests.
            # Restart will redeliver. In-flight failures are visible in /jobs.
            atomic_json(self.path, self.jobs)

    def run(self):
        while not self.stop.wait(1):
            try:
                self.poll()
            except Exception as exc:
                self.emit("notice", "Scheduler: " + str(exc))
