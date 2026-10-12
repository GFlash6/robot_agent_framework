"""In-process agent driver for simulators and benchmarks.

    with Agent(".runtime/bench", "g1-01", model_config) as agent:
        agent.install(profile, binding)            # binding with "adapter": "inprocess"
        agent.handle("base.navigate_to", nav)      # the simulator implements each bound skill
        agent.load_scene(scene)                    # static regions/locations, e.g. drawer_1.region
        task = agent.submit("把红色方块放进抽屉")    # L0 plans it; or plan=<TaskPlan>, l1_plans={...}
        while not agent.done(task):
            sim.step()                             # advance physics
            agent.observe_many(sim.observations()) # push robot/object state
            agent.step()                           # advance the agent once
        print(agent.report(task, text=True))

``step()`` is synchronous and deterministic in call order, so a benchmark can
interleave it with its own clock. The durable DBOS worker (``robot-agent run``)
drives the same ledger in production; do not run both on one ledger at once.
"""

import time

from .embodiment import install
from .report import render, task_report
from .runtime import QUIET, RECOVERING, Runtime
from .skills import register_handler
from .store import Store
from .task_manager import FINAL, WAITING, TaskManager
from .world_state import WorldState
from .contracts import require


class SkillHandler:
    """Base class for an in-process skill. Override start/query/cancel.

    Each method returns an execution envelope; use the helpers below. Keep your
    own state per execution_id: query and cancel must report on that same
    execution, and a terminal result must only be returned once the body is
    actually still (``quiescent``).
    """

    source = "inprocess"

    def __call__(self, operation, execution_id, args, robot_id):
        return getattr(self, operation)(execution_id, args, robot_id)

    def start(self, execution_id, args, robot_id):
        raise NotImplementedError

    def query(self, execution_id, args, robot_id):
        raise NotImplementedError

    def cancel(self, execution_id, args, robot_id):
        raise NotImplementedError

    def running(self, execution_id, progress=None, observations=None):
        return {"execution_id": execution_id, "status": "running", "quiescent": False, "output": {},
                "evidence": [{"source": f"{self.source}:{execution_id}"}],
                **({"feedback": {"progress": progress}} if progress is not None else {}),
                **({"observations": observations} if observations else {})}

    def finished(self, execution_id, status, output=None, error=None, observations=None, evidence=None):
        require(status in {"succeeded", "failed", "canceled"}, "terminal status required")
        return {"execution_id": execution_id, "status": status, "quiescent": True,
                "output": output if output is not None else ({"done": True} if status == "succeeded" else {}),
                "evidence": evidence or [{"source": f"{self.source}:{execution_id}"}],
                **({"error": error} if error else {}),
                **({"observations": observations} if observations else {})}


class Agent:
    def __init__(self, root, robot_id, model_config=None, *, source="sim", evidence_level="simulation"):
        self.store = Store(root)
        self.runtime = Runtime(self.store)
        self.tasks = TaskManager(self.store, self.runtime)
        self.world = WorldState(self.store, robot_id)
        self.robot_id = robot_id
        self.model_config = model_config
        self.source = source
        self.evidence_level = evidence_level

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.store.close()

    # ------------------------------------------------------------ setup

    def install(self, profile, binding):
        record = install(self.runtime, profile, binding, self.robot_id)
        self.evidence_level = record["evidence_level"]
        return record

    def handle(self, skill, handler):
        """Attach the in-process implementation of a bound skill."""
        spec = self.runtime.catalog().get(skill)
        require(spec is not None and spec["adapter"] == "inprocess", f"{skill} is not bound in-process")
        register_handler(spec["handler"], handler)

    def set_policy(self, policy):
        """Rules, paradigm skills and map overview that L0 plans under."""
        return self.tasks.set_policy(self.robot_id, policy)

    def load_scene(self, scene, *, source_ref="scene"):
        return self.world.load_scene(scene, source_ref=source_ref, evidence_level=self.evidence_level)

    def observe(self, key, value, *, stamp_ns=None, frame_id=None, ttl_s=None, confidence=1.0, ref=None,
                map_version=None):
        return self.world.observe(key, value, source={"kind": "observation", "ref": ref or self.source},
                                  stamp_ns=stamp_ns or time.time_ns(), evidence_level=self.evidence_level,
                                  frame_id=frame_id, ttl_s=ttl_s, confidence=confidence, map_version=map_version)

    def observe_many(self, items):
        """items: [{"key", "value", "stamp_ns"?, "frame_id"?, "ttl_s"?, "confidence"?, "ref"?}]"""
        return [self.observe(item["key"], item["value"], **{k: v for k, v in item.items() if k not in {"key", "value"}})
                for item in items]

    # ------------------------------------------------------------ running

    def submit(self, instruction, *, plan=None, l1_plans=None, **options):
        """Start a task from one user instruction; returns its id (t1, t2, ...)."""
        return self.tasks.create(instruction, self.robot_id, self.model_config, plan=plan, l1_plans=l1_plans,
                                 **options)["id"]

    def step(self):
        """Advance every unfinished task and skill plan once. Returns their statuses."""
        for task in self.store.list("agent_tasks"):
            if task["status"] not in FINAL | WAITING and task["robot_id"] == self.robot_id:
                self.tasks.advance(task["id"])
        for task in self.store.list("tasks"):
            if task["robot_id"] != self.robot_id:
                continue
            if task["status"] in RECOVERING:
                self.runtime.recover_plan(task["id"])
            elif task["status"] not in QUIET:
                self.runtime.tick(task["id"])
        return {"tasks": {t["id"]: t["status"] for t in self.store.list("agent_tasks")},
                "skill_plans": {t["id"]: t["status"] for t in self.store.list("tasks")}}

    def done(self, task_id):
        """Final, or waiting for a person (the user's answer or an operator decision)."""
        return self.tasks.get(task_id)["status"] in FINAL | WAITING

    def run(self, task_id, *, max_steps=10000, on_step=None, sleep_s=0.0):
        """Step until the task is final or waits for a person. ``on_step(agent)`` runs between steps."""
        for _ in range(max_steps):
            if self.done(task_id):
                break
            if on_step:
                on_step(self)
            self.step()
            if sleep_s:
                time.sleep(sleep_s)
        return self.report(task_id)

    def report(self, task_id, *, text=False):
        report = task_report(self.store, task_id)
        return render(report) if text else report

    def say(self, task_id, text, *, confirm=False):
        """The user answers, confirms or interjects."""
        return self.tasks.say(task_id, text, confirm=confirm)

    def resolve(self, task_id, action, subtask_id=None, note="", value=None):
        return self.tasks.resolve(task_id, action, subtask_id, note, value)

    def revise(self, task_id, plan, l1_plans=None):
        return self.tasks.revise(task_id, plan, l1_plans)
