from uuid import UUID

from posthog.models.user import User

from products.context_layer.backend.selection_execution import SelectionUnavailable, bounded_request
from products.context_layer.backend.selection_service import (
    SelectionDenied,
    SelectionRunNotFound,
    check_deadline,
    get_selection_run,
    prepare,
)
from products.context_layer.backend.selection_sources import SelectionSources
from products.context_layer.backend.selection_types import (
    MAX_HISTORY_CHARS,
    MAX_PROMPT_CHARS,
    PreparedContext,
    SelectionInput,
)
from products.tasks.backend.facade.api import is_current_task_run_actor

__all__ = [
    "MAX_HISTORY_CHARS",
    "MAX_PROMPT_CHARS",
    "SelectionInput",
    "SelectionDenied",
    "SelectionRunNotFound",
    "SelectionUnavailable",
    "prepare_context_selection",
]


def prepare_context_selection(
    team_id: int,
    run_id: UUID,
    task_id: UUID,
    actor: User,
    selection: SelectionInput,
    authorization: str,
) -> PreparedContext:
    def execute(deadline: float) -> PreparedContext:
        run = get_selection_run(team_id, run_id, task_id, actor)
        check_deadline(deadline)
        result = prepare(run, actor, selection, SelectionSources(team_id, authorization))
        check_deadline(deadline)
        if not is_current_task_run_actor(team_id, run_id, actor.id):
            raise SelectionDenied("The credential no longer belongs to the current actor.")
        return result

    return bounded_request(team_id, 3.5, execute)
