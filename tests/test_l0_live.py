"""L0 with the actual model: one instruction on the G1 fixture scene must yield a valid, rule-checked TaskPlan.

Consumes API quota; runs only with RUN_LIVE_MODEL_TESTS=1. The evidence is the
model_responses record (request, raw response, validation). Execution here uses
the kinematic fixture world and the same model as L1, so a pass is software
(L0) evidence about the call path and plan contract, not about robot behaviour.
"""

import json
import os
from pathlib import Path

import pytest

from test_agent_driver import INSTRUCTION, make_agent, run


def live_config():
    if os.environ.get("RUN_LIVE_MODEL_TESTS") != "1":
        pytest.skip("set RUN_LIVE_MODEL_TESTS=1 to call the actual model")
    path = Path(os.environ.get("ROBOT_AGENT_MODEL_CONFIG", ".runtime/model-config.json"))
    config = json.loads(path.read_text(encoding="utf-8"))
    if not os.environ.get(config.get("token_env", "")):
        pytest.fail(f"required model token environment variable is missing: {config.get('token_env')}")
    return config


@pytest.mark.live_model
def test_actual_model_plans_place_block_task(tmp_path):
    agent, world = make_agent(tmp_path, live_config())
    agent.set_policy({"map": {"summary": "单个房间：桌子 table_1 上有红色方块，墙边有抽屉 drawer_1。"}})
    task = agent.submit(INSTRUCTION)
    agent.step()
    record = next(r for r in agent.store.list("model_responses") if r["method"] == "task_planning")
    assert record["status"] == "validated", record.get("error")
    state = agent.tasks.get(task)
    plan = state["plan"]
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    assert plan["feasibility"]["verdict"] in {"feasible", "feasible_with_assumptions"}, plan["reply"]
    assert {s["category"] for s in plan["subtasks"]} <= {"SYS", "MOV", "PER", "MAN"}
    report = run(agent, world, task, steps=400)
    print(agent.report(task, text=True))
    assert report["metrics"]["success"], agent.report(task, text=True)
    agent.close()
