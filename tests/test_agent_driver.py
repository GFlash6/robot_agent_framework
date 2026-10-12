"""In-process agent driver: TaskPlans run as task trees over a kinematic fixture world.

``KinematicWorld`` is a deterministic protocol fixture: it moves a few numbers
when skills run and publishes them as observations. It exercises the agent
loop the way a simulator or benchmark will drive it. Plans here are operator
TaskPlans with pre-authored skill plans (no model). It is L0 evidence only and
says nothing about physical behaviour or planning quality.
"""

import json
import time
from pathlib import Path

import pytest

from robot_agent.agent import Agent, SkillHandler
from robot_agent.contracts import ContractError, validate_plan
from robot_agent.runtime import Runtime
from robot_agent.store import Store
from robot_agent.world_state import WorldState, evaluate

PROFILES = Path(__file__).resolve().parents[1] / "profiles"
SCENE = {"frame": "map", "objects": {
    "table_1": {"approach": [1.6, 0.5, 0.0]},
    "drawer_1": {"approach": [1.0, -0.6, 0.0], "region": {"min": [0.8, -1.2, 0.3], "max": [1.2, -0.8, 0.6]}},
}}
INSTRUCTION = "把红色方块放进抽屉"
ALL_PASS = {"capability": "pass", "objects": "pass", "constraints": "pass", "resources": "pass", "ambiguity": "pass"}


class WorldSkill(SkillHandler):
    source = "kinematic-world"

    def __init__(self, world, act, steps=1):
        self.world, self.act, self.steps = world, act, steps

    def start(self, execution_id, args, robot_id):
        self.world.jobs[execution_id] = {"left": self.steps}
        return self.query(execution_id, args, robot_id)

    def query(self, execution_id, args, robot_id):
        job = self.world.jobs[execution_id]
        if "result" in job:
            return job["result"]
        job["left"] -= 1
        if job["left"] > 0:
            return self.running(execution_id, progress=1 - job["left"] / self.steps)
        error, output = self.act(args)
        job["result"] = self.finished(execution_id, "failed" if error else "succeeded",
                                      output=output or ({} if error else {"done": True}), error=error)
        return job["result"]

    def cancel(self, execution_id, args, robot_id):
        job = self.world.jobs[execution_id]
        job.setdefault("result", self.finished(execution_id, "canceled"))
        return job["result"]


class KinematicWorld:
    def __init__(self):
        self.pose, self.ready, self.estop, self.holding = [0.0, 0.0, 0.0], False, False, "none"
        self.objects = {"red_block": [2.0, 0.5, 0.8]}
        self.drawer_open, self.battery = False, 0.8
        self.pick_failures, self.retract_fails = 0, False
        self.jobs = {}

    def observations(self):
        items = [{"key": "robot.pose", "value": list(self.pose), "frame_id": "map"},
                 {"key": "robot.mobility_ready", "value": self.ready}, {"key": "robot.estop", "value": self.estop},
                 {"key": "robot.holding", "value": self.holding}, {"key": "robot.battery", "value": self.battery},
                 {"key": "drawer_1.open", "value": self.drawer_open}]
        items += [{"key": f"{name}.location", "value": list(loc), "frame_id": "map"} for name, loc in self.objects.items()]
        return [{**item, "ttl_s": 5, "ref": "camera"} for item in items]

    def _stand(self, args):
        self.ready = True
        return None, None

    def _navigate(self, args):
        self.pose = list(args["target"])
        return None, None

    def _locate(self, args):
        return None, {"found": True, "location": self.objects[args["object"]]}

    def _pick(self, args):
        if self.pick_failures:
            self.pick_failures -= 1
            return "grasp slipped", None
        self.holding = args["object"]
        return None, None

    def _place(self, args):
        self.holding, self.objects[args["object"]] = "none", list(args["target"])
        return None, None

    def _retract(self, args):
        return ("arm blocked", None) if self.retract_fails else (None, None)

    def handlers(self):
        return {"robot.stand": WorldSkill(self, self._stand, steps=2),
                "base.navigate_to": WorldSkill(self, self._navigate, steps=3),
                "perception.locate_object": WorldSkill(self, self._locate),
                "manip.pick": WorldSkill(self, self._pick),
                "manip.place": WorldSkill(self, self._place),
                "manip.retract": WorldSkill(self, self._retract)}


