"""Explicit call methods: each method owns task-specific prompt construction."""

from dataclasses import dataclass
import json

from .contracts import require


@dataclass(frozen=True)
class MethodPrompt:
    system_messages: tuple[dict, ...]
    user_message: dict
    response_format: dict | None = None
    temperature: float | None = None

    @property
    def base_messages(self):
        return self.system_messages + (self.user_message,)


class QuestionAnswerMethod:
    name = "qa"

    def prepare(self, arguments):
        require(isinstance(arguments, dict), "qa arguments must be an object")
        question = arguments.get("question")
        require(isinstance(question, str) and bool(question.strip()), "question required")
        instruction = arguments.get(
            "instruction", "Answer the question accurately and concisely."
        )
        require(isinstance(instruction, str) and bool(instruction), "instruction required")
        response_format = arguments.get("response_format")
        require(
            response_format is None or isinstance(response_format, dict),
            "response_format must be an object",
        )
        return MethodPrompt(
            system_messages=({"role": "system", "content": instruction},),
            user_message={"role": "user", "content": question},
            response_format=response_format,
            temperature=arguments.get("temperature"),
        )


class PlanningMethod:
    name = "planning"
    _instruction = (
        'Return only a JSON object shaped as {"steps":[...],"verification":"step_id"}. Each step has a unique id, skill from the supplied catalog, args, deps, optional retries (0..10) and fallback [{skill,args}]. '
        "verification must be a JSON string containing the id of a final independent goal-checking step that transitively depends on every other step; never return an object in verification. "
        'Use {"$ref":"dependency_id.output_field"} to reference actual direct dependency output fields. '
        'For example, file.ingest step "save" exposes asset_id directly: use {"$ref":"save.asset_id"}, never "save.output.asset_id". '
        "Never invent sensor observations, successful skill results, skill names or paths. Split long goals into an executable dependency DAG. "
        'A step may have "when": a world-state predicate (same form as skill preconditions) checked when the step becomes ready; '
        "if false the step is skipped, and steps that $ref its output are skipped too. The verification step cannot have when. "
        "Respect each skill's preconditions: add the steps that make them true, or a when condition, instead of assuming them. "
        "Unresolved grounding requests are work to perform with registered skills, not known facts. "
        "Include supported observation and verification steps to resolve them; if the available skills cannot do so, return an error. "
        'If the catalog cannot express or verify the goal, return {"error":"reason"}; do not claim success.'
    )

    def prepare(self, arguments):
        require(isinstance(arguments, dict), "planning arguments must be an object")
        goal = arguments.get("goal")
        skills = arguments.get("skills")
        require(isinstance(goal, str) and bool(goal.strip()), "goal required")
        require(isinstance(skills, dict), "skill catalog required")
        return MethodPrompt(
            system_messages=({"role": "system", "content": self._instruction},),
            user_message={
                "role": "user",
                "content": json.dumps(
                    {"goal": goal, "skills": skills}, ensure_ascii=False
                ),
            },
            response_format={"type": "json_object"},
        )


class RepairMethod(PlanningMethod):
    """Middle feedback loop: repair the failed part of a partially executed plan."""

    name = "repair"
    _instruction = PlanningMethod._instruction + (
        " This is a local repair of a partially executed plan; the task-state context lists the previous plan and each step's status. "
        "Return the complete repaired plan. Copy every succeeded step that is still needed unchanged (same id, skill, args, deps, retries and fallback): "
        "the runtime keeps its verified result and does not execute it again. Change or add steps only for the failed part and for steps that depend on it, "
        "and give every changed step a new id. Use the world-state context and each skill's preconditions and effects: "
        "do not rely on an effect that the world state does not confirm, and add an observation step when a needed fact is stale or unavailable."
    )


