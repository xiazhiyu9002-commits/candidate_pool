"""软删除 API：仅 JD 支持软删除；候选人走物理删除（返回 400 提示）。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from kerui_recruit.api.errors import ApiError
from kerui_recruit.api.services import AppServices
from kerui_recruit.soft_delete.service import SoftDeleteService

router = APIRouter(prefix="/api/soft-delete", tags=["soft-delete"])


class SoftDeleteRequest(BaseModel):
    entity_type: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)


@router.post("")
def soft_delete(command: SoftDeleteRequest, request: Request) -> dict:
    if command.entity_type != "jd":
        # 候选人走物理删除（/api/resumes/candidate/{id}），不做软删除。
        raise ApiError(400, "E_SOFT_DELETE_UNSUPPORTED", "仅岗位支持软删除；候选人请使用物理删除")
    services: AppServices = request.app.state.services
    deleted = SoftDeleteService(services.session_factory).soft_delete(command.entity_type, command.entity_id)
    if not deleted:
        raise ApiError(404, "E_JD_NOT_FOUND", "岗位不存在")
    return {"entity_type": command.entity_type, "entity_id": command.entity_id, "deleted": True}


@router.post("/restore")
def restore(command: SoftDeleteRequest, request: Request) -> dict:
    if command.entity_type != "jd":
        raise ApiError(400, "E_SOFT_DELETE_UNSUPPORTED", "仅岗位支持软删除恢复")
    services: AppServices = request.app.state.services
    restored = SoftDeleteService(services.session_factory).restore(command.entity_type, command.entity_id)
    if not restored:
        raise ApiError(404, "E_JD_NOT_FOUND", "岗位不存在")
    return {"entity_type": command.entity_type, "entity_id": command.entity_id, "restored": True}