def make_agent(tmp_path, model_config=None):
    profile = json.loads((PROFILES / "unitree_g1.json").read_text(encoding="utf-8"))
    binding = {"schema_version": 1, "embodiment": "unitree_g1", "track": "simulation", "provider": "benchmark",
               "adapter": "inprocess", "skills": {name: {} for name in profile["skills"]}}
    agent = Agent(tmp_path / "ledger", "g1-01", model_config)
    agent.install(profile, binding)
    world = KinematicWorld()
    for skill, handler in world.handlers().items():
        agent.handle(skill, handler)
    agent.load_scene(SCENE)
    agent.observe_many(world.observations())
    return agent, world


def subtask(sid, description, goal, depends_on=(), **extra):
    category, kind = sid.split("-")[2:]
    key = {"A": "achieve", "C": "check", "Q": "obtain"}[kind]
    typed = {"type": extra.pop("type")} if "type" in extra else {}
    return {"id": sid, "category": category, "kind": kind, "description": description, "goal": {key: goal, **typed},
            "depends_on": list(depends_on), **extra}


def task_plan(subtasks, *, version=1, instruction=INSTRUCTION, targets=("red_block", "drawer_1"), intent="place_object"):
    return {"task_id": "t1", "plan_version": version, "instruction": instruction,
            "understanding": {"intent": intent, "targets": [{"ref": t, "desc": t} for t in targets],
                              "recipient": None, "constraints": [], "ambiguities": []},
            "situation": "机器人在原点，手里没有东西。",
            "grounding": [{"ref": t, "status": "known", "id": t} for t in targets],
            "feasibility": {"verdict": "feasible", "checks": dict(ALL_PASS), "assumptions": []},
            "subtasks": subtasks, "reply": "好的，开始执行。"}


WALK = {"steps": [{"id": "stand", "skill": "robot.stand", "args": {}},
                  {"id": "walk", "skill": "base.navigate_to", "deps": ["stand"], "args": {"target": [1.6, 0.5, 0.0]}}],
        "verification": "walk"}
PICK = {"steps": [{"id": "find", "skill": "perception.locate_object", "args": {"object": "red_block"}},
                  {"id": "grasp", "skill": "manip.pick", "deps": ["find"], "args": {"object": "red_block"}}],
        "verification": "grasp"}
PLACE = {"steps": [{"id": "walk", "skill": "base.navigate_to", "args": {"target": [1.0, -0.6, 0.0]}},
                   {"id": "drop", "skill": "manip.place", "deps": ["walk"],
                    "args": {"object": "red_block", "target": [1.0, -1.0, 0.45]}}], "verification": "drop"}
LOOK = {"steps": [{"id": "look", "skill": "perception.locate_object", "args": {"object": "red_block"}}],
        "verification": "look"}


def place_block():
    plan = task_plan([
        subtask("t1-01-MOV-A", "走到桌前", "robot_at(table_1)"),
        subtask("t1-02-MAN-A", "抓起红色方块", "holding(red_block)", ["t1-01-MOV-A"]),
        subtask("t1-03-MAN-A", "把方块放进抽屉", "object_in(red_block, drawer_1)", ["t1-02-MAN-A"]),
    ])
    return plan, {"t1-01-MOV-A": WALK, "t1-02-MAN-A": PICK, "t1-03-MAN-A": PLACE}


def run(agent, world, task, steps=300):
    return agent.run(task, max_steps=steps, on_step=lambda a: a.observe_many(world.observations()))


