"""Robot task coordination. All dispatches are journaled before external IO."""

import copy
import time
import uuid
from .contracts import (
    ContractError,
    require,
    schema_check,
    validate_plan,
    check_result,
    check_terminal,
    field,
)
from .skills import Skills, LOCAL_SPECS, validate_spec
from .world_state import WorldState, apply_effects, describe, effects_predicate
from .world_state import evaluate as evaluate_predicate
from .monitor import assess, ingest_observations

TERMINAL = {"succeeded", "failed", "canceled"}
# needs_operator: compensation failed or a person must decide; nothing runs until they do.
QUIET = TERMINAL | {"paused", "needs_operator"}
# A conditional step whose condition was false is skipped; it counts as done for its dependents.
STEP_DONE = TERMINAL | {"skipped"}
STEP_OK = {"succeeded", "skipped"}
# Failed tasks with remaining budget move up the feedback ladder:
# inner (retry/fallback in tick) -> middle (repair) -> outer (replan) -> operator.
RECOVERING = {"repairing", "replanning"}


def _ref_origins(value):
    if isinstance(value, dict):
        if "$ref" in value:
            return {value["$ref"].partition(".")[0]}
        return set().union(set(), *(_ref_origins(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(set(), *(_ref_origins(v) for v in value))
    return set()


def _bind_args(template, args):
    """Resolve {"$arg": "path"} placeholders against the original execution's arguments."""
    if isinstance(template, dict):
        if set(template) == {"$arg"}:
            return field(args, template["$arg"])
        return {k: _bind_args(v, args) for k, v in template.items()}
    if isinstance(template, list):
        return [_bind_args(v, args) for v in template]
    return template


def _definition(step):
    """What a step does, independent of key order and omitted defaults."""
    import json
    return json.dumps({"skill": step["skill"], "args": step.get("args", {}), "deps": sorted(step.get("deps", [])),
                       "retries": step.get("retries", 0), "fallback": step.get("fallback", []),
                       "when": step.get("when")}, sort_keys=True)


class Runtime:
    def __init__(self, store):
        self.store = store
        self.skills = Skills(store)

    def install_local_skills(self, allowed_roots):
        for name, body in LOCAL_SPECS.items():
            spec = copy.deepcopy(body)
            spec.update(
                resources={},
                cancelable=True,
                replay_safe=name != "file.copy",
                allowed_roots=allowed_roots,
                verifier=name in {"asset.verify", "asset.verify-set", "file.copy"},
            )
            self.register(name, spec)

    def register(self, name, spec):
        validate_spec(name, spec)
        self.store.put("skills", name, {"name": name, **spec})

    def catalog(self):
        return {x["name"]: x for x in self.store.list("skills")}

    def analyze_goal(self, goal, model_config, conversation=()):
        from .goal_analysis import GoalAnalyzer

        return GoalAnalyzer(self.store, model_config).analyze(goal, conversation)

    def submit(self, plan, robot_id, priority=0, goal=None, *, agent_context=None, task_id=None, completion_contract=None,
               parent=None, compensates=None):
        require(
            isinstance(robot_id, str) and robot_id and "/" not in robot_id,
            "robot_id must be one namespace segment",
        )
        require(type(priority) is int, "priority must be integer")
        catalog = self.catalog()
        self._check_plan(plan, catalog, robot_id, completion_contract)
        task = {
            "id": task_id or str(uuid.uuid4()),
            "robot_id": robot_id,
            "goal": goal,
            "priority": priority,
            "created_at": time.time(),
            "status": "queued",
            "plan": copy.deepcopy(plan),
            "catalog": catalog,
            "revision": 0,
            "generation": 0,
            "steps": {
                s["id"]: {"status": "pending", "attempts": 0, "alternative": 0}
                for s in plan["steps"]
            },
        }
        if completion_contract is not None:
            from .completion import validate_contract
            from .context_memory import record_hash
            task["completion_contract"] = validate_contract(completion_contract, catalog)
            task["completion_contract_hash"] = record_hash(task["completion_contract"])
        if agent_context is not None:
            require(
                set(agent_context)
                <= {"model_config", "max_replans", "replans", "max_repairs", "repairs", "model_response_id"},
                "invalid agent context fields",
            )
            task.update(copy.deepcopy(agent_context))
        if parent is not None:
            require(isinstance(parent, dict) and set(parent) == {"agent_task_id", "subtask_id"}
                    and all(isinstance(v, str) and v for v in parent.values()), "invalid task parent")
            task["parent"] = dict(parent)
        if compensates is not None:
            task["compensates"] = compensates
        with self.store.transaction():
            require(self.store.get("tasks", task["id"]) is None, "task already exists")
            self.store.put("tasks", task["id"], task)
            self.store.event(
                task["id"],
                "submitted",
                {"plan": plan, "robot_id": robot_id, "priority": priority},
            )
        return task

    def _check_plan(self, plan, catalog, robot_id, completion_contract=None):
        validate_plan(plan, catalog)
        from .completion import check_plan_contract
        check_plan_contract(plan, catalog, completion_contract)
        for step in plan["steps"]:
            for item in [step, *step.get("fallback", [])]:
                spec = catalog[item["skill"]]
                if spec.get("compensation"):
                    require(spec["compensation"]["skill"] in catalog,
                            f"compensation skill not registered: {spec['compensation']['skill']}")
                if spec.get('fencing_domain'):
                    domain = spec['fencing_domain']
                    require(spec['adapter'] == 'http', 'fenced runtime skills require enforcing HTTP endpoint')
                    require(spec.get('resources', {}).get(domain) == 1, 'fencing domain requires exclusive resource')
                    capacity = self.store.db.execute('SELECT capacity FROM resources WHERE name=?',
                        (self.resource_name(robot_id, domain),)).fetchone()
                    require(capacity is not None and capacity[0] == 1, 'fencing resource capacity must be one')
                if step.get("retries", 0):
                    require(
                        spec.get("replay_safe", False),
                        "retry requires replay_safe skill",
                    )
                names = [
                    self.resource_name(robot_id, k) for k in spec.get("resources", {})
                ]
                require(
                    len(set(names)) == len(names), "duplicate normalized resource names"
                )
                # Capacities must be configured before any task is accepted.
                for key, units in spec.get("resources", {}).items():
                    resource = self.resource_name(robot_id, key)
                    row = self.store.db.execute(
                        "SELECT capacity FROM resources WHERE name=?", (resource,)
                    ).fetchone()
                    require(
                        row is not None and row[0] >= units,
                        f"insufficient configured capacity: {resource}",
                    )

    def submit_goal(self, goal, robot_id, model_config, priority=0, max_replans=0):
        from .context_models import ContextRequest, GoalContext
        from .planner import Planner

        require(
            type(max_replans) is int and 0 <= max_replans <= 10,
            "replan budget must be 0..10",
        )
        planner = Planner(self.store, model_config)
        request = ContextRequest(
            request_id=str(uuid.uuid4()),
            phase="planning",
            goal=GoalContext.from_input(goal),
            robot_id=robot_id,
        )
        plan = planner.plan(goal, self.catalog(), context_request=request)
        context = {
            "model_config": {
                k: v
                for k, v in model_config.items()
                if k
                in {
                    "endpoint",
                    "model",
                    "token_env",
                    "timeout",
                    "max_input_tokens",
                    "max_output_tokens",
                }
            },
            "max_replans": max_replans,
            "replans": 0,
            "model_response_id": planner.last_response_id,
        }
        return self.submit(plan, robot_id, priority, goal, agent_context=context)

    def recover_plan(self, task_id):
        from .context_models import ContextRequest, GoalContext
        from .planner import Planner

        with self.store.transaction():
            task = self.store.get("tasks", task_id)
            if task is None or task["status"] not in {"failed", *RECOVERING} or not task.get("goal"):
                return task
            # Middle loop first: repair keeps verified progress. Outer loop replaces the plan.
            repair = task["status"] != "replanning" and task.get("repairs", 0) < task.get("max_repairs", 0)
            if not repair and task.get("replans", 0) >= task.get("max_replans", 0):
                return task
            counter = "repairs" if repair else "replans"
            task[counter] = task.get(counter, 0) + 1
            task["status"] = "repairing" if repair else "replanning"
            self._save(task)
        generation = task["generation"]
        try:
            snapshot_id = task.get("context_snapshot_id")
            from .context_snapshot import read_snapshot, snapshot_goal
            snapshot = read_snapshot(self.store, snapshot_id, robot_id=task["robot_id"],
                                     expected_hash=task.get("context_snapshot_hash")) if snapshot_id else None
            goal = GoalContext.from_input(task["goal"])
            if snapshot:
                goal = snapshot_goal(snapshot)
            planner = Planner(self.store, task["model_config"])
            request = ContextRequest(
                request_id=str(uuid.uuid4()),
                phase="repair" if repair else "replanning",
                goal=goal,
                robot_id=task["robot_id"],
                session_id=snapshot["session"]["id"] if snapshot else None,
                context_snapshot_id=snapshot_id,
                task_id=task_id,
                revision=task["revision"],
                generation=task["generation"],
            )
            recovery_guard = None
            if snapshot:
                from .recovery_budget import fit_recovery
                request, recovery_guard = fit_recovery(self.store, task, request)
            plan = planner.plan(
                goal.interpreted_intent, task["catalog"], context_request=request,
                method="repair" if repair else "planning",
            )
            if recovery_guard:
                recovery_guard()
            if snapshot:
                from .context_memory import verify_bindings
                verify_bindings(self.store, snapshot["session"].get("memory_bindings", []), task["robot_id"])
                from .context_assets import verify_session_assets
                verify_session_assets(self.store, snapshot["session"])
            return (self.repair if repair else self.revise)(
                task_id,
                plan,
                model_response_id=planner.last_response_id,
                automatic=True,
                expected_generation=generation,
            )
        except Exception as exc:
            with self.store.transaction():
                self.store.event(
                    task_id,
                    "repair_rejected" if repair else "replan_rejected",
                    {
                        "error": type(exc).__name__ + ": " + str(exc),
                        "attempt": task[counter],
                    },
                )
                rejected = self.store.get("tasks", task_id)
                if rejected["generation"] != generation:
                    return rejected
                control = self.store.get("controls", task_id)
                # An accepted stop request still needs the worker to apply it.
                # Do not turn its target terminal while rejecting a late model result.
                if not control and any(c["task_id"] == task_id and c["status"] == "accepted"
                                       and c["action"] in {"task.cancel", "task.pause"}
                                       for c in self.store.list("commands")):
                    return rejected
                rejected["status"] = (
                    ("canceled" if control["mode"] == "cancel" else "paused")
                    if control and not control.get("failure")
                    else "failed"
                )
                if (rejected["status"] == "failed" and repair
                        and rejected.get("replans", 0) < rejected.get("max_replans", 0)):
                    # A rejected local repair escalates to the outer loop.
                    rejected["status"] = "replanning"
                self._save(rejected)
                return rejected

    @staticmethod
    def resource_name(robot_id, key):
        require(
            "/" not in key
            or key.startswith(robot_id + "/")
            or key.startswith("shared/"),
            "resource outside robot scope",
        )
        return key if "/" in key else robot_id + "/" + key

    def interrupt(self, task_id, mode):
        require(mode in {"pause", "cancel"}, "interrupt must be pause or cancel")
        with self.store.transaction():
            task = self.store.get("tasks", task_id)
            require(task is not None, "unknown task")
            require(task["status"] not in TERMINAL, "task already terminal")
            old = self.store.get("controls", task_id)
            if old and old["mode"] == "cancel":
                mode = "cancel"
            self.store.put(
                "controls", task_id, {"mode": mode, "requested_at": time.time()}
            )
            # Wake a paused task to process cancellation through a new durable run.
            if task["status"] == "paused" and mode == "cancel":
                task["generation"] += 1
                task["status"] = "canceling"
                self.store.put("tasks", task_id, task)
            self.store.event(task_id, "interrupt_requested", {"mode": mode})

    def resume(self, task_id):
        with self.store.transaction():
            task = self.store.get("tasks", task_id)
            require(
                task is not None and task["status"] == "paused",
                "only a safely paused task can resume",
            )
            for step in task["plan"]["steps"]:
                state = task["steps"][step["id"]]
                require(
                    state["status"] not in {"running", "unknown"},
                    "unresolved execution",
                )
                if state["status"] == "canceled":
                    spec = task["catalog"][self._choice(step, state)["skill"]]
                    require(
                        spec.get("replay_safe", False),
                        "canceled skill is not safe to re-enter",
                    )
                    state.update(status="pending", attempts=0)
                    state.pop("execution_id", None)
                elif state["status"] == "verifying":
                    # Observation time spent paused does not count against the effect deadline.
                    state["effects"]["started_at"] = time.time()
            self.store.db.execute(
                "DELETE FROM objects WHERE collection=? AND id=?", ("controls", task_id)
            )
            task["status"] = "queued"
            task["generation"] += 1
            self.store.put("tasks", task_id, task)
            self.store.event(task_id, "resumed", {})
        return task

    def revise(
        self,
        task_id,
        plan,
        *,
        model_response_id=None,
        automatic=False,
        expected_generation=None,
    ):
        with self.store.transaction():
            task = self.store.get("tasks", task_id)
            require(task is not None, "unknown task")
            if self._discard_late_plan(task, automatic, expected_generation, model_response_id,
                                       {"paused", "failed", "replanning", "needs_operator"}):
                return task
            require(task["revision"] < 10, "plan revision budget exhausted")
            from .completion import task_contract
            self._check_plan(plan, self.catalog(), task["robot_id"], task_contract(task))
            # A revision is a new plan, never an implicit replay of completed side effects.
            for step in plan["steps"]:
                for item in [step, *step.get("fallback", [])]:
                    require(
                        self.catalog()[item["skill"]].get("replay_safe", False),
                        "new plan requires replay-safe skills; submit a separately reviewed task for irreversible actions",
                    )
            self.store.put(
                "plan_history",
                f"{task_id}:{task['revision']}",
                {
                    "revision": task["revision"],
                    "generation": task["generation"],
                    "saved_at": time.time(),
                    "plan": copy.deepcopy(task["plan"]),
                    "steps": copy.deepcopy(task["steps"]),
                    "catalog": copy.deepcopy(task["catalog"]),
                },
            )
            task.update(
                plan=copy.deepcopy(plan),
                catalog=self.catalog(),
                steps={
                    s["id"]: {"status": "pending", "attempts": 0, "alternative": 0}
                    for s in plan["steps"]
                },
                status="queued",
                revision=task["revision"] + 1,
                generation=task["generation"] + 1,
            )
            self.store.db.execute(
                "DELETE FROM objects WHERE collection=? AND id=?", ("controls", task_id)
            )
            if model_response_id is not None:
                task["model_response_id"] = model_response_id
            self.store.put("tasks", task_id, task)
            self.store.event(
                task_id, "plan_revised", {"revision": task["revision"], "plan": plan}
            )
        return task

    def _discard_late_plan(self, task, automatic, expected_generation, model_response_id, statuses):
        """Guards shared by revise and repair. True when an automatic plan must be dropped."""
        task_id = task["id"]
        if automatic and expected_generation is not None and task["generation"] != expected_generation:
            self.store.event(task_id, "stale_plan_discarded", {"expected_generation": expected_generation})
            return True
        require(task["status"] in statuses, "replan requires safely stopped task")
        require(not any(s.get("status") in {"running", "unknown"} for s in task["steps"].values()),
                "replan has unresolved execution")
        control = self.store.get("controls", task_id)
        if automatic and any(c["task_id"] == task_id and c["action"] == "task.cancel" and c["status"] == "accepted"
                             for c in self.store.list("commands")):
            self.store.event(task_id, "late_plan_discarded", {"reason": "accepted cancellation command", "model_response_id": model_response_id})
            return True
        if automatic and control and not control.get("failure"):
            task["status"] = "canceled" if control["mode"] == "cancel" else "paused"
            self.store.put("tasks", task_id, task)
            self.store.event(task_id, "late_plan_discarded",
                             {"model_response_id": model_response_id, "interrupt": control["mode"]})
            return True
        return False

    def repair(self, task_id, plan, *, model_response_id=None, automatic=False, expected_generation=None):
        """Middle feedback loop: replace only the failed part of a plan.

        A step keeps its verified result when the new plan contains it unchanged
        (same id and definition), every dependency is also kept, and the world
        does not contradict its declared effects. Everything else restarts.
        Unlike ``revise``, new steps may use irreversible skills when those
        skills are guarded by world-state preconditions.
        """
        from graphlib import TopologicalSorter
        from .completion import task_contract

        with self.store.transaction():
            task = self.store.get("tasks", task_id)
            require(task is not None, "unknown task")
            if self._discard_late_plan(task, automatic, expected_generation, model_response_id,
                                       {"paused", "failed", "repairing", "needs_operator"}):
                return task
            require(task["revision"] < 10, "plan revision budget exhausted")
            catalog = self.catalog()
            self._check_plan(plan, catalog, task["robot_id"], task_contract(task))
            old = {s["id"]: s for s in task["plan"]["steps"]}
            world = WorldState(self.store, task["robot_id"])
            kept = {}
            graph = {s["id"]: set(s.get("deps", [])) for s in plan["steps"]}
            by_id = {s["id"]: s for s in plan["steps"]}
            for step_id in TopologicalSorter(graph).static_order():
                step, previous = by_id[step_id], old.get(step_id)
                state = task["steps"].get(step_id)
                if (previous is None or step_id == plan["verification"] or state["status"] != "succeeded"
                        or _definition(previous) != _definition(step) or not graph[step_id] <= set(kept)):
                    continue
                spec = task["catalog"][self._choice(previous, state)["skill"]]
                if spec.get("effects"):
                    execution = self.store.get("executions", state["execution_id"])
                    verdict = evaluate_predicate(world, effects_predicate(spec["effects"]), execution["args"],
                                                 output=state["result"]["output"])
                    if verdict["result"] == "false":
                        continue  # the world has changed since; this progress no longer holds
                kept[step_id] = copy.deepcopy(state)
            for step in plan["steps"]:
                if step["id"] in kept:
                    continue
                for item in [step, *step.get("fallback", [])]:
                    spec = catalog[item["skill"]]
                    require(spec.get("replay_safe", False) or spec.get("preconditions"),
                            "repaired steps need replay-safe skills or world-state preconditions")
            self.store.put("plan_history", f"{task_id}:{task['revision']}", {
                "revision": task["revision"], "generation": task["generation"], "saved_at": time.time(),
                "plan": copy.deepcopy(task["plan"]), "steps": copy.deepcopy(task["steps"]),
                "catalog": copy.deepcopy(task["catalog"])})
            task.update(
                plan=copy.deepcopy(plan),
                catalog=catalog,
                steps={s["id"]: kept.get(s["id"]) or {"status": "pending", "attempts": 0, "alternative": 0}
                       for s in plan["steps"]},
                status="queued",
                revision=task["revision"] + 1,
                generation=task["generation"] + 1,
            )
            self.store.db.execute("DELETE FROM objects WHERE collection=? AND id=?", ("controls", task_id))
            if model_response_id is not None:
                task["model_response_id"] = model_response_id
            self.store.put("tasks", task_id, task)
            self.store.event(task_id, "plan_repaired", {
                "revision": task["revision"], "plan": plan, "kept": sorted(kept),
                "restarted": sorted(set(by_id) - set(kept))})
        return task

    def preempt(self, task_id):
        target = self.store.get("tasks", task_id)
        require(target is not None, "unknown task")
        affected = []
        for task in self.store.list("tasks"):
            if (
                task["id"] != task_id
                and task["robot_id"] == target["robot_id"]
                and task["priority"] < target["priority"]
                and task["status"] not in QUIET
            ):
                self.interrupt(task["id"], "pause")
                affected.append(task["id"])
        return affected

    @staticmethod
    def _choice(step, state):
        return [step, *step.get("fallback", [])][state["alternative"]]

    def _args(self, value, task):
        if isinstance(value, dict):
            if "$ref" in value:
                origin, _, path = value["$ref"].partition(".")
                state = task["steps"][origin]
                require(
                    state["status"] == "succeeded", "dependency output not verified"
                )
                return field(state["result"]["output"], path)
            return {k: self._args(v, task) for k, v in value.items()}
        if isinstance(value, list):
            return [self._args(v, task) for v in value]
        return value

    def _higher_waiting(self, task, needs):
        for other in self.store.list("tasks"):
            if (
                other["id"] == task["id"]
                or other["status"] not in {"queued", "running"}
                or other["priority"] <= task["priority"]
            ):
                continue
            for step in other["plan"]["steps"]:
                state = other["steps"][step["id"]]
                if state["status"] != "pending" or not all(
                    other["steps"][d]["status"] in STEP_OK
                    for d in step.get("deps", [])
                ):
                    continue
                spec = other["catalog"][self._choice(step, state)["skill"]]
                wanted = {
                    self.resource_name(other["robot_id"], k)
                    for k in spec.get("resources", {})
                }
                if wanted.intersection(needs):
                    return True
        return False

    def _save(self, task):
        self.store.put("tasks", task["id"], task)

    def _inner_loop(self, task, step, state, spec, *, allow_retry=True):
        """Inner feedback loop: bounded retry of a replay-safe skill, then the next fallback."""
        if state["status"] != "failed" or self.store.get("controls", task["id"]):
            return
        if allow_retry and spec.get("replay_safe", False) and state["attempts"] <= step.get("retries", 0):
            state.update(status="pending")
        elif state["alternative"] < len(step.get("fallback", [])):
            state.update(status="pending", alternative=state["alternative"] + 1, attempts=0)
        else:
            return
        state.pop("execution_id", None)
        state.pop("effects", None)

    def _preconditions_hold(self, task, step, state, spec, args):
        """Gate dispatch on world state. Unknown waits (bounded); false fails without dispatching."""
        verdict = evaluate_predicate(WorldState(self.store, task["robot_id"]), spec["preconditions"], args)
        if verdict["result"] == "true":
            state.pop("blocked", None)
            return True
        now = time.time()
        blocked = state.get("blocked")
        if verdict["result"] == "false" or (blocked and now - blocked["since"] > spec.get("precondition_timeout", 10)):
            state.pop("blocked", None)
            state.update(status="failed", error=f"precondition {verdict['result']}: {describe(verdict)}")
            self.store.event(task["id"], "precondition_failed",
                             {"step": step["id"], "skill": self._choice(step, state)["skill"],
                              "verdict": verdict["result"], "atoms": verdict["atoms"]})
            # Retrying the same skill against the same world cannot help; a fallback may.
            self._inner_loop(task, step, state, spec, allow_retry=False)
        elif not blocked:
            state["blocked"] = {"since": now, "reason": describe(verdict)}
            self.store.event(task["id"], "precondition_waiting",
                             {"step": step["id"], "reason": state["blocked"]["reason"], "atoms": verdict["atoms"]})
        return False

    def _expect_effects(self, task, step, state, spec, execution):
        """Record declared effects as expectations; the step succeeds only once observed."""
        try:
            applied = apply_effects(WorldState(self.store, task["robot_id"]), spec["effects"], execution["args"],
                                    state["result"]["output"], execution_id=execution["id"],
                                    evidence_level=execution.get("evidence_level", "software_integration"))
        except ContractError as exc:
            state.update(status="failed", error="effect could not be resolved: " + str(exc))
            self.store.event(task["id"], "effects_rejected", {"step": step["id"], "error": state["error"]})
            return
        if spec.get("confirm_effects", True):
            state.update(status="verifying",
                         effects={"applied": applied, "since_ns": time.time_ns(), "started_at": time.time()})
            self.store.event(task["id"], "effects_pending",
                             {"step": step["id"], "execution_id": execution["id"], "effects": applied})

    def _confirm_effects(self, task, step, state, spec):
        execution = self.store.get("executions", state["execution_id"])
        verdict = evaluate_predicate(WorldState(self.store, task["robot_id"]), effects_predicate(spec["effects"]),
                                     execution["args"], since_ns=state["effects"]["since_ns"],
                                     output=state["result"]["output"], exclude_source=execution["id"])
        if verdict["result"] == "true":
            state["status"] = "succeeded"
            self.store.event(task["id"], "effects_confirmed", {"step": step["id"], "atoms": verdict["atoms"]})
            return
        timed_out = time.time() - state["effects"]["started_at"] > spec.get("effect_timeout", 10)
        if verdict["result"] == "false" or timed_out:
            reason = "contradicted by observation" if verdict["result"] == "false" else "not observed in time"
            state.update(status="failed", error=f"effect {reason}: {describe(verdict)}")
            self.store.event(task["id"], "effects_rejected",
                             {"step": step["id"], "reason": reason, "atoms": verdict["atoms"]})
            self._inner_loop(task, step, state, spec)
            self._note_compensation(task, step, state, spec, execution)

    def tick(self, task_id):
        # One worker owns this process; controls are a separate durable collection so
        # CLI interrupts are not overwritten by an in-flight response.
        with self.store.lock:
            task = self.store.get("tasks", task_id)
            require(task is not None, "unknown task")
            if task["status"] in QUIET:
                return task
            if task["status"] == "compensating":
                return self._await_compensation(task)
            for step in task["plan"]["steps"]:
                state = task["steps"][step["id"]]
                control = self.store.get("controls", task_id)
                if state["status"] in STEP_DONE:
                    continue
                choice = self._choice(step, state)
                spec = task["catalog"][choice["skill"]]
                if state["status"] == "verifying":
                    self._confirm_effects(task, step, state, spec)
                    continue
                if not state.get("execution_id"):
                    if control or any(
                        s["status"] in {"failed", "canceled"}
                        for s in task["steps"].values()
                    ):
                        continue
                    if not all(
                        task["steps"][d]["status"] in STEP_OK
                        for d in step.get("deps", [])
                    ):
                        continue
                    skipped = sorted(o for o in _ref_origins(choice.get("args", {}))
                                     if task["steps"][o]["status"] == "skipped")
                    if skipped:
                        state.update(status="skipped", reason=f"input comes from skipped step {skipped[0]}")
                        self.store.event(task_id, "step_skipped", {"step": step["id"], "reason": state["reason"]})
                        continue
                    if "when" in step and not self._condition_holds(task, step, state):
                        continue
                    try:
                        args = self._args(choice.get("args", {}), task)
                        schema_check(spec["input_schema"], args)
                    except ContractError as exc:
                        state.update(status="failed", error=str(exc))
                        self.store.event(
                            task_id,
                            "input_rejected",
                            {"step": step["id"], "error": str(exc)},
                        )
                        continue
                    if spec.get("preconditions") and not self._preconditions_hold(task, step, state, spec, args):
                        continue
                    if spec["adapter"] == "inprocess" and not self.skills.has_handler(spec):
                        # The skill lives in another process's driver; never dispatch blind.
                        if not state.get("handler_wait"):
                            state["handler_wait"] = time.time()
                            self.store.event(task_id, "handler_unavailable",
                                             {"step": step["id"], "handler": spec["handler"]})
                        continue
                    state.pop("handler_wait", None)
                    needs = {
                        self.resource_name(task["robot_id"], k): v
                        for k, v in spec.get("resources", {}).items()
                    }
                    if self._higher_waiting(task, needs):
                        continue
                    execution_id = str(uuid.uuid4())
                    with self.store.transaction():
                        try:
                            from .completion import task_contract, same_contract
                            completion = task_contract(task)
                            if task.get("context_snapshot_id"):
                                from .context_snapshot import read_snapshot
                                from .context_assets import verify_session_assets
                                snapshot = read_snapshot(self.store, task["context_snapshot_id"],
                                    robot_id=task["robot_id"], session_id=task.get("session_id"),
                                    expected_hash=task.get("context_snapshot_hash"))
                                verify_session_assets(self.store, snapshot["session"])
                                require(same_contract(snapshot["session"].get("completion_contract"), completion),
                                        "snapshot completion contract differs from task")
                        except ContractError as exc:
                            state.update(status="failed", error=str(exc))
                            self._save(task)
                            self.store.event(task_id, "context_rejected", {"step": step["id"], "error": str(exc)})
                            continue
                        if not self.store._acquire(execution_id, needs):
                            continue
                        state.update(
                            execution_id=execution_id,
                            status="running",
                            attempts=state["attempts"] + 1,
                        )
                        execution = {
                            "id": execution_id,
                            "task_id": task_id,
                            "step": step["id"],
                            "skill": choice["skill"],
                            "args": args,
                            "created_at": time.time(),
                            "status": "dispatched",
                            "evidence_level": spec.get("evidence_level", "software_integration"),
                        }
                        if spec.get('fencing_domain'):
                            from .fencing import issue
                            execution['authority'] = issue(self.store, task['robot_id'], spec['fencing_domain'])
                        self.store.put("executions", execution_id, execution)
                        self._save(task)
                        self.store.event(task_id, "dispatch_intent", execution)
                    operation = "start"
                else:
                    execution_id = state["execution_id"]
                    execution = self.store.get("executions", execution_id)
                    operation = "query"
                expired = time.time() - execution["created_at"] > spec.get(
                    "execution_timeout", 300
                )
                finding = execution.get("finding")
                if (control or expired or finding) and spec.get("cancelable", False):
                    operation = "cancel"
                try:
                    result = self.skills.call(
                        operation,
                        choice["skill"],
                        spec,
                        execution_id,
                        execution["args"],
                        task["robot_id"],
                        **({'authority': execution.get('authority')} if spec.get('fencing_domain') else {}),
                    )
                    require(
                        isinstance(result, dict)
                        and result.get("execution_id") == execution_id,
                        "execution response identity mismatch",
                    )
                    require(
                        result.get("status")
                        in TERMINAL | {"running", "accepted", "canceling", "unknown"},
                        "invalid skill state",
                    )
                    level = execution.get("evidence_level", "software_integration")
                    ingest_observations(self.store, task["robot_id"], result, execution_id=execution_id,
                                        evidence_level=level, event_task=task_id)
                    if result["status"] not in TERMINAL:
                        state["status"] = (
                            "unknown" if result["status"] == "unknown" else "running"
                        )
                        execution.update(status=state["status"], result=result)
                        if state["status"] == "running" and not finding:
                            finding = assess(spec, execution, result, WorldState(self.store, task["robot_id"]))
                            if finding:
                                execution["finding"] = finding
                                self.store.event(task_id, "monitor_finding",
                                                 {"step": step["id"], "execution_id": execution_id, **finding,
                                                  "will_cancel": spec.get("cancelable", False)})
                    else:
                        check_terminal(result, execution_id)
                        if result["status"] == "succeeded":
                            try:
                                check_result(spec, result, execution_id)
                                check_result(
                                    task["catalog"][step["skill"]], result, execution_id
                                )
                                if step["id"] == task["plan"]["verification"]:
                                    from .completion import task_contract, evaluate
                                    completion = task_contract(task)
                                    if completion is not None:
                                        evaluation = evaluate(completion, result["output"], execution_id)
                                        result = {**result, "reported_status": result["status"], "completion_evaluation": evaluation}
                                        require(evaluation["status"] == "passed", "task completion criteria failed")
                            except ContractError as exc:
                                result = {
                                    **result,
                                    "status": "failed",
                                    "error": str(exc),
                                }
                        if expired and result["status"] == "canceled" and not control:
                            result = {
                                **result,
                                "status": "failed",
                                "error": "execution deadline exceeded",
                            }
                        if finding and not control and result["status"] in {"succeeded", "canceled"}:
                            # A violated invariant or stall invalidates even a reported success.
                            result = {**result, "reported_status": result["status"], "status": "failed",
                                      "error": f"monitor {finding['kind']}: {finding['reason']}"}
                        state.update(status=result["status"], result=result)
                        execution.update(status=result["status"], result=result)
                        with self.store.transaction():
                            self.store.release(execution_id)
                            self.store.put("executions", execution_id, execution)
                            self.store.event(task_id, "execution_result", result)
                            if state["status"] == "succeeded" and spec.get("effects"):
                                self._expect_effects(task, step, state, spec, execution)
                            self._inner_loop(task, step, state, spec)
                            self._note_compensation(task, step, state, spec, execution)
                            self._save(task)
                except Exception as exc:
                    state.update(
                        status="unknown", error=type(exc).__name__ + ": " + str(exc)
                    )
                    execution.update(status="unknown", error=state["error"])
                    self.store.event(
                        task_id,
                        "execution_unknown",
                        {"execution_id": execution_id, "error": state["error"]},
                    )
                self.store.put("executions", execution_id, execution)
            control = self.store.get("controls", task_id)
            active = any(
                s["status"] in {"running", "unknown"} for s in task["steps"].values()
            )
            if control:
                if active:
                    task["status"] = (
                        "canceling" if control["mode"] == "cancel" else "pausing"
                    )
                else:
                    task["status"] = (
                        "canceled" if control["mode"] == "cancel" else "paused"
                    )
            elif any(s["status"] == "unknown" for s in task["steps"].values()):
                task["status"] = "unknown"
            elif any(
                s["status"] in {"failed", "canceled"} for s in task["steps"].values()
            ):
                # Stop still-active siblings before marking the whole task failed.
                if active:
                    self.store.put(
                        "controls",
                        task_id,
                        {
                            "mode": "cancel",
                            "requested_at": time.time(),
                            "failure": True,
                        },
                    )
                    task["status"] = "canceling"
                else:
                    task["status"] = "failed"
            elif all(s["status"] in STEP_OK for s in task["steps"].values()):
                task["status"] = "succeeded"
            else:
                task["status"] = "running"
            if control and control.get("failure") and not active:
                task["status"] = "failed"
            if task["status"] == "failed" and task.get("compensation_due"):
                self._start_compensation(task)
            else:
                self._escalate(task)
            self._save(task)
            return task

    @staticmethod
    def _escalate(task):
        """Feedback ladder above the inner loop: repair, then replan, else stay failed."""
        if task["status"] == "failed" and task.get("goal"):
            if task.get("repairs", 0) < task.get("max_repairs", 0):
                task["status"] = "repairing"
            elif task.get("replans", 0) < task.get("max_replans", 0):
                task["status"] = "replanning"

    # ------------------------------------------------------------ conditions and compensation

    def _condition_holds(self, task, step, state):
        """A conditional step runs on true, is skipped on false, and waits (bounded) on unknown."""
        verdict = evaluate_predicate(WorldState(self.store, task["robot_id"]), step["when"])
        if verdict["result"] == "true":
            state.pop("blocked", None)
            return True
        now = time.time()
        blocked = state.get("blocked")
        if verdict["result"] == "false":
            state.pop("blocked", None)
            state.update(status="skipped", reason="condition false: " + describe(verdict))
            self.store.event(task["id"], "step_skipped", {"step": step["id"], "reason": state["reason"],
                                                          "atoms": verdict["atoms"]})
        elif blocked and now - blocked["since"] > step.get("when_timeout", 10):
            state.pop("blocked", None)
            state.update(status="failed", error="condition unknown: " + describe(verdict))
            self.store.event(task["id"], "condition_unknown", {"step": step["id"], "atoms": verdict["atoms"]})
        elif not blocked:
            state["blocked"] = {"since": now, "reason": describe(verdict)}
            self.store.event(task["id"], "condition_waiting", {"step": step["id"], "atoms": verdict["atoms"]})
        return False

    def _note_compensation(self, task, step, state, spec, execution):
        """A dispatched action that finally failed may have left side effects; queue its compensation."""
        if state["status"] != "failed" or not spec.get("compensation"):
            return
        due = task.setdefault("compensation_due", [])
        if any(item["execution_id"] == execution["id"] for item in due):
            return
        compensation = spec["compensation"]
        due.append({"step": step["id"], "execution_id": execution["id"], "skill": compensation["skill"],
                    "args": _bind_args(compensation.get("args", {}), execution["args"])})
        self.store.event(task["id"], "compensation_queued", due[-1])

    def _start_compensation(self, task):
        due = task["compensation_due"]
        steps = [{"id": f"compensate_{i}", "skill": item["skill"], "args": item["args"],
                  "deps": [f"compensate_{i - 1}"] if i else []} for i, item in enumerate(due)]
        try:
            child = self.submit({"steps": steps, "verification": steps[-1]["id"]}, task["robot_id"],
                                task["priority"] + 1, goal=None, compensates=task["id"])
        except ContractError as exc:
            task["status"] = "needs_operator"
            self.store.event(task["id"], "compensation_rejected", {"error": str(exc), "due": due})
            return
        task.update(status="compensating", compensation_task_id=child["id"])
        self.store.event(task["id"], "compensation_started", {"task_id": child["id"], "due": due})

    def _await_compensation(self, task):
        child = self.store.get("tasks", task["compensation_task_id"])
        if child["status"] == "succeeded":
            task.setdefault("compensated", []).extend(task.pop("compensation_due"))
            task["status"] = "failed"
            self.store.event(task["id"], "compensation_succeeded", {"task_id": child["id"]})
            self._escalate(task)
        elif child["status"] in QUIET:
            # The body may be in an unsafe intermediate state; only a person can decide what is next.
            task["status"] = "needs_operator"
            self.store.event(task["id"], "compensation_failed", {"task_id": child["id"], "status": child["status"]})
        else:
            return task
        self._save(task)
        return task

    def resolve(self, task_id, decision, note=""):
        """Operator decision for a task waiting on a person: recover (re-enter the ladder) or fail."""
        require(decision in {"recover", "fail"}, "decision must be recover or fail")
        with self.store.transaction():
            task = self.store.get("tasks", task_id)
            require(task is not None and task["status"] in {"needs_operator", "failed"},
                    "only a failed task or one waiting for an operator can be resolved")
            require(not any(s["status"] in {"running", "unknown"} for s in task["steps"].values()),
                    "task has unresolved executions")
            task.pop("compensation_due", None)
            task["status"] = "failed"
            if decision == "recover":
                # Operator vouches the body is safe; continue with whatever recovery budget remains.
                self._escalate(task)
            task["generation"] += 1
            self._save(task)
            self.store.event(task_id, "operator_resolved", {"decision": decision, "note": note,
                                                            "status": task["status"]})
        return task
