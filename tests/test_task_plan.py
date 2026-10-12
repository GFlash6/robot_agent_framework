"""TaskPlan contract (L0 output), revision rules, subtask lifecycle, results and the goal language. L0 checks."""

import copy
import time

import pytest

from robot_agent import goals
from robot_agent.contracts import ContractError
from robot_agent.store import Store
from robot_agent.task_plan import (TRANSITIONS, capability_view, make_result, next_seq, transition,
                                   validate_revision, validate_task_plan)
from robot_agent.world_state import WorldState

ALL = {"MOV", "PER", "MAN", "INT", "MEM", "SYS"}
INSTRUCTION = "把厨房桌上的红杯子拿给我"


def example():
    """The design document's §8 example, written in the goal vocabulary."""
    return {
        "task_id": "t1", "plan_version": 1, "instruction": INSTRUCTION,
        "understanding": {"intent": "fetch_and_deliver", "targets": [{"ref": "red_cup", "desc": "红色杯子"}],
                          "recipient": {"ref": "user", "desc": "说话的用户，当前在客厅沙发"},
                          "constraints": ["轻拿轻放"], "ambiguities": []},
        "situation": "机器人在客厅充电座旁，手里没有东西，电量 80%。没有红色杯子的记忆记录。",
        "grounding": [{"ref": "red_cup", "status": "findable", "hint": "厨房餐桌"}],
        "feasibility": {"verdict": "feasible_with_assumptions",
                        "checks": {"capability": "pass", "objects": "pass", "constraints": "pass",
                                   "resources": "pass", "ambiguity": "pass"},
                        "assumptions": ["杯子在厨房餐桌上"]},
        "subtasks": [
            {"id": "t1-01-MOV-A", "category": "MOV", "kind": "A", "description": "前往厨房餐桌附近",
             "goal": {"achieve": "robot_at(kitchen_table)"}, "depends_on": [], "required": True},
            {"id": "t1-02-PER-Q", "category": "PER", "kind": "Q", "description": "找到红色杯子并确定位姿",
             "goal": {"obtain": "location(red_cup)", "type": "ObjectRef"}, "depends_on": ["t1-01-MOV-A"],
             "required": True},
            {"id": "t1-03-MAN-A", "category": "MAN", "kind": "A", "description": "抓起红色杯子",
             "goal": {"achieve": "holding($t1-02-PER-Q.value.object)"}, "depends_on": ["t1-02-PER-Q"],
             "constraints": ["轻拿轻放"], "required": True},
            {"id": "t1-04-INT-A", "category": "INT", "kind": "A", "description": "回到用户身边并递交",
             "goal": {"achieve": "handed_over($t1-02-PER-Q.value.object, user)"}, "depends_on": ["t1-03-MAN-A"],
             "constraints": ["轻拿轻放"], "required": True},
        ],
        "reply": "好的，我去厨房餐桌找红杯子，拿到后给你送过来。",
    }


def check(plan, categories=ALL, version=1):
    return validate_task_plan(plan, task_id="t1", plan_version=version, instruction=INSTRUCTION, categories=categories)


def edited(**changes):
    plan = example()
    for path, value in changes.items():
        target = plan
        keys = path.split("__")
        for key in keys[:-1]:
            target = target[int(key)] if isinstance(target, list) else target[key]
        last = keys[-1]
        target[int(last) if isinstance(target, list) else last] = value
    return plan


def test_design_example_is_a_valid_plan_with_defaults():
    plan = check(example())
    first = plan["subtasks"][0]
    assert first["budget"] == {"retries": 1, "timeout_s": 600} and first["constraints"] == [] and first["replaces"] is None
    assert next_seq([s["id"] for s in plan["subtasks"]]) == 5


