"""World state, skill pre/postconditions, monitoring, feedback loops and TaskPlan execution.

Executions are real local file operations or the real HTTP skill service. World
facts are written through the operator observation path; no robot or model
responses are simulated. These are L0 (software_integration) checks.
"""

import json
import threading
import time
from pathlib import Path

import pytest

from robot_agent.contracts import ContractError
from robot_agent.embodiment import install
from robot_agent.monitor import assess
from robot_agent.runtime import Runtime
from robot_agent.store import Store
from robot_agent.task_manager import TaskManager
from robot_agent.world_state import WorldState, evaluate, validate_predicate

PROFILES = Path(__file__).resolve().parents[1] / "profiles"
OPERATOR = {"kind": "operator", "ref": "test-operator"}


def observe(world, key, value, **extra):
    return world.observe(key, value, source=extra.pop("source", OPERATOR), stamp_ns=extra.pop("stamp_ns", time.time_ns()),
                         evidence_level="software_integration", **extra)


def setup(tmp_path):
    store = Store(tmp_path / "ledger")
    runtime = Runtime(store)
    runtime.install_local_skills([str(tmp_path)])
    return store, runtime


def ingest_step(step_id, path, deps=(), **extra):
    return {"id": step_id, "skill": "file.ingest", "deps": list(deps),
            "args": {"path": str(path), "metadata": {"kind": "document", "encoding": "utf8", "source": "test"}}, **extra}


def verify_step(step_id, origin):
    return {"id": step_id, "skill": "asset.verify", "deps": [origin], "args": {"asset_id": {"$ref": origin + ".asset_id"}}}


def finish(runtime, task_id, until=("succeeded", "failed", "canceled", "paused"), rounds=40):
    for _ in range(rounds):
        task = runtime.tick(task_id)
        if task["status"] in until:
            return task
    raise AssertionError(f"task stuck in {task['status']}")


# ---------------------------------------------------------------- world state


def test_world_facts_carry_validity_and_keep_conflicting_sources(tmp_path):
    store = Store(tmp_path / "ledger")
    now = [time.time_ns()]
    world = WorldState(store, "r1", clock=lambda: now[0])
    observe(world, "robot.pose", [0, 0, 0], stamp_ns=now[0], ttl_s=1.0, frame_id="map", map_version="m1")
    assert world.get("robot.pose")["validity"] == "valid"
    now[0] += 2_000_000_000
    assert world.get("robot.pose")["validity"] == "stale"
    assert world.get("robot.battery") is None
    # A late sample never replaces a newer one; a future sample means clocks disagree.
    observe(world, "robot.pose", [1, 0, 0], stamp_ns=now[0], frame_id="map", map_version="m1")
    assert observe(world, "robot.pose", [9, 9, 9], stamp_ns=now[0] - 10) is None
    with pytest.raises(ContractError):
        observe(world, "robot.pose", [2, 0, 0], stamp_ns=now[0] + 10_000_000_000)
    observe(world, "robot.pose", [1.5, 0, 0], source={"kind": "observation", "ref": "lidar"}, stamp_ns=now[0] + 1,
            frame_id="map", map_version="m1")
    kinds = [e["type"] for e in store.events("world:r1")]
    assert {"late_observation_ignored", "observation_rejected", "observation_conflict"} <= set(kinds)
    # Relocalization: spatial facts from the old map need re-observation.
    observe(world, "door.open", True)
    assert world.set_map_version("m2") == ["robot.pose"]
    assert world.get("robot.pose")["validity"] == "invalid"
    assert world.get("door.open")["validity"] == "valid"
    store.close()


