"""DBOS owns durable polling/recovery. A single worker owns each robot ledger."""

import time
from dbos import DBOS, SetWorkflowID
from .store import Store
from .runtime import Runtime, QUIET, RECOVERING
from .contracts import require
from .actions import process_commands
from . import filelock


@DBOS.step(retries_allowed=False)
def advance(root, task_id, generation):
    store = Store(root)
    try:
        process_commands(store, task_id)
        with store.task_lock(task_id):
            task = store.get("tasks", task_id)
            if task["generation"] != generation:
                return "superseded"
            runtime = Runtime(store)
            task = runtime.tick(task_id)
        if task["status"] in {"failed", *RECOVERING}:
            task = runtime.recover_plan(task_id)
        return "superseded" if task["generation"] != generation else task["status"]
    finally:
        store.close()


@DBOS.workflow()
def execute(root, task_id, generation):
    while True:
        status = advance(root, task_id, generation)
        if status in QUIET or status == "superseded":
            return status
        DBOS.sleep(0.2)


@DBOS.step(retries_allowed=False)
def advance_agent_task(root, task_id):
    from .task_manager import TaskManager

    store = Store(root)
    try:
        return TaskManager(store).advance(task_id)["status"]
    finally:
        store.close()


@DBOS.workflow()
def execute_agent_task(root, task_id):
    # Skill plans run in their own workflows; this loop plans, orchestrates, verifies and accepts.
    from .task_manager import FINAL, WAITING

    while True:
        status = advance_agent_task(root, task_id)
        if status in FINAL | WAITING:
            return status
        DBOS.sleep(0.5)


def run(root, until=None, until_agent_task=None):
    store = Store(root)
    worker_lock = open(store.root / "worker.lock", "a+")
    try:
        try:
            filelock.lock(worker_lock, blocking=False)
        except BlockingIOError as exc:
            raise RuntimeError("a worker already owns this ledger") from exc
        if until:
            require(store.get("tasks", until) is not None or any(
                c["task_id"] == until and c["action"] == "task.submit" and c["status"] == "accepted"
                for c in store.list("commands")
            ), "unknown target task")
        DBOS(
            config={
                "name": "robot-agent-framework",
                "application_version": "0.1.0",
                "system_database_url": "sqlite:///"
                + str(store.root / "workflows.sqlite"),
                "run_admin_server": False,
                "enable_otlp": False,
                "log_level": "WARNING",
                "max_executor_threads": 32,
            }
        )
        DBOS.launch()
        started = set()
        while True:
            process_commands(store)
            tasks = sorted(
                store.list("tasks"), key=lambda t: (-t["priority"], t["created_at"])
            )
            for task in tasks:
                if task["status"] in QUIET:
                    continue
                key = f"{task['id']}:{task['generation']}"
                if key not in started:
                    with SetWorkflowID(key):
                        DBOS.start_workflow(
                            execute, str(store.root), task["id"], task["generation"]
                        )
                    started.add(key)
                status = DBOS.get_workflow_status(key)
                if status and status.status == "ERROR":
                    store.event(task["id"], "workflow_error", {"workflow_id": key})
                    raise RuntimeError(
                        f"workflow {key} failed; resource claims retained for reconciliation"
                    )
            from .task_manager import FINAL, WAITING
            for agent_task in store.list("agent_tasks"):
                if agent_task["status"] in FINAL | WAITING:
                    continue
                # Waking a task that waited for a person bumps its generation: a fresh workflow.
                key = f"agent_task:{agent_task['id']}:{agent_task['generation']}"
                if key not in started:
                    with SetWorkflowID(key):
                        DBOS.start_workflow(execute_agent_task, str(store.root), agent_task["id"])
                    started.add(key)
                status = DBOS.get_workflow_status(key)
                if status and status.status == "ERROR":
                    store.event("agent_task:" + agent_task["id"], "workflow_error", {"workflow_id": key})
                    raise RuntimeError(f"task workflow {key} failed; skill plans keep their own state")
            if until_agent_task:
                agent_task = store.get("agent_tasks", until_agent_task)
                require(agent_task is not None, "unknown target task")
                if agent_task["status"] in FINAL | WAITING:
                    return agent_task
            if until:
                task = store.get("tasks", until)
                if task is None:
                    raise RuntimeError("task submission rejected; inspect command receipt")
                if task["status"] in QUIET:
                    return task
            time.sleep(0.1)
    finally:
        # A process shutdown is never presented as a confirmed robot stop.
        DBOS.destroy(workflow_completion_timeout_sec=0)
        worker_lock.close()
        store.close()
