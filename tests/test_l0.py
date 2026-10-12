"""L0 call path: context assembly, evidence records, rule checks, clarification, re-planning, acceptance.

The model endpoint is a local OpenAI-compatible fixture that returns scripted
TaskPlans, so these are protocol and contract checks (L0 evidence). They prove
nothing about the quality of a real model's plans; see test_l0_live.py.
"""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from test_agent_driver import ALL_PASS, INSTRUCTION, PICK, PLACE, WALK, make_agent, run, subtask, task_plan


class ScriptedModel:
    """Answers each chat-completions request with the next scripted JSON object, and keeps the requests."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []
        model = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                model.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                content = json.dumps(model.replies.pop(0), ensure_ascii=False)
                body = json.dumps({"id": f"resp-{len(model.requests)}", "model": "fixture-model",
                                   "choices": [{"finish_reason": "stop",
                                                "message": {"content": content, "refusal": None}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = {"endpoint": f"http://127.0.0.1:{self.server.server_port}/v1/chat/completions",
                       "model": "fixture-model", "timeout": 10}

    def arguments(self, index):
        return json.loads(self.requests[index]["messages"][-1]["content"])

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def model():
    servers = []

    def start(*replies):
        servers.append(ScriptedModel(*replies))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


def place_plan(version=1, subtasks=None):
    plan = task_plan(subtasks or [
        subtask("t1-01-MOV-A", "走到桌前", "robot_at(table_1)"),
        subtask("t1-02-MAN-A", "抓起红色方块", "holding(red_block)", ["t1-01-MOV-A"], budget={"retries": 0}),
        subtask("t1-03-MAN-A", "把方块放进抽屉", "object_in(red_block, drawer_1)", ["t1-02-MAN-A"]),
    ], version=version)
    return plan


L1 = {"t1-01-MOV-A": WALK, "t1-02-MAN-A": PICK, "t1-03-MAN-A": PLACE}
ACCEPTED = {"achieved": True, "unmet": [], "reply": "方块已经放进抽屉了。"}


def records(agent):
    return sorted(agent.store.list("model_responses"), key=lambda r: r["created_at"])


def test_l0_plan_is_built_from_summarized_inputs_recorded_and_adopted(tmp_path, model):
    server = model(place_plan(), ACCEPTED)
    agent, world = make_agent(tmp_path, server.config)
    agent.set_policy({"rules": {"notes": ["不要进卧室"]},
                      "paradigms": [{"name": "fetch", "chain": ["perception.locate_object", "manip.pick"]}],
                      "map": {"summary": "一个房间：桌子 table_1 和抽屉 drawer_1"}})
    task = agent.submit(INSTRUCTION, l1_plans=L1)
    report = run(agent, world, task)
    assert report["metrics"]["success"], agent.report(task, text=True)
    assert report["task"]["acceptance"]["by"] == "model" and report["task"]["reply"] == ACCEPTED["reply"]
    assert [(r["method"], r["status"]) for r in records(agent)] == [("task_planning", "validated"),
                                                                    ("task_acceptance", "validated")]
    first = records(agent)[0]
    manifest = agent.store.get("context_manifests", first["context_manifest_id"])
    assert {"rules", "capabilities", "robot-state", "map", "known-objects"} <= set(manifest["included"])
    bundle = {f["id"]: f["content"] for f in agent.store.get("context_bundles", first["context_bundle_id"])["fragments"]}
    # Capabilities are categories and skill names, never parameter schemas.
    assert set(bundle["capabilities"]["categories"]) == {"SYS", "MOV", "PER", "MAN"}
    assert "input_schema" not in json.dumps(bundle["capabilities"])
    assert bundle["robot-state"]["summary"] == "机器人位于 [0.0, 0.0, 0.0]，手里没有东西，电量 80%。"
    assert {o["id"]: o["place"] for o in bundle["known-objects"]["objects"]}["red_block"] == "table_1"
    assert bundle["rules"]["rule_findings"]["capability"]["missing_categories"] == ["INT", "MEM"]
    assert bundle["map"]["summary"].startswith("一个房间") and "drawer_1" in bundle["map"]["places"]
    # Agent policy renders with system authority; the request carries the goal vocabulary and id rule inputs.
    messages = server.requests[0]["messages"]
    assert messages[0]["role"] == "system" and messages[0]["content"].startswith("You are L0")
    assert any(m["role"] == "system" and "rule_findings" in m["content"] for m in messages[1:])
    arguments = server.arguments(0)
    assert arguments["mode"] == "plan" and arguments["next_seq"] == 1 and "robot_at" in arguments["goal_atoms"]
    assert agent.store.get("task_plans", "t1@1")["model_response_id"] == first["id"]
    agent.close()


def test_rejected_candidates_are_audited_then_the_operator_is_asked(tmp_path, model):
    unknown_atom = place_plan()
    unknown_atom["subtasks"][1]["goal"] = {"achieve": "grab(red_block)"}
    invented_id = place_plan()
    invented_id["grounding"][0]["id"] = "cup_99"
    server = model(unknown_atom, invented_id)
    agent, world = make_agent(tmp_path, server.config)
    task = agent.submit(INSTRUCTION, l1_plans=L1)
    report = run(agent, world, task)
    assert report["task"]["status"] == "needs_operator" and "world state does not know" in report["task"]["error"]
    rejected = records(agent)
    assert [r["status"] for r in rejected] == ["rejected", "rejected"]
    assert "unknown goal atom" in rejected[0]["error"] and rejected[0]["raw"]  # the actual response is kept
    assert not agent.store.list("tasks")
    agent.close()


def test_rules_make_a_feasible_model_verdict_stricter(tmp_path, model):
    server = model(place_plan())
    agent, world = make_agent(tmp_path, server.config)
    agent.set_policy({"rules": {"forbidden_places": ["drawer_1"]}})
    task = agent.submit(INSTRUCTION, l1_plans=L1)
    report = run(agent, world, task)
    feasibility = report["feasibility"]
    assert report["task"]["status"] == "infeasible" and feasibility["verdict"] == "infeasible"
    assert feasibility["rule_overrides"] == [{"check": "constraints", "verdict": "infeasible", "model_verdict": "feasible",
                                              "reason": "计划会进入禁止区域：drawer_1。"}]
    assert not agent.store.list("tasks")
    agent.close()


def test_a_clarifying_question_is_answered_and_planning_restarts(tmp_path, model):
    question = place_plan()
    question.update(subtasks=[], reply="放进哪个抽屉？")
    question["understanding"]["ambiguities"] = ["放进哪个抽屉？"]
    question["feasibility"] = {"verdict": "needs_clarification", "checks": {**ALL_PASS, "ambiguity": "fail"},
                               "assumptions": []}
    server = model(question, place_plan(version=2), ACCEPTED)
    agent, world = make_agent(tmp_path, server.config)
    task = agent.submit(INSTRUCTION, l1_plans=L1)
    report = run(agent, world, task)
    assert report["task"]["status"] == "awaiting_user" and report["task"]["reply"] == "放进哪个抽屉？"
    agent.say(task, "就是 drawer_1")
    report = run(agent, world, task)
    assert report["metrics"]["success"] and report["task"]["plan_version"] == 2
    # The answer reaches L0 as dialogue turns, and it plans again from step 1.
    session = [m for m in server.requests[1]["messages"] if m["role"] == "assistant" or m["content"] == "就是 drawer_1"]
    assert [m["content"] for m in session] == ["放进哪个抽屉？", "就是 drawer_1"]
    assert server.arguments(1)["mode"] == "plan" and server.arguments(1)["plan_version"] == 2
    agent.close()


def replacement_plan():
    plan = place_plan()
    kept = plan["subtasks"][0]
    return place_plan(version=2, subtasks=[
        kept,
        {**subtask("t1-04-MAN-A", "换个角度再抓红色方块", "holding(red_block)", ["t1-01-MOV-A"]),
         "replaces": "t1-02-MAN-A"},
        {**subtask("t1-05-MAN-A", "把方块放进抽屉", "object_in(red_block, drawer_1)", ["t1-04-MAN-A"]),
         "replaces": "t1-03-MAN-A"},
    ])


def test_a_failed_subtask_is_replanned_and_finished_work_is_kept(tmp_path, model):
    # max_repairs=0 keeps L1's own repair loop out of the way, so only L0 answers here.
    server = model(place_plan(), replacement_plan(), ACCEPTED)
    agent, world = make_agent(tmp_path, server.config)
    world.pick_failures = 1
    task = agent.submit(INSTRUCTION, l1_plans={**L1, "t1-04-MAN-A": PICK, "t1-05-MAN-A": PLACE}, max_repairs=0)
    report = run(agent, world, task)
    assert report["metrics"]["success"], agent.report(task, text=True)
    subs = {s["id"]: s for s in report["subtasks"]}
    assert report["task"]["plan_version"] == 2 and report["metrics"]["replans"] == 1
    assert len(subs["t1-01-MOV-A"]["runtime_tasks"]) == 1  # finished work is not redone
    assert subs["t1-02-MAN-A"]["status"] == "FAILED" and subs["t1-02-MAN-A"]["result"]["error_ref"]
    assert subs["t1-03-MAN-A"]["status"] == "CANCELED" and not subs["t1-03-MAN-A"]["current"]
    assert subs["t1-05-MAN-A"]["status"] == "SUCCEEDED"
    replan = server.arguments(1)
    assert replan["mode"] == "replan" and replan["next_seq"] == 4 and replan["plan_version"] == 2
    history = next(f["content"] for f in agent.store.get("context_bundles", records(agent)[1]["context_bundle_id"])["fragments"]
                   if f["id"] == "history")
    assert history["trigger"]["trigger"] == "subtask_failed"
    failed = next(item for item in history["tree"] if item["id"] == "t1-02-MAN-A")
    assert failed["status"] == "FAILED" and "grasp slipped" in failed["errors"][-1]["error"]
    agent.close()


def test_a_replan_that_rewrites_finished_work_is_rejected(tmp_path, model):
    rewritten = replacement_plan()
    rewritten["subtasks"][0] = {**rewritten["subtasks"][0], "description": "再走一次"}
    server = model(place_plan(), rewritten, copy.deepcopy(rewritten))
    agent, world = make_agent(tmp_path, server.config)
    world.pick_failures = 1
    task = agent.submit(INSTRUCTION, l1_plans=L1, max_planning_failures=1, max_repairs=0)
    report = run(agent, world, task)
    assert report["task"]["status"] == "needs_operator" and "changed; give the changed subtask a new id" in report["task"]["error"]
    agent.close()


def test_user_interjection_cancels_work_in_progress_and_replans(tmp_path, model):
    walk_only = place_plan(version=2, subtasks=[
        {**subtask("t1-04-MOV-A", "走到抽屉旁", "robot_at(drawer_1)"), "replaces": "t1-01-MOV-A"}])
    walk_only["instruction"] = INSTRUCTION
    to_drawer = {"steps": [{"id": "stand", "skill": "robot.stand", "args": {}},
                           {"id": "walk", "skill": "base.navigate_to", "deps": ["stand"],
                            "args": {"target": [1.0, -0.6, 0.0]}}], "verification": "walk"}
    server = model(place_plan(), walk_only, {"achieved": True, "unmet": [], "reply": "到抽屉旁边了。"})
    agent, world = make_agent(tmp_path, server.config)
    task = agent.submit(INSTRUCTION, l1_plans={**L1, "t1-04-MOV-A": to_drawer})
    for _ in range(2):
        agent.observe_many(world.observations())
        agent.step()
    assert agent.tasks.get(task)["state"]["t1-01-MOV-A"]["status"] == "RUNNING"
    agent.say(task, "先别拿了，走到抽屉旁边就行")
    report = run(agent, world, task)
    subs = {s["id"]: s for s in report["subtasks"]}
    assert report["metrics"]["success"] and subs["t1-01-MOV-A"]["status"] == "CANCELED"
    assert subs["t1-01-MOV-A"]["runtime_tasks"][0]["status"] == "canceled"  # stopped before re-planning
    assert subs["t1-02-MAN-A"]["status"] == "CANCELED" and subs["t1-04-MOV-A"]["status"] == "SUCCEEDED"
    assert json.loads(next(e["data"] for e in agent.store.events("agent_task:t1")
                           if e["type"] == "replan_requested"))["trigger"] == "user_interjection"
    agent.close()
