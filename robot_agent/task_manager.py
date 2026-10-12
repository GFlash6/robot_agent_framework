"""Task manager: runs a TaskPlan as a task tree and is the only writer of subtask status and result.

    instruction ──L0──▶ TaskPlan ──▶ subtasks  PENDING → READY → PLANNING → RUNNING → SUCCEEDED
                          ▲                      │ L1 turns the subtask into a skill plan (Runtime task)
                          │                      │ the goal is then checked against world state
                          │                      ▼
     re-plan (L0) ◀── required subtask failed, precondition missing, assumption false, user spoke
                                                 │ every subtask ended
                                                 ▼
                                     acceptance (L0) ──▶ reply to the user

L0 writes plans only; L1 and the Runtime write skill plans and executions;
this module turns their events into each subtask's status and result.

Task statuses: planning, replanning, running, accepting; waiting for a person:
awaiting_user (L0 asked a question), needs_operator; final: succeeded, failed,
infeasible, canceled.
"""

import copy
import json
import time
import uuid

from . import goals
from .contracts import ContractError, require, validate_plan
from .l0 import EXECUTABLE, L0Planner, enforce_rules, gather, validate_policy
from .task_plan import (DONE, TERMINAL, make_result, parse_id, transition, validate_revision, validate_task_plan)
from .world_state import WorldState

FINAL = {"succeeded", "failed", "infeasible", "canceled"}
WAITING = {"awaiting_user", "needs_operator"}
ACTIVE = {"PLANNING", "RUNNING", "BLOCKED", "RECOVERING", "MISSING_PREREQ"}
RUNTIME_QUIET = {"succeeded", "failed", "canceled", "paused", "needs_operator"}
RUNTIME_RECOVERING = {"repairing", "replanning", "compensating"}
BROKEN = {"FAILED", "PARTIAL", "CANCELED"}
MODEL_FIELDS = {"endpoint", "model", "token_env", "timeout", "max_input_tokens", "max_output_tokens"}


def _stream(task_id):
    return "agent_task:" + task_id


def history(task, trigger):
    """Task-tree snapshot for re-planning: every subtask issued so far, its status, result and errors."""
    tree = []
    for sid in sorted(task["defs"], key=lambda s: parse_id(s)["seq"]):
        spec, state = task["defs"][sid], task["state"][sid]
        tree.append({**spec, "status": state["status"], "result": state["result"],
                     "errors": state["errors"][-3:], "in_current_plan": sid in task["order"]})
    return {"trigger": trigger, "plan_version": task["plan_version"],
            "understanding": task["plan"]["understanding"] if task["plan"] else None,
            "assumptions": task["plan"]["feasibility"]["assumptions"] if task["plan"] else [], "tree": tree}


