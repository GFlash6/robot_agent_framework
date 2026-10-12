"""Embodiment abstraction: abstract skills, per-body profiles and per-track bindings.

    ABSTRACT_SKILLS   what a skill means, independent of any body: schemas,
                      preconditions, effects, invariants and monitoring.
    Embodiment Profile what one body has: which abstract skills it supports,
                      its resources, frames and safety limits.
    Track binding     where those skills execute for one track (simulation or
                      hardware): provider, endpoint, evidence level, fencing.

``install`` composes the three into ordinary SkillSpecs. L0, L1, the Runtime
and the monitor only ever see abstract skill names (L0 only their categories),
so adding a body means adding a profile and an adapter, not changing the Agent
layer.

World-state conventions used by the abstract contracts:
    robot.estop           bool   hardware or software stop latched
    robot.mobility_ready  bool   body can accept locomotion commands
                                 (legged: standing and balanced; wheeled: drives enabled)
    robot.pose            [x, y, yaw] in the profile's world frame
    robot.holding         object id, or "none"
    robot.battery         charge as a fraction 0..1
    <object>.location     [x, y, z] in the world frame
    <object>.handed_to    who received the object from the robot
    <entity>.open         bool, e.g. a door or drawer
    <place>.approach      [x, y, yaw] pose from which the robot works at a place
    <place>.region        {"min": [...], "max": [...]} extent of a place or container

The goal atoms L0 plans with (goals.py) are defined over these keys.
"""

import copy
import json

from .contracts import require
from .context_memory import record_hash
from .world_state import EVIDENCE_LEVELS

PROVIDERS = {"isaaclab", "mujoco", "gazebo", "ros2", "unitree_sdk", "http", "benchmark"}
TRACKS = {"simulation": "simulation", "hardware": "hardware_observation"}

_NOT_STOPPED = {"fact": "robot.estop", "op": "eq", "value": False}
_READY = {"all": [_NOT_STOPPED, {"fact": "robot.mobility_ready", "op": "eq", "value": True}]}
_VECTOR3 = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}
_OBJECT_ID = {"type": "string", "pattern": "^[a-z0-9_]{1,64}$"}

ABSTRACT_SKILLS = {
    "robot.observe_state": {
        "category": "SYS",
        "summary": "Read the robot state stream once and report it as observations.",
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {}},
        "output_schema": {"type": "object", "required": ["mobility_ready", "estop"],
                          "properties": {"mobility_ready": {"type": "boolean"}, "estop": {"type": "boolean"},
                                         "pose": _VECTOR3}},
        "checks": [], "cancelable": True, "replay_safe": True, "verifier": True,
    },
    "robot.stand": {
        "category": "SYS",
        "summary": "Bring a legged body to a balanced stand.",
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {}},
        "output_schema": {"type": "object"},
        "checks": [], "cancelable": True, "replay_safe": True,
        "preconditions": _NOT_STOPPED,
        "effects": [{"fact": "robot.mobility_ready", "value": True}],
        "monitor": {"stall_s": 20},
    },
    "robot.stop": {
        "category": "SYS",
        "summary": "Controller-level safe stop of all motion. Not a hardware e-stop.",
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {}},
        "output_schema": {"type": "object"},
        "checks": [], "cancelable": False, "replay_safe": True,
    },
    "base.move_velocity": {
        "category": "MOV",
        "summary": "Track a body-frame velocity for a bounded duration, then stop.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["vx", "vy", "wz", "duration_s"],
                         "properties": {"vx": {"type": "number"}, "vy": {"type": "number"}, "wz": {"type": "number"},
                                        "duration_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 10}}},
        "output_schema": {"type": "object"},
        "checks": [], "cancelable": True, "replay_safe": False,
        "preconditions": _READY, "invariants": _NOT_STOPPED,
    },
    "base.navigate_to": {
        "category": "MOV",
        "summary": "Navigate to a pose in the world frame and stop there.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["target"],
                         "properties": {"target": _VECTOR3, "max_speed": {"type": "number", "exclusiveMinimum": 0}}},
        "output_schema": {"type": "object"},
        "checks": [], "cancelable": True, "replay_safe": True,
        "preconditions": _READY, "invariants": _NOT_STOPPED,
        "effects": [{"fact": "robot.pose", "value": {"$arg": "target"}, "tolerance": 0.3}],
        "monitor": {"stall_s": 15, "min_progress_delta": 0.01},
    },
    "perception.locate_object": {
        "category": "PER",
        "summary": "Find an object and report its location as an observation.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["object"],
                         "properties": {"object": _OBJECT_ID}},
        "output_schema": {"type": "object", "required": ["found"],
                          "properties": {"found": {"type": "boolean"}, "location": _VECTOR3}},
        "checks": [{"path": "found", "op": "eq", "value": True}],
        "cancelable": True, "replay_safe": True, "verifier": True,
    },
    "manip.pick": {
        "category": "MAN",
        "summary": "Grasp an object whose location is known.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["object"],
                         "properties": {"object": _OBJECT_ID}},
        "output_schema": {"type": "object"},
        "checks": [], "cancelable": True, "replay_safe": False,
        "preconditions": {"all": [_READY, {"fact": "robot.holding", "op": "eq", "value": "none"},
                                  {"fact": "{object}.location", "op": "exists"}]},
        "invariants": _NOT_STOPPED,
        "effects": [{"fact": "robot.holding", "value": {"$arg": "object"}}],
        "compensation": {"skill": "manip.retract", "args": {}},
    },
    "manip.retract": {
        "category": "MAN",
        "summary": "Move the arm to its safe stowed pose without releasing what it holds.",
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {}},
        "output_schema": {"type": "object"},
        "checks": [], "cancelable": True, "replay_safe": True,
        "preconditions": _NOT_STOPPED,
    },
    "manip.place": {
        "category": "MAN",
        "summary": "Place the held object at a location in the world frame.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["object", "target"],
                         "properties": {"object": _OBJECT_ID, "target": _VECTOR3}},
        "output_schema": {"type": "object"},
        "checks": [], "cancelable": True, "replay_safe": False,
        "preconditions": {"all": [_READY, {"fact": "robot.holding", "op": "eq", "value": {"$arg": "object"}}]},
        "invariants": {"all": [_NOT_STOPPED, {"fact": "robot.holding", "op": "eq", "value": {"$arg": "object"}}]},
        "monitor": {"invariant_unknown": "stop"},
        "effects": [{"fact": "robot.holding", "value": "none"},
                    {"fact": "{object}.location", "value": {"$arg": "target"}, "tolerance": 0.05}],
        "compensation": {"skill": "manip.retract", "args": {}},
    },
    "verify.object_at": {
        "category": "PER",
        "summary": "Independently observe whether an object is within tolerance of a location.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["object", "target", "tolerance"],
                         "properties": {"object": _OBJECT_ID, "target": _VECTOR3,
                                        "tolerance": {"type": "number", "exclusiveMinimum": 0, "maximum": 1}}},
        "output_schema": {"type": "object", "required": ["at_target", "distance"],
                          "properties": {"at_target": {"type": "boolean"}, "distance": {"type": "number"}}},
        "checks": [{"path": "at_target", "op": "eq", "value": True}],
        "cancelable": True, "replay_safe": True, "verifier": True,
    },
}

