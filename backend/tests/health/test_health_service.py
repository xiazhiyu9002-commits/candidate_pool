from pathlib import Path

from pydantic import SecretStr

from kerui_recruit.core.settings import Settings
from kerui_recruit.health.service import HealthService
from kerui_recruit.runtime import build_runtime


def test_health_reports_local_components(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path / "data", session_token=SecretStr("launch-token"))
    services = build_runtime(settings).services

    report = HealthService(services).check()

    assert report["database"]["status"] == "healthy"
    assert report["blob_store"]["status"] == "healthy"
    assert report["search"]["status"] == "healthy"
    assert report["disk"]["status"] == "healthy"