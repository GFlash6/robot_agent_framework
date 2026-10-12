"""Goal language for TaskPlan subtasks: checkable atoms with a fixed world-state meaning.

A subtask goal says what must become true (achieve), what must be answered
(check) or what must be found out (obtain), written as atoms such as
``holding(red_cup)``. Every atom compiles to a world-state predicate over the
conventions in embodiment.py, so the task manager verifies a goal against
observed facts instead of a skill's own report. An argument may reference the
result of an earlier subtask: ``holding($t1-02-PER-Q.value.object)``.

    atom      := name "(" [arg {"," arg}] ")"
    arg       := ref | name | key | number | true | false | "text"
    ref       := "$" subtask_id ".value" {"." field}
"""

import json
import re

from .contracts import ContractError, require
from .world_state import KEY, SEGMENT, evaluate

SUBTASK_ID = r"t\d{1,6}-\d{2,4}-(?:MOV|PER|MAN|INT|MEM|SYS)-[ACQ]"
REF = re.compile(r"\$(" + SUBTASK_ID + r")\.value((?:\.[a-z0-9_]{1,64}){0,4})")
_ATOM = re.compile(r"\s*([a-z_][a-z0-9_]{0,63})\s*\((.*)\)\s*")
_NUMBER = re.compile(r"-?\d+(\.\d+)?")
_APPROACH_TOLERANCE_M = 0.3

# name -> argument roles, allowed kinds, obtain type and meaning (shown to the model).
ATOMS = {
    "robot_at": {"args": ("place",), "kinds": "A",
                 "meaning": "robot.pose is within 0.3 m of <place>.approach"},
    "holding": {"args": ("object",), "kinds": "A", "meaning": "robot.holding == <object>"},
    "hands_free": {"args": (), "kinds": "A", "meaning": "robot.holding == \"none\""},
    "standing": {"args": (), "kinds": "A", "meaning": "robot.mobility_ready == true"},
    "object_in": {"args": ("object", "place"), "kinds": "AC",
                  "meaning": "<object>.location is inside <place>.region"},
    "open": {"args": ("entity",), "kinds": "AC", "meaning": "<entity>.open == true"},
    "handed_over": {"args": ("object", "person"), "kinds": "A",
                    "meaning": "<object>.handed_to == <person>"},
    "fact_is": {"args": ("key", "value"), "kinds": "AC", "meaning": "world fact <key> == <value>"},
    "location": {"args": ("object",), "kinds": "Q", "type": "ObjectRef",
                 "meaning": "<object>.location is observed; value {object, location, frame, seen_at}"},
    "fact": {"args": ("key",), "kinds": "Q", "type": "Value",
             "meaning": "world fact <key> is observed; value is its value"},
}
GOAL_KEYS = {"A": "achieve", "C": "check", "Q": "obtain"}
OBTAIN_TYPES = {"ObjectRef", "Value"}


def describe_atoms():
    """Model-facing reference of the goal vocabulary."""
    return {name: {"args": list(spec["args"]), "kinds": list(spec["kinds"]), "meaning": spec["meaning"],
                   **({"type": spec["type"]} if "type" in spec else {})} for name, spec in ATOMS.items()}


def _split_args(text):
    args, current, quoted = [], "", False
    for char in text:
        if char == '"':
            quoted = not quoted
        if char == "," and not quoted:
            args.append(current.strip())
            current = ""
        else:
            current += char
    require(not quoted, "unterminated string in goal atom")
    if current.strip() or args:
        args.append(current.strip())
    return args


def parse(atom):
    """Return (name, [raw args]) for a syntactically valid atom of a known name."""
    require(isinstance(atom, str) and 0 < len(atom) <= 256, "goal atom must be a short string")
    match = _ATOM.fullmatch(atom)
    require(match is not None, f"goal atom must look like name(arg, ...): {atom!r}")
    name, args = match.group(1), _split_args(match.group(2))
    require(name in ATOMS, f"unknown goal atom {name!r}; use one of {sorted(ATOMS)}")
    roles = ATOMS[name]["args"]
    require(len(args) == len(roles), f"{name} takes {len(roles)} argument(s)")
    for role, arg in zip(roles, args):
        if REF.fullmatch(arg):
            continue
        if role == "key":
            require(KEY.fullmatch(arg) is not None, f"{name}: {arg!r} is not a world fact key")
        elif role == "value":
            _literal(arg)
        else:
            require(SEGMENT.fullmatch(arg) is not None, f"{name}: {arg!r} is not an entity name")
    return name, args


def _literal(text):
    if text in {"true", "false"}:
        return text == "true"
    if _NUMBER.fullmatch(text):
        return json.loads(text)
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return text[1:-1]
    require(SEGMENT.fullmatch(text) is not None, f"invalid literal {text!r}")
    return text


def atoms_of(goal):
    """The goal's atoms as a list, whatever its kind."""
    value = goal.get("achieve", goal.get("check", goal.get("obtain")))
    return list(value) if isinstance(value, list) else [value]


