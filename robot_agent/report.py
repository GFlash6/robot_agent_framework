"""Task results: a structured report, benchmark metrics and a readable task tree."""

import json
import time

from .goals import atoms_of
from .task_plan import CATEGORIES, KINDS, parse_id
from .world_state import WorldState

ICONS = {"succeeded": "✅", "skipped": "⏭", "running": "⏳", "verifying": "🔍", "pending": "⏸", "queued": "⏸",
         "failed": "❌", "needs_operator": "✋", "canceled": "⛔", "canceling": "⛔", "repairing": "🔧",
         "replanning": "🔧", "compensating": "↩", "unknown": "❓", "paused": "⏸", "pausing": "⏸",
         "planning": "🧩", "accepting": "🔍", "awaiting_user": "💬", "infeasible": "🚫",
         "SUCCEEDED": "✅", "SKIPPED": "⏭", "PARTIAL": "◐", "FAILED": "❌", "CANCELED": "⛔", "PENDING": "⏸",
         "READY": "▶", "PLANNING": "🧩", "RUNNING": "⏳", "BLOCKED": "⏯", "MISSING_PREREQ": "⚠", "RECOVERING": "🔧"}


def task_summary(store, task_id):
    """One Runtime task (an L1 skill plan): steps, executions and lower-layer recovery."""
    task = store.get("tasks", task_id)
    steps, executions, failed = [], 0, 0
    for step in task["plan"]["steps"]:
        state = task["steps"][step["id"]]
        choice = [step, *step.get("fallback", [])][state.get("alternative", 0)]
        execution = store.get("executions", state["execution_id"]) if state.get("execution_id") else None
        steps.append({
            "id": step["id"], "skill": choice["skill"], "status": state["status"],
            "attempts": state.get("attempts", 0), "fallbacks_used": state.get("alternative", 0),
            "error": state.get("error") or (state.get("result") or {}).get("error"),
            "skip_reason": state.get("reason"),
            "progress": ((execution or {}).get("monitor") or {}).get("progress"),
            "evidence_level": (execution or {}).get("evidence_level"),
        })
    with store.lock:
        rows = store.db.execute("SELECT data FROM objects WHERE collection='executions' "
                                "AND json_extract(data, '$.task_id') = ?", (task_id,)).fetchall()
    for row in rows:
        executions += 1
        failed += json.loads(row["data"]).get("status") == "failed"
    compensation = task.get("compensation_task_id")
    return {"id": task["id"], "status": task["status"], "revision": task["revision"],
            "repairs": task.get("repairs", 0), "replans": task.get("replans", 0),
            "compensation": task_summary(store, compensation) if compensation else None,
            "steps": steps, "executions": executions, "failed_executions": failed,
            "retries": sum(max(0, s["attempts"] - 1) for s in steps),
            "fallbacks": sum(s["fallbacks_used"] for s in steps)}


def task_report(store, task_id, *, include_world=True):
    """Everything a person or a benchmark needs to judge one task."""
    task = store.get("agent_tasks", task_id)
    events = store.events("agent_task:" + task_id)
    counts = {status: 0 for status in ("SUCCEEDED", "SKIPPED", "PARTIAL", "FAILED", "CANCELED")}
    metrics = {"plan_versions": task["plan_version"], "replans": task["replans"], "model_calls": task["model_calls"],
               "operator_actions": len(task["operator_actions"]), "runtime_tasks": 0, "executions": 0,
               "failed_executions": 0, "retries": 0, "fallbacks": 0, "repairs": 0, "compensations": 0,
               "subtask_attempts": 0, "subtasks": {"total": len(task["defs"]), "current": len(task["order"]), **counts}}
    subtasks = []
    for sid in sorted(task["defs"], key=lambda s: parse_id(s)["seq"]):
        spec, state = task["defs"][sid], task["state"][sid]
        runtime = [task_summary(store, t) for t in state["runtime_tasks"]]
        for summary in runtime:
            metrics["runtime_tasks"] += 1
            for key in ("executions", "failed_executions", "retries", "fallbacks", "repairs"):
                metrics[key] += summary[key]
            metrics["model_calls"] += summary["repairs"] + summary["replans"]
            if summary["compensation"]:
                metrics["compensations"] += 1
                metrics["executions"] += summary["compensation"]["executions"]
        metrics["subtask_attempts"] += state["attempts"]
        if state["status"] in counts:
            counts[state["status"]] += 1
        subtasks.append({**spec, "status": state["status"], "result": state["result"], "attempts": state["attempts"],
                         "errors": state["errors"], "error": state.get("error"), "resolved_goal": state.get("goal"),
                         "current": sid in task["order"], "runtime_tasks": runtime})
    metrics["subtasks"].update(counts)
    ended = task["status"] in {"succeeded", "failed", "infeasible", "canceled"}
    finished = events[-1]["ts"] if events and ended else time.time()
    metrics.update(success=task["status"] == "succeeded", status=task["status"],
                   duration_s=round(finished - task["created_at"], 3))
    plan = task["plan"] or {}
    report = {"task": {k: task.get(k) for k in ("id", "instruction", "robot_id", "status", "plan_version", "reply",
                                                "error", "acceptance")},
              "understanding": plan.get("understanding"), "situation": plan.get("situation"),
              "grounding": plan.get("grounding"), "feasibility": plan.get("feasibility"),
              "plans": task["plans"], "dialogue": task["dialogue"], "subtasks": subtasks, "metrics": metrics}
    if include_world:
        report["world"] = WorldState(store, task["robot_id"]).snapshot()
    return report


