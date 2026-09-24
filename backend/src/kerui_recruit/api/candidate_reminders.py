"""候选人提醒接口：在人才库行内建立、在「今日待办」完成。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from kerui_recruit.api.errors import ApiError
from kerui_recruit.api.services import AppServices
from kerui_recruit.reminders.candidate_service import (
    MAX_CONTENT_LENGTH,
    CandidateReminderNotFound,
)

router = APIRouter(prefix="/api/candidate-reminders", tags=["candidate-reminders"])


class CreateReminderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1, max_length=36)
    content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)


class ReminderResponse(BaseModel):
    id: str
    candidate_id: str
    name: str
    content: str
    done: bool


def _service(request: Request):
    services: AppServices = request.app.state.services
    service = services.candidate_reminder_service
    if service is None:
        raise ApiError(503, "E_CANDIDATE_REMINDER_UNAVAILABLE", "候选人提醒服务不可用")
    return service


@router.post("", response_model=ReminderResponse)
def create_reminder(request: Request, body: CreateReminderRequest) -> ReminderResponse:
    service = _service(request)
    try:
        return ReminderResponse(**service.create(candidate_id=body.candidate_id, content=body.content))
    except CandidateReminderNotFound as error:
        raise ApiError(404, "E_CANDIDATE_NOT_FOUND", str(error)) from error
    except ValueError as error:
        raise ApiError(422, "E_REMINDER_INVALID", str(error)) from error


@router.post("/{reminder_id}/done", response_model=ReminderResponse)
def complete_reminder(request: Request, reminder_id: str) -> ReminderResponse:
    """勾选 = 任务完成（与系统项「今天处理过、次日重置」不同）。"""
    service = _service(request)
    try:
        return ReminderResponse(**service.mark_done(reminder_id))
    except CandidateReminderNotFound as error:
        raise ApiError(404, "E_REMINDER_NOT_FOUND", str(error)) from error
