"""TaskPlan: the L0 planning layer's output, and the subtask lifecycle around it.

Design: "L0 规划层设计.md" §4–§6. L0 writes the plan fields; the task manager
alone writes each subtask's runtime fields (status, result). Nothing here calls
a model or touches the ledger.

    TaskPlan  task_id, plan_version, instruction, understanding, situation,
              grounding, feasibility, subtasks, reply
    subtask   id (t1-02-PER-Q), category, kind, description, goal, depends_on,
              constraints, required, budget, replaces
"""

import copy
import re

from . import goals
from .contracts import ContractError, require

CATEGORIES = {"MOV": "移动", "PER": "感知", "MAN": "操作", "INT": "交互", "MEM": "记忆", "SYS": "系统"}
KINDS = {"A": "执行类", "C": "判别类", "Q": "获取类"}
VERDICTS = ("feasible", "feasible_with_assumptions", "needs_clarification", "infeasible")
CHECK_ITEMS = ("capability", "objects", "constraints", "resources", "ambiguity")
GROUNDING_STATUS = ("known", "findable", "unknown", "satisfied")
TASK_ID = re.compile(r"t\d{1,6}")
SUBTASK_ID = re.compile(r"(t\d{1,6})-(\d{2,4})-(MOV|PER|MAN|INT|MEM|SYS)-([ACQ])")
REF_NAME = re.compile(r"[a-z0-9_]{1,64}")
MAX_SUBTASKS = 16
DEFAULT_BUDGET = {"retries": 1, "timeout_s": 600}

PLAN_FIELDS = ("id", "category", "kind", "description", "goal", "depends_on", "constraints", "required",
               "budget", "replaces")

# Subtask lifecycle (§5.3). Process states, then terminal states.
PROCESS = ("PENDING", "READY", "PLANNING", "RUNNING", "BLOCKED", "MISSING_PREREQ", "RECOVERING")
TERMINAL = frozenset({"SUCCEEDED", "PARTIAL", "FAILED", "SKIPPED", "CANCELED"})
DONE = {"SUCCEEDED", "SKIPPED"}
TRANSITIONS = {
    "PENDING": {"READY", "SKIPPED", "CANCELED"},
    "READY": {"PLANNING", "SKIPPED", "CANCELED"},
    "PLANNING": {"RUNNING", "RECOVERING", "FAILED", "CANCELED"},
    "RUNNING": {"SUCCEEDED", "PARTIAL", "FAILED", "BLOCKED", "MISSING_PREREQ", "RECOVERING", "CANCELED"},
    # Waiting on an external condition; a wait that times out becomes an error.
    "BLOCKED": {"RUNNING", "RECOVERING", "SUCCEEDED", "CANCELED"},
    # A precondition is false: another subtask must make it true first.
    "MISSING_PREREQ": {"READY", "SUCCEEDED", "CANCELED"},
    # The lower layers are retrying or repairing within budget.
    "RECOVERING": {"PLANNING", "RUNNING", "BLOCKED", "SUCCEEDED", "PARTIAL", "FAILED", "CANCELED"},
    **{state: set() for state in TERMINAL},
}


# ---------------------------------------------------------------- capabilities

# Skills declare a category; older specs fall back to their name prefix.
CATEGORY_BY_PREFIX = {"base": "MOV", "nav": "MOV", "perception": "PER", "verify": "PER", "manip": "MAN",
                      "speech": "INT", "interaction": "INT", "hri": "INT", "file": "MEM", "asset": "MEM",
                      "memory": "MEM", "robot": "SYS", "power": "SYS"}


def skill_category(name, spec):
    category = spec.get("category") or CATEGORY_BY_PREFIX.get(name.partition(".")[0])
    return category if category in CATEGORIES else None


def capability_view(catalog, paradigms=()):
    """Skill categories and paradigm skills, without parameter details (§2)."""
    categories = {}
    for name, spec in sorted(catalog.items()):
        category = skill_category(name, spec)
        if category:
            categories.setdefault(category, []).append(
                name + (": " + spec["summary"] if spec.get("summary") else ""))
    return {"categories": {c: categories[c] for c in CATEGORIES if c in categories},
            "paradigms": [dict(p) for p in paradigms]}