def by_id(report):
    return {s["id"]: s for s in report["subtasks"]}


def test_scene_regions_and_fact_references(tmp_path):
    store = Store(tmp_path / "ledger")
    world = WorldState(store, "r1")
    world.load_scene(SCENE, source_ref="scene", evidence_level="simulation")
    world.observe("red_block.location", [1.0, -1.0, 0.45], source={"kind": "observation", "ref": "camera"},
                  stamp_ns=time.time_ns(), evidence_level="simulation", frame_id="map")
    inside = {"fact": "red_block.location", "op": "inside", "value": {"$fact": "drawer_1.region"}}
    assert evaluate(world, inside)["result"] == "true"
    world.observe("red_block.location", [2.0, 0.5, 0.8], source={"kind": "observation", "ref": "camera"},
                  stamp_ns=time.time_ns(), evidence_level="simulation", frame_id="map")
    assert evaluate(world, inside)["result"] == "false"
    # Different frames cannot be compared; a missing reference is unknown, not false.
    world.observe("red_block.location", [1.0, -1.0, 0.45], source={"kind": "observation", "ref": "camera"},
                  stamp_ns=time.time_ns(), evidence_level="simulation", frame_id="odom")
    assert evaluate(world, inside)["result"] == "unknown"
    assert evaluate(world, {"fact": "red_block.location", "op": "inside",
                            "value": {"$fact": "shelf_9.region"}})["result"] == "unknown"
    with pytest.raises(ContractError):
        world.load_scene({"objects": {"bad": {"region": {"min": [1, 1], "max": [0, 0]}}}},
                         source_ref="scene", evidence_level="simulation")
    store.close()


def test_conditional_steps_skip_their_branch(tmp_path):
    store = Store(tmp_path / "ledger")
    runtime = Runtime(store)
    runtime.install_local_skills([str(tmp_path)])
    doc = tmp_path / "doc.txt"
    doc.write_text("document")
    meta = {"kind": "document", "encoding": "utf8", "source": "test"}
    plan = {"steps": [
        {"id": "open_drawer", "skill": "file.ingest", "args": {"path": str(doc), "metadata": meta},
         "when": {"fact": "drawer_1.open", "op": "eq", "value": False}},
        {"id": "check_open", "skill": "asset.verify", "deps": ["open_drawer"],
         "args": {"asset_id": {"$ref": "open_drawer.asset_id"}}},
        {"id": "save", "skill": "file.ingest", "args": {"path": str(doc), "metadata": meta}},
        {"id": "check", "skill": "asset.verify", "deps": ["save", "check_open"], "args": {"asset_id": {"$ref": "save.asset_id"}}},
    ], "verification": "check"}
    world = WorldState(store, "r1")
    task = runtime.submit(plan, "r1")
    runtime.tick(task["id"])
    assert "condition_waiting" in [e["type"] for e in store.events(task["id"])]  # unknown: wait
    world.observe("drawer_1.open", True, source={"kind": "operator", "ref": "t"}, stamp_ns=time.time_ns(),
                  evidence_level="software_integration")
    for _ in range(10):
        task = runtime.tick(task["id"])
    assert task["status"] == "succeeded"
    assert task["steps"]["open_drawer"]["status"] == "skipped" and task["steps"]["check_open"]["status"] == "skipped"
    world.observe("drawer_1.open", False, source={"kind": "operator", "ref": "t"}, stamp_ns=time.time_ns(),
                  evidence_level="software_integration")
    second = runtime.submit(plan, "r1")
    for _ in range(10):
        second = runtime.tick(second["id"])
    assert second["status"] == "succeeded" and second["steps"]["open_drawer"]["status"] == "succeeded"
    with pytest.raises(ContractError, match="cannot be conditional"):
        validate_plan({**plan, "steps": plan["steps"][:3] + [{**plan["steps"][3], "when": plan["steps"][0]["when"]}]},
                      runtime.catalog())
    store.close()