@pytest.mark.parametrize("plan, message", [
    (edited(subtasks__0__id="t1-01-PER-A"), "prefix disagrees"),
    (edited(subtasks__1__id="t2-02-PER-Q"), "another task"),
    (edited(subtasks__2__goal={"achieve": "grab(red_cup)"}), "unknown goal atom"),
    (edited(subtasks__0__goal={"achieve": "location(kitchen_table)"}), "cannot be used in a kind A"),
    (edited(subtasks__1__goal={"obtain": "location(red_cup)", "type": "Value"}), "yields type ObjectRef"),
    (edited(subtasks__2__depends_on=["t1-01-MOV-A"]), "does not depend on"),
    (edited(subtasks__0__depends_on=["t1-04-INT-A"]), "cycle"),
    (edited(subtasks__0__depends_on=["t1-09-MOV-A"]), "unknown subtask"),
    (edited(instruction="别的指令"), "unchanged"),
    (edited(plan_version=2), "plan_version"),
    (edited(understanding__ambiguities=["哪个杯子？"]), "ambiguities"),
    (edited(feasibility__assumptions=[], feasibility__verdict="feasible_with_assumptions"), "needs assumptions"),
    (edited(feasibility__verdict="infeasible"), "failed check"),
    (edited(grounding=[]), "targets without grounding"),
    (edited(grounding=[{"ref": "red_cup", "status": "known"}]), "world id"),
    ({"error": "cannot"}, "declined"),
])
def test_invalid_plans_are_rejected(plan, message):
    with pytest.raises(ContractError, match=message):
        check(plan)


def test_rules_tie_verdicts_grounding_and_categories_together():
    # Rule 6: a category the robot lacks must be reported, not planned.
    with pytest.raises(ContractError, match="lacks"):
        check(example(), categories=ALL - {"INT"})
    # Rule 3: a findable object needs an obtain subtask before it is used.
    plan = example()
    plan["subtasks"] = [plan["subtasks"][0]]
    with pytest.raises(ContractError, match="obtain subtask"):
        check(plan)
    # Not executable: no subtasks, and the user is told why.
    blocked = edited(feasibility__verdict="needs_clarification", feasibility__checks__ambiguity="fail",
                     understanding__ambiguities=["给哪位用户？"])
    with pytest.raises(ContractError, match="no subtasks"):
        check(blocked)
    blocked["subtasks"], blocked["reply"] = [], ""
    with pytest.raises(ContractError, match="tell the user"):
        check(blocked)
    blocked["reply"] = "要送给哪位？"
    assert check(blocked)["subtasks"] == []


def states(**overrides):
    base = {"t1-01-MOV-A": "SUCCEEDED", "t1-02-PER-Q": "FAILED", "t1-03-MAN-A": "PENDING", "t1-04-INT-A": "PENDING"}
    return {**base, **overrides}


def revision(*subtasks):
    plan = example()
    plan["plan_version"] = 2
    plan["subtasks"] = list(subtasks)
    return check(plan, version=2)


def test_revision_keeps_succeeded_work_and_gives_changes_new_ids():
    previous = {s["id"]: s for s in check(example())["subtasks"]}
    original = copy.deepcopy(example()["subtasks"])
    search = {"id": "t1-05-PER-Q", "category": "PER", "kind": "Q", "description": "在整个厨房找红杯子",
              "goal": {"obtain": "location(red_cup)", "type": "ObjectRef"}, "depends_on": ["t1-01-MOV-A"],
              "replaces": "t1-02-PER-Q"}
    grab = {**original[2], "id": "t1-06-MAN-A", "goal": {"achieve": "holding($t1-05-PER-Q.value.object)"},
            "depends_on": ["t1-05-PER-Q"], "replaces": "t1-03-MAN-A"}
    hand = {**original[3], "id": "t1-07-INT-A", "goal": {"achieve": "handed_over($t1-05-PER-Q.value.object, user)"},
            "depends_on": ["t1-06-MAN-A"], "replaces": "t1-04-INT-A"}
    good = revision(original[0], search, grab, hand)
    assert validate_revision(good, previous, states()) is good
    with pytest.raises(ContractError, match="keep it in the plan"):
        validate_revision(revision(search | {"depends_on": []}, grab, hand), previous, states())
    with pytest.raises(ContractError, match="changed"):
        validate_revision(revision({**original[0], "description": "走到厨房"}, search, grab, hand), previous, states())
    with pytest.raises(ContractError, match="ended FAILED"):
        validate_revision(revision(*original), previous, states())
    with pytest.raises(ContractError, match="sequence numbers from 05"):
        validate_revision(revision(original[0], {**search, "id": "t1-03-PER-Q"},
                                   {**grab, "goal": {"achieve": "holding($t1-03-PER-Q.value.object)"},
                                    "depends_on": ["t1-03-PER-Q"]},
                                   {**hand, "goal": {"achieve": "handed_over($t1-03-PER-Q.value.object, user)"}}),
                          previous, states())
    with pytest.raises(ContractError, match="must be an earlier subtask left out"):
        validate_revision(revision(original[0], search, grab, {**hand, "replaces": "t1-01-MOV-A"}), previous, states())