def validate_paradigms(paradigms, catalog=None):
    require(isinstance(paradigms, list) and len(paradigms) <= 64, "paradigms must be a list")
    for item in paradigms:
        require(isinstance(item, dict) and set(item) <= {"name", "description", "chain"}
                and isinstance(item.get("name"), str) and item["name"]
                and isinstance(item.get("chain"), list) and item["chain"]
                and all(isinstance(s, str) and s for s in item["chain"]), "paradigm needs name and skill chain")
        require(catalog is None or all(s in catalog for s in item["chain"]),
                f"paradigm {item['name']} uses an unregistered skill")
    return paradigms


# ---------------------------------------------------------------- ids


def subtask_id(task_id, seq, category, kind):
    return f"{task_id}-{seq:02d}-{category}-{kind}"


def parse_id(value):
    match = SUBTASK_ID.fullmatch(value) if isinstance(value, str) else None
    require(match is not None, f"invalid subtask id {value!r}; expected t1-02-PER-Q")
    return {"task_id": match.group(1), "seq": int(match.group(2)), "category": match.group(3),
            "kind": match.group(4)}


def next_seq(ids):
    return max((parse_id(i)["seq"] for i in ids), default=0) + 1


# ---------------------------------------------------------------- validation


def _text(value, name, *, empty=False, limit=2000):
    require(isinstance(value, str) and (empty or value.strip()) and len(value) <= limit, f"{name} must be text")
    return value


def _texts(value, name, *, limit=32):
    require(isinstance(value, list) and len(value) <= limit, f"{name} must be a list")
    for item in value:
        _text(item, name + " item", limit=500)
    return value


def _ref_desc(value, name):
    require(isinstance(value, dict) and set(value) == {"ref", "desc"}, f"{name} needs ref and desc")
    require(isinstance(value["ref"], str) and REF_NAME.fullmatch(value["ref"]), f"{name} ref must be a name")
    _text(value["desc"], name + " desc", limit=500)
    return value


def _understanding(value):
    require(isinstance(value, dict) and {"intent", "targets", "constraints", "ambiguities"} <= set(value)
            <= {"intent", "targets", "recipient", "destination", "constraints", "ambiguities"},
            "understanding needs intent, targets, constraints and ambiguities")
    _text(value["intent"], "intent", limit=64)
    require(isinstance(value["targets"], list) and len(value["targets"]) <= 16, "targets must be a list")
    refs = [_ref_desc(t, "target")["ref"] for t in value["targets"]]
    require(len(set(refs)) == len(refs), "target refs must be unique")
    for key in ("recipient", "destination"):
        if value.get(key) is not None:
            _ref_desc(value[key], key)
    _texts(value["constraints"], "constraints")
    _texts(value["ambiguities"], "ambiguities")
    return value


def _grounding(value, targets):
    require(isinstance(value, list) and len(value) <= 32, "grounding must be a list")
    seen = set()
    for item in value:
        require(isinstance(item, dict) and item.get("status") in GROUNDING_STATUS, "invalid grounding status")
        require(set(item) <= {"ref", "status", "id", "hint", "cond"}, "unknown grounding field")
        require(isinstance(item.get("ref"), str) and REF_NAME.fullmatch(item["ref"]), "grounding ref must be a name")
        require(item["ref"] not in seen, f"{item['ref']} is grounded twice")
        seen.add(item["ref"])
        if item["status"] == "known":
            require(isinstance(item.get("id"), str) and REF_NAME.fullmatch(item["id"]),
                    f"known object {item['ref']} needs its world id")
        if item["status"] == "satisfied":
            require(isinstance(item.get("cond"), str), f"satisfied goal {item['ref']} needs its condition")
            goals.predicate(item["cond"])
        if "hint" in item:
            _text(item["hint"], "grounding hint", limit=500)
    missing = set(targets) - seen
    require(not missing, f"targets without grounding: {sorted(missing)}")
    return value


def _feasibility(value):
    require(isinstance(value, dict) and {"verdict", "checks", "assumptions"} <= set(value)
            <= {"verdict", "checks", "assumptions", "reasons", "alternatives", "rule_overrides"},
            "feasibility needs verdict, checks and assumptions")
    require(value["verdict"] in VERDICTS, f"verdict must be one of {VERDICTS}")
    checks = value["checks"]
    require(isinstance(checks, dict) and set(checks) == set(CHECK_ITEMS)
            and all(v in {"pass", "fail"} for v in checks.values()), f"checks must rate {CHECK_ITEMS} pass|fail")
    _texts(value["assumptions"], "assumptions")
    reasons = value.get("reasons", {})
    require(isinstance(reasons, dict) and set(reasons) <= set(CHECK_ITEMS), "reasons are keyed by check item")
    for reason in reasons.values():
        _text(reason, "reason", limit=500)
    _texts(value.get("alternatives", []), "alternatives", limit=8)
    failed = [k for k, v in checks.items() if v == "fail"]
    verdict = value["verdict"]
    if verdict == "feasible":
        require(not failed and not value["assumptions"], "a feasible plan passes every check without assumptions")
    elif verdict == "feasible_with_assumptions":
        require(not failed and value["assumptions"], "feasible_with_assumptions needs assumptions")
    else:
        require(failed, f"{verdict} needs at least one failed check")
    return value


