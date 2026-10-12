"""World state: time-, frame- and source-qualified facts for planning, monitoring and verification.

A fact is one ``entity.attribute`` value for one robot. Observations come from
perception skills, robot state streams or operators. Skill effects only write
*expected* facts; they become trusted when a later observation confirms them.
Nothing here is inferred from a skill's own success report.
"""

import json
import math
import re
import time

from .contracts import ContractError, field, require

EVIDENCE_LEVELS = {"software_integration", "simulation", "hardware_observation"}
SOURCE_KINDS = {"observation", "operator", "effect"}
VALIDITY = ("valid", "stale", "unavailable", "invalid")
KEY = re.compile(r"[a-z0-9_]{1,64}(\.[a-z0-9_]{1,64}){1,4}")
SEGMENT = re.compile(r"[a-z0-9_]{1,64}")
TEMPLATE = re.compile(r"\{([a-z_][a-z0-9_]{0,63})\}")
ATOM_OPS = {"eq", "ne", "gte", "lte", "in", "exists", "near", "inside"}
MAX_PREDICATE_NODES = 64


def _json(value):
    try:
        encoded = json.dumps(value, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ContractError("world value must be finite JSON") from exc
    require(len(encoded) <= 65536, "world value exceeds 64 KiB")
    return json.loads(encoded)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


class WorldState:
    """Per-robot fact table in the shared ledger; events go to ``world:<robot_id>``."""

    def __init__(self, store, robot_id, *, clock=time.time_ns, max_future_s=1.0):
        require(isinstance(robot_id, str) and robot_id and "/" not in robot_id,
                "robot_id must be one namespace segment")
        self.store = store
        self.robot_id = robot_id
        self.clock = clock
        self.max_future_ns = int(max_future_s * 1e9)

    @property
    def stream(self):
        return "world:" + self.robot_id

    def _id(self, key):
        return self.robot_id + "/" + key

    def meta(self):
        return self.store.get("world_meta", self.robot_id) or {"robot_id": self.robot_id, "map_version": None}

    def observe(self, key, value, *, source, stamp_ns, evidence_level, frame_id=None,
                clock_domain="system", confidence=1.0, ttl_s=None, map_version=None):
        """Record an observation. Returns the stored fact, or None for a late sample."""
        require(isinstance(key, str) and KEY.fullmatch(key), f"invalid world fact key: {key!r}")
        require(isinstance(source, dict) and source.get("kind") in {"observation", "operator"}
                and isinstance(source.get("ref"), str) and source["ref"].strip(),
                "observation source needs kind observation|operator and a nonempty ref")
        require(evidence_level in EVIDENCE_LEVELS, "invalid evidence level")
        require(type(stamp_ns) is int and stamp_ns > 0, "sample stamp_ns must be a positive integer")
        require(_number(confidence) and 0 <= confidence <= 1, "confidence must be within 0..1")
        require(ttl_s is None or (_number(ttl_s) and ttl_s > 0), "ttl_s must be positive")
        require(frame_id is None or (isinstance(frame_id, str) and frame_id), "invalid frame_id")
        require(isinstance(clock_domain, str) and clock_domain, "clock_domain required")
        value = _json(value)
        now = self.clock()
        if stamp_ns > now + self.max_future_ns:
            # A sample from the future means the clocks disagree; audit it outside any rollback.
            self.store.event(self.stream, "observation_rejected",
                             {"key": key, "reason": "sample stamp ahead of receiver clock",
                              "stamp_ns": stamp_ns, "received_ns": now, "source": source})
            raise ContractError("observation stamp is ahead of the receiver clock")
        with self.store.transaction():
            current = self.store.get("world_facts", self._id(key))
            observed = current if current and current["status"] == "observed" else (current or {}).get("last_observed")
            if observed and observed["stamp_ns"] > stamp_ns:
                self.store.event(self.stream, "late_observation_ignored",
                                 {"key": key, "stamp_ns": stamp_ns, "current_stamp_ns": observed["stamp_ns"],
                                  "source": source})
                return None
            fact = {"key": key, "robot_id": self.robot_id, "value": value, "status": "observed",
                    "frame_id": frame_id, "clock_domain": clock_domain, "stamp_ns": stamp_ns,
                    "received_ns": now, "confidence": confidence, "ttl_s": ttl_s, "source": source,
                    "evidence_level": evidence_level, "map_version": map_version, "invalidated": None}
            if current and current["status"] == "expected" and stamp_ns < current["stamp_ns"]:
                # Sampled before the action finished: it can neither confirm nor replace the expectation.
                self.store.put("world_facts", self._id(key), {**current, "last_observed": fact})
                return fact
            if current and current["status"] == "expected":
                same = current["value"] == value
                self.store.event(self.stream, "effect_confirmed" if same else "effect_contradicted",
                                 {"key": key, "expected": current["value"], "observed": value,
                                  "expected_by": current["source"]["ref"], "observation": source})
            elif observed and observed["value"] != value and observed["source"] != source:
                # Keep conflicting sources visible; the newest sample is current.
                self.store.event(self.stream, "observation_conflict",
                                 {"key": key, "previous": {k: observed[k] for k in ("value", "source", "stamp_ns")},
                                  "current": {"value": value, "source": source, "stamp_ns": stamp_ns}})
            self.store.put("world_facts", self._id(key), fact)
        return fact

    def expect(self, key, value, *, execution_id, evidence_level):
        """Record a skill effect as an unconfirmed belief, keeping the last observation."""
        require(isinstance(key, str) and KEY.fullmatch(key), f"invalid world fact key: {key!r}")
        require(evidence_level in EVIDENCE_LEVELS, "invalid evidence level")
        value = _json(value)
        now = self.clock()
        with self.store.transaction():
            current = self.store.get("world_facts", self._id(key))
            last = current if current and current["status"] == "observed" else (current or {}).get("last_observed")
            fact = {"key": key, "robot_id": self.robot_id, "value": value, "status": "expected",
                    "frame_id": (last or {}).get("frame_id"), "clock_domain": "system", "stamp_ns": now,
                    "received_ns": now, "confidence": None, "ttl_s": None,
                    "source": {"kind": "effect", "ref": execution_id}, "evidence_level": evidence_level,
                    "map_version": (last or {}).get("map_version"), "invalidated": None, "last_observed": last}
            self.store.put("world_facts", self._id(key), fact)
            self.store.event(self.stream, "effect_expected", {"key": key, "value": value, "execution_id": execution_id})
        return fact

    def invalidate(self, key, reason):
        require(isinstance(reason, str) and reason, "invalidation reason required")
        with self.store.transaction():
            fact = self.store.get("world_facts", self._id(key))
            if fact is None:
                return None
            fact["invalidated"] = reason
            self.store.put("world_facts", self._id(key), fact)
            self.store.event(self.stream, "fact_invalidated", {"key": key, "reason": reason})
        return fact

    def set_map_version(self, version):
        """A new map or TF baseline makes every spatial fact from the old one need re-observation."""
        require(isinstance(version, str) and version, "map version required")
        affected = []
        with self.store.transaction():
            meta = self.meta()
            previous = meta.get("map_version")
            if previous == version:
                return []
            for fact in self.facts(raw=True):
                if fact.get("map_version") not in (None, version) and not fact.get("invalidated"):
                    fact["invalidated"] = "map_version_changed"
                    self.store.put("world_facts", self._id(fact["key"]), fact)
                    affected.append(fact["key"])
            self.store.put("world_meta", self.robot_id, {**meta, "map_version": version})
            self.store.event(self.stream, "map_version_changed",
                             {"previous": previous, "current": version, "needs_reverification": affected})
        return affected

    def validity(self, fact, now=None):
        if fact is None:
            return "unavailable"
        if fact.get("invalidated"):
            return "invalid"
        now = self.clock() if now is None else now
        if fact.get("ttl_s") is not None and now - fact["stamp_ns"] > fact["ttl_s"] * 1e9:
            return "stale"
        return "valid"

    def get(self, key):
        fact = self.store.get("world_facts", self._id(key))
        return None if fact is None else {**fact, "validity": self.validity(fact)}

    def facts(self, raw=False):
        prefix = self.robot_id + "/"
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT data FROM objects WHERE collection='world_facts' AND id >= ? AND id < ? ORDER BY id",
                (prefix, prefix + "￿")).fetchall()
        facts = [json.loads(row["data"]) for row in rows]
        if raw:
            return facts
        now = self.clock()
        return [{**fact, "validity": self.validity(fact, now)} for fact in facts]

    def load_scene(self, scene, *, source_ref, evidence_level, stamp_ns=None):
        """Register static scene knowledge: {"frame": "map", "objects": {"drawer_1": {"region": {...}}}}.

        Each attribute becomes an operator fact without ttl, e.g. ``drawer_1.region``.
        A region is {"min": [x, y, z], "max": [x, y, z]} in the scene frame.
        """
        require(isinstance(scene, dict) and set(scene) <= {"frame", "map_version", "objects"}
                and isinstance(scene.get("objects"), dict) and scene["objects"], "scene needs objects")
        frame = scene.get("frame")
        stamp_ns = stamp_ns or self.clock()
        keys = []
        for name, attributes in scene["objects"].items():
            require(isinstance(attributes, dict) and attributes, f"scene object {name} needs attributes")
            for attribute, value in attributes.items():
                if attribute == "region":
                    _check_region(value)
                key = f"{name}.{attribute}"
                self.observe(key, value, source={"kind": "operator", "ref": source_ref}, stamp_ns=stamp_ns,
                             evidence_level=evidence_level, frame_id=frame, map_version=scene.get("map_version"))
                keys.append(key)
        return keys

    def snapshot(self):
        """Compact, model-facing view; every value keeps its qualification."""
        now = self.clock()
        return {"robot_id": self.robot_id, "map_version": self.meta().get("map_version"), "as_of_ns": now,
                "facts": {f["key"]: {k: f[k] for k in ("value", "status", "validity", "frame_id", "stamp_ns",
                                                       "confidence", "evidence_level")}
                          | {"age_s": round((now - f["stamp_ns"]) / 1e9, 3)} for f in self.facts()}}


