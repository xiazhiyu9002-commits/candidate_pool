from fastapi import APIRouter, Request

from kerui_recruit.api.services import AppServices
from kerui_recruit.health.service import HealthService

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


@router.get("/status")
def onboarding_status(request: Request) -> dict:
    """Aggregate first-run status: data root, provider configuration and health."""
    services: AppServices = request.app.state.services
    settings = services.settings
    ai_manager = services.ai_manager
    llm_enabled = bool(ai_manager and ai_manager.llm_enabled)
    return {
        "data_root": str(settings.paths.root),
        "llm_enabled": llm_enabled,
        "search_enabled": settings.search_providers_enabled,
        "bd_search_enabled": settings.bd_search_enabled,
        "mail_enabled": settings.mail_enabled,
        "smtp_enabled": settings.smtp_enabled,
        "health": HealthService(services).check(),
    }


@router.post("/test-providers")
async def test_providers(request: Request) -> list[dict]:
    """Probe each configured provider with a minimal call."""
    services: AppServices = request.app.state.services
    if services.provider_connectivity is None:
        return []
    checks = await services.provider_connectivity.check()
    return [{"name": c.name, "ok": c.ok, "message": c.message} for c in checks]