def _budget(value):
    if value is None:
        return dict(DEFAULT_BUDGET)
    require(isinstance(value, dict) and set(value) <= set(DEFAULT_BUDGET), "budget has retries and timeout_s")
    budget = {**DEFAULT_BUDGET, **value}
    require(type(budget["retries"]) is int and 0 <= budget["retries"] <= 5, "budget retries must be 0..5")
    require(type(budget["timeout_s"]) in (int, float) and 0 < budget["timeout_s"] <= 3600,
            "budget timeout_s must be 0..3600")
    return budget


def normalize_subtask(item, task_id):
    """Fill defaults so a subtask has every plan field; validates field shapes."""
    require(isinstance(item, dict), "subtask must be an object")
    require({"id", "category", "kind", "description", "goal"} <= set(item) <= set(PLAN_FIELDS),
            "subtask needs id, category, kind, description and goal")
    parsed = parse_id(item["id"])
    require(parsed["task_id"] == task_id, f"{item['id']} belongs to another task")
    require(item["category"] in CATEGORIES and item["kind"] in KINDS, "invalid category or kind")
    require((parsed["category"], parsed["kind"]) == (item["category"], item["kind"]),
            f"{item['id']}: id prefix disagrees with category/kind")
    _text(item["description"], "description", limit=500)
    goals.validate_goal(item["goal"], item["kind"])
    depends_on = item.get("depends_on", [])
    require(isinstance(depends_on, list) and len(set(depends_on)) == len(depends_on), "depends_on must be unique ids")
    constraints = _texts(item.get("constraints", []), "constraints", limit=16)
    required = item.get("required", True)
    require(type(required) is bool, "required must be boolean")
    replaces = item.get("replaces")
    require(replaces is None or parse_id(replaces)["task_id"] == task_id, "replaces must name a subtask of this task")
    return {"id": item["id"], "category": item["category"], "kind": item["kind"],
            "description": item["description"].strip(), "goal": copy.deepcopy(item["goal"]),
            "depends_on": list(depends_on), "constraints": list(constraints), "required": required,
            "budget": _budget(item.get("budget")), "replaces": replaces}


def _ancestors(subtasks):
    deps = {s["id"]: set(s["depends_on"]) for s in subtasks}
    closure, visiting = {}, set()

    def visit(sid):
        if sid in closure:
            return closure[sid]
        require(sid not in visiting, f"dependency cycle through {sid}")
        visiting.add(sid)
        found = set()
        for dep in deps[sid]:
            found |= {dep} | visit(dep)
        visiting.discard(sid)
        closure[sid] = found
        return found

    for sid in deps:
        visit(sid)
    return closure