def test_task_plan_runs_in_process_and_reports(tmp_path):
    agent, world = make_agent(tmp_path)
    plan, l1 = place_block()
    task = agent.submit(INSTRUCTION, plan=plan, l1_plans=l1)
    assert task == "t1"
    report = run(agent, world, task)
    assert report["metrics"]["success"], agent.report(task, text=True)
    subs = by_id(report)
    assert [s["status"] for s in report["subtasks"]] == ["SUCCEEDED"] * 3
    result = subs["t1-03-MAN-A"]["result"]
    assert result["success"] == 1 and result["goal_progress"] == [{"cond": "object_in(red_block, drawer_1)", "met": True}]
    assert result["error_ref"] is None and any(e.startswith("task:") for e in result["evidence"])
    assert report["metrics"]["executions"] == 6 and report["metrics"]["failed_executions"] == 0
    assert report["world"]["facts"]["red_block.location"]["value"] == [1.0, -1.0, 0.45]
    assert report["task"]["acceptance"]["achieved"] and report["task"]["acceptance"]["by"] == "rules"
    # Each runtime skill plan points back at its subtask.
    first = agent.store.get("tasks", subs["t1-01-MOV-A"]["runtime_tasks"][0]["id"])
    assert first["parent"] == {"agent_task_id": "t1", "subtask_id": "t1-01-MOV-A"}
    text = agent.report(task, text=True)
    assert "任务 t1：把红色方块放进抽屉" in text and "目标：达成 object_in(red_block, drawer_1)" in text
    assert "结果：成功" in text
    agent.close()


def test_failed_grasp_is_compensated_then_retried(tmp_path):
    agent, world = make_agent(tmp_path)
    world.pick_failures = 1
    plan, l1 = place_block()
    task = agent.submit(INSTRUCTION, plan=plan, l1_plans=l1)
    report = run(agent, world, task)
    assert report["metrics"]["success"]
    pick = by_id(report)["t1-02-MAN-A"]
    assert pick["attempts"] == 2 and len(pick["errors"]) == 1
    first = pick["runtime_tasks"][0]
    assert first["status"] == "failed" and first["compensation"]["status"] == "succeeded"
    assert first["compensation"]["steps"][0]["skill"] == "manip.retract"
    assert report["metrics"]["compensations"] == 1
    statuses = [e for e in agent.store.events("agent_task:t1") if e["type"] == "subtask_status"]
    path = [json.loads(e["data"])["to"] for e in statuses if json.loads(e["data"])["subtask"] == "t1-02-MAN-A"]
    # The Runtime compensates (lower-layer recovery), gives up, then the task manager retries the subtask.
    assert path == ["READY", "PLANNING", "RUNNING", "RECOVERING", "RUNNING", "RECOVERING", "PLANNING", "RUNNING",
                    "SUCCEEDED"]
    agent.close()


def test_failed_compensation_blocks_until_the_operator_decides(tmp_path):
    agent, world = make_agent(tmp_path)
    world.pick_failures, world.retract_fails = 1, True
    plan, l1 = place_block()
    task = agent.submit(INSTRUCTION, plan=plan, l1_plans=l1)
    report = run(agent, world, task)
    assert report["metrics"]["status"] == "needs_operator"
    pick = by_id(report)["t1-02-MAN-A"]
    assert pick["status"] == "BLOCKED" and pick["runtime_tasks"][0]["status"] == "needs_operator"
    # The operator clears the arm, then asks for the grasp to be tried again.
    world.retract_fails = False
    agent.resolve(task, "retry", "t1-02-MAN-A", note="arm cleared by hand")
    report = run(agent, world, task)
    assert report["metrics"]["success"] and report["metrics"]["operator_actions"] == 1
    agent.close()