# Safety limits a profile may declare, and the schema bound each one enforces.
LIMIT_BOUNDS = {
    "max_linear_mps": [("base.move_velocity", "vx"), ("base.move_velocity", "vy"), ("base.navigate_to", "max_speed")],
    "max_angular_rps": [("base.move_velocity", "wz")],
}


def validate_profile(profile):
    require(isinstance(profile, dict) and profile.get("schema_version") == 1, "unsupported embodiment profile")
    require(set(profile) <= {"schema_version", "embodiment", "description", "frames", "clock_domain", "resources",
                             "skills", "safety_limits", "notes"}, "unknown embodiment profile field")
    require(isinstance(profile.get("embodiment"), str) and profile["embodiment"], "embodiment name required")
    frames = profile.get("frames")
    require(isinstance(frames, dict) and isinstance(frames.get("world"), str) and frames["world"],
            "profile frames.world required")
    resources = profile.get("resources")
    require(isinstance(resources, dict) and resources
            and all(isinstance(k, str) and k and "/" not in k and type(v) is int and v > 0 for k, v in resources.items()),
            "profile resources must map names to positive capacities")
    skills = profile.get("skills")
    require(isinstance(skills, dict) and skills, "profile skills required")
    for name, entry in skills.items():
        require(name in ABSTRACT_SKILLS, f"profile names unknown abstract skill: {name}")
        require(isinstance(entry, dict) and set(entry) <= {"resources", "control_domain"},
                f"invalid profile entry for {name}")
        for resource in entry.get("resources", {}):
            require(resource in resources, f"{name} uses undeclared resource {resource}")
        domain = entry.get("control_domain")
        require(domain is None or resources.get(domain) == 1,
                f"{name} control domain must be a declared resource with capacity 1")
    limits = profile.get("safety_limits", {})
    require(isinstance(limits, dict), "safety_limits must be an object")
    for key, value in limits.items():
        require(key in LIMIT_BOUNDS or key == "calibrated", f"unknown safety limit: {key}")
        require(key == "calibrated" or (type(value) in (int, float) and value > 0), f"invalid safety limit: {key}")
    require(type(limits.get("calibrated", False)) is bool, "safety_limits.calibrated must be boolean")
    return profile