def validate_task_plan(plan, *, task_id, plan_version, instruction, categories):
    """Structural and internal-consistency checks; returns the plan with subtask defaults filled.

    ``categories`` are the skill categories the robot actually has (rule 6).
    """
    require(isinstance(plan, dict), "TaskPlan must be an object")
    if set(plan) == {"error"}:
        raise ContractError(f"model declined to plan: {str(plan['error'])[:500]}")
    fields = {"task_id", "plan_version", "instruction", "understanding", "situation", "grounding", "feasibility",
              "subtasks", "reply"}
    require(set(plan) == fields, f"TaskPlan fields must be exactly {sorted(fields)}")
    require(plan["task_id"] == task_id, "TaskPlan task_id mismatch")
    require(plan["plan_version"] == plan_version, f"plan_version must be {plan_version}")
    require(plan["instruction"] == instruction, "TaskPlan must quote the user instruction unchanged")
    understanding = _understanding(plan["understanding"])
    _text(plan["situation"], "situation")
    grounding = _grounding(plan["grounding"], [t["ref"] for t in understanding["targets"]])
    feasibility = _feasibility(plan["feasibility"])
    reply = _text(plan["reply"], "reply", empty=True)
    require(isinstance(plan["subtasks"], list) and len(plan["subtasks"]) <= MAX_SUBTASKS,
            f"subtasks must be a list of at most {MAX_SUBTASKS}")
    subtasks = [normalize_subtask(item, task_id) for item in plan["subtasks"]]
    ids = [s["id"] for s in subtasks]
    require(len(set(ids)) == len(ids), "duplicate subtask id")
    seqs = [parse_id(i)["seq"] for i in ids]
    require(len(set(seqs)) == len(seqs), "subtask sequence numbers must be unique")
    for s in subtasks:
        require(all(d in ids for d in s["depends_on"]), f"{s['id']} depends on an unknown subtask")
        require(s["id"] not in s["depends_on"], f"{s['id']} depends on itself")
    ancestors = _ancestors(subtasks)
    for s in subtasks:
        outside = goals.refs([s["goal"], s["description"]]) - ancestors[s["id"]]
        require(not outside, f"{s['id']} references {sorted(outside)}, which it does not depend on")
    verdict = feasibility["verdict"]
    executable = verdict in {"feasible", "feasible_with_assumptions"}
    if executable:
        require(not understanding["ambiguities"], "unresolved ambiguities need clarification first")
    else:
        require(not subtasks, f"a {verdict} plan has no subtasks")
        require(reply.strip(), f"a {verdict} plan must tell the user why")
    if understanding["ambiguities"]:
        require(verdict == "needs_clarification" and feasibility["checks"]["ambiguity"] == "fail",
                "listed ambiguities mean the plan needs clarification")
    unavailable = sorted({s["category"] for s in subtasks} - set(categories))
    require(not unavailable, f"subtasks use categories the robot lacks: {unavailable}; report infeasible instead")
    if executable:
        obtained = {goals.parse(goals.atoms_of(s["goal"])[0])[1][0] for s in subtasks if s["kind"] == "Q"}
        for item in grounding:
            if item["status"] in {"findable", "unknown"}:
                require(item["ref"] in obtained,
                        f"{item['ref']} is {item['status']}: add an obtain subtask that finds it first")
    return {**plan, "subtasks": subtasks}


def validate_revision(plan, previous, states):
    """Re-planning rules (§7): ids are immutable, succeeded work is kept, changes get new ids.

    ``previous`` is every subtask definition issued so far (id -> plan fields);
    ``states`` maps those ids to their current status.
    """
    new = {s["id"]: s for s in plan["subtasks"]}
    floor = next_seq(previous)
    for sid, status in states.items():
        if status in DONE and plan["feasibility"]["verdict"] in {"feasible", "feasible_with_assumptions"}:
            require(sid in new, f"{sid} already {status}; keep it in the plan unchanged")
    for sid, item in new.items():
        if sid in previous:
            require(item == previous[sid], f"{sid} changed; give the changed subtask a new id and set replaces")
            require(states.get(sid) not in {"FAILED", "PARTIAL", "CANCELED"},
                    f"{sid} ended {states.get(sid)}; replace it with a new subtask")
        else:
            require(parse_id(sid)["seq"] >= floor, f"{sid}: new subtasks take sequence numbers from {floor:02d}")
        if item["replaces"] is not None:
            require(item["replaces"] in previous and item["replaces"] not in new,
                    f"{sid} replaces {item['replaces']}, which must be an earlier subtask left out of this plan")
    return plan


# ---------------------------------------------------------------- lifecycle and results


def transition(state, status, **fields):
    """Move a subtask's runtime record; only the task manager calls this."""
    require(status in TRANSITIONS.get(state["status"], set()), f"illegal subtask transition {state['status']} -> {status}")
    state.update(status=status, **fields)
    return state


def make_result(sid, outcome, *, goal_progress, value=None, produced=(), evidence=(), error_ref=None,
                duration_s=0.0):
    """The subtask result written once at its end (§5.4)."""
    require(outcome in TERMINAL, "result outcome must be terminal")
    kind = parse_id(sid)["kind"]
    if outcome == "SUCCEEDED" and kind == "C":
        require(isinstance(value, bool), "a check answer is true or false")
    if outcome == "SUCCEEDED" and kind == "Q":
        require(value is not None, "an obtain result needs a value")
    return {"id": sid, "outcome": outcome, "success": 1 if outcome == "SUCCEEDED" else 0,
            "goal_progress": list(goal_progress), "value": value, "produced": list(produced),
            "evidence": list(evidence), "error_ref": error_ref, "duration_s": round(duration_s, 3)}
