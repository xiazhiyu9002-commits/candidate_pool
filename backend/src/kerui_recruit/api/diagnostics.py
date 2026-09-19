from fastapi import APIRouter, Request
from fastapi.responses import Response

from kerui_recruit.api.services import AppServices


router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


@router.get("")
def collect(request: Request) -> dict:
    services: AppServices = request.app.state.services
    return services.diagnostics_service.collect()


@router.get("/export")
def export_json(request: Request) -> Response:
    services: AppServices = request.app.state.services
    data = services.diagnostics_service.export_json()
    return Response(
        content=data,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="diagnostics.json"'},
    )