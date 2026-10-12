import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from threading import BoundedSemaphore, Lock
from uuid import UUID, uuid4

from django.conf import settings

import structlog

from posthog.models.user import User
from posthog.ph_client import get_feature_flag_or_none, ph_background_capture

from products.context_layer.backend.selection_model import SelectionJudge
from products.context_layer.backend.selection_search import RenderedContext, render
from products.context_layer.backend.selection_sources import SOURCE_PATHS, SelectionSources
from products.context_layer.backend.selection_types import (
    CONFIG_VERSION,
    GATE_THRESHOLD,
    Candidate,
    PreparedContext,
    SelectionInput,
)
from products.tasks.backend.facade.api import is_current_task_run_actor
from products.tasks.backend.models import Task, TaskRun

logger = structlog.get_logger(__name__)

# Busy processes skip optional context rather than accumulate unbounded work.
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="context-selection")
_CAPACITY = BoundedSemaphore(64)


class SelectionDenied(PermissionError):
    pass


class SelectionRunNotFound(LookupError):
    pass


def get_selection_run(team_id: int, run_id: UUID, task_id: UUID, actor: User) -> TaskRun:
    try:
        run = TaskRun.objects.select_related("task__created_by", "team__organization", "task__team").get(
            id=run_id,
            team_id=team_id,
            task_id=task_id,
        )
    except TaskRun.DoesNotExist as error:
        raise SelectionRunNotFound() from error
    if not is_current_task_run_actor(run.team_id, run.id, actor.id):
        raise SelectionDenied("The credential no longer belongs to the current actor.")
    if run.status != TaskRun.Status.IN_PROGRESS or run.environment != TaskRun.Environment.CLOUD:
        raise SelectionDenied("The cloud run is not active.")
    return run


def selection_mode(run: TaskRun, actor: User) -> str:
    if (
        run.task.runtime != Task.Runtime.ACP
        or (run.state or {}).get("runtime_adapter", "claude") not in ("claude", "codex")
        or run.environment != TaskRun.Environment.CLOUD
        or run.task.origin_product not in (Task.OriginProduct.POSTHOG_AI, Task.OriginProduct.SLACK)
    ):
        return "disabled"
    value = get_feature_flag_or_none(
        "phai-context-selection",
        str(run.task_id),
        groups={"organization": str(run.team.organization_id), "project": str(run.team_id)},
        group_properties={"organization": {"id": str(run.team.organization_id)}, "project": {"id": str(run.team_id)}},
        send_feature_flag_events=False,
    )
    return str(value) if value in ("shadow", "control", "treatment") else "disabled"


def check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("selector_deadline")


def prepare(run: TaskRun, actor: User, selection: SelectionInput, sources: SelectionSources) -> PreparedContext:
    started = time.monotonic()
    selection_id = str(uuid4())
    mode = "disabled"
    context = ""
    reason = "disabled"
    properties = {
        "task_id": str(run.task_id),
        "task_run_id": str(run.id),
        "run_id": str(run.id),
        "message_id": selection.message_id,
    }
    observation: dict[str, object] = {}
    try:
        mode = selection_mode(run, actor)
        if mode == "disabled":
            return PreparedContext()
        deadline = started + settings.CONTEXT_SELECTION_TIMEOUT_SECONDS
        if not selection.prompt.strip():
            reason = "no_text"
        elif mode == "control":
            reason = "control"
        else:
            rendered = _select(actor, selection, sources, selection_id, deadline, properties, observation)
            context = rendered.context
            reason = str(observation["reason"])
    except Exception as error:
        context, reason = "", "error"
        observation["error_type"] = type(error).__name__
    try:
        ph_background_capture()(
            distinct_id=str(actor.distinct_id),
            event="$ai_span",
            properties={
                **properties,
                "$ai_trace_id": selection_id,
                "$ai_span_id": selection_id,
                "$ai_span_name": "Context selection",
                "$ai_product": "posthog_ai",
                "$ai_latency": time.monotonic() - started,
                "$ai_input_state": {"prompt": selection.prompt, "history": selection.history},
                "$ai_output_state": {**observation, "context": context, "mode": mode, "reason": reason},
                "ai_stage": "context_selection",
                "selection_id": selection_id,
                "config_version": CONFIG_VERSION,
                "team_id": run.team_id,
                "runtime_version": selection.runtime_version,
                "history_source": selection.history_source,
                "prompt_char_count": selection.prompt_char_count,
            },
        )
    except Exception:
        logger.exception("context_selection_capture_failed", selection_id=selection_id)
    return PreparedContext(
        selection_id=selection_id, mode=mode, reason=reason, context=context if mode == "treatment" else ""
    )


