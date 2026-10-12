"""Operator CLI; no built-in model answers or robot success responses."""

import argparse
import json
import sys
import time
from pathlib import Path
from .store import Store
from .runtime import Runtime
from .memory import Memory
from .contracts import ContractError
from .actions import Actions, local_principal


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parser():
    p = argparse.ArgumentParser(description="Robot task and evidence framework")
    p.add_argument("--root", default=".runtime", help="persistent state directory")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--allow-read", action="append", required=True)
    reg = sub.add_parser("register")
    reg.add_argument("name")
    reg.add_argument("spec")
    cap = sub.add_parser("capacity")
    cap.add_argument("name")
    cap.add_argument("units", type=int)
    submit = sub.add_parser("submit")
    submit.add_argument("plan")
    submit.add_argument("--robot", required=True)
    submit.add_argument("--priority", type=int, default=0)
    submit.add_argument("--idempotency-key")
    for cmd in ("status", "events", "pause", "cancel", "resume", "preempt"):
        control = sub.add_parser(cmd)
        control.add_argument("task")
        control.add_argument("--idempotency-key")
    action = sub.add_parser("action", help="invoke the shared application action registry")
    action.add_argument("name")
    action.add_argument("input", help="JSON argument file")
    action.add_argument("--idempotency-key")
    sub.add_parser("tasks")
    sub.add_parser("skills")
    sub.add_parser("leases")
    serve = sub.add_parser("serve-skills")
    serve.add_argument("--port", type=int, default=8088)
    runner = sub.add_parser("run")
    runner.add_argument("--until")
    runner.add_argument("--until-agent-task")
    body = sub.add_parser("embodiment-install", help="register a body's bound abstract skills for one robot")
    body.add_argument("profile")
    body.add_argument("binding")
    body.add_argument("--robot", required=True)
    world = sub.add_parser("world", help="show the robot's world state with validity")
    world.add_argument("--robot", required=True)
    observe = sub.add_parser("observe", help="record an operator or bridge observation")
    observe.add_argument("key")
    observe.add_argument("value", help="JSON value")
    observe.add_argument("--robot", required=True)
    observe.add_argument("--source-ref", required=True)
    observe.add_argument("--source-kind", choices=["operator", "observation"], default="operator")
    observe.add_argument("--evidence-level", required=True,
                         choices=["software_integration", "simulation", "hardware_observation"])
    observe.add_argument("--stamp-ns", type=int)
    observe.add_argument("--frame")
    observe.add_argument("--ttl", type=float)
    observe.add_argument("--confidence", type=float, default=1.0)
    observe.add_argument("--map-version")
    mapv = sub.add_parser("map-version", help="declare a new map/TF baseline; older spatial facts need re-observation")
    mapv.add_argument("version")
    mapv.add_argument("--robot", required=True)
    instruct = sub.add_parser("instruct", help="start a task from one user instruction (L0 plans it)")
    instruct.add_argument("instruction")
    instruct.add_argument("--robot", required=True)
    instruct.add_argument("--model-config")
    instruct.add_argument("--plan", help="operator TaskPlan JSON instead of asking L0")
    instruct.add_argument("--l1-plans", help="JSON {subtask_id: skill plan} for subtasks without a model")
    instruct.add_argument("--view", help="JSON {description, keyframe_asset} from the camera interface")
    instruct.add_argument("--confirm", action="store_true", help="the user already confirmed actions that need it")
    instruct.add_argument("--priority", type=int, default=0)
    instruct.add_argument("--max-replans", type=int, default=2)
    instruct.add_argument("--max-repairs", type=int, default=1)
    policy = sub.add_parser("policy-set", help="agent-side rules, paradigm skills and map overview for L0")
    policy.add_argument("policy")
    policy.add_argument("--robot", required=True)
    sub.add_parser("agent-tasks")
    for cmd in ("agent-task-status", "agent-task-cancel", "agent-task-events"):
        sub.add_parser(cmd).add_argument("task")
    report = sub.add_parser("agent-task-report", help="task tree, per-step status and benchmark metrics")
    report.add_argument("task")
    report.add_argument("--text", action="store_true")
    say = sub.add_parser("agent-task-say", help="the user answers, confirms or interjects")
    say.add_argument("task")
    say.add_argument("text")
    say.add_argument("--confirm", action="store_true")
    scene = sub.add_parser("scene-load", help="register static scene objects and regions")
    scene.add_argument("scene")
    scene.add_argument("--robot", required=True)
    scene.add_argument("--evidence-level", required=True,
                       choices=["software_integration", "simulation", "hardware_observation"])
    tres_agent = sub.add_parser("agent-task-resolve", help="operator decision for a stuck task")
    tres_agent.add_argument("task")
    tres_agent.add_argument("action", choices=["confirm", "retry", "replan", "abort"])
    tres_agent.add_argument("--subtask")
    tres_agent.add_argument("--note", default="")
    tres_agent.add_argument("--value", help="JSON answer when confirming a check or obtain subtask")
    revise = sub.add_parser("agent-task-revise", help="operator re-plan: a new TaskPlan version")
    revise.add_argument("task")
    revise.add_argument("plan")
    revise.add_argument("--l1-plans")
    tres = sub.add_parser("task-resolve", help="operator decision for a task waiting on a person")
    tres.add_argument("task")
    tres.add_argument("decision", choices=["recover", "fail"])
    tres.add_argument("--note", default="")
    automation = sub.add_parser("automation-worker", help="run owned event diagnostics independently of execution")
    automation.add_argument("--once", action="store_true")
    automation.add_argument("--scan-only", action="store_true", help="persist pending event jobs without model calls")
    agent = sub.add_parser("agent")
    agent.add_argument("goal")
    agent.add_argument("--robot", required=True)
    agent.add_argument("--model-config", required=True)
    agent.add_argument("--priority", type=int, default=0)
    agent.add_argument("--max-replans", type=int, default=0)
    analyze = sub.add_parser("analyze-goal")
    analyze.add_argument("goal")
    analyze.add_argument("--model-config", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("goal")
    plan.add_argument("--model-config", required=True)
    plan.add_argument("--context")
    replan = sub.add_parser("replan")
    replan.add_argument("task")
    replan.add_argument("--plan")
    replan.add_argument("--model-config")
    replan.add_argument("--goal")
    assets = sub.add_parser("assets")
    assets.add_argument("--kind")
    assets.add_argument("--robot")
    assets.add_argument("--frame")
    assets.add_argument("--start-ns", type=int)
    assets.add_argument("--end-ns", type=int)
    assets.add_argument("--version")
    ingest = sub.add_parser("ingest")
    ingest.add_argument("path")
    ingest.add_argument("metadata")
    verify = sub.add_parser("verify-asset")
    verify.add_argument("asset")
    remember = sub.add_parser("remember")
    remember.add_argument("kind")
    remember.add_argument("text")
    remember.add_argument("--attributes", required=True)
    remember.add_argument("--evidence", nargs="+", required=True)
    search = sub.add_parser("search")
    search.add_argument("text")
    search.add_argument("--kind")
    search.add_argument("--robot")
    record = sub.add_parser("record-file")
    record.add_argument("path")
    record.add_argument("output")
    record.add_argument("--topic", required=True)
    record.add_argument("--encoding", required=True)
    record.add_argument("--metadata", required=True)
    record.add_argument("--publish-time", type=int, required=True)
    sub.add_parser("inspect-recording").add_argument("path")
    sub.add_parser("ros-topics")
    ros = sub.add_parser("record-ros")
    ros.add_argument("output")
    ros.add_argument("--topics", nargs="+", required=True)
    ros.add_argument("--duration", type=float, required=True)
    ros.add_argument("--robot", required=True)
    ros.add_argument("--storage", choices=["sqlite3", "mcap"], default="sqlite3")
    ros.add_argument("--use-sim-time", action="store_true")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    store = Store(args.root)
    rt = Runtime(store)
    memory = Memory(store)
    try:
        cmd = args.command
        actions = Actions(store)
        principal = local_principal()
        def invoke(name, body):
            import uuid
            return actions.call(name, body, principal,
                idempotency_key=getattr(args, "idempotency_key", None) or str(uuid.uuid4()))["result"]
        def versioned():
            task = actions.task(args.task, principal)
            return {"task_id": task["id"], "expected_revision": task["revision"], "expected_generation": task["generation"]}
        if cmd == "init":
            rt.install_local_skills([str(Path(p).resolve()) for p in args.allow_read])
            result = {"root": str(store.root), "skills": list(rt.catalog())}
        elif cmd == "register":
            rt.register(args.name, read_json(args.spec))
            result = {"registered": args.name}
        elif cmd == "capacity":
            store.set_capacity(args.name, args.units)
            result = {"resource": args.name, "capacity": args.units}
        elif cmd == "submit":
            result = invoke("task.submit", {"plan": read_json(args.plan), "robot_id": args.robot, "priority": args.priority})
            result["id"] = result["task_id"]
        elif cmd == "action":
            result = invoke(args.name, read_json(args.input))
        elif cmd == "status":
            result = invoke("task.get", {"task_id": args.task})["task"]
        elif cmd == "events":
            result = invoke("task.events", {"task_id": args.task})["events"]
        elif cmd == "tasks":
            result = invoke("task.list", {})["items"]
        elif cmd == "skills":
            result = rt.catalog()
        elif cmd == "leases":
            result = store.leases()
        elif cmd in {"pause", "cancel"}:
            result = invoke("task." + cmd, versioned())
        elif cmd == "resume":
            result = invoke("task.resume", versioned())
        elif cmd == "preempt":
            result = invoke("task.preempt", versioned())
        elif cmd == "serve-skills":
            from .service import skill_server

            server = skill_server(store, port=args.port)
            try:
                server.serve_forever()
            finally:
                server.server_close()
            result = {"stopped": True}
        elif cmd == "automation-worker":
            from .automations import run_once
            while True:
                result = run_once(store, principal, scan_only=args.scan_only)
                if args.once or args.scan_only:
                    break
                time.sleep(1)
        elif cmd == "run":
            from .durable import run

            result = run(store.root, args.until, args.until_agent_task)
        elif cmd == "embodiment-install":
            from .embodiment import install

            result = install(rt, read_json(args.profile), read_json(args.binding), args.robot)
        elif cmd == "world":
            from .world_state import WorldState

            result = WorldState(store, args.robot).snapshot()
        elif cmd == "observe":
            from .world_state import WorldState

            result = WorldState(store, args.robot).observe(
                args.key, json.loads(args.value), source={"kind": args.source_kind, "ref": args.source_ref},
                stamp_ns=args.stamp_ns or time.time_ns(), evidence_level=args.evidence_level,
                frame_id=args.frame, confidence=args.confidence, ttl_s=args.ttl, map_version=args.map_version)
            result = result or {"ignored": "older than the current observation"}
        elif cmd == "map-version":
            from .world_state import WorldState

            result = {"needs_reverification": WorldState(store, args.robot).set_map_version(args.version)}
        elif cmd == "instruct":
            from .task_manager import TaskManager

            result = TaskManager(store, rt).create(
                args.instruction, args.robot, read_json(args.model_config) if args.model_config else None,
                plan=read_json(args.plan) if args.plan else None,
                l1_plans=read_json(args.l1_plans) if args.l1_plans else None,
                view=read_json(args.view) if args.view else None, confirmed=args.confirm,
                priority=args.priority, max_replans=args.max_replans, max_repairs=args.max_repairs)
        elif cmd == "policy-set":
            from .task_manager import TaskManager

            result = TaskManager(store, rt).set_policy(args.robot, read_json(args.policy))
        elif cmd == "agent-tasks":
            result = [{k: t[k] for k in ("id", "robot_id", "instruction", "status", "plan_version", "created_at")}
                      for t in store.list("agent_tasks")]
        elif cmd == "agent-task-status":
            from .task_manager import TaskManager

            result = TaskManager(store, rt).get(args.task)
        elif cmd == "agent-task-cancel":
            from .task_manager import TaskManager

            result = TaskManager(store, rt).cancel(args.task)
        elif cmd == "agent-task-events":
            result = [{**e, "data": json.loads(e["data"])} for e in store.events("agent_task:" + args.task)]
        elif cmd == "agent-task-report":
            from .report import render, task_report

            report = task_report(store, args.task)
            if args.text:
                print(render(report))
                return 0
            result = report
        elif cmd == "agent-task-say":
            from .task_manager import TaskManager

            result = TaskManager(store, rt).say(args.task, args.text, confirm=args.confirm)
        elif cmd == "agent-task-revise":
            from .task_manager import TaskManager

            result = TaskManager(store, rt).revise(args.task, read_json(args.plan),
                                                   read_json(args.l1_plans) if args.l1_plans else None)
        elif cmd == "scene-load":
            from .world_state import WorldState

            result = {"facts": WorldState(store, args.robot).load_scene(
                read_json(args.scene), source_ref="scene:" + Path(args.scene).name, evidence_level=args.evidence_level)}
        elif cmd == "agent-task-resolve":
            from .task_manager import TaskManager

            result = TaskManager(store, rt).resolve(args.task, args.action, args.subtask, args.note,
                                                    json.loads(args.value) if args.value else None)
        elif cmd == "task-resolve":
            result = rt.resolve(args.task, args.decision, args.note)
        elif cmd == "agent":
            actions = Actions(store, model_config=read_json(args.model_config))
            session = invoke("session.create", {"robot_id": args.robot, "goal": args.goal,
                             "priority": args.priority, "max_replans": args.max_replans})["session"]
            session = invoke("goal.analyze", {"session_id": session["id"], "expected_revision": session["revision"]})["session"]
            analysis = session["analysis"]
            if analysis["status"] == "needs_clarification" or analysis["questions"]:
                result = {"session_id": session["id"], "analysis": analysis, "status": "needs_clarification"}
            else:
                version = {"session_id": session["id"], "expected_revision": session["revision"]}
                draft = invoke("plan.propose", version)["draft"]
                result = invoke("plan.submit", {**version, "draft_id": draft["id"], "plan_hash": draft["plan_hash"]})
        elif cmd == "analyze-goal":
            result = rt.analyze_goal(
                args.goal,
                read_json(args.model_config),
            ).as_dict()
        elif cmd == "plan":
            from .planner import Planner

            result = Planner(store, read_json(args.model_config)).plan(
                args.goal,
                rt.catalog(),
                read_json(args.context) if args.context else None,
            )
        elif cmd == "replan":
            if args.plan:
                body = read_json(args.plan)
            else:
                from .planner import Planner

                if not args.model_config or not args.goal:
                    raise ContractError(
                        "replan needs --plan or actual --model-config and --goal"
                    )
                task = store.get("tasks", args.task)
                body = Planner(store, read_json(args.model_config)).plan(
                    args.goal, rt.catalog(), task
                )
            result = invoke("task.revise", {**versioned(), "plan": body})
        elif cmd == "assets":
            result = memory.query_assets(
                args.kind,
                args.robot,
                args.frame,
                args.start_ns,
                args.end_ns,
                args.version,
            )
        elif cmd == "ingest":
            result = memory.ingest(args.path, read_json(args.metadata))
        elif cmd == "verify-asset":
            result = {
                "asset_id": args.asset,
                "size": len(memory.read(args.asset)),
                "verified": True,
            }
        elif cmd == "remember":
            result = memory.remember(
                args.kind, args.text, read_json(args.attributes), args.evidence
            )
        elif cmd == "search":
            result = memory.search(args.text, args.kind, args.robot)
        elif cmd == "record-file":
            from .recording import Recorder

            data = Path(args.path).read_bytes()
            with Recorder(args.output) as recorder:
                recorder.append(
                    args.topic,
                    args.encoding,
                    data,
                    args.publish_time,
                    time.time_ns(),
                    read_json(args.metadata),
                )
            result = {"path": str(Path(args.output).resolve()), "bytes": len(data)}
        elif cmd == "ros-topics":
            from .ros_recording import discover

            result = discover()
        elif cmd == "record-ros":
            from .ros_recording import record

            result = record(
                args.topics,
                args.output,
                args.duration,
                args.robot,
                args.storage,
                args.use_sim_time,
            )
        elif cmd == "inspect-recording":
            from .recording import messages

            result = [
                {k: v for k, v in x.items() if k not in {"data", "schema"}}
                | {"bytes": len(x["data"])}
                for x in messages(args.path)
            ]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return (
            0 if not isinstance(result, dict) or result.get("status") != "failed" else 1
        )
    except KeyboardInterrupt:
        print(
            "Worker interrupted; external stop is not confirmed. Restart and reconcile active executions.",
            file=sys.stderr,
        )
        return 130
    except Exception as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