class TaskManager:
    def __init__(self, store, runtime=None):
        from .runtime import Runtime
        self.store = store
        self.runtime = runtime or Runtime(store)

    # ------------------------------------------------------------ policy and creation

    def set_policy(self, robot_id, policy):
        """Agent-side rules, paradigm skills and map overview that L0 sees for this robot."""
        validate_policy(policy)
        self.store.put("agent_policies", robot_id, copy.deepcopy(policy))
        self.store.event("world:" + robot_id, "agent_policy_set", {"policy": policy})
        return policy

    def _new_id(self):
        with self.store.transaction():
            counter = self.store.get("counters", "agent_task") or {"next": 1}
            while self.store.get("agent_tasks", f"t{counter['next']}") is not None:
                counter["next"] += 1
            task_id = f"t{counter['next']}"
            self.store.put("counters", "agent_task", {"next": counter["next"] + 1})
        return task_id

    def create(self, instruction, robot_id, model_config=None, *, plan=None, l1_plans=None, dialogue=(),
               view=None, confirmed=False, priority=0, max_replans=2, max_planning_failures=1,
               verify_timeout_s=30, block_timeout_s=120, max_repairs=1):
        """Start a task from one user instruction.

        Without ``plan`` the worker asks L0. An operator ``plan`` is a TaskPlan
        (its task_id names the task) checked by the same rules; ``l1_plans`` maps
        subtask ids to pre-authored skill plans, so no model is needed for them.
        """
        require(isinstance(instruction, str) and instruction.strip(), "instruction required")
        require(isinstance(robot_id, str) and robot_id and "/" not in robot_id, "robot_id must be one namespace segment")
        require(isinstance(model_config, dict) or (model_config is None and plan is not None),
                "a model configuration or an operator plan is required")
        require(type(priority) is int, "priority must be integer")
        require(all(isinstance(t, dict) and set(t) == {"role", "content"} and t["role"] in {"user", "assistant"}
                    and isinstance(t["content"], str) for t in dialogue), "dialogue turns are {role, content}")
        for name, value, upper in (("max_replans", max_replans, 10), ("max_planning_failures", max_planning_failures, 5),
                                   ("max_repairs", max_repairs, 10)):
            require(type(value) is int and 0 <= value <= upper, f"{name} must be 0..{upper}")
        for name, value in (("verify_timeout_s", verify_timeout_s), ("block_timeout_s", block_timeout_s)):
            require(type(value) in (int, float) and 0 < value <= 3600, f"{name} must be 0..3600")
        l1_plans = dict(l1_plans or {})
        catalog = self.runtime.catalog()
        for sid, skill_plan in l1_plans.items():
            parse_id(sid)
            validate_plan(skill_plan, catalog)
        task_id = plan["task_id"] if isinstance(plan, dict) and "task_id" in plan else self._new_id()
        now = time.time()
        task = {
            "id": task_id, "robot_id": robot_id, "instruction": instruction.strip(), "priority": priority,
            "status": "planning", "created_at": now, "generation": 0,
            "model_config": None if model_config is None else {k: v for k, v in model_config.items() if k in MODEL_FIELDS},
            "budgets": {"max_replans": max_replans, "max_planning_failures": max_planning_failures,
                        "verify_timeout_s": verify_timeout_s, "block_timeout_s": block_timeout_s,
                        "max_repairs": max_repairs},
            "dialogue": [{"role": "user", "content": instruction.strip(), "at": now}]
                        + [{**turn, "at": now} for turn in dialogue],
            "view": view, "confirmed": bool(confirmed), "plan_version": 0, "plan": None, "plans": [],
            "defs": {}, "state": {}, "order": [], "replans": 0, "planning_failures": 0, "model_calls": 0,
            "operator_actions": [], "pending_replan": None, "pending_revision": None, "operator_candidate": None,
            "reply": "",
            "acceptance": None, "l1_plans": l1_plans, "error": None,
        }
        require(self.store.get("agent_tasks", task_id) is None, f"task {task_id} already exists")
        self._event(task, "task_created", {"instruction": task["instruction"], "robot_id": robot_id,
                                           "plan": None if plan is None else "operator", "budgets": task["budgets"]})
        if plan is not None:
            task["pending_revision"] = {"plan": copy.deepcopy(plan), "l1_plans": {}, "source": "operator"}
            self._install_pending(task)  # an invalid operator plan is rejected here, before the task exists
        with self.store.transaction():
            require(self.store.get("agent_tasks", task_id) is None, f"task {task_id} already exists")
            self._save(task)
        return task

    def get(self, task_id):
        task = self.store.get("agent_tasks", task_id)
        require(task is not None, "unknown task")
        return task

    def _event(self, task, kind, data=None):
        task.setdefault("_events", []).append((kind, data or {}))

    def _save(self, task):
        events = task.pop("_events", [])
        task["updated_at"] = time.time()
        with self.store.transaction():
            self.store.put("agent_tasks", task["id"], task)
            for kind, data in events:
                self.store.event(_stream(task["id"]), kind, data)

    # ------------------------------------------------------------ people: user dialogue and operator decisions

    def say(self, task_id, text, *, confirm=False):
        """A user turn: answers a clarifying question, confirms, or interjects mid-task (re-plans)."""
        require(isinstance(text, str) and text.strip(), "text required")
        with self.store.task_lock(_stream(task_id)):
            task = self.get(task_id)
            require(task["status"] not in FINAL, "task already ended")
            waiting = task["status"] in WAITING
            task["dialogue"].append({"role": "user", "content": text.strip(), "at": time.time()})
            task["confirmed"] = task["confirmed"] or bool(confirm)
            self._event(task, "user_said", {"text": text.strip(), "confirm": bool(confirm)})
            if task["status"] == "awaiting_user":
                # Clarification answered: L0 starts again from step 1.
                task["status"] = "planning"
            elif task["status"] in {"running", "accepting", "needs_operator"}:
                self._request_replan(task, "user_interjection", text.strip())
            self._wake(task, waiting)
            self._save(task)
        return task

    @staticmethod
    def _wake(task, was_waiting):
        # A worker's loop ends while the task waits for a person; a new generation starts a new one.
        if was_waiting and task["status"] not in WAITING:
            task["generation"] += 1

    def assumption_failed(self, task_id, assumption, evidence=""):
        """An assumption the plan relies on was shown false during execution: re-plan."""
        with self.store.task_lock(_stream(task_id)):
            task = self.get(task_id)
            require(task["plan"] is not None and assumption in task["plan"]["feasibility"]["assumptions"],
                    "not an assumption of the current plan")
            require(task["status"] in {"running", "accepting"}, "task is not executing")
            self._request_replan(task, "assumption_falsified", {"assumption": assumption, "evidence": evidence})
            self._save(task)
        return task

    def revise(self, task_id, plan, l1_plans=None):
        """Operator re-plan: a new TaskPlan version, held to the same revision rules as L0's."""
        with self.store.task_lock(_stream(task_id)):
            task = self.get(task_id)
            require(task["status"] not in FINAL, "task already ended")
            validate_task_plan(plan, task_id=task_id, plan_version=task["plan_version"] + 1,
                               instruction=task["instruction"], categories=self._inputs(task).categories)
            catalog = self.runtime.catalog()
            for sid, skill_plan in (l1_plans or {}).items():
                parse_id(sid)
                validate_plan(skill_plan, catalog)
            task["pending_revision"] = {"plan": copy.deepcopy(plan), "l1_plans": dict(l1_plans or {}),
                                        "source": "operator"}
            task["operator_actions"].append({"action": "revise", "at": time.time()})
            waiting = task["status"] in WAITING
            task.update(status="replanning", pending_replan={"trigger": "operator_revision", "detail": "",
                                                             "at": time.time()})
            self._wake(task, waiting)
            self._event(task, "revision_submitted", {"plan_version": plan.get("plan_version")})
            self._save(task)
        return task

    def resolve(self, task_id, action, subtask_id=None, note="", value=None):
        """Operator decision for a stuck task.

        confirm   the subtask is done (operator evidence; check/obtain goals need ``value``)
        retry     orchestrate a blocked or prerequisite-missing subtask again, fresh retry budget
        replan    ask L0 for a new plan now (needs a model; overrides the re-plan budget once)
        abort     cancel the task
        """
        require(action in {"confirm", "retry", "replan", "abort"}, "unknown operator action")
        if action == "abort":
            return self.cancel(task_id)
        with self.store.task_lock(_stream(task_id)):
            task = self.get(task_id)
            require(task["status"] in {"running", "needs_operator"}, "task is not waiting for a decision")
            waiting = task["status"] in WAITING
            if action == "replan":
                require(task["model_config"] is not None, "re-planning needs a model; revise the plan instead")
                task["budgets"]["max_replans"] = max(task["budgets"]["max_replans"], task["replans"] + 1)
                task["status"] = "running"
                self._request_replan(task, "operator", note)
            else:
                require(subtask_id in task["order"], "choose a subtask of the current plan")
                state = task["state"][subtask_id]
                runtime_task = self._runtime_task(state)
                require(runtime_task is None or runtime_task["status"] in RUNTIME_QUIET,
                        "the subtask's skills are still executing")
                if action == "confirm":
                    require(state["status"] in {"BLOCKED", "RECOVERING", "MISSING_PREREQ"},
                            "choose a blocked, recovering or prerequisite-missing subtask")
                    goal = state.get("goal", task["defs"][subtask_id]["goal"])
                    self._finish(task, subtask_id, "SUCCEEDED", value=value,
                                 progress=[{"cond": c, "met": True} for c in goals.atoms_of(goal)],
                                 evidence=["operator:" + (note or "confirmed")])
                else:
                    require(state["status"] in {"BLOCKED", "MISSING_PREREQ"},
                            "choose a blocked or prerequisite-missing subtask")
                    state.update(attempts=0, started_at=time.time(), retry_pending=True)
                    state.pop("canceling", None)
                    self._move(task, subtask_id, "RECOVERING" if state["status"] == "BLOCKED" else "READY",
                               reason="operator retry")
                task["status"] = "running"
            self._wake(task, waiting)
            task["operator_actions"].append({"action": action, "subtask": subtask_id, "note": note, "at": time.time()})
            self._event(task, "operator_resolved", {"action": action, "subtask": subtask_id, "note": note})
            self._save(task)
        return task

    def cancel(self, task_id):
        with self.store.task_lock(_stream(task_id)):
            task = self.get(task_id)
            require(task["status"] not in FINAL, "task already ended")
            waiting = task["status"] in WAITING
            task["cancel_requested"] = True
            if waiting:
                task["status"] = "running"
            self._wake(task, waiting)
            self._event(task, "cancel_requested")
            self._save(task)
        return task

    # ------------------------------------------------------------ control step

    def advance(self, task_id):
        """One idempotent control step; safe to call repeatedly from a worker."""
        with self.store.task_lock(_stream(task_id)):
            task = self.get(task_id)
            if task["status"] in FINAL:
                return task
            if task.get("cancel_requested"):
                self._advance_cancel(task)
                self._save(task)
                return task
            if task["status"] in WAITING:
                return task
            if task["status"] == "planning":
                self._plan(task)
            if task["status"] == "replanning":
                self._replan(task)
            if task["status"] == "running":
                world = WorldState(self.store, task["robot_id"])
                for sid in task["order"]:
                    self._advance_subtask(task, sid, world)
                self._settle(task)
            if task["status"] == "accepting":
                self._accept(task)
            self._save(task)
            return task

    # ------------------------------------------------------------ L0: plan, re-plan, accept

    def _inputs(self, task, history_view=None):
        dialogue = [{"role": t["role"], "content": t["content"]} for t in task["dialogue"][1:]][-12:]
        return gather(self.store, task["robot_id"], self.runtime.catalog(), instruction=task["instruction"],
                      dialogue=dialogue, view=task.get("view"), history=history_view, confirmed=task["confirmed"])

    def _l0(self, task):
        require(task["model_config"] is not None, "this step needs L0 and the task has no model configuration")
        return L0Planner(self.store, task["model_config"])

    def _plan(self, task):
        if task["model_config"] is None and task.get("operator_candidate"):
            # An operator plan that waited for the user (e.g. a confirmation): check it again now.
            candidate = task["operator_candidate"]
            task["pending_revision"] = {**candidate, "plan": {**candidate["plan"],
                                                              "plan_version": task["plan_version"] + 1}}
        if task["pending_revision"] is not None:
            self._try_install_pending(task)
            return
        try:
            task["model_calls"] += 1
            plan, response_id = self._l0(task).plan(task, self._inputs(task), mode="plan")
        except Exception as exc:  # noqa: BLE001 - every rejected candidate is audited and budgeted
            self._planning_failed(task, exc)
            return
        self._install(task, plan, source="model", response_id=response_id)

    def _replan(self, task):
        if not self._quiesce(task):
            return
        if task["pending_revision"] is not None:
            self._try_install_pending(task)
            return
        trigger = task["pending_replan"] or {"trigger": "unknown", "detail": ""}
        try:
            task["model_calls"] += 1
            plan, response_id = self._l0(task).plan(task, self._inputs(task, history(task, trigger)), mode="replan")
        except Exception as exc:  # noqa: BLE001
            self._planning_failed(task, exc)
            return
        self._install(task, plan, source="model", response_id=response_id)

    def _install_pending(self, task):
        """Adopt an operator plan under the same contract and rules as an L0 candidate."""
        pending, task["pending_revision"] = task["pending_revision"], None
        inputs = self._inputs(task)
        plan = validate_task_plan(pending["plan"], task_id=task["id"], plan_version=task["plan_version"] + 1,
                                  instruction=task["instruction"], categories=inputs.categories)
        validate_revision(plan, task["defs"], {sid: s["status"] for sid, s in task["state"].items()})
        plan = enforce_rules(plan, inputs, WorldState(self.store, task["robot_id"]))
        task["l1_plans"].update(pending["l1_plans"])
        task["operator_candidate"] = pending
        self._install(task, plan, source=pending["source"])

    def _try_install_pending(self, task):
        # The task may have moved on since the revision was submitted.
        try:
            self._install_pending(task)
        except ContractError as exc:
            self._planning_failed(task, exc)

    def _planning_failed(self, task, exc):
        task["planning_failures"] += 1
        error = type(exc).__name__ + ": " + str(exc)
        exhausted = task["planning_failures"] > task["budgets"]["max_planning_failures"]
        if exhausted:
            task.update(status="needs_operator", error=error)
        self._event(task, "plan_rejected", {"error": error, "needs_operator": exhausted})

    def _install(self, task, plan, *, source, response_id=None):
        """Adopt a plan version: new subtasks start PENDING, ones it leaves out are canceled."""
        now = time.time()
        for spec in plan["subtasks"]:
            if spec["id"] not in task["defs"]:
                task["defs"][spec["id"]] = spec
                task["state"][spec["id"]] = {"status": "PENDING", "result": None, "attempts": 0, "runtime_tasks": [],
                                             "errors": [], "since": now}
        kept = [s["id"] for s in plan["subtasks"]]
        for sid in task["order"]:
            if sid not in kept and task["state"][sid]["status"] not in TERMINAL:
                self._finish(task, sid, "CANCELED", error=f"left out of plan version {plan['plan_version']}")
        task.update(plan=plan, plan_version=plan["plan_version"], order=kept, reply=plan["reply"], planning_failures=0,
                    pending_replan=None, error=None)
        task["plans"].append({"version": plan["plan_version"], "source": source, "model_response_id": response_id,
                              "verdict": plan["feasibility"]["verdict"], "at": now})
        if plan["reply"].strip():
            task["dialogue"].append({"role": "assistant", "content": plan["reply"], "at": now})
        self.store.put("task_plans", f"{task['id']}@{plan['plan_version']}",
                       {**plan, "source": source, "model_response_id": response_id, "created_at": now})
        verdict = plan["feasibility"]["verdict"]
        task["status"] = ("running" if verdict in EXECUTABLE else
                          "awaiting_user" if verdict == "needs_clarification" else "infeasible")
        self._event(task, "plan_adopted", {"plan_version": plan["plan_version"], "source": source, "verdict": verdict,
                                           "subtasks": kept, "reply": plan["reply"], "model_response_id": response_id})

    def _request_replan(self, task, trigger, detail):
        if task["model_config"] is None or task["replans"] >= task["budgets"]["max_replans"]:
            why = "no model is configured" if task["model_config"] is None else "the re-planning budget is used up"
            task.update(status="needs_operator", error=f"re-planning needed ({trigger}) but {why}")
            self._event(task, "needs_operator", {"trigger": trigger, "detail": detail, "reason": why})
            return
        task["replans"] += 1
        task.update(status="replanning", pending_replan={"trigger": trigger, "detail": detail, "at": time.time()})
        self._event(task, "replan_requested", {"trigger": trigger, "detail": detail, "replans": task["replans"]})

    def _quiesce(self, task):
        """Cancel subtasks in progress before re-planning; True once none is executing."""
        busy = False
        for sid in task["order"]:
            state = task["state"][sid]
            if state["status"] not in ACTIVE:
                continue
            runtime_task = self._runtime_task(state)
            if runtime_task is not None and runtime_task["status"] not in RUNTIME_QUIET:
                busy = True
                self._interrupt(state, runtime_task, "canceled for re-planning")
            else:
                self._finish(task, sid, "CANCELED", error="canceled for re-planning")
        return not busy

    def _accept(self, task):
        current = [(task["defs"][sid], task["state"][sid]) for sid in task["order"]]
        required_ok = all(state["status"] in DONE for spec, state in current if spec["required"])
        optional_failed = [spec["id"] for spec, state in current if not spec["required"] and state["status"] not in DONE]
        if task["model_config"] is None or not required_ok:
            acceptance = {"achieved": required_ok, "unmet": [] if required_ok else ["a required subtask did not succeed"],
                          "reply": ("已完成：" if required_ok else "未能完成：") + task["instruction"]
                                   + (f"（可选步骤未完成：{', '.join(optional_failed)}）" if optional_failed else ""),
                          "by": "rules"}
        else:
            try:
                task["model_calls"] += 1
                acceptance, response_id = self._l0(task).accept(task, self._inputs(task))
            except Exception as exc:  # noqa: BLE001
                self._planning_failed(task, exc)
                return
            acceptance = {**acceptance, "by": "model", "model_response_id": response_id}
        acceptance["optional_failed"] = optional_failed
        task.update(acceptance=acceptance, reply=acceptance["reply"],
                    status="succeeded" if acceptance["achieved"] else "failed")
        task["dialogue"].append({"role": "assistant", "content": acceptance["reply"], "at": time.time()})
        self._event(task, "task_accepted" if acceptance["achieved"] else "task_not_accepted", acceptance)

    # ------------------------------------------------------------ subtasks

    def _move(self, task, sid, status, **fields):
        state = task["state"][sid]
        previous = state["status"]
        transition(state, status, since=time.time(), **fields)
        self._event(task, "subtask_status", {"subtask": sid, "from": previous, "to": status,
                                             **{k: v for k, v in fields.items() if k in {"reason", "error"}}})

    def _finish(self, task, sid, outcome, *, progress=None, value=None, produced=(), evidence=(), error=None):
        """Write the subtask's one result and move it to the matching terminal status."""
        state, spec = task["state"][sid], task["defs"][sid]
        error_ref = None
        if outcome in {"FAILED", "PARTIAL"}:
            error_ref = str(uuid.uuid4())
            self.store.put("error_reports", error_ref, {
                "id": error_ref, "agent_task_id": task["id"], "subtask_id": sid, "outcome": outcome,
                "error": error, "attempts": state["attempts"], "errors": state["errors"],
                "runtime_tasks": state["runtime_tasks"], "goal": state.get("goal", spec["goal"]), "at": time.time()})
        if progress is None:
            progress = [{"cond": c, "met": False} for c in goals.atoms_of(state.get("goal", spec["goal"]))]
        evidence = list(evidence) + ["task:" + t for t in state["runtime_tasks"]]
        result = make_result(sid, outcome, goal_progress=progress, value=value, produced=produced, evidence=evidence,
                             error_ref=error_ref,
                             duration_s=time.time() - state["started_at"] if state.get("started_at") else 0.0)
        self._move(task, sid, outcome, result=result, **({"error": error} if error else {}))

    def _runtime_task(self, state):
        return self.store.get("tasks", state["runtime_tasks"][-1]) if state["runtime_tasks"] else None

    def _interrupt(self, state, runtime_task, reason):
        if not state.get("canceling") and (self.store.get("controls", runtime_task["id"]) or {}).get("mode") != "cancel":
            self.runtime.interrupt(runtime_task["id"], "cancel")
        state["canceling"] = reason

    def _advance_subtask(self, task, sid, world):
        spec, state = task["defs"][sid], task["state"][sid]
        if state["status"] == "PENDING":
            deps = [task["state"][d] for d in spec["depends_on"]]
            if any(d["status"] in TERMINAL - DONE for d in deps):
                if not spec["required"]:
                    self._finish(task, sid, "SKIPPED", error="a subtask it depends on did not succeed")
                return
            if not all(d["status"] in DONE for d in deps):
                return
            self._move(task, sid, "READY")
        if state["status"] == "READY":
            # References may only name ancestors (checked with the plan), all of which have ended.
            results = {k: s["result"] for k, s in task["state"].items() if s["result"]}
            try:
                state["goal"] = goals.resolve(spec["goal"], results)
                state["description"] = goals.resolve(spec["description"], results)
            except ContractError as exc:
                self._move(task, sid, "PLANNING")
                self._finish(task, sid, "FAILED", error="goal references an unavailable result: " + str(exc))
                return
            if spec["kind"] == "A":
                verdict = goals.check(world, state["goal"], "A")
                if verdict["state"] == "met":
                    self._finish(task, sid, "SKIPPED", progress=verdict["progress"])
                    return
            state["started_at"] = time.time()
            self._move(task, sid, "PLANNING")
        if state["status"] == "PLANNING":
            self._orchestrate(task, sid)
            return
        if state["status"] in {"RUNNING", "BLOCKED", "RECOVERING"}:
            runtime_task = self._runtime_task(state)
            if state.get("retry_pending") and (runtime_task is None or runtime_task["status"] in RUNTIME_QUIET):
                # Our own retry (not the Runtime's): orchestrate again against the world as it is now.
                state.pop("canceling", None)
                if self._over_time(task, sid):
                    self._finish(task, sid, self._failure_outcome(state), error="subtask time budget used up")
                    return
                self._move(task, sid, "PLANNING", reason="retry")
                self._orchestrate(task, sid)
                return
            self._watch(task, sid, runtime_task, world)

    def _orchestrate(self, task, sid):
        """L1: turn the subtask into a skill plan and hand it to the Runtime."""
        from .context_models import ContextRequest, GoalContext
        from .planner import Planner

        spec, state = task["defs"][sid], task["state"][sid]
        goal = state["goal"]
        conditions = json.dumps([goals.predicate(atom) for atom in goals.atoms_of(goal)], ensure_ascii=False)
        criterion = {"A": "Observed world state must then show: " + conditions,
                     "C": "Observe whether this holds; either answer completes the subtask: " + conditions,
                     "Q": "Observe this so that it is known: " + conditions}[spec["kind"]]
        text = (f"{state['description']}\nSubtask {sid} ({spec['kind']}): {json.dumps(goal, ensure_ascii=False)}\n"
                + criterion + (f"\nConstraints: {'; '.join(spec['constraints'])}" if spec["constraints"] else ""))
        state["attempts"] += 1
        try:
            plan, response_id = task["l1_plans"].get(sid), None
            if plan is None:
                require(task["model_config"] is not None, "no skill plan for this subtask and no model to orchestrate one")
                request = ContextRequest(
                    request_id=str(uuid.uuid4()), phase="subplanning", robot_id=task["robot_id"],
                    goal=GoalContext(original_input=state["description"], interpreted_intent=text,
                                     constraints=tuple(spec["constraints"]) + ("Part of: " + task["instruction"],),
                                     completion_criteria=(criterion,)))
                planner = Planner(self.store, task["model_config"])
                task["model_calls"] += 1
                plan = planner.plan(text, self.runtime.catalog(), context_request=request)
                response_id = planner.last_response_id
            context = None
            if task["model_config"] is not None:
                context = {"model_config": task["model_config"], "max_repairs": task["budgets"]["max_repairs"],
                           "repairs": 0, "max_replans": 0, "replans": 0,
                           **({"model_response_id": response_id} if response_id else {})}
            runtime_task = self.runtime.submit(plan, task["robot_id"], task["priority"], text, agent_context=context,
                                               parent={"agent_task_id": task["id"], "subtask_id": sid})
        except Exception as exc:  # noqa: BLE001 - orchestration failures consume the subtask budget
            self._attempt_failed(task, sid, "orchestration rejected: " + type(exc).__name__ + ": " + str(exc))
            return
        state["runtime_tasks"].append(runtime_task["id"])
        state.pop("verify_started", None)
        state.pop("retry_pending", None)
        self._move(task, sid, "RUNNING", runtime_task=runtime_task["id"], attempt=state["attempts"])

    def _watch(self, task, sid, runtime_task, world):
        """Map the Runtime task's progress onto the subtask, and verify its goal when it finishes."""
        state = task["state"][sid]
        status, now = runtime_task["status"], time.time()
        if state.get("canceling"):
            if status in RUNTIME_QUIET:
                if state["status"] == "BLOCKED":
                    self._move(task, sid, "RECOVERING", reason=state["canceling"])
                self._attempt_failed(task, sid, state.pop("canceling"))
            return
        if status not in RUNTIME_QUIET and self._over_time(task, sid):
            self._interrupt(state, runtime_task, "subtask time budget used up")
            return
        if status in {"succeeded", "failed", "canceled"} and state["status"] in {"BLOCKED", "RECOVERING"}:
            self._move(task, sid, "RUNNING", reason="lower layer finished")
        if status == "succeeded":
            self._verify(task, sid, runtime_task, world)
        elif status in {"failed", "canceled"}:
            error = self._runtime_error(runtime_task)
            if error.startswith("precondition false"):
                self._move(task, sid, "MISSING_PREREQ", error=error)
            else:
                self._attempt_failed(task, sid, f"skill plan {runtime_task['id']} {status}: {error}")
        elif status in {"needs_operator", "paused"}:
            reason = "operator" if status == "needs_operator" else "paused"
            if state["status"] != "BLOCKED" or state.get("blocked_reason") != reason:
                if state["status"] == "RECOVERING":
                    self._move(task, sid, "RUNNING", reason="lower layer stopped")
                self._move(task, sid, "BLOCKED", reason=f"skill plan {status}", blocked_reason=reason)
        elif status in RUNTIME_RECOVERING:
            if state["status"] != "RECOVERING":
                self._move(task, sid, "RECOVERING", reason=f"skill plan {status}")
        else:
            waiting = [s["blocked"]["reason"] for s in runtime_task["steps"].values() if s.get("blocked")]
            if waiting and state["status"] != "BLOCKED":
                if state["status"] == "RECOVERING":
                    self._move(task, sid, "RUNNING")
                self._move(task, sid, "BLOCKED", reason=waiting[0], blocked_reason="condition")
            elif waiting and now - state["since"] > task["budgets"]["block_timeout_s"]:
                # Waiting too long on an external condition becomes an error.
                self._interrupt(state, runtime_task, "waited too long: " + waiting[0])
            elif not waiting and state["status"] in {"BLOCKED", "RECOVERING"}:
                self._move(task, sid, "RUNNING", reason="condition cleared")

    def _verify(self, task, sid, runtime_task, world):
        spec, state = task["defs"][sid], task["state"][sid]
        final = runtime_task["steps"][runtime_task["plan"]["verification"]].get("result") or {}
        output = final.get("output") or {}
        subject = output.get("object") if isinstance(output.get("object"), str) else None
        verdict = goals.check(world, state["goal"], spec["kind"], subject=subject)
        state.setdefault("verify_started", time.time())
        if verdict["state"] == "met":
            produced = [verdict["value"]["object"]] if isinstance(verdict["value"], dict) and "object" in verdict["value"] else []
            evidence = [e["source"] for e in final.get("evidence", []) if isinstance(e, dict) and e.get("source")]
            self._finish(task, sid, "SUCCEEDED", progress=verdict["progress"], value=verdict["value"],
                         produced=produced, evidence=evidence)
        elif verdict["state"] == "unmet" or time.time() - state["verify_started"] > task["budgets"]["verify_timeout_s"]:
            unmet = [p["cond"] for p in verdict["progress"] if not p["met"]]
            state.pop("verify_started", None)
            self._attempt_failed(task, sid, f"goal not observed after the skills finished: {', '.join(unmet)}",
                                 progress=verdict["progress"])

    def _attempt_failed(self, task, sid, error, progress=None):
        spec, state = task["defs"][sid], task["state"][sid]
        state["errors"].append({"attempt": state["attempts"], "error": error, "at": time.time()})
        if progress is not None:
            state["progress"] = progress
        if state["attempts"] <= spec["budget"]["retries"] and not self._over_time(task, sid):
            if state["status"] != "RECOVERING":
                self._move(task, sid, "RECOVERING", error=error)
            state["retry_pending"] = True
            self._event(task, "subtask_retry", {"subtask": sid, "attempts": state["attempts"], "error": error})
        else:
            self._finish(task, sid, self._failure_outcome(state), progress=state.get("progress"), error=error)

    @staticmethod
    def _failure_outcome(state):
        met = [p["met"] for p in state.get("progress") or []]
        return "PARTIAL" if any(met) and not all(met) else "FAILED"

    @staticmethod
    def _runtime_error(runtime_task):
        for step in runtime_task["steps"].values():
            if step["status"] in {"failed", "canceled"}:
                return step.get("error") or (step.get("result") or {}).get("error") or step["status"]
        return runtime_task["status"]

    def _over_time(self, task, sid):
        state = task["state"][sid]
        started = state.get("started_at")
        return started is not None and time.time() - started > task["defs"][sid]["budget"]["timeout_s"]

    def _settle(self, task):
        order, states, defs = task["order"], task["state"], task["defs"]
        missing = [sid for sid in order if states[sid]["status"] == "MISSING_PREREQ"]
        broken = [sid for sid in order if defs[sid]["required"] and states[sid]["status"] in BROKEN]
        busy = any(states[sid]["status"] in {"PLANNING", "RUNNING", "RECOVERING"} for sid in order)
        if missing or broken:
            trigger = "missing_prerequisite" if missing else "subtask_failed"
            self._request_replan(task, trigger, {sid: states[sid].get("error") for sid in missing + broken})
        elif not busy and any(states[sid]["status"] == "BLOCKED" and states[sid].get("blocked_reason") == "operator"
                              for sid in order):
            task.update(status="needs_operator", error="a skill plan stopped for a person to decide")
            self._event(task, "needs_operator", {"waiting": [sid for sid in order if states[sid]["status"] == "BLOCKED"]})
        elif all(states[sid]["status"] in TERMINAL for sid in order):
            task["status"] = "accepting"

    def _advance_cancel(self, task):
        busy = False
        for sid in task["order"]:
            state = task["state"][sid]
            if state["status"] in TERMINAL:
                continue
            runtime_task = self._runtime_task(state)
            if runtime_task is not None and runtime_task["status"] not in RUNTIME_QUIET:
                busy = True
                self._interrupt(state, runtime_task, "task canceled")
            else:
                self._finish(task, sid, "CANCELED", error="task canceled")
        if not busy:
            task["status"] = "canceled"
            self._event(task, "task_canceled")
