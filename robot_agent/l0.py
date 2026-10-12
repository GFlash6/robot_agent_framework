"""L0 planning layer (vlm0): instruction -> TaskPlan, re-planning, and final acceptance.

Design: "L0 规划层设计.md". One model call runs the five steps (understanding,
situation, grounding, feasibility, subtasks). Deterministic rules run on both
sides of it: before the call they become ``rule_findings`` in the context;
after it they check the candidate against the world state and the agent-side
policy and may only make the verdict stricter, never looser. L0 returns a plan;
it never writes task state (the task manager does).

Inputs follow §2: summaries first, raw data only when the instruction needs it.

    dialogue        instruction and recent turns            task manager
    capabilities    skill categories + paradigm skills       skill registry, policy
    rules           safety/policy rules, confirmations       agent policy (per robot)
    view            scene sentence + keyframe asset          caller (camera interface)
    map             text overview + named places             agent policy, world state
    robot state     one sentence: place, holding, battery    world state
    known objects   name, place, last seen, confidence       world state
    history         task tree, results, error reports        task manager (re-planning)
    experience      similar past tasks and plans (optional)  caller
"""

from dataclasses import dataclass, field
import json
import math
import time
import uuid

from . import goals
from .context_codec import decode_bundle, encode_bundle
from .context_evidence import context_manifest
from .context_memory import record_hash
from .context_models import ContextBundle, ContextFragment, ContextRequest, GoalContext
from .contracts import ContractError, require
from .model_call import ModelCaller
from .model_transport import ModelCallError
from .store import dumps
from .task_plan import (CATEGORIES, capability_view, next_seq, validate_paradigms, validate_revision,
                        validate_task_plan)
from .world_state import WorldState, evaluate

PLACE_ATTRIBUTES = ("approach", "region")
NEAR_PLACE_M = 1.0
MAX_KNOWN_OBJECTS = 64
EXECUTABLE = {"feasible", "feasible_with_assumptions"}
RULE_FIELDS = {"forbidden_places", "confirm_categories", "confirm_intents", "min_battery", "notes"}


# ---------------------------------------------------------------- agent-side policy


def validate_policy(policy):
    """Per-robot policy: {"rules": {...}, "paradigms": [...], "map": {"summary", "rooms", "annotated_map_asset"}}."""
    require(isinstance(policy, dict) and set(policy) <= {"rules", "paradigms", "map"},
            "policy has rules, paradigms and map")
    rules = policy.get("rules", {})
    require(isinstance(rules, dict) and set(rules) <= RULE_FIELDS, f"rules fields are {sorted(RULE_FIELDS)}")
    for key in ("forbidden_places", "confirm_intents", "notes"):
        value = rules.get(key, [])
        require(isinstance(value, list) and all(isinstance(x, str) and x for x in value),
                f"rules.{key} must be a list of text")
    confirm = rules.get("confirm_categories", [])
    require(isinstance(confirm, list) and set(confirm) <= set(CATEGORIES), "confirm_categories must be categories")
    battery = rules.get("min_battery")
    require(battery is None or (type(battery) in (int, float) and 0 <= battery <= 1),
            "min_battery is a fraction 0..1")
    validate_paradigms(policy.get("paradigms", []))
    world_map = policy.get("map", {})
    require(isinstance(world_map, dict) and set(world_map) <= {"summary", "rooms", "annotated_map_asset"},
            "map has summary, rooms and annotated_map_asset")
    require(isinstance(world_map.get("summary", ""), str) and isinstance(world_map.get("rooms", []), list),
            "map summary is text and rooms a list")
    return policy


# ---------------------------------------------------------------- world digest


def _place_of(position, places):
    """The named place whose region contains a position, else the nearest approach point within 1 m."""
    if not (isinstance(position, list) and len(position) >= 2
            and all(type(x) in (int, float) for x in position)):
        return None
    for name, place in places.items():
        region = place.get("region")
        if region and len(position) >= len(region["min"]) and all(
                lo <= x <= hi for x, lo, hi in zip(position, region["min"], region["max"])):
            return name
    nearest = min(((math.dist(position[:2], place["approach"][:2]), name) for name, place in places.items()
                   if isinstance(place.get("approach"), list) and len(place["approach"]) >= 2), default=None)
    return nearest[1] if nearest and nearest[0] <= NEAR_PLACE_M else None