def test_predicates_are_three_valued_and_never_guess(tmp_path):
    store = Store(tmp_path / "ledger")
    world = WorldState(store, "r1")
    observe(world, "robot.pose", [1.0, 2.0, 0.0], frame_id="map")
    observe(world, "robot.holding", "none")
    observe(world, "cup.location", [0.5, 0.5, 0.8], frame_id="odom")
    near = {"fact": "robot.pose", "op": "near", "value": {"$arg": "target"}, "tolerance": 0.2, "frame": "map"}
    assert evaluate(world, near, {"target": [1.1, 2.0, 0.0]})["result"] == "true"
    assert evaluate(world, near, {"target": [3.0, 2.0, 0.0]})["result"] == "false"
    # Wrong frame or missing fact is unknown, not false; "exists" on a missing fact is false.
    assert evaluate(world, {"fact": "{object}.location", "op": "exists", "frame": "map"}, {"object": "cup"})["result"] == "unknown"
    assert evaluate(world, {"fact": "robot.estop", "op": "eq", "value": False})["result"] == "unknown"
    assert evaluate(world, {"fact": "plate.location", "op": "exists"})["result"] == "false"
    both = {"all": [{"fact": "robot.holding", "op": "eq", "value": "none"},
                    {"fact": "robot.estop", "op": "eq", "value": False}]}
    assert evaluate(world, both)["result"] == "unknown"
    assert evaluate(world, {"any": [both, {"fact": "robot.holding", "op": "eq", "value": "none"}]})["result"] == "true"
    assert evaluate(world, {"not": {"fact": "robot.holding", "op": "eq", "value": "cup"}})["result"] == "true"
    # Expected (unconfirmed) effects never satisfy a predicate.
    world.expect("robot.holding", "cup", execution_id="e1", evidence_level="software_integration")
    assert evaluate(world, {"fact": "robot.holding", "op": "eq", "value": "cup"})["result"] == "unknown"
    with pytest.raises(ContractError):
        validate_predicate({"fact": "robot.pose", "op": "near", "value": [0, 0]})
    with pytest.raises(ContractError):
        evaluate(world, {"fact": "{object}.location", "op": "exists"}, {"object": "../x"})
    store.close()


# ---------------------------------------------------------------- preconditions, effects, monitoring


def guarded_ingest(runtime, **extra):
    spec = dict(runtime.catalog()["file.ingest"])
    spec.pop("name")
    spec.update(extra)
    runtime.register("file.ingest", spec)


def test_preconditions_gate_dispatch_on_world_state(tmp_path):
    store, runtime = setup(tmp_path)
    source = tmp_path / "doc.txt"
    source.write_text("real document")
    guarded_ingest(runtime, preconditions={"fact": "storage.mounted", "op": "eq", "value": True}, precondition_timeout=30)
    world = WorldState(store, "r1")
    task = runtime.submit({"steps": [ingest_step("save", source), verify_step("check", "save")], "verification": "check"}, "r1")
    # Unknown: wait without dispatching anything.
    assert runtime.tick(task["id"])["status"] == "running"
    assert not store.list("executions")
    assert [e["type"] for e in store.events(task["id"])].count("precondition_waiting") == 1
    observe(world, "storage.mounted", True)
    assert finish(runtime, task["id"])["status"] == "succeeded"
    # False: fail before any side effect.
    observe(world, "storage.mounted", False)
    executions = len(store.list("executions"))
    task = runtime.submit({"steps": [ingest_step("save", source), verify_step("check", "save")], "verification": "check"}, "r1")
    result = finish(runtime, task["id"])
    assert result["status"] == "failed"
    assert "precondition false" in result["steps"]["save"]["error"]
    assert len(store.list("executions")) == executions
    store.close()


def test_effects_need_a_later_independent_observation(tmp_path):
    store, runtime = setup(tmp_path)
    source = tmp_path / "doc.txt"
    source.write_text("real document")
    guarded_ingest(runtime, effects=[{"fact": "doc.archived", "value": True}], effect_timeout=0.5)
    world = WorldState(store, "r1")
    observe(world, "doc.archived", True)  # true before the action: must not count
    plan = {"steps": [ingest_step("save", source)], "verification": "save"}
    plan["steps"][0]["skill"] = "file.ingest"
    runtime.register("file.ingest", {**{k: v for k, v in runtime.catalog()["file.ingest"].items() if k != "name"},
                                     "verifier": True})
    task = runtime.submit(plan, "r1")
    state = runtime.tick(task["id"])
    assert state["steps"]["save"]["status"] == "verifying"
    assert world.get("doc.archived")["status"] == "expected"
    assert runtime.tick(task["id"])["steps"]["save"]["status"] == "verifying"
    observe(world, "doc.archived", True)
    assert finish(runtime, task["id"])["status"] == "succeeded"
    assert "effects_confirmed" in [e["type"] for e in store.events(task["id"])]
    # Contradiction fails the step; silence fails it after effect_timeout.
    second = runtime.submit(plan, "r1")
    runtime.tick(second["id"])
    observe(world, "doc.archived", False)
    failed = finish(runtime, second["id"])
    assert failed["status"] == "failed" and "contradicted" in failed["steps"]["save"]["error"]
    third = runtime.submit(plan, "r1")
    runtime.tick(third["id"])
    time.sleep(0.6)
    timed_out = finish(runtime, third["id"])
    assert timed_out["status"] == "failed" and "not observed in time" in timed_out["steps"]["save"]["error"]
    store.close()