# ---------------------------------------------------------------- predicates
#
#   predicate := {"all": [p, ...]} | {"any": [p, ...]} | {"not": p} | atom
#   atom      := {"fact": "block.{object}.location", "op": eq|ne|gte|lte|in|exists|near|inside,
#                 "value": v | {"$arg": "path"} | {"$fact": "drawer_1.region"},
#                 "tolerance": t (near only), "frame": "map", "min_confidence": c}
#   inside    := value is a region {"min": [...], "max": [...]}; the first len(min) coordinates are compared.
#
# Evaluation is three-valued: "true", "false" or "unknown". A stale, invalid,
# unconfirmed (expected) or too-old fact is "unknown", never "false".


def validate_predicate(predicate, *, depth=0, counter=None):
    counter = counter if counter is not None else [0]
    counter[0] += 1
    require(counter[0] <= MAX_PREDICATE_NODES, "predicate too large")
    require(depth <= 8 and isinstance(predicate, dict) and predicate, "predicate must be a nonempty object")
    if set(predicate) in ({"all"}, {"any"}):
        items = next(iter(predicate.values()))
        require(isinstance(items, list) and 0 < len(items) <= 32, "all/any need 1..32 predicates")
        for item in items:
            validate_predicate(item, depth=depth + 1, counter=counter)
        return predicate
    if set(predicate) == {"not"}:
        validate_predicate(predicate["not"], depth=depth + 1, counter=counter)
        return predicate
    require(set(predicate) <= {"fact", "op", "value", "tolerance", "frame", "min_confidence"},
            "unknown predicate field")
    key = predicate.get("fact")
    require(isinstance(key, str) and KEY.fullmatch(TEMPLATE.sub("x", key)), f"invalid predicate fact: {key!r}")
    op = predicate.get("op")
    require(op in ATOM_OPS, f"unsupported predicate op: {op!r}")
    require(("value" in predicate) == (op != "exists"), "predicate value presence does not match op")
    if op == "near":
        require(_number(predicate.get("tolerance")) and predicate["tolerance"] >= 0, "near needs a tolerance")
    else:
        require("tolerance" not in predicate, "tolerance applies only to near")
    if op == "in":
        require(isinstance(predicate["value"], list) or _is_ref(predicate["value"]), "in needs a list")
    if op == "inside" and not _is_ref(predicate["value"]):
        _check_region(predicate["value"])
    if _is_fact(predicate.get("value")):
        require(KEY.fullmatch(TEMPLATE.sub("x", predicate["value"]["$fact"])),
                f"invalid $fact reference: {predicate['value']['$fact']!r}")
    require("frame" not in predicate or (isinstance(predicate["frame"], str) and predicate["frame"]), "invalid frame")
    require("min_confidence" not in predicate
            or (_number(predicate["min_confidence"]) and 0 <= predicate["min_confidence"] <= 1),
            "invalid min_confidence")
    if "value" in predicate:
        _json(predicate["value"])
        value = predicate["value"]
        require(not isinstance(value, dict) or _is_ref(value) or not ({"$arg", "$fact"} & set(value)),
                "invalid $arg/$fact reference")
    return predicate