def world_digest(world):
    """Robot state, named places and known objects, summarized from current facts."""
    now = world.clock()
    entities = {}
    for fact in world.facts():
        entity, _, attribute = fact["key"].partition(".")
        entities.setdefault(entity, {})[attribute] = fact

    def current(fact):
        return fact is not None and fact["status"] == "observed" and fact["validity"] == "valid"

    places = {}
    for name, attributes in entities.items():
        place = {a: attributes[a]["value"] for a in PLACE_ATTRIBUTES if current(attributes.get(a))}
        if name != "robot" and place:
            places[name] = place
    robot = entities.get("robot", {})
    state = {key: robot[key]["value"] if current(robot.get(key)) else None
             for key in ("pose", "holding", "battery", "estop", "mobility_ready")}
    state["place"] = _place_of(state["pose"], places)
    objects = []
    for name, attributes in entities.items():
        location = attributes.get("location")
        if name == "robot" or location is None:
            continue
        objects.append({"id": name, "place": _place_of(location["value"], places), "location": location["value"],
                        "frame": location.get("frame_id"), "last_seen_s": round((now - location["stamp_ns"]) / 1e9, 1),
                        "confidence": location["confidence"], "validity": location["validity"],
                        "status": location["status"]})
    objects.sort(key=lambda o: o["last_seen_s"])
    return {"robot": state, "places": places, "objects": objects[:MAX_KNOWN_OBJECTS], "entities": sorted(entities)}


def robot_summary(state):
    """One sentence: where the robot is, what it holds, its battery (§2)."""
    if state["place"]:
        where = f"在 {state['place']} 附近"
    elif state["pose"]:
        where = "位于 " + json.dumps(state["pose"])
    else:
        where = "位置未知"
    holding = state["holding"]
    held = "手里没有东西" if holding == "none" else f"手里拿着 {holding}" if holding else "是否持物未知"
    battery = state["battery"]
    charge = f"电量 {round(battery * 100)}%" if type(battery) in (int, float) else "电量未知"
    return f"机器人{where}，{held}，{charge}。"


# ---------------------------------------------------------------- inputs


@dataclass(frozen=True)
class L0Inputs:
    instruction: str
    capabilities: dict
    world: dict
    policy: dict = field(default_factory=dict)
    dialogue: tuple = ()
    view: dict | None = None
    history: dict | None = None
    experience: tuple = ()
    confirmed: bool = False

    @property
    def categories(self):
        return set(self.capabilities["categories"])

    @property
    def rules(self):
        return self.policy.get("rules", {})


def gather(store, robot_id, catalog, *, instruction, dialogue=(), view=None, history=None, experience=(),
           confirmed=False):
    policy = store.get("agent_policies", robot_id) or {}
    require(view is None or (isinstance(view, dict) and set(view) <= {"description", "keyframe_asset"}
                             and isinstance(view.get("description"), str) and view["description"].strip()),
            "view needs a scene description and optionally a keyframe asset id")
    require(all(isinstance(t, dict) and t.get("role") in {"user", "assistant"} and isinstance(t.get("content"), str)
                for t in dialogue), "dialogue turns need role user|assistant and content")
    return L0Inputs(instruction=instruction, capabilities=capability_view(catalog, policy.get("paradigms", [])),
                    world=world_digest(WorldState(store, robot_id)), policy=policy,
                    dialogue=tuple({"role": t["role"], "content": t["content"]} for t in dialogue),
                    view=view, history=history, experience=tuple(experience), confirmed=confirmed)


def rule_findings(inputs):
    """What the deterministic rules already know before the model is asked (§3 step 4)."""
    rules, robot = inputs.rules, inputs.world["robot"]
    battery, minimum = robot["battery"], rules.get("min_battery")
    low = type(battery) in (int, float) and minimum is not None and battery < minimum
    return {
        "capability": {"available_categories": sorted(inputs.categories),
                       "missing_categories": sorted(set(CATEGORIES) - inputs.categories)},
        "objects": {"known_world_ids": inputs.world["entities"]},
        "constraints": {"forbidden_places": rules.get("forbidden_places", []),
                        "confirmation_required_for": {"categories": rules.get("confirm_categories", []),
                                                      "intents": rules.get("confirm_intents", [])},
                        "user_confirmed": inputs.confirmed},
        "resources": {"battery": battery, "min_battery": minimum, "result": "fail" if low else "pass",
                      **({"note": "battery below the minimum: plan a SYS subtask to charge first, or report "
                                  "infeasible"} if low else {})},
    }


