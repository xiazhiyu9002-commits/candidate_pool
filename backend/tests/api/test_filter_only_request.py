from kerui_recruit.api.search import CandidateSearchRequest, _merge_filters
from kerui_recruit.search.contracts import CandidateFilters
import httpx
import pytest
from kerui_recruit.main import create_app
from tests.api.test_local_api import build_services, make_pdf_bytes


def test_filter_only_request_accepts_empty_query():
    command = CandidateSearchRequest(query="", filters={"highest_degree": "MASTER"})
    assert command.query == ""
    assert command.filters.highest_degree == "MASTER"


def test_multi_value_direction_filters_survive_request_to_contract():
    """HTTP 请求里的三层方向筛选必须原样落到 CandidateFilters，否则检索硬条件接不上。"""
    command = CandidateSearchRequest(query="", filters={
        "career_directions": ["BACKEND", "DATA"],
        "career_specializations": ["BACKEND_SERVICE"],
        "business_directions": ["INSURANCE", "MARKETING"],
    })
    merged = _merge_filters(CandidateFilters(), command.filters)

    assert merged.career_directions == ("BACKEND", "DATA")
    assert merged.career_specializations == ("BACKEND_SERVICE",)
    assert merged.business_directions == ("INSURANCE", "MARKETING")


def test_unspecified_direction_filters_default_to_empty_tuples():
    """未指定时必须为空元组（不放宽也不误过滤），保持与其它多值字段一致。"""
    merged = _merge_filters(CandidateFilters(), CandidateSearchRequest(query="").filters)
    assert merged.career_directions == ()
    assert merged.career_specializations == ()
    assert merged.business_directions == ()


@pytest.mark.asyncio
async def test_filter_only_http_search_returns_matching_current_candidates(tmp_path):
    services, pipeline = build_services(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(services)), base_url="http://local",
                                  headers={"X-Kerui-Session": "test-token"}) as client:
        imported = await client.post("/api/resumes/import", files={"file": ("synthetic.pdf", make_pdf_bytes(), "application/pdf")})
        revision = imported.json()["revision_id"]
        await pipeline.run(revision)
        await services.index_sync_service.run_once(force=True)
        response = await client.post("/api/search/candidates", json={"query": "", "filters": {"highest_degree": "MASTER", "locations": ["上海"]}})
        assert response.status_code == 200
        assert [item["revision_id"] for item in response.json()["items"]] == [revision]
        assert response.json()["query_plan"]["operator"] == "smart"
        excluded = await client.post("/api/search/candidates", json={"query": "", "filters": {"locations": ["北京"]}})
        assert excluded.status_code == 200
        assert excluded.json()["items"] == []
