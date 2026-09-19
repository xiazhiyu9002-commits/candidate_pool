from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from kerui_recruit.api.services import AppServices
from kerui_recruit.api.errors import ApiError
from kerui_recruit.correction.service import CorrectionConflict
from kerui_recruit.db.models import CorrectionLog


router = APIRouter(prefix="/api/correction", tags=["correction"])


class ApplyCorrectionRequest(BaseModel):
    entity_type: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    field_name: str = Field(min_length=1)
    new_value: str | None = None
    reason: str | None = None


class CorrectionResponse(BaseModel):
    correction_id: str
    entity_type: str
    field_name: str
    old_value: str | None
    new_value: str | None
    reverted: bool


@router.post("/apply", response_model=CorrectionResponse)
def apply_correction(command: ApplyCorrectionRequest, request: Request) -> CorrectionResponse:
    services: AppServices = request.app.state.services
    try:
        log = services.correction_service.apply_correction(
            entity_type=command.entity_type,
            entity_id=command.entity_id,
            field_name=command.field_name,
            new_value=command.new_value,
            reason=command.reason,
        )
    except (ValueError, LookupError) as error:
        raise _api_error(error) from error
    return CorrectionResponse(
        correction_id=log.id,
        entity_type=log.entity_type,
        field_name=log.field_name,
        old_value=log.old_value,
        new_value=log.new_value,
        reverted=log.reverted,
    )


@router.post("/{correction_id}/undo", response_model=CorrectionResponse)
def undo_correction(correction_id: str, request: Request) -> CorrectionResponse:
    services: AppServices = request.app.state.services
    try:
        log = services.correction_service.undo_correction(correction_id)
    except (ValueError, LookupError) as error:
        raise _api_error(error) from error
    return CorrectionResponse(
        correction_id=log.id,
        entity_type=log.entity_type,
        field_name=log.field_name,
        old_value=log.old_value,
        new_value=log.new_value,
        reverted=log.reverted,
    )


def _api_error(error: Exception) -> ApiError:
    if isinstance(error, CorrectionConflict):
        return ApiError(409, error.code, str(error))
    if isinstance(error, LookupError):
        return ApiError(404, "E_CORRECTION_NOT_FOUND", "更正记录或实体不存在")
    return ApiError(422, "E_INVALID_CORRECTION", str(error))