def validate_binding(binding, profile):
    require(isinstance(binding, dict) and binding.get("schema_version") == 1, "unsupported track binding")
    require(set(binding) <= {"schema_version", "embodiment", "track", "provider", "adapter", "endpoint", "token_env",
                             "timeout", "skills", "notes"}, "unknown binding field")
    adapter = binding.get("adapter", "http")
    require(adapter in {"http", "inprocess"}, "binding adapter must be http or inprocess")
    require(binding.get("embodiment") == profile["embodiment"], "binding is for a different embodiment")
    require(binding.get("track") in TRACKS, "track must be simulation or hardware")
    require(binding.get("provider") in PROVIDERS, f"provider must be one of {sorted(PROVIDERS)}")
    require(binding["track"] == "hardware" or binding["provider"] not in {"unitree_sdk"},
            "a vendor hardware SDK cannot back the simulation track")
    skills = binding.get("skills")
    require(isinstance(skills, dict) and skills, "binding skills required")
    for name, entry in skills.items():
        require(name in profile["skills"], f"binding names a skill the profile does not support: {name}")
        require(isinstance(entry, dict)
                and set(entry) <= {"endpoint", "handler", "fencing", "timeout", "execution_timeout"},
                f"invalid binding entry for {name}")
        require(adapter == "inprocess" or entry.get("endpoint", binding.get("endpoint")), f"{name} needs an endpoint")
        require(type(entry.get("fencing", False)) is bool, "fencing must be boolean")
        require(adapter == "http" or not entry.get("fencing"), "fencing requires an HTTP endpoint")
        compensation = ABSTRACT_SKILLS[name].get("compensation")
        require(compensation is None or compensation["skill"] in skills,
                f"{name} needs its compensation skill {compensation and compensation['skill']} bound too")
        require(not entry.get("fencing") or profile["skills"][name].get("control_domain"),
                f"{name} fencing needs a profile control_domain")
    return binding


def compose(name, profile, binding, robot_id="robot"):
    """One concrete SkillSpec for an abstract skill on one body and track."""
    abstract = copy.deepcopy(ABSTRACT_SKILLS[name])
    entry = profile["skills"][name]
    bound = binding["skills"][name]
    adapter = binding.get("adapter", "http")
    target = ({"endpoint": bound.get("endpoint", binding.get("endpoint"))} if adapter == "http"
              else {"handler": f"{robot_id}/{bound.get('handler', name)}"})
    spec = {
        **abstract,
        "adapter": adapter,
        **target,
        "resources": dict(entry.get("resources", {})),
        "provider": binding["provider"],
        "evidence_level": TRACKS[binding["track"]],
        "embodiment": profile["embodiment"],
        "timeout": bound.get("timeout", binding.get("timeout", 10)),
        "execution_timeout": bound.get("execution_timeout", 300),
    }
    if binding.get("token_env"):
        spec["token_env"] = binding["token_env"]
    domain = entry.get("control_domain")
    if domain:
        spec["resources"][domain] = 1
        if bound.get("fencing"):
            spec["fencing_domain"] = domain
    for limit, targets in LIMIT_BOUNDS.items():
        value = profile.get("safety_limits", {}).get(limit)
        for skill, prop in targets:
            if value is not None and skill == name:
                schema = spec["input_schema"]["properties"][prop]
                schema["maximum"] = value
                if prop != "max_speed":
                    schema["minimum"] = -value
    return spec


def install(runtime, profile, binding, robot_id):
    """Register a body's bound skills and resource capacities for one robot."""
    validate_profile(profile)
    validate_binding(binding, profile)
    require(binding["track"] in TRACKS and TRACKS[binding["track"]] in EVIDENCE_LEVELS, "invalid track")
    # The skill catalog is ledger-wide (one ledger per robot); two bodies would overwrite each other's skills.
    others = [e for e in runtime.store.list("embodiments") if e["robot_id"] != robot_id]
    require(not others, f"ledger already serves robot {others[0]['robot_id'] if others else ''}; use one ledger per robot")
    specs = {name: compose(name, profile, binding, robot_id) for name in binding["skills"]}
    for resource, capacity in profile["resources"].items():
        runtime.store.set_capacity(f"{robot_id}/{resource}", capacity)
    previous = runtime.store.get("embodiments", robot_id)
    with runtime.store.transaction():
        # Switching tracks must not leave the old track's skills callable (e.g. a sim endpoint on hardware).
        for name in set((previous or {}).get("skills", [])) - set(specs):
            runtime.store.db.execute("DELETE FROM objects WHERE collection='skills' AND id=?", (name,))
        for name, spec in specs.items():
            runtime.register(name, spec)
    record = {"robot_id": robot_id, "embodiment": profile["embodiment"], "track": binding["track"],
              "provider": binding["provider"], "evidence_level": TRACKS[binding["track"]],
              "profile_hash": record_hash(profile), "binding_hash": record_hash(binding),
              "skills": sorted(specs), "unbound_profile_skills": sorted(set(profile["skills"]) - set(specs)),
              "frames": profile["frames"], "safety_limits": profile.get("safety_limits", {})}
    runtime.store.put("embodiments", robot_id, record)
    runtime.store.event("world:" + robot_id, "embodiment_installed",
                        {k: record[k] for k in ("embodiment", "track", "provider", "skills", "profile_hash")})
    return record


def describe_skills():
    """Abstract skill catalog for documentation and tools."""
    return json.loads(json.dumps({name: {k: v for k, v in spec.items() if k != "checks"}
                                  for name, spec in ABSTRACT_SKILLS.items()}))