def test_obtained_value_feeds_later_subtasks(tmp_path):
    agent, world = make_agent(tmp_path)
    plan = task_plan([
        subtask("t1-01-PER-Q", "找到红色方块", "location(red_block)", type="ObjectRef"),
        subtask("t1-02-MOV-A", "走到桌前", "robot_at(table_1)"),
        subtask("t1-03-MAN-A", "抓起找到的方块", "holding($t1-01-PER-Q.value.object)", ["t1-01-PER-Q", "t1-02-MOV-A"]),
    ], targets=("red_block",), intent="pick_up")
    task = agent.submit("把红色方块拿起来", plan={**plan, "instruction": "把红色方块拿起来"},
                        l1_plans={"t1-01-PER-Q": LOOK, "t1-02-MOV-A": WALK, "t1-03-MAN-A": PICK})
    report = run(agent, world, task)
    assert report["metrics"]["success"], agent.report(task, text=True)
    subs = by_id(report)
    found = subs["t1-01-PER-Q"]["result"]
    assert found["value"]["object"] == "red_block" and found["value"]["location"] == [2.0, 0.5, 0.8]
    assert found["produced"] == ["red_block"]
    assert subs["t1-03-MAN-A"]["resolved_goal"] == {"achieve": "holding(red_block)"}
    agent.close()


def test_a_check_answering_no_is_success(tmp_path):
    agent, world = make_agent(tmp_path)
    plan = task_plan([subtask("t1-01-PER-C", "看看抽屉开没开", "open(drawer_1)")], targets=("drawer_1",),
                     intent="question", instruction="抽屉开着吗？")
    task = agent.submit("抽屉开着吗？", plan=plan, l1_plans={"t1-01-PER-C": LOOK})
    report = run(agent, world, task)
    result = by_id(report)["t1-01-PER-C"]["result"]
    assert report["metrics"]["success"] and result["outcome"] == "SUCCEEDED" and result["value"] is False
    agent.close()


def test_already_satisfied_goal_is_not_executed(tmp_path):
    agent, world = make_agent(tmp_path)
    world.pose = [1.6, 0.5, 0.0]
    agent.observe_many(world.observations())
    plan, l1 = place_block()
    plan["subtasks"] = plan["subtasks"][:1]
    task = agent.submit(INSTRUCTION, plan=plan, l1_plans=l1)
    report = run(agent, world, task)
    walk = by_id(report)["t1-01-MOV-A"]
    assert report["metrics"]["success"] and walk["status"] == "SKIPPED" and not walk["runtime_tasks"]
    agent.close()


def test_missing_precondition_waits_for_a_revised_plan(tmp_path):
    agent, world = make_agent(tmp_path)
    grab = subtask("t1-01-MAN-A", "抓起红色方块", "holding(red_block)")
    plan = task_plan([grab], targets=("red_block",), intent="pick_up", instruction="把红色方块拿起来")
    task = agent.submit("把红色方块拿起来", plan=plan, l1_plans={"t1-01-MAN-A": PICK})
    report = run(agent, world, task)
    # The body is not standing: pick's precondition is false, so no skill is dispatched blindly.
    assert by_id(report)["t1-01-MAN-A"]["status"] == "MISSING_PREREQ"
    assert report["metrics"]["status"] == "needs_operator" and "no model" in report["task"]["error"]
    revised = task_plan([subtask("t1-02-SYS-A", "站起来", "standing()"),
                         {**subtask("t1-03-MAN-A", "抓起红色方块", "holding(red_block)", ["t1-02-SYS-A"]),
                          "replaces": "t1-01-MAN-A"}],
                        version=2, targets=("red_block",), intent="pick_up", instruction="把红色方块拿起来")
    stand = {"steps": [{"id": "stand", "skill": "robot.stand", "args": {}}], "verification": "stand"}
    agent.revise(task, revised, {"t1-02-SYS-A": stand, "t1-03-MAN-A": PICK})
    report = run(agent, world, task)
    subs = by_id(report)
    assert report["metrics"]["success"] and report["task"]["plan_version"] == 2
    assert subs["t1-01-MAN-A"]["status"] == "CANCELED" and not subs["t1-01-MAN-A"]["current"]
    assert subs["t1-03-MAN-A"]["replaces"] == "t1-01-MAN-A" and subs["t1-03-MAN-A"]["status"] == "SUCCEEDED"
    assert "替换 t1-01-MAN-A" in agent.report(task, text=True)
    agent.close()


