from fastapi import APIRouter, Request

from kerui_recruit.api.errors import ApiError


router = APIRouter(prefix="/api/search", tags=["search"])


def _service(request: Request):
    service = request.app.state.services.index_sync_service
    if service is None:
        raise ApiError(503, "E_INDEX_SYNC_UNAVAILABLE", "索引同步服务未配置")
    return service


@router.get("/index-status")
def index_status(request: Request) -> dict:
    return _service(request).status()


@router.post("/index-retry")
def index_retry(request: Request) -> dict:
    """Schedule outstanding projections only; never rebuild an existing index."""
    return _service(request).retry_pending()