def test_lifecycle_and_results():
    state = {"status": "PENDING"}
    for status in ("READY", "PLANNING", "RUNNING", "BLOCKED", "RUNNING", "MISSING_PREREQ", "READY", "PLANNING",
                   "RECOVERING", "PLANNING", "RUNNING", "SUCCEEDED"):
        transition(state, status)
    with pytest.raises(ContractError, match="illegal"):
        transition(state, "RUNNING")
    assert all(not TRANSITIONS[t] for t in ("SUCCEEDED", "PARTIAL", "FAILED", "SKIPPED", "CANCELED"))
    # A check that answers "no" is a success; its answer is the value.
    answered = make_result("t1-05-PER-C", "SUCCEEDED", goal_progress=[{"cond": "open(door_1)", "met": True}],
                           value=False)
    assert answered["success"] == 1 and answered["value"] is False
    with pytest.raises(ContractError, match="true or false"):
        make_result("t1-05-PER-C", "SUCCEEDED", goal_progress=[], value=None)
    with pytest.raises(ContractError, match="needs a value"):
        make_result("t1-02-PER-Q", "SUCCEEDED", goal_progress=[])
    partial = make_result("t1-03-MAN-A", "PARTIAL", goal_progress=[{"cond": "a", "met": True}, {"cond": "b", "met": False}])
    assert partial["success"] == 0 and partial["outcome"] == "PARTIAL"


def test_goal_atoms_resolve_compile_and_check_against_the_world(tmp_path):
    store = Store(tmp_path / "ledger")
    world = WorldState(store, "r1")
    now = time.time_ns()

    def observe(key, value, **extra):
        world.observe(key, value, source={"kind": "observation", "ref": "camera"}, stamp_ns=now,
                      evidence_level="software_integration", **extra)

    observe("kitchen_table.approach", [1.0, 1.0, 0.0], frame_id="map")
    observe("robot.pose", [1.1, 1.0, 0.0], frame_id="map")
    observe("cup_7.location", [1.2, 1.1, 0.8], frame_id="map")
    observe("door_1.open", False)
    results = {"t1-02-PER-Q": make_result("t1-02-PER-Q", "SUCCEEDED", goal_progress=[],
                                          value={"object": "cup_7", "seen_at": 1.0})}
    goal = goals.resolve({"achieve": ["holding($t1-02-PER-Q.value.object)", "robot_at(kitchen_table)"]}, results)
    assert goal == {"achieve": ["holding(cup_7)", "robot_at(kitchen_table)"]}
    assert goals.refs({"achieve": "holding($t1-02-PER-Q.value.object)"}) == {"t1-02-PER-Q"}
    with pytest.raises(ContractError, match="no successful result"):
        goals.resolve("holding($t1-09-PER-Q.value.object)", results)
    # holding is unknown (never observed), robot_at is true: not yet met, never guessed.
    verdict = goals.check(world, goal, "A")
    assert verdict["state"] == "unknown" and verdict["progress"][1] == {"cond": "robot_at(kitchen_table)", "met": True}
    observe("robot.holding", "none")
    assert goals.check(world, goal, "A")["state"] == "unmet"
    assert goals.check(world, {"check": "open(door_1)"}, "C") == {
        "state": "met", "progress": [{"cond": "open(door_1)", "met": True}], "value": False}
    found = goals.check(world, {"obtain": "location(red_cup)", "type": "ObjectRef"}, "Q", subject="cup_7")
    assert found["state"] == "met" and found["value"]["object"] == "cup_7" and found["value"]["frame"] == "map"
    assert goals.check(world, {"obtain": "location(red_cup)"}, "Q")["state"] == "unknown"
    assert goals.predicate('fact_is(door_1.label, "门 1")') == {"fact": "door_1.label", "op": "eq", "value": "门 1"}
    store.close()


def test_capability_view_lists_categories_without_parameters():
    catalog = {"base.navigate_to": {"category": "MOV", "summary": "Navigate.", "input_schema": {"type": "object"}},
               "file.ingest": {"input_schema": {}}, "custom.thing": {}}
    view = capability_view(catalog, [{"name": "fetch", "chain": ["base.navigate_to"]}])
    assert view == {"categories": {"MOV": ["base.navigate_to: Navigate."], "MEM": ["file.ingest"]},
                    "paradigms": [{"name": "fetch", "chain": ["base.navigate_to"]}]}