def goal_text(goal):
    key = next(iter(k for k in goal if k != "type"))
    return {"achieve": "达成", "check": "判别", "obtain": "获取"}[key] + " " + " 且 ".join(atoms_of(goal))


def render(report):
    """Text tree: instruction -> subtasks (goal, status, result) -> skill steps."""
    task, feasibility = report["task"], report["feasibility"] or {}
    lines = [f"任务 {task['id']}：{task['instruction']}    [{ICONS.get(task['status'], '')} {task['status']}]"
             + (f"  计划 v{task['plan_version']} · {feasibility.get('verdict')}" if feasibility else "")]
    if feasibility.get("assumptions"):
        lines.append("假设：" + "；".join(feasibility["assumptions"]))
    shown = [s for s in report["subtasks"] if s["current"] or s["status"] not in {"CANCELED", "PENDING"}]
    for i, sub in enumerate(shown):
        branch, pad = ("└─ ", "   ") if i == len(shown) - 1 else ("├─ ", "│  ")
        tags = [f"{CATEGORIES[sub['category']]}/{KINDS[sub['kind']]}"]
        if not sub["required"]:
            tags.append("可选")
        if not sub["current"]:
            tags.append("已移出计划")
        if sub["replaces"]:
            tags.append(f"替换 {sub['replaces']}")
        lines.append(f"{branch}{sub['id']}  {sub['description']}    {ICONS.get(sub['status'], '')} {sub['status']}"
                     + (f"  (第{sub['attempts']}次)" if sub["attempts"] > 1 else "") + f"  [{', '.join(tags)}]")
        lines.append(f"{pad}   目标：{goal_text(sub['resolved_goal'] or sub['goal'])}")
        for earlier in sub["runtime_tasks"][:-1]:
            failed = next((s for s in earlier["steps"] if s["status"] == "failed"), None)
            lines.append(f"{pad}   前次：{earlier['status']}"
                         + (f"，{failed['skill']} 失败：{(failed['error'] or '')[:80]}" if failed else "")
                         + (f"，补偿 {earlier['compensation']['status']}" if earlier["compensation"] else ""))
        if sub["runtime_tasks"]:
            last = sub["runtime_tasks"][-1]
            chain = " → ".join(
                f"{s['skill']}{ICONS.get(s['status'], '')}" + (f"{int(s['progress'] * 100)}%" if s["status"] == "running"
                                                              and s["progress"] is not None else "")
                for s in last["steps"])
            lines.append(f"{pad}   技能：{chain}")
            if last["compensation"]:
                lines.append(f"{pad}   补偿：{last['compensation']['status']}")
        result = sub["result"]
        if result and result["value"] is not None:
            lines.append(f"{pad}   结果：{json.dumps(result['value'], ensure_ascii=False)}")
        if sub.get("error") and sub["status"] not in {"SUCCEEDED", "SKIPPED"}:
            lines.append(f"{pad}   原因：{sub['error'][:160]}")
    if task["reply"]:
        lines.append(f"\n回复：{task['reply']}")
    m = report["metrics"]
    lines.append(f"{'' if task['reply'] else chr(10)}结果：{'成功' if m['success'] else m['status']}  用时 {m['duration_s']}s  "
                 f"计划版本 {m['plan_versions']}  重规划 {m['replans']}  执行 {m['executions']} 次"
                 f"（失败 {m['failed_executions']}）  重试 {m['retries']}  修复 {m['repairs']}  补偿 {m['compensations']}  "
                 f"模型调用 {m['model_calls']}  人工 {m['operator_actions']}")
    return "\n".join(lines)
