from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from kerui_recruit.core.settings import Settings
from kerui_recruit.providers.connectivity import ProviderConnectivityService
from kerui_recruit.providers.factory import ProviderBundle, build_providers
from kerui_recruit.providers.local import (
    LocalHashEmbeddingProvider,
    LocalKeywordReranker,
    LocalResumeParser,
)


@pytest.mark.asyncio
async def test_connectivity_reports_local_offline_mode(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path / "data", session_token=SecretStr("token"))
    service = ProviderConnectivityService(
        settings=settings,
        providers=build_providers(settings),
    )

    checks = await service.check()
    by_name = {check.name: check for check in checks}

    assert by_name["llm"].ok is True
    assert "本地" in by_name["llm"].message
    assert by_name["embedding"].ok is True
    assert by_name["reranker"].ok is True
    assert by_name["web_search"].ok is True
    assert by_name["web_search"].message == "未配置"


@pytest.mark.asyncio
async def test_connectivity_reports_failure_for_broken_llm(tmp_path: Path) -> None:
    class FailingParser:
        async def parse_resume(self, text: str):
            raise RuntimeError("boom")

    class EnabledManager:
        llm_enabled = True

    settings = Settings(
        data_root=tmp_path / "data",
        session_token=SecretStr("token"),
    )
    providers = ProviderBundle(
        parser=FailingParser(),
        jd_parser=LocalResumeParser(),
        embedding=LocalHashEmbeddingProvider(dimension=64),
        reranker=LocalKeywordReranker(),
        ocr=None,
        vision_parser=None,
        vector_dimension=64,
        http_client=None,
    )
    service = ProviderConnectivityService(settings=settings, providers=providers, ai_manager=EnabledManager())

    checks = await service.check()
    by_name = {check.name: check for check in checks}

    assert by_name["llm"].ok is False
    assert by_name["llm"].message == "调用失败"


@pytest.mark.asyncio
async def test_connectivity_uses_a_schema_parseable_llm_probe(tmp_path: Path) -> None:
    class SchemaAwareParser:
        async def parse_resume(self, text: str):
            if "工作经历" not in text or "教育经历" not in text:
                raise ValueError("resume probe is too incomplete")
            return {"name": "测试候选人"}

    class EnabledManager:
        llm_enabled = True

    settings = Settings(
        data_root=tmp_path / "data",
        session_token=SecretStr("token"),
    )
    providers = ProviderBundle(
        parser=SchemaAwareParser(),
        jd_parser=LocalResumeParser(),
        embedding=LocalHashEmbeddingProvider(dimension=64),
        reranker=LocalKeywordReranker(),
        ocr=None,
        vision_parser=None,
        vector_dimension=64,
        http_client=None,
    )

    checks = await ProviderConnectivityService(
        settings=settings,
        providers=providers,
        ai_manager=EnabledManager(),
    ).check()

    assert {check.name: check for check in checks}["llm"].ok is True