def _check_region(region):
    require(isinstance(region, dict) and set(region) == {"min", "max"}
            and isinstance(region["min"], list) and isinstance(region["max"], list)
            and 1 <= len(region["min"]) == len(region["max"]) <= 3
            and all(_number(x) for x in region["min"] + region["max"])
            and all(lo <= hi for lo, hi in zip(region["min"], region["max"])),
            "region must be {min: [...], max: [...]} with 1..3 ordered coordinates")
    return region


def predicate_args(predicate):
    """Argument names a predicate needs, for checking against a skill input schema."""
    if "all" in predicate or "any" in predicate:
        return set().union(*(predicate_args(p) for p in next(iter(predicate.values()))))
    if "not" in predicate:
        return predicate_args(predicate["not"])
    names = set(TEMPLATE.findall(predicate["fact"]))
    if _is_arg(predicate.get("value")):
        names.add(predicate["value"]["$arg"].split(".")[0])
    if _is_fact(predicate.get("value")):
        names |= set(TEMPLATE.findall(predicate["value"]["$fact"]))
    return names


def _is_arg(value):
    return isinstance(value, dict) and set(value) == {"$arg"} and isinstance(value["$arg"], str) and value["$arg"]


def _is_fact(value):
    return isinstance(value, dict) and set(value) == {"$fact"} and isinstance(value["$fact"], str) and value["$fact"]