def validate_goal(goal, kind):
    """Check the goal object for a subtask kind; returns the goal unchanged."""
    key = GOAL_KEYS[kind]
    allowed = {key, "type"} if kind == "Q" else {key}
    require(isinstance(goal, dict) and key in goal and set(goal) <= allowed,
            f"kind {kind} goal must be {{\"{key}\": ...}}" + (" with type" if kind == "Q" else ""))
    value = goal[key]
    if kind == "A" and isinstance(value, list):
        require(1 <= len(value) <= 8, "achieve takes 1..8 conditions")
    else:
        require(isinstance(value, str), f"{key} must be one goal atom")
    for atom in atoms_of(goal):
        name, _ = parse(atom)
        require(kind in ATOMS[name]["kinds"], f"{name} cannot be used in a kind {kind} goal")
    if kind == "Q":
        name, _ = parse(value)
        expected = ATOMS[name]["type"]
        require(goal.get("type", expected) == expected, f"obtain {name} yields type {expected}")
    return goal


def refs(value):
    """Subtask ids referenced anywhere in a string, list or dict."""
    if isinstance(value, str):
        return {m.group(1) for m in REF.finditer(value)}
    if isinstance(value, dict):
        return set().union(set(), *(refs(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(set(), *(refs(v) for v in value))
    return set()


def resolve(value, results):
    """Substitute $id.value.path references with the referenced subtask's result value."""
    if isinstance(value, dict):
        return {k: resolve(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, results) for v in value]
    if not isinstance(value, str):
        return value

    def substitute(match):
        result = results.get(match.group(1))
        require(result is not None and result.get("outcome") == "SUCCEEDED",
                f"{match.group(1)} has no successful result to reference")
        current = result.get("value")
        for part in [p for p in match.group(2).split(".") if p]:
            require(isinstance(current, dict) and part in current, f"{match.group(0)} not found in result value")
            current = current[part]
        require(isinstance(current, (str, int, float, bool)), f"{match.group(0)} is not a scalar")
        if isinstance(current, str) and not SEGMENT.fullmatch(current):
            return json.dumps(current, ensure_ascii=False)
        return json.dumps(current) if isinstance(current, bool) else str(current)

    return REF.sub(substitute, value)


def predicate(atom):
    """World-state predicate of a resolved atom (no references left)."""
    require(not REF.search(atom), f"unresolved reference in {atom!r}")
    name, args = parse(atom)
    if name == "robot_at":
        return {"fact": "robot.pose", "op": "near", "value": {"$fact": f"{args[0]}.approach"},
                "tolerance": _APPROACH_TOLERANCE_M}
    if name == "holding":
        return {"fact": "robot.holding", "op": "eq", "value": args[0]}
    if name == "hands_free":
        return {"fact": "robot.holding", "op": "eq", "value": "none"}
    if name == "standing":
        return {"fact": "robot.mobility_ready", "op": "eq", "value": True}
    if name == "object_in":
        return {"fact": f"{args[0]}.location", "op": "inside", "value": {"$fact": f"{args[1]}.region"}}
    if name == "open":
        return {"fact": f"{args[0]}.open", "op": "eq", "value": True}
    if name == "handed_over":
        return {"fact": f"{args[0]}.handed_to", "op": "eq", "value": args[1]}
    if name == "fact_is":
        return {"fact": args[0], "op": "eq", "value": _literal(args[1])}
    if name == "location":
        return {"fact": f"{args[0]}.location", "op": "exists"}
    if name == "fact":
        return {"fact": args[0], "op": "exists"}
    raise ContractError(f"no predicate for {name}")


def places(atom):
    """Named places an atom sends the robot or an object to (for policy checks)."""
    name, args = parse(atom)
    return {args[0]} if name == "robot_at" else {args[1]} if name == "object_in" else set()


def check(world, goal, kind, *, subject=None):
    """Evaluate a resolved goal against the world.

    Returns {"state": "met"|"unmet"|"unknown", "progress": [{"cond", "met"}], "value": ...}.
    A check goal is answered ("met") when its predicate is true or false; the
    answer is the value. ``subject`` overrides the object an obtain goal reads,
    e.g. the id a perception skill reported for a placeholder.
    """
    progress, value = [], None
    if kind == "A":
        verdicts = []
        for atom in atoms_of(goal):
            result = evaluate(world, predicate(atom))["result"]
            verdicts.append(result)
            progress.append({"cond": atom, "met": result == "true"})
        state = ("met" if all(v == "true" for v in verdicts) else
                 "unmet" if "false" in verdicts else "unknown")
        return {"state": state, "progress": progress, "value": None}
    atom = atoms_of(goal)[0]
    if kind == "C":
        result = evaluate(world, predicate(atom))["result"]
        answered = result in {"true", "false"}
        return {"state": "met" if answered else "unknown", "progress": [{"cond": atom, "met": answered}],
                "value": (result == "true") if answered else None}
    name, args = parse(atom)
    if name == "location" and subject:
        args = [subject]
        atom = f"location({subject})"
    result = evaluate(world, predicate(atom))["result"]
    if result == "true":
        fact = world.get(f"{args[0]}.location" if name == "location" else args[0])
        value = ({"object": args[0], "location": fact["value"], "frame": fact.get("frame_id"),
                  "seen_at": round(fact["stamp_ns"] / 1e9, 3)} if name == "location" else fact["value"])
    return {"state": "met" if result == "true" else "unknown",
            "progress": [{"cond": atom, "met": result == "true"}], "value": value}
