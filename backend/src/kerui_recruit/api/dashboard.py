from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import Response

from kerui_recruit.api.services import AppServices
from kerui_recruit.api.errors import ApiError
from kerui_recruit.dashboard.service import DashboardFilters


router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _filters(
    company: str | None,
    jd_id: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> DashboardFilters:
    try:
        return DashboardFilters(company=company, jd_id=jd_id, date_from=date_from, date_to=date_to)
    except ValueError as error:
        raise ApiError(422, "E_DASHBOARD_FILTER", str(error)) from error


@router.get("/overview")
def overview(
    request: Request,
    company: str | None = None,
    jd_id: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict:
    services: AppServices = request.app.state.services
    return services.dashboard_service.overview(
        _filters(company, jd_id, date_from, date_to)
    )


@router.get("/by-jd")
def by_jd(
    request: Request,
    company: str | None = None,
    jd_id: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[dict]:
    services: AppServices = request.app.state.services
    return services.dashboard_service.by_jd(
        _filters(company, jd_id, date_from, date_to)
    )


@router.get("/trend")
def trend(
    granularity: Literal["week", "month", "quarter"],
    request: Request,
    company: str | None = None,
    jd_id: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[dict]:
    services: AppServices = request.app.state.services
    return services.dashboard_service.trend(
        granularity,
        _filters(company, jd_id, date_from, date_to),
    )


@router.get("/export")
def export(
    request: Request,
    company: str | None = None,
    jd_id: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> Response:
    services: AppServices = request.app.state.services
    content = services.dashboard_service.export(
        _filters(company, jd_id, date_from, date_to)
    )
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="dashboard.xlsx"'},
    )
