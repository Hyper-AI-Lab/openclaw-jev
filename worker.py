import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from app.activities.db_activities import (
    acquire_process_run_lease,
    build_process_memory_context,
    compact_episodic_memory,
    ensure_process_run,
    execute_compensation,
    finalize_task_failure,
    list_process_artifacts,
    promote_completion_memory,
    read_process_memory,
    record_event,
    record_observation,
    record_step,
    register_artifact,
    release_process_run_lease,
    update_process_state,
    update_task_status,
    write_episodic_observation,
    write_process_memory,
)
from app.activities.openclaw_activities import (
    check_intermediate_updates_enabled,
    notify_slack_user,
    parse_agent_evaluation,
    send_to_openclaw,
    validate_openclaw_output,
    verify_response_quality,
)
from app.activities.intake_activities import classify_task_intake_activity
from app.activities.plan_activities import generate_process_plan, save_process_plan
from app.telemetry import get_temporal_client_kwargs, init_telemetry
from app.workflows.catalog_step_child import CatalogStepChildWorkflow
from app.workflows.catalog_task import CatalogTaskWorkflow
from app.workflows.generic_execute_child import GenericExecuteChildWorkflow
from app.workflows.generic_task import GenericTaskWorkflow
from app.workflows.intake_workflow import IntakeWorkflow

logging.basicConfig(level=logging.INFO)


async def main():
    logging.info("Starting OpenClaw RMP Worker...")
    init_telemetry("rmp-worker")
    client = await Client.connect("localhost:7233", **get_temporal_client_kwargs())
    worker = Worker(
        client,
        task_queue="openclaw-tasks",
        workflows=[
            GenericTaskWorkflow,
            CatalogTaskWorkflow,
            CatalogStepChildWorkflow,
            GenericExecuteChildWorkflow,
            IntakeWorkflow,
        ],
        activities=[
            send_to_openclaw,
            validate_openclaw_output,
            parse_agent_evaluation,
            update_task_status,
            notify_slack_user,
            check_intermediate_updates_enabled,
            verify_response_quality,
            ensure_process_run,
            acquire_process_run_lease,
            release_process_run_lease,
            finalize_task_failure,
            execute_compensation,
            update_process_state,
            record_step,
            record_observation,
            record_event,
            write_process_memory,
            read_process_memory,
            build_process_memory_context,
            write_episodic_observation,
            compact_episodic_memory,
            promote_completion_memory,
            register_artifact,
            list_process_artifacts,
            generate_process_plan,
            save_process_plan,
            classify_task_intake_activity,
        ],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
