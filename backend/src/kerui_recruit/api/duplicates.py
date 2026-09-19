from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from kerui_recruit.api.errors import ApiError
from kerui_recruit.api.services import AppServices


router = APIRouter(prefix="/api/duplicates", tags=["duplicates"])


class MergeDryRunRequest(BaseModel):
    group_id: str = Field(min_length=1)
    primary_candidate_id: str = Field(min_length=1)
    duplicate_candidate_ids: list[str] = Field(min_length=1)


class MergeRequest(BaseModel):
    candidate_ids: list[str] = Field(min_length=2)


@router.get("/report")
def duplicates_report(request: Request) -> dict:
    services: AppServices = request.app.state.services
    if services.duplicates_service is None:
        raise ApiError(503, "E_DUPLICATES_UNAVAILABLE", "重复报告服务未配置")
    report = services.duplicates_service.generate()
    return {
        "summary": report["summary"],
        "csv_path": report["csv_path"],
        "group_count": len(report["groups"]),
    }


@router.post("/merge-dry-run")
def merge_dry_run(command: MergeDryRunRequest, request: Request) -> dict:
    services: AppServices = request.app.state.services
    if services.merge_plan_service is None:
        raise ApiError(503, "E_MERGE_UNAVAILABLE", "合并服务未配置")
    return services.merge_plan_service.plan(
        group_id=command.group_id,
        primary_candidate_id=command.primary_candidate_id,
        duplicate_candidate_ids=command.duplicate_candidate_ids,
    )


@router.get("/identical-groups")
def identical_groups(request: Request) -> dict:
    services: AppServices = request.app.state.services
    if services.merge_plan_service is None:
        raise ApiError(503, "E_MERGE_UNAVAILABLE", "合并服务未配置")
    groups = services.merge_plan_service.find_identical_groups()
    return {"group_count": len(groups), "groups": groups}


@router.post("/merge")
def merge_duplicates(command: MergeRequest, request: Request) -> dict:
    services: AppServices = request.app.state.services
    if services.merge_plan_service is None:
        raise ApiError(503, "E_MERGE_UNAVAILABLE", "合并服务未配置")
    try:
        return services.merge_plan_service.execute_merge(command.candidate_ids)
    except (ValueError, LookupError) as error:
        raise ApiError(422, "E_MERGE_INVALID", str(error)) from error