def _is_ref(value):
    return _is_arg(value) or _is_fact(value)


def resolve_key(template, args):
    def substitute(match):
        value = (args or {}).get(match.group(1))
        require(isinstance(value, str) and SEGMENT.fullmatch(value),
                f"predicate argument {match.group(1)!r} must be a fact-key segment")
        return value
    key = TEMPLATE.sub(substitute, template)
    require(KEY.fullmatch(key), f"invalid resolved fact key: {key!r}")
    return key


def _resolve_value(value, args, output=None):
    if _is_arg(value):
        return field(args or {}, value["$arg"])
    if isinstance(value, dict) and set(value) == {"$output"}:
        return field(output or {}, value["$output"])
    return value


def _compare(op, actual, expected, tolerance=None):
    if op == "exists":
        return True
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if op in {"gte", "lte"}:
        if not (_number(actual) and _number(expected)):
            return False
        return actual >= expected if op == "gte" else actual <= expected
    if op == "in":
        return actual in expected
    if op == "near":
        # Compare the shared leading coordinates, so a pose [x, y, yaw] can be near a point [x, y].
        if not (isinstance(actual, list) and isinstance(expected, list) and actual and expected
                and all(_number(x) for x in actual + expected)):
            return False
        n = min(len(actual), len(expected))
        return math.dist(actual[:n], expected[:n]) <= tolerance
    if op == "inside":
        if not (isinstance(expected, dict) and isinstance(actual, list)
                and len(actual) >= len(expected.get("min", [])) and all(_number(x) for x in actual)):
            return False
        return all(lo <= x <= hi for x, lo, hi in zip(actual, expected["min"], expected["max"]))
    raise ContractError(f"unsupported predicate op: {op}")