class TaskPlanningMethod:
    """L0 (vlm0): one user instruction -> TaskPlan of subtasks with checkable goals."""

    name = "task_planning"
    _instruction = (
        "You are L0, the planning layer of a robot agent. Turn one user instruction into a TaskPlan: subtasks with "
        "checkable goals that the orchestration layer (L1) will map to skills. Do not choose skills, fill skill "
        "parameters or control the robot. Work in five steps; each fills one field.\n"
        "1 understanding: intent (e.g. fetch_and_deliver, patrol, question); targets as placeholders "
        '{"ref":"red_cup","desc":"红色杯子"}; recipient and destination {ref, desc} or null; constraints (e.g. 轻拿轻放, '
        "keep out of the bedroom, a time limit); ambiguities = open questions that would change the plan.\n"
        "2 situation: one paragraph on where the robot is, what it holds, what is around it, which places and "
        "objects are known. Use only the supplied state.\n"
        '3 grounding: one entry per target: {"ref","status":"known","id":"<world id>"} | '
        '{"ref","status":"findable","hint":"where to look"} | {"ref","status":"unknown"}; a goal that already holds: '
        '{"ref","status":"satisfied","cond":"<atom>"}.\n'
        '4 feasibility: {"verdict","checks":{"capability","objects","constraints","resources","ambiguity":"pass|fail"},'
        '"assumptions":[...],"reasons":{"<failed check>":"why"},"alternatives":[...]}. verdict: feasible (all pass, no '
        "assumptions) | feasible_with_assumptions (all pass; list every assumption the plan relies on; if one proves "
        "false during execution the task is re-planned) | needs_clarification (ask in reply) | infeasible (say why in "
        "reply; offer alternatives when there are any). The rule_findings context is authoritative: a failed rule "
        "finding is a failed check.\n"
        '5 subtasks, only when feasible, else []: {"id","category","kind","description","goal","depends_on",'
        '"constraints","required","budget"}. id is "<task_id>-<seq, 2 digits>-<category>-<kind>", seq from next_seq '
        "upward in creation order, not execution order. category: MOV move, PER perceive, MAN manipulate, INT "
        "interact, MEM memory, SYS system; use only categories listed in capabilities and report infeasible when one "
        'is missing. kind A achieve: goal {"achieve": atom or [atoms]}; kind C check: goal {"check": atom}, answering '
        'no is still success; kind Q obtain: goal {"obtain": atom, "type": T}. Atoms come only from goal_atoms. One '
        "subtask is one independently checkable stage, usually 1-5 skills. For a findable or unknown object add a Q "
        "subtask that obtains it first and refer to its result as $<id>.value.object in later goals, only from "
        "subtasks you depend on. Do not plan goals that already hold. Copy task-level constraints into each relevant "
        "subtask. depends_on lists only real ordering dependencies. required=false only when failure must not block "
        'the task. budget {"retries":0-5,"timeout_s":seconds} is optional.\n'
        "reply: what to tell the user, in the user's language: a clarifying question, why it is infeasible, or a short "
        "confirmation that work starts.\n"
        "Return only one JSON object with exactly task_id, plan_version, instruction (verbatim), understanding, "
        "situation, grounding, feasibility, subtasks, reply. Never invent observations, object ids or capabilities."
    )
    _replan = (
        "\nThis is re-planning. The history context holds the task tree, finished results, error reports and the "
        "trigger. Re-assess from step 2. Keep every SUCCEEDED or SKIPPED subtask exactly as it was. A subtask that has "
        "not started may stay unchanged or be dropped. A changed or new subtask gets a new id with seq >= next_seq; "
        'when it takes over from an earlier one set "replaces":"<old id>". Never reuse a FAILED, PARTIAL or CANCELED id.'
    )

    def prepare(self, arguments):
        require(isinstance(arguments, dict), "task planning arguments must be an object")
        require(arguments.get("mode") in {"plan", "replan"}, "mode must be plan or replan")
        require(isinstance(arguments.get("instruction"), str) and arguments["instruction"].strip(), "instruction required")
        require(isinstance(arguments.get("goal_atoms"), dict), "goal atom vocabulary required")
        instruction = self._instruction + (self._replan if arguments["mode"] == "replan" else "")
        return MethodPrompt(
            system_messages=({"role": "system", "content": instruction},),
            user_message={"role": "user", "content": json.dumps(arguments, ensure_ascii=False)},
            response_format={"type": "json_object"},
            temperature=0,
        )


