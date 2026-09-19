import httpx
import pytest

from kerui_recruit.main import create_app


@pytest.mark.asyncio
async def test_liveness_endpoint_reports_process_is_alive() -> None:
    """Removing the liveness route must break the desktop startup probe."""
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