def evaluate(world, predicate, args=None, *, since_ns=None, output=None, exclude_source=None):
    """Return {"result": "true"|"false"|"unknown", "atoms": [...]} for a predicate.

    ``exclude_source`` treats facts reported by that execution as unknown, so an
    action cannot confirm its own effects.
    """
    atoms = []
    now = world.clock()

    def visit(node):
        if "all" in node or "any" in node:
            results = [visit(item) for item in next(iter(node.values()))]
            decisive, other = ("false", "true") if "all" in node else ("true", "false")
            if decisive in results:
                return decisive
            return "unknown" if "unknown" in results else other
        if "not" in node:
            inner = visit(node["not"])
            return {"true": "false", "false": "true"}.get(inner, "unknown")
        key = resolve_key(node["fact"], args)
        fact = world.store.get("world_facts", world._id(key))
        validity = world.validity(fact, now)
        record = {"fact": key, "op": node["op"], "validity": validity}
        if fact is None:
            result = "false" if node["op"] == "exists" else "unknown"
        elif validity != "valid" or fact["status"] != "observed":
            result, record["reason"] = "unknown", "unconfirmed effect" if fact["status"] != "observed" else validity
        elif since_ns is not None and fact["stamp_ns"] < since_ns:
            result, record["reason"] = "unknown", "not observed since the action finished"
        elif exclude_source is not None and fact["source"].get("ref") == exclude_source:
            result, record["reason"] = "unknown", "reported only by the acting skill"
        elif "frame" in node and fact.get("frame_id") != node["frame"]:
            result, record["reason"] = "unknown", f"frame {fact.get('frame_id')} differs from {node['frame']}"
        elif "min_confidence" in node and fact["confidence"] < node["min_confidence"]:
            result, record["reason"] = "unknown", "confidence below threshold"
        else:
            value = node.get("value")
            reference = None
            if _is_fact(value):
                ref_key = resolve_key(value["$fact"], args)
                reference = world.store.get("world_facts", world._id(ref_key))
                record["reference"] = ref_key
            if reference is not None and (world.validity(reference, now) != "valid" or reference["status"] != "observed"):
                result, record["reason"] = "unknown", f"reference {record['reference']} is not a valid observation"
            elif _is_fact(value) and reference is None:
                result, record["reason"] = "unknown", f"reference {record['reference']} unavailable"
            elif reference is not None and reference.get("frame_id") and fact.get("frame_id") \
                    and reference["frame_id"] != fact["frame_id"]:
                result, record["reason"] = "unknown", "fact and reference use different frames"
            else:
                expected = reference["value"] if reference is not None else _resolve_value(value, args, output)
                record.update(actual=fact["value"], expected=expected)
                result = "true" if _compare(node["op"], fact["value"], expected, node.get("tolerance")) else "false"
        record["result"] = result
        atoms.append(record)
        return result

    return {"result": visit(predicate), "atoms": atoms}


def describe(verdict):
    """Short human-readable reason for a non-true verdict."""
    bad = [a for a in verdict["atoms"] if a["result"] != "true"]
    return "; ".join(f"{a['fact']} {a['op']} -> {a['result']}" + (f" ({a['reason']})" if a.get("reason") else "")
                     for a in bad[:4]) or verdict["result"]


# ---------------------------------------------------------------- effects
#
#   effects := [{"fact": "{object}.held_by", "value": v | {"$arg": ..} | {"$output": ..}, "tolerance": t}]


def validate_effects(effects):
    require(isinstance(effects, list) and len(effects) <= 32, "effects must be a list of at most 32 items")
    for effect in effects:
        require(isinstance(effect, dict) and set(effect) <= {"fact", "value", "tolerance"}
                and "fact" in effect and "value" in effect, "effect needs fact and value")
        require(isinstance(effect["fact"], str) and KEY.fullmatch(TEMPLATE.sub("x", effect["fact"])),
                f"invalid effect fact: {effect['fact']!r}")
        require("tolerance" not in effect or (_number(effect["tolerance"]) and effect["tolerance"] >= 0),
                "invalid effect tolerance")
    return effects


def effects_predicate(effects):
    """The observation that must follow a successful action for its effects to count."""
    atoms = [{"fact": e["fact"], "op": "near", "value": e["value"], "tolerance": e["tolerance"]}
             if "tolerance" in e else {"fact": e["fact"], "op": "eq", "value": e["value"]} for e in effects]
    return {"all": atoms}


def apply_effects(world, effects, args, output, *, execution_id, evidence_level):
    applied = []
    for effect in effects:
        key = resolve_key(effect["fact"], args)
        value = _resolve_value(effect["value"], args, output)
        world.expect(key, value, execution_id=execution_id, evidence_level=evidence_level)
        applied.append({"fact": key, "value": value})
    return applied