class TaskAcceptanceMethod:
    """L0 final acceptance: did the finished subtasks achieve what the user asked for?"""

    name = "task_acceptance"
    _instruction = (
        "You are L0 doing final acceptance for a robot task. Compare the user's instruction and the plan's "
        "understanding with the subtask results (observed outcomes, values and goal progress). Decide whether the "
        'overall goal was achieved. Return only one JSON object {"achieved": true|false, "unmet": ["..."], '
        '"reply": "..."}. reply is what to tell the user, in their language; mention optional subtasks that failed. '
        "Judge only from the supplied results; never claim an outcome they do not show."
    )

    def prepare(self, arguments):
        require(isinstance(arguments, dict) and isinstance(arguments.get("results"), list),
                "acceptance needs the subtask results")
        return MethodPrompt(
            system_messages=({"role": "system", "content": self._instruction},),
            user_message={"role": "user", "content": json.dumps(arguments, ensure_ascii=False)},
            response_format={"type": "json_object"},
            temperature=0,
        )


class GoalAnalysisMethod:
    name = "goal_analysis"
    _instruction = (
        "Analyze the user's robot goal before planning. Return only one JSON object. "
        "Preserve uncertainty: never invent object identities, poses, observations, user preferences, or robot capabilities. "
        "Separate explicit meaning from ambiguities, missing information, assumptions, information the robot can ground through sensing, and information that requires user clarification. "
        "Use exactly these fields: interpreted_intent (string), entities (array of objects), relations (array of objects), constraints (array of strings), completion_criteria (array of strings), ambiguities (array of strings), missing_information (array of strings), assumptions (array of strings), grounding_requests (array of strings), clarification_requests (array of strings)."
    )

    def prepare(self, arguments):
        require(isinstance(arguments, dict), "goal analysis arguments must be an object")
        original_input = arguments.get("original_input")
        require(
            isinstance(original_input, str) and bool(original_input.strip()),
            "original goal input required",
        )
        conversation = arguments.get("conversation", [])
        require(isinstance(conversation, list), "conversation context must be a list")
        return MethodPrompt(
            system_messages=({"role": "system", "content": self._instruction},),
            user_message={
                "role": "user",
                "content": json.dumps(
                    {"original_input": original_input, "conversation": conversation},
                    ensure_ascii=False,
                ),
            },
            response_format={"type": "json_object"},
            temperature=0,
        )


class ClarificationMethod:
    name = "goal_clarification"
    _instruction = (
        "Generate concise clarification questions for an unresolved robot goal. Return only one JSON object shaped as "
        '{"questions":[{"field":"...","question":"...","reason":"..."}]}. '
        "Ask only questions that require the user; do not ask for information the robot can obtain through sensing or tools. "
        "Prefer one question and never return more than three. Do not answer the questions or modify the goal."
    )

    def prepare(self, arguments):
        require(isinstance(arguments, dict), "clarification arguments must be an object")
        goal_context = arguments.get("goal_context")
        require(isinstance(goal_context, dict), "goal context required")
        return MethodPrompt(
            system_messages=({"role": "system", "content": self._instruction},),
            user_message={
                "role": "user",
                "content": json.dumps(goal_context, ensure_ascii=False),
            },
            response_format={"type": "json_object"},
            temperature=0,
        )


DEFAULT_METHODS = {
    QuestionAnswerMethod.name: QuestionAnswerMethod(),
    PlanningMethod.name: PlanningMethod(),
    RepairMethod.name: RepairMethod(),
    TaskPlanningMethod.name: TaskPlanningMethod(),
    TaskAcceptanceMethod.name: TaskAcceptanceMethod(),
    GoalAnalysisMethod.name: GoalAnalysisMethod(),
    ClarificationMethod.name: ClarificationMethod(),
}
