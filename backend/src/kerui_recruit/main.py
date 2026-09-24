from __future__ import annotations

import logging
from collections.abc import Callable
from typing import AsyncContextManager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

Lifespan = Callable[[FastAPI], AsyncContextManager[None]]


def create_app(
    services: AppServices | None = None,
    *,
    lifespan: Lifespan | None = None,
) -> FastAPI:
    app = FastAPI(title="KeRui Recruit", version="0.1.0", lifespan=lifespan)
    if services is not None:
        app.state.services = services

    # 延迟 import：避免 import kerui_recruit.main 时一次性加载全部 router 的重依赖。
    from kerui_recruit.api.auth import valid_session
    from kerui_recruit.api.ai_settings import router as ai_settings_router
    from kerui_recruit.api.backfill import router as backfill_router
    from kerui_recruit.api.backup import router as backup_router
    from kerui_recruit.api.bd_agent import router as bd_agent_router
    from kerui_recruit.api.bd_search import router as bd_search_router
    from kerui_recruit.api.cases import router as cases_router
    from kerui_recruit.api.candidate_reminders import router as candidate_reminders_router
    from kerui_recruit.api.correction import router as correction_router
    from kerui_recruit.api.daily_followup import router as daily_followup_router
    from kerui_recruit.api.dashboard import router as dashboard_router
    from kerui_recruit.api.diagnostics import router as diagnostics_router
    from kerui_recruit.api.duplicates import router as duplicates_router
    from kerui_recruit.api.errors import ApiError
    from kerui_recruit.api.jd import router as jd_router
    from kerui_recruit.api.indexes import router as indexes_router
    from kerui_recruit.api.mapping import router as mapping_router
    from kerui_recruit.api.match import router as match_router
    from kerui_recruit.api.migration import router as migration_router
    from kerui_recruit.api.onboarding import router as onboarding_router
    from kerui_recruit.api.org import router as org_router
    from kerui_recruit.api.reminders import router as reminders_router
    from kerui_recruit.api.resumes import router as resumes_router
    from kerui_recruit.api.search import router as search_router
    from kerui_recruit.api.services import AppServices
    from kerui_recruit.api.settings import router as settings_router
    from kerui_recruit.api.schools import router as schools_router
    from kerui_recruit.api.soft_delete import router as soft_delete_router
    from kerui_recruit.api.tasks import router as tasks_router
    from kerui_recruit.health.service import HealthService
    from kerui_recruit.resumes.ingest import UnsupportedResumeType
    from kerui_recruit.providers.errors import ProviderError
    from kerui_recruit.match.service import MatchEligibilityError, ReverseMatchUnavailableError
    from kerui_recruit.cases.service import CaseStateError

    @app.middleware("http")
    async def local_session_guard(request: Request, call_next):
        request_id = str(uuid4())
        request.state.request_id = request_id
        if request.url.path.startswith("/api/"):
            if services is None or not valid_session(request, services):
                return JSONResponse(
                    status_code=401,
                    content={
                        "code": "E_LOCAL_SESSION",
                        "message": "本地会话无效，请重新启动应用",
                        "request_id": request_id,
                        "details": None,
                    },
                )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, error: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={
                "code": error.code,
                "message": error.message,
                "request_id": request.state.request_id,
                "details": error.details,
            },
        )

    @app.exception_handler(UnsupportedResumeType)
    async def handle_unsupported_resume(
        request: Request,
        error: UnsupportedResumeType,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=415,
            content={
                "code": error.code,
                "message": "仅支持 PDF、DOC 和 DOCX 简历",
                "request_id": request.state.request_id,
                "details": None,
            },
        )

    @app.exception_handler(MatchEligibilityError)
    async def handle_match_conflict(request: Request, error: MatchEligibilityError) -> JSONResponse:
        return await handle_api_error(request, ApiError(409, "E_MATCH_NOT_ELIGIBLE",
            "候选人或岗位状态已变化；请确认候选人可推荐、岗位开放且当前版本解析完成后重试"))

    @app.exception_handler(CaseStateError)
    async def handle_case_conflict(request: Request, error: CaseStateError) -> JSONResponse:
        return await handle_api_error(request, ApiError(409, "E_CASE_STATE_CONFLICT", str(error)))

    @app.exception_handler(ReverseMatchUnavailableError)
    async def handle_reverse_unavailable(request: Request, error: ReverseMatchUnavailableError) -> JSONResponse:
        return await handle_api_error(request, ApiError(503, "E_REVERSE_MATCH_UNAVAILABLE",
            "岗位索引尚未就绪、不兼容或匹配超时；请检查索引同步状态后重试"))

    @app.exception_handler(ProviderError)
    async def handle_provider_error(request: Request, error: ProviderError) -> JSONResponse:
        return await handle_api_error(request, ApiError(
            error.http_status, error.code, error.user_message, error.details))

    @app.exception_handler(LookupError)
    async def handle_lookup_error(request: Request, error: LookupError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={
                "code": "E_NOT_FOUND",
                "message": str(error),
                "request_id": getattr(request.state, "request_id", None),
                "details": None,
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, error: Exception) -> JSONResponse:
        # 必须留服务端痕迹：响应体只给一句 `str(error)`，没有 traceback 就无从定位——
        # 实测一个 500 `E_INTERNAL: Event loop is closed` 在客户端侧完全看不出是哪条路径。
        logger.exception(
            "未处理异常：%s %s request_id=%s",
            request.method, request.url.path, getattr(request.state, "request_id", None),
        )
        return JSONResponse(
            status_code=500,
            content={
                "code": "E_INTERNAL",
                "message": str(error),
                "request_id": getattr(request.state, "request_id", None),
                "details": None,
            },
        )

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "alive"}

    if services is not None:
        @app.get("/health/ready")
        def ready() -> dict[str, str]:
            return {"status": "ready"}

        @app.get("/health/checks")
        def checks() -> dict[str, dict]:
            return HealthService(services).check()

        app.include_router(resumes_router)
        app.include_router(tasks_router)
        app.include_router(search_router)
        app.include_router(indexes_router)
        app.include_router(jd_router)
        app.include_router(match_router)
        app.include_router(backup_router)
        app.include_router(correction_router)
        app.include_router(diagnostics_router)
        app.include_router(mapping_router)
        app.include_router(reminders_router)
        app.include_router(bd_search_router)
        app.include_router(bd_agent_router)
        app.include_router(cases_router)
        app.include_router(dashboard_router)
        app.include_router(daily_followup_router)
        app.include_router(candidate_reminders_router)
        app.include_router(settings_router)
        app.include_router(ai_settings_router)
        app.include_router(migration_router)
        app.include_router(onboarding_router)
        app.include_router(org_router)
        app.include_router(duplicates_router)
        app.include_router(schools_router)
        app.include_router(backfill_router)
        app.include_router(soft_delete_router)

    # Added last so CORS is outermost and answers preflight OPTIONS before the
    # session-token guard. The sidecar binds loopback only and authenticates
    # every request with a short-lived token; opening CORS lets the WebView
    # (tauri:// or the vite dev origin) call it from a different origin.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    return app
