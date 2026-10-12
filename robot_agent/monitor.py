"""Execution monitoring between dispatch and a trusted terminal result.

The monitor never stops hardware itself. It returns a finding; the Runtime turns
a finding into a cancel request and keeps resources until the adapter reports a
quiescent terminal state, exactly as for operator cancellation.
"""

import math
import time

from .contracts import require
from .world_state import (WorldState, EVIDENCE_LEVELS, KEY, describe, evaluate, validate_effects,
                          validate_predicate, predicate_args)

FEEDBACK_LIMIT = 4096


def validate_monitoring(spec):
    """Validate the world-facing parts of a SkillSpec."""
    properties = spec.get("input_schema", {}).get("properties")
    for name in ("preconditions", "invariants"):
        if name in spec:
            validate_predicate(spec[name])
            if isinstance(properties, dict):
                missing = predicate_args(spec[name]) - set(properties)
                require(not missing, f"{name} use undeclared arguments: {sorted(missing)}")
    if "effects" in spec:
        validate_effects(spec["effects"])
    for name, upper in (("precondition_timeout", 600), ("effect_timeout", 600)):
        value = spec.get(name, 10)
        require(type(value) in (int, float) and 0 < value <= upper, f"{name} must be 0..{upper} seconds")
    require(type(spec.get("confirm_effects", True)) is bool, "confirm_effects must be boolean")
    monitor = spec.get("monitor", {})
    require(isinstance(monitor, dict) and set(monitor) <= {"stall_s", "min_progress_delta", "invariant_unknown"},
            "invalid monitor block")
    require("stall_s" not in monitor or (type(monitor["stall_s"]) in (int, float) and 0 < monitor["stall_s"] <= 3600),
            "stall_s must be 0..3600 seconds")
    require(type(monitor.get("min_progress_delta", 0.0)) in (int, float)
            and 0 <= monitor.get("min_progress_delta", 0.0) < 1, "min_progress_delta must be 0..1")
    require(monitor.get("invariant_unknown", "stop") in {"stop", "continue"}, "invariant_unknown must be stop|continue")
    compensation = spec.get("compensation")
    require(compensation is None or (isinstance(compensation, dict) and set(compensation) <= {"skill", "args"}
                                     and isinstance(compensation.get("skill"), str) and compensation["skill"]
                                     and isinstance(compensation.get("args", {}), dict)),
            "compensation must be {skill, args}")
    require(spec.get("evidence_level", "software_integration") in EVIDENCE_LEVELS, "invalid evidence level")
    require(isinstance(spec.get("provider", "local"), str), "provider must be a string")


def assess(spec, execution, result, world, now=None):
    """Update ``execution['monitor']`` from one non-terminal response; return a finding or None."""
    now = time.time() if now is None else now
    state = execution.setdefault("monitor", {"progress": None, "progress_at": execution["created_at"]})
    feedback = result.get("feedback")
    if isinstance(feedback, dict):
        import json
        if len(json.dumps(feedback, default=str)) <= FEEDBACK_LIMIT:
            execution["feedback"] = feedback
        progress = feedback.get("progress")
        delta = spec.get("monitor", {}).get("min_progress_delta", 0.0)
        if type(progress) in (int, float) and math.isfinite(progress) and 0 <= progress <= 1 and (
                state["progress"] is None or progress > state["progress"] + delta):
            state.update(progress=progress, progress_at=now)
    stall_s = spec.get("monitor", {}).get("stall_s")
    if stall_s and now - state["progress_at"] > stall_s:
        return {"kind": "stalled", "reason": f"no progress for {now - state['progress_at']:.1f}s (limit {stall_s}s)",
                "progress": state["progress"], "at": now}
    if spec.get("invariants"):
        verdict = evaluate(world, spec["invariants"], execution["args"])
        if verdict["result"] == "false":
            return {"kind": "invariant_violated", "reason": describe(verdict), "atoms": verdict["atoms"], "at": now}
        if verdict["result"] == "unknown" and spec.get("monitor", {}).get("invariant_unknown", "stop") == "stop":
            return {"kind": "invariant_unknown", "reason": describe(verdict), "atoms": verdict["atoms"], "at": now}
    return None


def ingest_observations(store, robot_id, result, *, execution_id, evidence_level, event_task):
    """Record observations an adapter attached to a response. Invalid items are audited, not trusted."""
    items = result.get("observations")
    if items is None:
        return []
    world = WorldState(store, robot_id)
    accepted = []
    if not isinstance(items, list) or len(items) > 256:
        store.event(event_task, "observations_rejected", {"execution_id": execution_id,
                                                          "reason": "observations must be a list of <=256 items"})
        return []
    for item in items:
        try:
            require(isinstance(item, dict) and isinstance(item.get("key"), str) and KEY.fullmatch(item["key"]),
                    "observation needs a valid key")
            require(set(item) <= {"key", "value", "stamp_ns", "frame_id", "clock_domain", "confidence", "ttl_s",
                                  "map_version"} and "value" in item, "invalid observation fields")
            fact = world.observe(item["key"], item["value"], source={"kind": "observation", "ref": execution_id},
                                 stamp_ns=item.get("stamp_ns"), evidence_level=evidence_level,
                                 frame_id=item.get("frame_id"), clock_domain=item.get("clock_domain", "system"),
                                 confidence=item.get("confidence", 1.0), ttl_s=item.get("ttl_s"),
                                 map_version=item.get("map_version"))
            if fact is not None:
                accepted.append(item["key"])
        except Exception as exc:  # noqa: BLE001 - every rejection is audited with its reason
            store.event(event_task, "observation_rejected", {"execution_id": execution_id,
                                                             "error": type(exc).__name__ + ": " + str(exc)})
    return accepted