def _submit[T, **P](operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> Future[T] | None:
    if not _CAPACITY.acquire(blocking=False):
        return None
    try:
        future = _EXECUTOR.submit(operation, *args, **kwargs)
    except Exception:
        _CAPACITY.release()
        raise
    future.add_done_callback(lambda _: _CAPACITY.release())
    return future


def _select(
    actor: User,
    selection: SelectionInput,
    sources: SelectionSources,
    selection_id: str,
    deadline: float,
    properties: dict[str, str],
    observation: dict[str, object],
) -> RenderedContext:
    check_deadline(deadline)
    errors: list[dict[str, str]] = []
    error_lock = Lock()

    def report_error(candidate_id: str, error_type: str) -> None:
        with error_lock:
            errors.append({"candidate_id": candidate_id, "error_type": error_type})
            observation["scorer_errors"] = list(errors)

    observation["scorer_errors"] = []
    judge = SelectionJudge(selection_id, str(actor.distinct_id), deadline, properties, report_error)
    gate = judge.judge(selection.prompt, selection.history)
    observation["gate_probability"] = gate
    if gate is None or gate <= GATE_THRESHOLD:
        observation["reason"] = "gate_error" if gate is None else "gate_skipped"
        return render([], selection_id)
    started = time.monotonic()
    retrieval_deadline = started + max(0, (deadline - started) / 2)
    searches = {}
    for kind in SOURCE_PATHS:
        search_future = _submit(sources.search, kind, selection.prompt, selection.history, retrieval_deadline)
        if search_future is not None:
            searches[search_future] = kind
    search_done, search_unfinished = wait(searches, timeout=max(0, retrieval_deadline - time.monotonic()))
    candidates: list[Candidate] = []
    source_errors: dict[str, str] = {}
    for search_future in search_done:
        try:
            candidates.extend(search_future.result())
        except Exception as error:
            source_errors[searches[search_future]] = type(error).__name__
    for search_future in search_unfinished:
        search_future.cancel()
        source_errors[searches[search_future]] = "TimeoutError"
    observation["source_errors"] = source_errors
    observation["retrieval_seconds"] = time.monotonic() - started
    observation["candidate_count"] = len(candidates)
    check_deadline(deadline)
    # Leave time to pack completed scores even when another candidate times out.
    scoring_deadline = deadline - min(0.25, max(0, deadline - time.monotonic()) / 4)
    judge.deadline = scoring_deadline
    pending = {}
    for candidate in candidates:
        future = _submit(judge.judge, selection.prompt, selection.history, candidate)
        if future is not None:
            pending[future] = candidate
    done, unfinished = wait(pending, timeout=max(0, scoring_deadline - time.monotonic()))
    scored: list[tuple[Candidate, float]] = []
    for future in done:
        try:
            probability = future.result()
            if probability is not None:
                scored.append((pending[future], probability))
        except Exception as error:
            report_error(pending[future].id, type(error).__name__)
    observation["unscored_count"] = len(candidates) - len(scored)
    for future in unfinished:
        future.cancel()
    check_deadline(deadline)
    rendered = render(scored, selection_id)
    observation["decisions"] = rendered.decisions
    observation["selected_ids"] = rendered.selected_ids
    observation["reason"] = "selected" if rendered.selected_ids else "empty"
    return rendered
