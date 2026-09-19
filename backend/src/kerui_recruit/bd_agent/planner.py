from __future__ import annotations

from pydantic import BaseModel


class SearchPlan(BaseModel):
    sub_queries: list[str] = []


_PLAN_PROMPT = """你是招聘领域的检索规划器。

任务：把用户的需求拆解成最多 3 条适合搜索引擎的查询式，从不同角度覆盖用户意图。
每条查询式应能命中公司招聘官网或岗位页，倾向于包含「招聘」「官网」「careers」等词。

规则：
- 只输出 JSON 对象，不要输出 markdown 或多余文字；
- sub_queries 数组最多 3 条，可为空；
- 若用户已给出明确搜索词，也可直接返回该词本身；
- 不要臆造用户未提及的地点、公司或技能；
- 每次尽量从不同角度拆分（换用不同的关键词组合、行业词、地点或公司类型），避免每次都生成完全相同的查询式。

用户需求：{query}"""


class QueryPlanner:
    """Generate sub-queries with an LLM. Falls back to the raw query on failure."""

    def __init__(self, llm) -> None:
        self._llm = llm

    async def plan(self, query: str, max_queries: int = 3) -> list[str]:
        try:
            plan = await self._llm.complete_json(
                messages=[
                    {
                        "role": "user",
                        "content": _PLAN_PROMPT.format(query=query),
                    }
                ],
                response_model=SearchPlan,
            )
        except Exception:
            return [query]

        queries = [q.strip() for q in plan.sub_queries if q and q.strip()]
        return queries[:max_queries] if queries else [query]