def test_optional_subtask_failure_does_not_block(tmp_path):
    agent, world = make_agent(tmp_path)
    world.pick_failures = 5
    plan = task_plan([subtask("t1-01-MOV-A", "走到桌前", "robot_at(table_1)"),
                      subtask("t1-02-MAN-A", "顺手拿起方块", "holding(red_block)", ["t1-01-MOV-A"], required=False,
                              budget={"retries": 0})], targets=("red_block",), intent="go")
    task = agent.submit(INSTRUCTION, plan=plan, l1_plans={"t1-01-MOV-A": WALK, "t1-02-MAN-A": PICK})
    report = run(agent, world, task)
    pick = by_id(report)["t1-02-MAN-A"]
    assert report["metrics"]["success"] and pick["status"] == "FAILED"
    report_record = agent.store.get("error_reports", pick["result"]["error_ref"])
    assert report_record["subtask_id"] == "t1-02-MAN-A" and "grasp slipped" in report_record["error"]
    assert report["task"]["acceptance"]["optional_failed"] == ["t1-02-MAN-A"] and "可选" in report["task"]["reply"]
    agent.close()


def test_policy_rules_decide_before_anything_moves(tmp_path):
    agent, world = make_agent(tmp_path)
    plan, l1 = place_block()
    agent.set_policy({"rules": {"forbidden_places": ["drawer_1"]}})
    task = agent.submit(INSTRUCTION, plan=plan, l1_plans=l1)
    report = agent.report(task)
    assert report["task"]["status"] == "infeasible" and "禁止区域" in report["task"]["reply"]
    assert report["feasibility"]["rule_overrides"][0]["check"] == "constraints"
    assert not agent.store.list("tasks")
    agent.close()

    agent, world = make_agent(tmp_path / "battery")
    world.battery = 0.1
    agent.observe_many(world.observations())
    agent.set_policy({"rules": {"min_battery": 0.2}})
    task = agent.submit(INSTRUCTION, plan=plan, l1_plans=l1)
    assert agent.report(task)["feasibility"]["checks"]["resources"] == "fail"
    agent.close()

    # Manipulation needs the user's confirmation: the task asks, then runs once confirmed.
    agent, world = make_agent(tmp_path / "confirm")
    agent.set_policy({"rules": {"confirm_categories": ["MAN"]}})
    task = agent.submit(INSTRUCTION, plan=plan, l1_plans=l1)
    assert agent.tasks.get(task)["status"] == "awaiting_user" and agent.done(task)
    agent.say(task, "可以，开始吧", confirm=True)
    report = run(agent, world, task)
    assert report["metrics"]["success"] and report["task"]["plan_version"] == 2
    assert [t["role"] for t in report["dialogue"]] == ["user", "assistant", "user", "assistant", "assistant"]
    agent.close()


def test_skill_without_handler_is_never_dispatched(tmp_path):
    agent, world = make_agent(tmp_path)
    from robot_agent.skills import HANDLERS
    HANDLERS.pop("g1-01/robot.stand")
    plan, l1 = place_block()
    plan["subtasks"] = plan["subtasks"][:1]
    task = agent.submit("站起来走到桌前", plan={**plan, "instruction": "站起来走到桌前"}, l1_plans=l1)
    for _ in range(5):
        agent.observe_many(world.observations())
        agent.step()
    runtime_task = agent.tasks.get(task)["state"]["t1-01-MOV-A"]["runtime_tasks"][0]
    assert not agent.store.list("executions")
    assert "handler_unavailable" in [e["type"] for e in agent.store.events(runtime_task)]
    agent.close()
