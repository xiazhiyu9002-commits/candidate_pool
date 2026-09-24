"""任务组 8.5 验证：检索「硬筛退化（relaxable）」开/关的真实查询集 A/B。

背景（用户 2026-09-22 修订的口径）：AI 解析产出的职业方向 / 业务方向 / 公司 / 职位这些条件，
处理顺序是**先判断这一条能不能安全地硬筛 → 能就硬筛 → 不能就退化成软排加权**。任务组 8
把这条落到了 `search/service.py` 的 `_relax_unmatchable_filters`，但一直缺「退化到底救回了多少、
有没有把本该命中的弄丢」的实测。这个脚本就是补那一环。

两条臂（**只有退化开关不同，其余全部同源**）：

| 臂 | `relaxable_fields` | 含义 |
| --- | --- | --- |
| `relax0` | `()` | 不退化：AI 解析出的硬条件一律当真（8.1 之前的现网行为） |
| `relax1` | AI 解析实际接受且面板未手填的字段 | 上线形态：硬筛筛空就退化成软排 |

取数口径与生产一致：查什么、过滤器怎么算、`keywords`/`concepts`/`semantic_query`/`operator`
怎么传，都照 `api/search.py` 那条路径走，只是把 `relaxable_fields` 换成两条臂各自的取值。
标注复用 `.tmp-judge` 的判决式 grade（sha256 别名，与报告同构），指标函数直接复用
`scripts/retrieval_ablation_2026_09_20.py`，避免第二套实现。

判定口径：**nDCG@10 不降、空结果率不升**才算退化是净收益；另单独列出
「relax0 为空但 relax1 非空」的**救回案例**与「relax0 非空但 relax1 为空」的**反向退化**。

安全：只读 `.dev-data`；不打印密钥；候选人 ID 一律 sha256 别名。

用法：

    py -3.12 scripts/relaxation_ab_2026_09_22.py --phrasings standard,colloquial,vague
    py -3.12 scripts/relaxation_ab_2026_09_22.py --limit 12   # 先小样试跑
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

DEV = ROOT / ".dev-data"
JUDGE_DIR = ROOT / ".tmp-judge"
OUT = ROOT / ".tmp-ablation" / "relaxation-ab.json"
SESSION = "0" * 64
# 与生产一致的 top-K 口径（`limit=20` 时内部候选池随之变化，所以两条臂必须同值）。
TOP_K = 20
ARMS = ("relax0", "relax1")
BASELINE = "relax0"

_ABLATION = None


def _load_ablation():
    """复用既有消融脚本的指标与取数函数（只读、无副作用）；只加载一次。"""
    global _ABLATION
    if _ABLATION is None:
        path = ROOT / "scripts" / "retrieval_ablation_2026_09_20.py"
        spec = importlib.util.spec_from_file_location("retrieval_ablation_2026_09_20", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _ABLATION = module
    return _ABLATION


def load_queries(phrasings: tuple[str, ...], limit: int | None) -> list[tuple[str, str]]:
    """返回 [(qid, 查询文本)]；qid 取 `J01:vague` 形态，便于与判决标签对齐。"""
    payload = json.loads((JUDGE_DIR / "queries.json").read_text(encoding="utf-8"))
    queries: list[tuple[str, str]] = []
    for jid, phrasings_table in payload.items():
        if jid.startswith("_") or not isinstance(phrasings_table, dict):
            continue
        for phrasing in phrasings:
            text = (phrasings_table.get(phrasing) or "").strip()
            if text:
                queries.append((f"{jid}:{phrasing}", text))
    queries.sort()
    return queries[:limit] if limit else queries


def build_real_value_queries(count: int) -> list[dict]:
    """用 `.dev-data` 的真实取值拼查询，专门覆盖「AI 解析真的产出可退化字段」的场景。

    为什么要自己拼：判决集（`.tmp-judge`）是自然语言岗位描述，实测 AI 解析对它们
    `accepted_fields` 为空（可退化字段只接受**能在原文里逐字找到**的值），两臂完全一致、
    A/B 等于空转。这里改成从真实 `parsed_data` 取公司 / 职位 / 方向，两类各一半：

    - `natural`：公司 + 职位**取自同一个人** → 硬筛通常筛得出人，退化**不该**触发（证明不误伤）。
    - `narrowed`：在上一类基础上再叠一个**属于别人**的方向 → 组合通常筛空，
      正是「本该命中却被硬筛掉」的形态，退化应当把它救回来。

    安全：只读 `.dev-data`；写出的查询文件里只有公司/职位/方向这类公开字段，不含候选人身份。
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from kerui_recruit.db.models import ResumeRevision
    from kerui_recruit.db.session import create_engine_for
    from kerui_recruit.sidecar import RuntimeArgs, build_settings

    settings = build_settings(RuntimeArgs(host="127.0.0.1", port=1, token=SESSION, data_root=DEV))
    engine = create_engine_for(settings.paths.database)
    with Session(engine) as session:
        payloads = list(session.scalars(
            select(ResumeRevision.parsed_data).where(
                ResumeRevision.is_current.is_(True), ResumeRevision.status == "READY",
            ).limit(4000)
        ))
    people: list[dict] = []
    directions: list[str] = []
    for data in payloads:
        if not isinstance(data, dict):
            continue
        company = str(data.get("current_company") or "").strip()
        title = str(data.get("current_title") or "").strip()
        for key in ("business_directions", "career_directions"):
            for item in data.get(key) or []:
                text = str(item).strip()
                if 2 <= len(text) <= 12:
                    directions.append(text)
        if company and title and len(company) <= 20 and len(title) <= 16:
            people.append({"company": company, "title": title,
                           "location": str(data.get("location") or "").strip()})
    directions = sorted(set(directions))

    half = max(1, count // 2)
    chosen = people[:: max(1, len(people) // (half * 2))][:half] if people else []
    built: list[dict] = []
    for index, person in enumerate(chosen):
        suffix = f" {person['location']}" if person["location"] else ""
        # 一并写出「查询想找的公司/职位」：确定性判分器靠它算相关性标签（见
        # `build_deterministic_grades`），不然 built 集的 nDCG@10 恒为 None。
        built.append({"qid": f"N{index:03d}:natural", "kind": "natural",
                      "company": person["company"], "title": person["title"],
                      "text": f"{person['company']} {person['title']}{suffix}"})
    for index, person in enumerate(chosen):
        if not directions:
            break
        extra = directions[(index * 7) % len(directions)]
        if extra in (person["company"], person["title"]):
            continue
        suffix = f" {person['location']}" if person["location"] else ""
        built.append({"qid": f"N{index:03d}:narrowed", "kind": "narrowed",
                      "company": person["company"], "title": person["title"],
                      "text": f"{person['company']} {person['title']} {extra}{suffix}"})
    return built


def load_candidate_facts() -> list[dict]:
    """读 `.dev-data`，返回每个候选人的判分事实：`[{alias, companies, titles}]`。

    `alias` 与 `run_ab` 里对命中结果做的 `ab.alias(candidate_id)` 同构，两边能直接对齐。

    为什么取「顶层字段 + 全部经历里的同名字段」：索引里的 `company_text` / `title_text`
    是**全部工作经历**抽出的词项拼起来的（`search/documents.py`），只取 `current_company`
    会把「早期在 X 公司」的人误判成不相关。

    安全：只读；返回的是 sha256 别名与公开字段，不含身份信息。
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from kerui_recruit.db.models import ResumeDocument, ResumeRevision
    from kerui_recruit.db.session import create_engine_for
    from kerui_recruit.sidecar import RuntimeArgs, build_settings

    settings = build_settings(RuntimeArgs(host="127.0.0.1", port=1, token=SESSION, data_root=DEV))
    engine = create_engine_for(settings.paths.database)
    with Session(engine) as session:
        # **必须带 join 条件**：漏了会变成 resume_document × resume_revision 的笛卡尔积，
        # 每行是「随便一个候选人 ID + 随便一份 parsed_data」，判分标签会全错（实测第一版就这么写的）。
        rows = list(session.execute(
            select(ResumeDocument.candidate_id, ResumeRevision.parsed_data)
            .join(ResumeDocument, ResumeDocument.id == ResumeRevision.document_id)
            .where(
                ResumeRevision.is_current.is_(True), ResumeRevision.status == "READY",
            ).limit(4000)
        ))
    facts: list[dict] = []
    ab = _load_ablation()
    for candidate_id, data in rows:
        if not isinstance(data, dict) or not candidate_id:
            continue
        facts.append({
            "alias": ab.alias(candidate_id),
            "companies": _terms(data, "company"),
            "titles": _terms(data, "title"),
        })
    return facts


def _terms(data: dict, field: str) -> list[str]:
    """收集某字段的值：顶层 `current_<field>` + 全部经历里的 `<field>`。"""
    terms: list[str] = []
    top = str(data.get(f"current_{field}") or "").strip()
    if top:
        terms.append(top)
    for item in data.get("experiences") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get(field) or "").strip()
        if text:
            terms.append(text)
    return sorted(set(terms))


def build_deterministic_grades(built: list[dict], facts: list[dict]) -> dict[str, dict[str, int]]:
    """给 `--build` 拼出的查询算**确定性**相关性标签（不需要人工或 LLM 判分）。

    判据只用两个可机检的事实：查询里的公司与职位是否能在候选人的公司/职位词里子串命中——

    - `2`：公司与职位**都**命中（这就是查询想找的那类人）；
    - `1`：命中其一；
    - `0`：都不命中。

    为什么要它：built 集的 qid 是 `N000:natural`，`grades.get("N000")` 恒为 None，
    于是 `score_arm` 把它们整批跳过、nDCG@10 全是 None，「退化救回来的这批人质量如何」
    根本没法看。有了确定性标签，built 集的空结果率**与** nDCG 就能同时算出来。

    限制（写清楚，避免过度解读）：只覆盖公司/职位两个维度。方向的「相关性」没有确定性
    定义（一个方向对某个查询算不算相关，取决于查询本意），不硬凑。
    """
    grades: dict[str, dict[str, int]] = {}
    for item in built:
        company = str(item.get("company") or "").strip()
        title = str(item.get("title") or "").strip()
        if not company or not title:
            continue
        table: dict[str, int] = {}
        for fact in facts:
            hit_company = any(company in term for term in fact["companies"])
            hit_title = any(title in term for term in fact["titles"])
            if hit_company and hit_title:
                table[fact["alias"]] = 2
            elif hit_company or hit_title:
                table[fact["alias"]] = 1
            else:
                table[fact["alias"]] = 0
        grades[item["qid"].split(":", 1)[0]] = table
    return grades


def absent_companies(facts: list[dict], count: int) -> list[str]:
    """造 `count` 个**语料里不存在**的公司名，用于判决集的「必然筛空」问法。

    为什么要**单个**不可能条件，而不是一对「不可能的公司 + 职位」：一对组合里每个分量
    各自都是可满足的（拿的都是语料里真实存在的值），于是「只丢一条」之后剩下那一半仍然
    非空 —— 结果既不是该 JD 的本意、也不是空，测出来的差异来自**注入方式**而不是策略。
    单个不存在的公司则不同：丢掉它之后剩下的正好就是该 JD 自己的条件，
    于是「救回的名单 vs 该 JD 的 standard 名单」才能干净地验证「退化保住了本意」。

    名字按固定词表拼、并逐个验证语料里确实没有，因此任何一次运行都可复现。
    """
    haystack = " ".join(term for fact in facts for term in fact["companies"])
    names: list[str] = []
    for prefix in ("环宇智联", "星辰智造", "云栖数智", "瀚海方舟", "恒锐信息", "博远数据", "朗越智能"):
        for suffix in ("科技", "数据", "信息"):
            if len(names) >= count:
                return names
            candidate = f"{prefix}{suffix}"
            if candidate in haystack or any(candidate in name for name in names):
                continue
            names.append(candidate)
    return names


def narrowed_judge_queries(queries: list[tuple[str, str]], injected: list[str]) -> list[tuple[str, str]]:
    """给判决集的每条 `standard` 问法各拼一个**语料里不存在的公司名**。

    判决集的 grade 是「这个候选人适不适合这个 JD」，与查询文本**无关**
    （`load_grades` 只按 jid 取表，同一 JD 的不同问法共用同一张表），所以补一族问法
    不需要重新判分。

    拼完的查询里那条公司名必然筛空 → 退化会把它丢掉，剩下的就是该 JD 自己那些条件
    ≈ `standard` 问法的检索条件。于是「救回来的名单质量能不能追平 standard」
    就是**退化有没有误伤精度**的直接答案。
    """
    if not injected:
        return []
    narrowed: list[tuple[str, str]] = []
    for index, (qid, text) in enumerate(queries):
        if not qid.endswith(":standard") or not text.strip():
            continue
        narrowed.append((f"{qid.rsplit(':', 1)[0]}:narrowed",
                         f"{text} {injected[index % len(injected)]}"))
    return narrowed


def load_query_file(path: Path) -> list[tuple[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [(str(item["qid"]), str(item["text"])) for item in payload if item.get("text")]


async def run_ab(queries: list[tuple[str, str]], timeout: float) -> dict:
    from fastapi.testclient import TestClient

    from kerui_recruit.runtime import create_runtime_app
    from kerui_recruit.search.service import RELAXABLE_FIELDS
    from kerui_recruit.sidecar import RuntimeArgs, build_settings

    ab = _load_ablation()
    settings = build_settings(RuntimeArgs(host="127.0.0.1", port=1, token=SESSION, data_root=DEV))
    app = create_runtime_app(settings)
    arms: dict[str, dict] = {name: {"top": {}, "empty": [], "items": {}} for name in ARMS}
    parsed_rows: dict[str, dict] = {}

    with TestClient(app, headers={"X-Kerui-Session": SESSION}) as client:
        services = app.state.services
        school_groups = ab.school_alias_groups()
        for index, (qid, text) in enumerate(queries, start=1):
            started = time.monotonic()
            try:
                plan = await services.query_parser.parse(
                    text, school_alias_groups=school_groups,
                    deadline_monotonic=time.monotonic() + timeout)
            except Exception as error:  # noqa: BLE001 — 解析失败要如实记，不能静默跳过
                parsed_rows[qid] = {"parse_error": f"{type(error).__name__}: {error}"}
                continue
            filters = plan.filters
            llm_fields = set(plan.accepted_fields)
            relaxable = tuple(name for name in RELAXABLE_FIELDS if name in llm_fields)
            parsed_rows[qid] = {
                "source": getattr(plan, "source", None),
                # `degraded` 是分辨「模型没给可用条件（rejected）」与「供应商不可用（provider_error）」
                # 的唯一线索——两者都会退回规则链路，只看 source 分不出来。
                "degraded": getattr(plan, "degraded", None),
                "llm_fields": sorted(llm_fields),
                "relaxable": list(relaxable),
            }
            for arm in ARMS:
                page = await services.search_service.search(
                    plan.keywords, filters, limit=TOP_K, mode="hybrid",
                    deadline=time.monotonic() + timeout,
                    concepts=plan.concepts,
                    semantic_query=getattr(plan, "semantic_query", None),
                    relaxable_fields=relaxable if arm == "relax1" else (),
                )
                ids = [ab.alias(hit.candidate_id) for hit in page.items[:TOP_K]]
                arms[arm]["top"].setdefault(qid, {})["hybrid"] = ids
                arms[arm]["items"][qid] = len(page.items)
                arms[arm]["empty"].append(qid) if not page.items else None
                if arm == "relax1":
                    parsed_rows[qid]["relaxed"] = list(getattr(page, "relaxed", ()) or ())
            print(f"[{index}/{len(queries)}] {qid} "
                  f"relax0={arms['relax0']['items'][qid]} relax1={arms['relax1']['items'][qid]} "
                  f"source={parsed_rows[qid]['source']}/{parsed_rows[qid]['degraded']} "
                  f"llm={sorted(llm_fields)} 退化={parsed_rows[qid].get('relaxed')} "
                  f"{time.monotonic() - started:.1f}s", flush=True)

    return {"arms": arms, "parsed": parsed_rows}


def _narrowed_vs_control(scored: dict, parsed: dict) -> dict:
    """配对比较「过度收窄查询被救回后的名单」与「同一批数据的对照查询名单」。

    对照取同一 jid 的 `standard` 问法，没有就取 `natural`（built 集）。判分表按 jid 共享
    （`load_grades` 只按 jid 取表），所以这两条查询的 nDCG@10 **可比**。

    差值就是退化的精度代价：≈0 说明「丢掉那条过严条件」正好还原了这批数据的本意，
    明显为负说明救援把不相关的人顶了上来。只统计**真的触发了退化**的查询——
    没触发的两臂本来一致，放进来只会稀释结论。
    """
    rescued = scored.get("relax1", ({}, {}))[1]
    baseline = scored.get(BASELINE, ({}, {}))[1]
    rows: list[dict] = []
    for key, entry in rescued.items():
        qid, _, mode = key.rpartition("|")
        if not qid.endswith(":narrowed") or not (parsed.get(qid, {}).get("relaxed")):
            continue
        stem = qid.rsplit(":", 1)[0]
        control = baseline.get(f"{stem}:standard|{mode}") or baseline.get(f"{stem}:natural|{mode}")
        value = entry.get("ndcg@10")
        base = (control or {}).get("ndcg@10")
        if value is None or base is None:
            continue
        rows.append({
            "qid": qid, "mode": mode,
            "rescued_ndcg@10": value, "control_ndcg@10": base,
            "delta": round(value - base, 4),
            "rescued_p@5": entry.get("p@5"), "control_p@5": (control or {}).get("p@5"),
            # 记下这次到底丢了哪些条件：差值大时，首先要看的就是「丢的是用户本意、
            # 还是那条过严的附加条件」——`_RELAXATION_ORDER` 是固定优先级，不认元凶。
            "relaxed": list(parsed.get(qid, {}).get("relaxed") or ()),
        })
    if not rows:
        return {"queries": 0, "mean_delta": None, "rows": []}
    return {
        "queries": len(rows),
        "mean_delta": round(statistics.mean(row["delta"] for row in rows), 4),
        "mean_rescued_ndcg@10": round(statistics.mean(row["rescued_ndcg@10"] for row in rows), 4),
        "mean_control_ndcg@10": round(statistics.mean(row["control_ndcg@10"] for row in rows), 4),
        "rows": rows,
    }


def summarize(payload: dict, queries: list[tuple[str, str]], kinds: dict[str, str],
              extra_grades: dict[str, dict[str, int]] | None = None) -> dict:
    ab = _load_ablation()
    # `extra_grades` 是 built 集的确定性标签（`build_deterministic_grades`）：判决集的
    # grade 按 jid 存放、与问法无关，两者键空间不重叠，直接合并即可。
    grades = {**ab.load_grades(JUDGE_DIR), **(extra_grades or {})}
    arms, parsed = payload["arms"], payload["parsed"]
    scored: dict[str, tuple[dict, dict]] = {}
    report: dict = {"arms": {}, "rescued": [], "regressed": [], "relaxed_fields": {}}

    for arm in ARMS:
        summary, per_query = ab.score_arm(arms[arm]["top"], grades)
        scored[arm] = (summary, per_query)
        items = arms[arm]["items"]
        # `**summary` 放前面：它自己也有 `queries` 键（按有标注的查询数算），放后面会把
        # 这里的「本臂实际跑了几条」覆盖成 0，打印出来就成了误导性的 `空结果 11/0`。
        report["arms"][arm] = {
            **summary,
            "queries": len(items),
            "empty_count": len(arms[arm]["empty"]),
            "empty_rate": round(len(arms[arm]["empty"]) / len(items), 4) if items else None,
            "items_mean": round(statistics.mean(items.values()), 2) if items else None,
        }
    report["ndcg_delta_vs_baseline"] = ab.compare_arms(scored, BASELINE)
    report["narrowed_vs_standard"] = _narrowed_vs_control(scored, parsed)

    for qid, _ in queries:
        left = arms[BASELINE]["items"].get(qid)
        right = arms["relax1"]["items"].get(qid)
        if left is None or right is None:
            continue
        if left == 0 and right > 0:
            report["rescued"].append({"qid": qid, "relax0": left, "relax1": right,
                                      "relaxed": parsed.get(qid, {}).get("relaxed")})
        elif left > 0 and right == 0:
            report["regressed"].append({"qid": qid, "relax0": left, "relax1": right})
    counts: dict[str, int] = {}
    for row in parsed.values():
        for name in row.get("relaxed") or []:
            counts[name] = counts.get(name, 0) + 1
    report["relaxed_fields"] = dict(sorted(counts.items()))
    report["parse_failed"] = sorted(qid for qid, row in parsed.items() if row.get("parse_error"))
    if kinds:
        by_kind: dict[str, dict] = {}
        for kind in sorted(set(kinds.values())):
            qids = [q for q, value in kinds.items() if value == kind]
            by_kind[kind] = {
                "queries": len(qids),
                "relax0_empty": sum(1 for q in qids if arms[BASELINE]["items"].get(q) == 0),
                "relax1_empty": sum(1 for q in qids if arms["relax1"]["items"].get(q) == 0),
                "relaxed": sum(1 for q in qids if parsed.get(q, {}).get("relaxed")),
                "rescued": sum(1 for row in report["rescued"] if kinds.get(row["qid"]) == kind),
            }
        report["by_kind"] = by_kind
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="硬筛退化开/关的真实查询集 A/B")
    parser.add_argument("--phrasings", default="standard,colloquial,vague")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（小样试跑）")
    parser.add_argument("--timeout", type=float, default=60.0, help="单次解析/检索预算（秒）")
    parser.add_argument("--build", type=int, default=0,
                        help="先用 `.dev-data` 真实取值拼 N 条查询（含 natural/narrowed 两类）再跑")
    parser.add_argument("--queries-file", type=Path, default=None, help="改用指定的查询文件")
    options = parser.parse_args()

    phrasings = tuple(item.strip() for item in options.phrasings.split(",") if item.strip())
    kinds: dict[str, str] = {}
    extra_grades: dict[str, dict[str, int]] = {}
    built_path = ROOT / ".tmp-ablation" / "relaxation-queries.json"
    if options.build:
        built = build_real_value_queries(options.build)
        built_path.parent.mkdir(parents=True, exist_ok=True)
        built_path.write_text(json.dumps(built, ensure_ascii=False, indent=1), encoding="utf-8")
        queries = [(item["qid"], item["text"]) for item in built]
        kinds = {item["qid"]: item["kind"] for item in built}
        # 确定性标签：让 built 集也能算 nDCG@10（否则 `grades.get("N000")` 恒为 None）。
        extra_grades = build_deterministic_grades(built, load_candidate_facts())
        graded = sum(1 for qid in extra_grades if any(v == 2 for v in extra_grades[qid].values()))
        print(f"已拼查询 {len(queries)} 条 → {built_path}", flush=True)
        print(f"确定性判分：{len(extra_grades)} 条查询有标签，"
              f"其中 {graded} 条至少有一个强相关（grade=2）候选", flush=True)
    elif options.queries_file:
        queries = load_query_file(options.queries_file)
        payload = json.loads(options.queries_file.read_text(encoding="utf-8"))
        kinds = {str(item["qid"]): str(item.get("kind") or "") for item in payload
                 if item.get("kind")}
    else:
        queries = load_queries(phrasings, options.limit or None)
        if "narrowed" in phrasings:
            # 判决集本身一条都筛不空（A/B 空转），这里按需拼一族「必然筛空」的问法。
            # grade 按 jid 共享，所以不需要重新判分——见 `narrowed_judge_queries`。
            base = [item for item in queries if not item[0].endswith(":narrowed")]
            injected = absent_companies(load_candidate_facts(), 30)
            added = narrowed_judge_queries(base, injected)
            queries = sorted(base + added)
            kinds = {qid: qid.rsplit(":", 1)[-1] for qid, _ in queries}
            print(f"已拼「必然筛空」问法 {len(added)} 条（注入语料里不存在的公司 "
                  f"{injected[:3]}…）", flush=True)
    if not queries:
        raise SystemExit("查询集为空：检查 .tmp-judge/queries.json")
    print(f"查询 {len(queries)} 条", flush=True)

    payload = asyncio.run(run_ab(queries, options.timeout))
    report = {"frozen": {"count": len(queries), "top_k": TOP_K, "baseline": BASELINE,
                         "source": "built" if options.build else
                                   (str(options.queries_file) if options.queries_file
                                    else str(JUDGE_DIR / "queries.json")),
                         "phrasings": list(phrasings) if not (options.build or options.queries_file) else None},
              **summarize(payload, queries, kinds, extra_grades), "raw": payload}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n=== 结论 ===")
    for arm in ARMS:
        row = report["arms"][arm]
        print(f"{arm}: 空结果 {row['empty_count']}/{row['queries']}（{row['empty_rate']}）"
              f" 平均条数 {row['items_mean']} nDCG@10={row['ndcg@10']} p@5={row['p@5']}"
              f" hits@10={row['hits@10']} top1_ge2={row['top1_ge2']}")
    print("relax1 相对 relax0 的 nDCG 配对差异：", report["ndcg_delta_vs_baseline"])
    print(f"救回案例（relax0 空 → relax1 非空）：{len(report['rescued'])}")
    print(f"反向退化（relax0 非空 → relax1 空）：{len(report['regressed'])}")
    print("各字段退化次数：", report["relaxed_fields"])
    if report.get("by_kind"):
        print("按查询类型：", json.dumps(report["by_kind"], ensure_ascii=False))
    injury = report.get("narrowed_vs_standard") or {}
    if injury.get("queries"):
        print(f"误伤判据（narrowed 被救回 vs 同一批数据的对照查询，n={injury['queries']}）："
              f"救回 nDCG@10 均值 {injury['mean_rescued_ndcg@10']}、"
              f"对照 {injury['mean_control_ndcg@10']}、差值 {injury['mean_delta']}")
        for row in sorted(injury["rows"], key=lambda item: item["delta"])[:5]:
            print(f"    {row['qid']} 救回 {row['rescued_ndcg@10']} vs 对照 {row['control_ndcg@10']}"
                  f"（丢掉了 {'、'.join(row['relaxed']) or '—'}）")
    else:
        print("误伤判据：本批没有「触发退化的 narrowed 查询」可比（两臂一致 ⇒ 无从判断）")
    if report["parse_failed"]:
        print(f"解析失败 {len(report['parse_failed'])} 条：{report['parse_failed'][:8]}")
    print(f"产物：{OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
