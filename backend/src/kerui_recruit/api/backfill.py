from uuid import uuid4

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel

from kerui_recruit.api.errors import ApiError
from kerui_recruit.api.services import AppServices
from kerui_recruit.tasks.repository import TaskSpec

router = APIRouter(prefix="/api/backfill", tags=["backfill"])

_KINDS = {
    "school-mappings": "BACKFILL_SCHOOL_MAPPING",
    "candidate-profiles": "BACKFILL_CANDIDATE_PROFILE",
    "jd-profiles": "BACKFILL_JD_PROFILE",
    "reparse-failed": "REPARSE_FAILED",
    "directions": "BACKFILL_DIRECTION",
}


class BackfillRequest(BaseModel):
    entity_type: str | None = None
    force: bool = False


class BackfillResponse(BaseModel):
    task_id: str
    task_type: str


@router.post("/{kind}", response_model=BackfillResponse)
def trigger_backfill(
    kind: str,
    request: Request,
    body: BackfillRequest | None = Body(default=None),
) -> BackfillResponse:
    services: AppServices = request.app.state.services
    task_type = _KINDS.get(kind)
    if task_type is None:
        raise ApiError(404, "E_BACKFILL_NOT_FOUND", "不支持的回填类型")

    payload: dict = {}
    if body is not None:
        if body.entity_type:
            if body.entity_type not in ("candidate", "jd"):
                raise ApiError(422, "E_BACKFILL_INVALID_ARG", "entity_type 仅支持 candidate 或 jd")
            payload["entity_type"] = body.entity_type
        payload["force"] = body.force

    task_id = services.task_repository.enqueue(TaskSpec(
        task_type=task_type,
        queue_name="batch",
        priority=5,
        payload=payload,
        # 每次触发都新建任务；回填内部按实体哈希/缺失字段幂等，重复触发不会重复调用 LLM。
        idempotency_key=f"backfill:{kind}:{uuid4().hex}",
    ))
    return BackfillResponse(task_id=task_id, task_type=task_type)
