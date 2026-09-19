from __future__ import annotations

import pytest

from kerui_recruit.bd_agent.planner import QueryPlanner, SearchPlan


class FakeLLM:
    def __init__(self, plan: SearchPlan) -> None:
        self._plan = plan

    async def complete_json(self, messages, response_model, temperature=0.0):
        return self._plan


@pytest.mark.asyncio
async def test_plan_returns_sub_queries() -> None:
    planner = QueryPlanner(
        FakeLLM(SearchPlan(sub_queries=["北京 大模型 招聘", "上海 算法 官网"]))
    )
    queries = await planner.plan("找大模型算法公司")
    assert queries == ["北京 大模型 招聘", "上海 算法 官网"]


@pytest.mark.asyncio
async def test_plan_falls_back_on_error() -> None:
    class Boom:
        async def complete_json(self, messages, response_model, temperature=0.0):
            raise RuntimeError("boom")

    planner = QueryPlanner(Boom())  # type: ignore[arg-type]
    assert await planner.plan("Java 招聘") == ["Java 招聘"]


@pytest.mark.asyncio
async def test_plan_trims_and_caps() -> None:
    planner = QueryPlanner(FakeLLM(SearchPlan(sub_queries=["a", "", "b", "c"])))
    assert await planner.plan("x", max_queries=2) == ["a", "b"]