def test_invariant_violation_cancels_real_http_execution_and_fails_step(tmp_path):
    from robot_agent.service import skill_server

    source = tmp_path / "source"
    source.write_bytes(b"x" * 4096)
    service_store = Store(tmp_path / "service")
    Runtime(service_store).install_local_skills([str(tmp_path)])
    server = skill_server(service_store, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    store = Store(tmp_path / "client")
    runtime = Runtime(store)
    spec = {k: v for k, v in Runtime(service_store).catalog()["file.copy"].items() if k != "name"}
    runtime.register("file.copy", {**spec, "adapter": "http", "endpoint": f"http://127.0.0.1:{server.server_port}",
                                   "resources": {"disk": 1}, "invariants": {"fact": "robot.estop", "op": "eq", "value": False}})
    store.set_capacity("r1/disk", 1)
    world = WorldState(store, "r1")
    observe(world, "robot.estop", False)
    try:
        task = runtime.submit({"steps": [{"id": "copy", "skill": "file.copy", "args": {
            "source": str(source), "target": str(tmp_path / "copy"), "chunk_bytes": 256}}], "verification": "copy"}, "r1")
        state = runtime.tick(task["id"])
        assert state["status"] == "running"
        execution = store.get("executions", state["steps"]["copy"]["execution_id"])
        assert 0 < execution["monitor"]["progress"] < 1  # real feedback from the copy service
        observe(world, "robot.estop", True)
        runtime.tick(task["id"])  # monitor finding
        result = finish(runtime, task["id"])  # cancel, then fail
        assert result["status"] == "failed"
        assert result["steps"]["copy"]["result"]["reported_status"] == "canceled"
        assert "invariant_violated" in result["steps"]["copy"]["result"]["error"]
        assert not store.leases()
        assert 0 < (tmp_path / "copy").stat().st_size < 4096
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        service_store.close()
        store.close()


def test_stall_detection_uses_progress_not_elapsed_time(tmp_path):
    store = Store(tmp_path / "ledger")
    world = WorldState(store, "r1")
    spec = {"monitor": {"stall_s": 5, "min_progress_delta": 0.01}}
    execution = {"created_at": 100.0, "args": {}}
    assert assess(spec, execution, {"feedback": {"progress": 0.1}}, world, now=104.0) is None
    assert assess(spec, execution, {"feedback": {"progress": 0.105}}, world, now=108.0) is None  # below delta
    finding = assess(spec, execution, {"feedback": {"progress": 0.105}}, world, now=109.5)
    assert finding["kind"] == "stalled"
    store.close()


# ---------------------------------------------------------------- feedback loops


def test_repair_keeps_verified_progress_and_restarts_only_the_failed_part(tmp_path):
    store, runtime = setup(tmp_path)
    good_a, good_b = tmp_path / "a.txt", tmp_path / "b.txt"
    good_a.write_text("first")
    good_b.write_text("second")
    plan = {"steps": [ingest_step("ingest_a", good_a), verify_step("verify_a", "ingest_a"),
                      ingest_step("ingest_b", tmp_path / "missing.txt", deps=["verify_a"]), verify_step("verify_b", "ingest_b")],
            "verification": "verify_b"}
    task = runtime.submit(plan, "r1", goal="archive a and b", agent_context={"max_repairs": 1, "max_replans": 1,
                                                                          "repairs": 0, "replans": 0})
    failed = finish(runtime, task["id"], until=("repairing", "succeeded", "failed"))
    assert failed["status"] == "repairing"  # middle loop before outer loop
    executions_before = len(store.list("executions"))
    repaired = dict(plan, steps=plan["steps"][:2] + [ingest_step("ingest_b2", good_b, deps=["verify_a"]),
                                                     verify_step("verify_b2", "ingest_b2")], verification="verify_b2")
    task = runtime.repair(task["id"], repaired)
    assert task["steps"]["ingest_a"]["status"] == "succeeded" and task["steps"]["verify_a"]["status"] == "succeeded"
    event = next(e for e in store.events(task["id"]) if e["type"] == "plan_repaired")
    assert json.loads(event["data"])["kept"] == ["ingest_a", "verify_a"]
    assert finish(runtime, task["id"])["status"] == "succeeded"
    assert len(store.list("executions")) == executions_before + 2  # only the repaired part ran
    store.close()


def test_repair_rejects_unguarded_irreversible_steps_and_drops_contradicted_progress(tmp_path):
    store, runtime = setup(tmp_path)
    source = tmp_path / "a.txt"
    source.write_text("first")
    guarded_ingest(runtime, effects=[{"fact": "doc.archived", "value": True}], confirm_effects=False)
    world = WorldState(store, "r1")
    plan = {"steps": [ingest_step("ingest", source), ingest_step("broken", tmp_path / "missing", deps=["ingest"]),
                      verify_step("verify", "broken")], "verification": "verify"}
    task = runtime.submit(plan, "r1")
    assert finish(runtime, task["id"])["status"] == "failed"
    copy_plan = {"steps": [plan["steps"][0], {"id": "copy", "skill": "file.copy", "deps": ["ingest"],
                 "args": {"source": str(source), "target": str(tmp_path / "out")}}], "verification": "copy"}
    with pytest.raises(ContractError, match="replay-safe skills or world-state preconditions"):
        runtime.repair(task["id"], copy_plan)
    # The world no longer shows the effect of the earlier step, so it is not kept.
    observe(world, "doc.archived", False)
    fixed = {"steps": [plan["steps"][0], verify_step("verify2", "ingest")], "verification": "verify2"}
    task = runtime.repair(task["id"], fixed)
    assert task["steps"]["ingest"]["status"] == "pending"
    store.close()


def test_rejected_repair_escalates_to_the_outer_loop(tmp_path, monkeypatch):
    monkeypatch.delenv("RAF_TEST_ABSENT_TOKEN", raising=False)
    store, runtime = setup(tmp_path)
    plan = {"steps": [ingest_step("save", tmp_path / "missing.txt"), verify_step("check", "save")], "verification": "check"}
    config = {"endpoint": "http://127.0.0.1:1/v1", "model": "unavailable", "token_env": "RAF_TEST_ABSENT_TOKEN"}
    task = runtime.submit(plan, "r1", goal="archive", agent_context={
        "model_config": config, "max_repairs": 1, "repairs": 0, "max_replans": 1, "replans": 0})
    assert finish(runtime, task["id"], until=("repairing",))["status"] == "repairing"
    after_repair = runtime.recover_plan(task["id"])
    assert after_repair["status"] == "replanning" and after_repair["repairs"] == 1
    after_replan = runtime.recover_plan(task["id"])
    assert after_replan["status"] == "failed" and after_replan["replans"] == 1
    kinds = [e["type"] for e in store.events(task["id"])]
    assert kinds.index("repair_rejected") < kinds.index("replan_rejected")
    store.close()


# ---------------------------------------------------------------- task manager (TaskPlan execution)

ARCHIVE = "archive both documents"


def archive_plan(tmp_path):
    """Operator TaskPlan over local file skills, with pre-authored skill plans per subtask."""
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("first")
    b.write_text("second")
    checks = {k: "pass" for k in ("capability", "objects", "constraints", "resources", "ambiguity")}
    plan = {"task_id": "t1", "plan_version": 1, "instruction": ARCHIVE,
            "understanding": {"intent": "archive", "targets": [], "recipient": None, "constraints": [],
                              "ambiguities": []},
            "situation": "two documents on disk", "grounding": [],
            "feasibility": {"verdict": "feasible", "checks": checks, "assumptions": []},
            "subtasks": [
                {"id": "t1-01-MEM-A", "category": "MEM", "kind": "A", "description": "archive document a",
                 "goal": {"achieve": "fact_is(doc_a.archived, true)"}},
                {"id": "t1-02-MEM-A", "category": "MEM", "kind": "A", "description": "archive document b",
                 "goal": {"achieve": "fact_is(doc_b.archived, true)"}, "depends_on": ["t1-01-MEM-A"]},
            ], "reply": ""}
    l1 = {"t1-01-MEM-A": {"steps": [ingest_step("save", a), verify_step("check", "save")], "verification": "check"},
          "t1-02-MEM-A": {"steps": [ingest_step("save", b), verify_step("check", "save")], "verification": "check"}}
    return plan, l1


def test_subtasks_expand_lazily_and_are_verified_against_the_world(tmp_path):
    store, runtime = setup(tmp_path)
    tasks = TaskManager(store, runtime)
    world = WorldState(store, "r1")
    plan, l1 = archive_plan(tmp_path)
    task = tasks.create(ARCHIVE, "r1", plan=plan, l1_plans=l1, verify_timeout_s=60)
    task = tasks.advance(task["id"])
    a = task["state"]["t1-01-MEM-A"]
    assert a["status"] == "RUNNING" and task["state"]["t1-02-MEM-A"]["status"] == "PENDING"
    assert len(store.list("tasks")) == 1  # b is not orchestrated before a is done
    assert store.get("tasks", a["runtime_tasks"][0])["parent"] == {"agent_task_id": "t1", "subtask_id": "t1-01-MEM-A"}
    finish(runtime, a["runtime_tasks"][0])
    task = tasks.advance(task["id"])
    assert task["state"]["t1-01-MEM-A"]["status"] == "RUNNING"  # skill success is not subtask success
    observe(world, "doc_a.archived", True)
    observe(world, "doc_b.archived", True)  # already true: b needs no execution
    task = tasks.advance(task["id"])
    assert task["state"]["t1-01-MEM-A"]["status"] == "SUCCEEDED"
    assert task["state"]["t1-02-MEM-A"]["status"] == "SKIPPED"
    assert task["status"] == "succeeded" and task["acceptance"]["achieved"]
    assert len(store.list("tasks")) == 1
    store.close()


def test_subtask_retries_then_the_task_waits_for_the_operator(tmp_path):
    store, runtime = setup(tmp_path)
    tasks = TaskManager(store, runtime)
    world = WorldState(store, "r1")
    plan, l1 = archive_plan(tmp_path)
    task = tasks.create(ARCHIVE, "r1", plan=plan, l1_plans=l1)
    for attempt in (1, 2):
        task = tasks.advance(task["id"])
        finish(runtime, task["state"]["t1-01-MEM-A"]["runtime_tasks"][-1])
        observe(world, "doc_a.archived", False)  # the world contradicts the reported success
        task = tasks.advance(task["id"])
    state = task["state"]["t1-01-MEM-A"]
    assert state["status"] == "FAILED" and len(state["runtime_tasks"]) == 2
    assert state["result"]["goal_progress"] == [{"cond": "fact_is(doc_a.archived, true)", "met": False}]
    assert store.get("error_reports", state["result"]["error_ref"])["attempts"] == 2
    assert task["status"] == "needs_operator"  # a required subtask failed and there is no model to re-plan
    store.close()


def test_cancel_stops_skill_plans_first(tmp_path):
    store, runtime = setup(tmp_path)
    tasks = TaskManager(store, runtime)
    plan, l1 = archive_plan(tmp_path)
    task = tasks.advance(tasks.create(ARCHIVE, "r1", plan=plan, l1_plans=l1)["id"])
    tasks.cancel(task["id"])
    task = tasks.advance(task["id"])
    child = task["state"]["t1-01-MEM-A"]["runtime_tasks"][0]
    assert store.get("controls", child)["mode"] == "cancel" and task["status"] != "canceled"
    finish(runtime, child)
    task = tasks.advance(task["id"])
    assert task["status"] == "canceled"
    assert {s["status"] for s in task["state"].values()} == {"CANCELED"}
    store.close()


# ---------------------------------------------------------------- embodiment


def load(name):
    return json.loads((PROFILES / name).read_text(encoding="utf-8"))


def test_profiles_bind_abstract_skills_with_limits_and_evidence_level(tmp_path):
    store = Store(tmp_path / "g1")
    runtime = Runtime(store)
    g1 = load("unitree_g1.json")
    sim = load("bindings/unitree_g1.simulation.example.json")
    record = install(runtime, g1, sim, "g1-01")
    catalog = runtime.catalog()
    assert record["evidence_level"] == "simulation" and "manip.pick" in catalog
    move = catalog["base.move_velocity"]["input_schema"]["properties"]
    assert move["vx"]["maximum"] == 0.5 and move["vx"]["minimum"] == -0.5 and move["wz"]["maximum"] == 0.8
    assert catalog["base.navigate_to"]["resources"] == {"legs": 1, "waist": 1, "locomotion": 1}
    # Out-of-limit commands are rejected at dispatch, before any adapter call.
    task = runtime.submit({"steps": [{"id": "go", "skill": "base.move_velocity",
                                      "args": {"vx": 2.0, "vy": 0, "wz": 0, "duration_s": 1}}], "verification": "go"}, "g1-01")
    assert finish(runtime, task["id"])["status"] == "failed"
    assert not store.list("executions")
    # Switching to the hardware track removes skills the hardware binding does not cover.
    hardware = load("bindings/unitree_g1.hardware.example.json")
    install(runtime, g1, hardware, "g1-01")
    catalog = runtime.catalog()
    assert "manip.pick" not in catalog
    assert catalog["robot.stand"]["fencing_domain"] == "locomotion"
    assert catalog["robot.stand"]["evidence_level"] == "hardware_observation"
    with pytest.raises(ContractError, match="one ledger per robot"):
        install(runtime, g1, sim, "g1-02")
    store.close()


def test_preconditions_protect_a_body_without_any_adapter_call(tmp_path):
    store = Store(tmp_path / "g1")
    runtime = Runtime(store)
    binding = load("bindings/unitree_g1.simulation.example.json")
    binding["endpoint"] = "http://127.0.0.1:1"  # nothing listens: any dispatch would surface as unknown
    install(runtime, load("unitree_g1.json"), binding, "g1-01")
    world = WorldState(store, "g1-01")
    task = runtime.submit({"steps": [{"id": "stand", "skill": "robot.stand", "args": {}}], "verification": "stand"}, "g1-01")
    assert runtime.tick(task["id"])["status"] == "running"  # estop unknown: wait
    observe(world, "robot.estop", True)
    result = finish(runtime, task["id"])
    assert result["status"] == "failed" and "robot.estop" in result["steps"]["stand"]["error"]
    assert not store.list("executions")
    store.close()


def test_second_body_gets_only_its_own_capabilities(tmp_path):
    store = Store(tmp_path / "base")
    runtime = Runtime(store)
    wheeled = load("wheeled_base.json")
    binding = {"schema_version": 1, "embodiment": "wheeled_base", "track": "simulation", "provider": "http",
               "endpoint": "http://127.0.0.1:8771", "skills": {name: {} for name in wheeled["skills"]}}
    install(runtime, wheeled, binding, "base-01")
    catalog = runtime.catalog()
    assert "robot.stand" not in catalog and "manip.pick" not in catalog
    assert catalog["base.move_velocity"]["input_schema"]["properties"]["vx"]["maximum"] == 0.8
    with pytest.raises(ContractError, match="unknown skill"):
        runtime.submit({"steps": [{"id": "pick", "skill": "manip.pick", "args": {"object": "cup"}}],
                        "verification": "pick"}, "base-01")
    store.close()


def test_planner_sees_world_contracts_and_world_state(tmp_path):
    from robot_agent.context_builder import ContextBuilder
    from robot_agent.context_models import ContextRequest, GoalContext

    store = Store(tmp_path / "g1")
    runtime = Runtime(store)
    install(runtime, load("unitree_g1.json"), load("bindings/unitree_g1.simulation.example.json"), "g1-01")
    observe(WorldState(store, "g1-01"), "robot.estop", False, ttl_s=5)
    request = ContextRequest("ctx", "subplanning", GoalContext.from_input("stand up"), robot_id="g1-01")
    fragments = {f.id: f for f in ContextBuilder(store).build(request, runtime.catalog()).fragments}
    assert fragments["capabilities"].content["manip.pick"]["preconditions"]["all"][1]["fact"] == "robot.holding"
    assert fragments["capabilities"].content["robot.stand"]["effects"] == [{"fact": "robot.mobility_ready", "value": True}]
    world = fragments["world-state"]
    assert world.required and world.content["facts"]["robot.estop"]["validity"] == "valid"
    store.close()