def fragments(inputs, findings):
    policy, world = inputs.policy, inputs.world
    items = [
        ContextFragment(id="rules", kind="policy", authority="framework", source="agent_policy", priority=100,
                        required=True, content={"rules": inputs.rules, "rule_findings": findings}),
        ContextFragment(id="capabilities", kind="capability", authority="framework", source="skill_registry",
                        priority=95, required=True, content=inputs.capabilities),
        ContextFragment(id="robot-state", kind="world_state", authority="data", source="world_state_ledger",
                        priority=92, required=True, content={"summary": robot_summary(world["robot"]),
                                                             **world["robot"]}),
        ContextFragment(id="map", kind="world_state", authority="data", source="agent_policy+world_state_ledger",
                        priority=90, required=True,
                        content={**policy.get("map", {}), "places": world["places"]},
                        evidence_ids=tuple(x for x in [policy.get("map", {}).get("annotated_map_asset")] if x)),
        ContextFragment(id="known-objects", kind="world_state", authority="data", source="world_state_ledger",
                        priority=88, required=True,
                        content={"objects": world["objects"],
                                 "rules": "Only status=observed with validity=valid is current. Anything else is a "
                                          "hint of where to look, not a fact."}),
    ]
    if inputs.dialogue:
        items.append(ContextFragment(id="dialogue", kind="session", authority="data", source="task_dialogue",
                                     priority=97, required=True, content=list(inputs.dialogue)))
    if inputs.view:
        items.append(ContextFragment(id="view", kind="world_state", authority="data", source="camera_view",
                                     priority=93, required=True, content=dict(inputs.view),
                                     evidence_ids=tuple(x for x in [inputs.view.get("keyframe_asset")] if x)))
    if inputs.history:
        items.append(ContextFragment(id="history", kind="task_state", authority="data", source="task_manager",
                                     priority=96, required=True, content=inputs.history))
    if inputs.experience:
        items.append(ContextFragment(id="experience", kind="memory", authority="data", source="procedural_memory",
                                     priority=50, required=False, content=list(inputs.experience)))
    return tuple(items)


# ---------------------------------------------------------------- rules after the model


def enforce_rules(plan, inputs, world):
    """Check a validated candidate against the world and policy.

    A false claim about the world (a known id that does not exist, a goal said to
    hold that does not) rejects the candidate. A policy or resource violation
    turns the verdict stricter: infeasible, or needs_clarification to ask the user.
    """
    entities = set(inputs.world["entities"])
    for item in plan["grounding"]:
        if item["status"] == "known":
            require(item["id"] in entities, f"{item['ref']} grounded to {item['id']}, which the world state does not know")
        if item["status"] == "satisfied":
            result = evaluate(world, goals.predicate(item["cond"]))["result"]
            require(result == "true", f"{item['cond']} is said to hold but the world state says {result}")
    feasibility = plan["feasibility"]
    if feasibility["verdict"] not in EXECUTABLE:
        return plan
    rules, subtasks = inputs.rules, plan["subtasks"]
    failures = {}
    visited = set().union(set(), *(goals.places(atom) for s in subtasks for atom in goals.atoms_of(s["goal"])
                                   if not goals.refs(atom)))
    forbidden = sorted(visited & set(rules.get("forbidden_places", [])))
    if forbidden:
        failures["constraints"] = ("infeasible", f"计划会进入禁止区域：{', '.join(forbidden)}。")
    battery, minimum = inputs.world["robot"]["battery"], rules.get("min_battery")
    if (type(battery) in (int, float) and minimum is not None and battery < minimum
            and not any(s["category"] == "SYS" for s in subtasks)):
        failures["resources"] = ("infeasible", f"电量 {battery:.0%} 低于下限 {minimum:.0%}，需要先充电。")
    confirm = sorted({s["category"] for s in subtasks} & set(rules.get("confirm_categories", [])))
    if plan["understanding"]["intent"] in rules.get("confirm_intents", []):
        confirm.append(plan["understanding"]["intent"])
    if confirm and not inputs.confirmed and "constraints" not in failures:
        failures["constraints"] = ("needs_clarification",
                                   f"这个任务包含需要您确认的动作（{', '.join(confirm)}），确认后我再开始。要继续吗？")
    if not failures:
        return plan
    verdict = "infeasible" if any(v == "infeasible" for v, _ in failures.values()) else "needs_clarification"
    overridden = {
        **feasibility, "verdict": verdict,
        "checks": {**feasibility["checks"], **{item: "fail" for item in failures}},
        "reasons": {**feasibility.get("reasons", {}), **{item: reason for item, (_, reason) in failures.items()}},
        "rule_overrides": [{"check": item, "verdict": v, "reason": reason, "model_verdict": feasibility["verdict"]}
                           for item, (v, reason) in failures.items()],
    }
    reply = "".join(reason for _, reason in failures.values())
    return {**plan, "feasibility": overridden, "subtasks": [], "reply": reply}


# ---------------------------------------------------------------- model calls


def _recorded_call(store, caller, method, arguments, bundle, accept):
    """Send one call with its context bundle and manifest on record; return (accepted, record id)."""
    key = str(uuid.uuid4())
    record = {"id": key, "model": caller.config.model, "created_at": time.time(), "method": method,
              "status": "requesting", "agent_task_id": arguments.get("task_id")}
    store.put("model_responses", key, record)
    try:
        # Persisted JSON ordering on the first request as well as on replay.
        arguments = json.loads(dumps(arguments))
        encoded = encode_bundle(bundle)
        bundle = decode_bundle(encoded)
        prepared = caller.prepare(method, arguments, context=bundle.fragments)
        manifest = context_manifest(bundle, prepared, caller._renderer.version)
        record.update(request={"messages": list(prepared.request.messages),
                               "response_format": prepared.request.response_format, "model": caller.config.model},
                      context_bundle_id=key, context_bundle_hash=record_hash(encoded), context_manifest_id=key,
                      context_allocation=prepared.allocation.as_dict(),
                      preparation={"method": method, "arguments": arguments})
        with store.transaction():
            store.put("context_bundles", key, encoded)
            store.put("context_manifests", key, {"id": key, **manifest.as_dict()})
            store.put("model_responses", key, record)
        result = caller.send_prepared(prepared)
        # Keep the actual response even when the candidate is rejected below.
        record.update(http_status=result.transport.status_code, raw=result.transport.raw,
                      elapsed_ms=result.transport.elapsed_ms, actual_model=result.actual_model,
                      response_id=result.response_id)
        store.put("model_responses", key, record)
        accepted = accept(json.loads(result.content))
        record.update(status="validated", parsed=accepted)
        store.put("model_responses", key, record)
        return accepted, key
    except Exception as exc:
        if isinstance(exc, ModelCallError) and exc.response is not None:
            record.update(http_status=exc.response.status_code, raw=exc.response.raw,
                          elapsed_ms=exc.response.elapsed_ms)
        record.update(status="rejected", error=type(exc).__name__ + ": " + str(exc))
        store.put("model_responses", key, record)
        raise ContractError(f"{method} rejected; evidence record {key}: {type(exc).__name__}: {str(exc)[:300]}") from exc


class L0Planner:
    """vlm0. v0 shares the model with L1; only the context recipe differs."""

    def __init__(self, store, config, *, caller=None):
        self.store = store
        self.caller = caller or ModelCaller(config)

    def plan(self, task, inputs, *, mode="plan"):
        """Return (TaskPlan, model response id) for the next plan version of ``task``."""
        require(mode in {"plan", "replan"}, "mode must be plan or replan")
        version = task["plan_version"] + 1
        world = WorldState(self.store, task["robot_id"])
        arguments = {"mode": mode, "task_id": task["id"], "plan_version": version, "next_seq": next_seq(task["defs"]),
                     "instruction": task["instruction"], "goal_atoms": goals.describe_atoms()}
        request = ContextRequest(str(uuid.uuid4()), "task_planning", GoalContext.from_input(task["instruction"]),
                                 robot_id=task["robot_id"])
        bundle = ContextBundle(request, fragments(inputs, rule_findings(inputs)))
        states = {sid: s["status"] for sid, s in task["state"].items()}

        def accept(value):
            plan = validate_task_plan(value, task_id=task["id"], plan_version=version,
                                      instruction=task["instruction"], categories=inputs.categories)
            validate_revision(plan, task["defs"], states)
            return enforce_rules(plan, inputs, world)

        return _recorded_call(self.store, self.caller, "task_planning", arguments, bundle, accept)

    def accept(self, task, inputs):
        """Final acceptance: return ({"achieved", "unmet", "reply"}, model response id)."""
        results = []
        for sid in task["order"]:
            spec, state = task["defs"][sid], task["state"][sid]
            results.append({"id": sid, "description": spec["description"], "goal": spec["goal"],
                            "required": spec["required"], "status": state["status"], "result": state["result"]})
        arguments = {"task_id": task["id"], "instruction": task["instruction"],
                     "understanding": task["plan"]["understanding"],
                     "assumptions": task["plan"]["feasibility"]["assumptions"], "results": results}
        request = ContextRequest(str(uuid.uuid4()), "task_acceptance", GoalContext.from_input(task["instruction"]),
                                 robot_id=task["robot_id"])
        context = tuple(f for f in fragments(inputs, rule_findings(inputs))
                        if f.id in {"robot-state", "known-objects", "dialogue"})
        bundle = ContextBundle(request, context)

        def accept(value):
            require(isinstance(value, dict) and set(value) == {"achieved", "unmet", "reply"}
                    and type(value["achieved"]) is bool and isinstance(value["unmet"], list)
                    and all(isinstance(x, str) for x in value["unmet"])
                    and isinstance(value["reply"], str) and value["reply"].strip(),
                    "acceptance needs achieved, unmet and reply")
            return value

        return _recorded_call(self.store, self.caller, "task_acceptance", arguments, bundle, accept)
