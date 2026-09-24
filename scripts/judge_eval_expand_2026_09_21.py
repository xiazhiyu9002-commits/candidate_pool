"""分层问法集扩样（20 JD → 36 JD）：追加 16 个未删除 JD 的问法、检索与盲测判决输入。

背景：阶段 1 的 A/B 定档在 20 JD 的分层集上判「不达标」，但该集只有 **20 个独立单元**
（同一 JD 的三种问法共用同一候选池），样本量撑不起定档；用户决策为「维持现状 + 扩样复核」。

扩样上限由语料决定：库里 `deleted_at IS NULL` 的 JD 共 **31 个**，其中 20 个已在原集合里
（含 6 个后来被软删除的），可新增 16 个 → 合计 36 个 JD / 108 条查询。

产物全部落在 **新目录** `.tmp-judge-expanded/`，不改动原 `.tmp-judge/`：

- `jd_selected.json`：原 20 个（顺序不变，J01–J20 的编号与盲测字母保持不变）+ 新增 16 个；
- `queries.json`：J01–J20 直接复制原文件，J21–J36 为本脚本内作者化的三种问法
  （standard=忠实 JD 的完整问法；colloquial=猎头日常口语；vague=信息高度不足的短问法；
  真值不使用这些文本，判断方只看 JD 原文）；
- `search_results.json`：J01–J20 复制原文件，J21–J36 用真实检索管线跑（3 问法 × 3 模式 × 前 10）；
- `blinding_map.json`：J01–J20 复制原文件（**不重新盲化**，否则原判决标签全部作废），
  J21–J36 新建盲化（同一 `random.Random(jd_alias)` 口径）；
- `judge_input_new_batch{N}.json`：只含新 JD 的判决输入；
- `judge_out_batch{1..5}.json`：原文件原样复制（它们按 J01–J20 的字母编号，字母未变，仍然有效）。

用法：

    py -3.12 scripts/judge_eval_expand_2026_09_21.py --stage prepare   # 选样 + 检索
    py -3.12 scripts/judge_eval_expand_2026_09_21.py --stage blind     # 盲测输入
    py -3.12 scripts/judge_eval_expand_2026_09_21.py --stage verify    # 只读校验

安全：只读 `.dev-data`；候选人 ID 一律 sha256 别名；`blinding_map.json` 不得交给判决子 Agent。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

DEV = ROOT / ".dev-data"
SRC = ROOT / ".tmp-judge"
OUT = ROOT / ".tmp-judge-expanded"
MODES = ("keyword", "vector", "hybrid")
PHRASINGS = ("standard", "colloquial", "vague")
TOP_K = 10
LIMIT = 100
BATCH_SIZE = 4
NARRATIVE_CHARS = 200
SKILL_CHARS = 100
EXP_SUMMARY_CHARS = 70
PROJ_SUMMARY_CHARS = 100
MAX_EXPERIENCES = 3
MAX_PROJECTS = 2

# J21–J36 的三种问法（作者化）。键 = JD 别名，与 jd_selected 追加顺序一致。
NEW_QUERIES: dict[str, dict[str, str]] = {
    "37af9b0c4d6f": {  # AI研发效能全栈工程师（AI 质效系统）
        "standard": "AI 研发效能全栈工程师，3 年以上开发或测试开发经验，基于 LLM 构建研发提效工具与 Agent，"
                    "RAG 与 MCP，有 Multi-Agent 落地经验，能产出可运行系统",
        "colloquial": "做 AI 研发效能的，能给研发测试团队做提效工具和 Agent 那种，最好玩过 LangChain 这类框架",
        "vague": "AI 质效 工具链",
    },
    "0f9447c04362": {  # Java 后端与大数据开发工程师
        "standard": "Java 后端与大数据开发工程师，5 年以上，Spring Boot 微服务，PostgreSQL 与 Elasticsearch，"
                    "Docker/Kubernetes/OpenShift，有 Spark 经验优先",
        "colloquial": "找个五年以上的 Java 后端，会 Spring Boot 微服务，数据库和 ES 都要熟，能上容器和 K8s",
        "vague": "Java 大数据",
    },
    "763faa889628": {  # WMT 项目经理
        "standard": "财富管理科技项目经理，负责科技项目端到端规划与交付，预算与资源管理，熟练 JIRA，"
                    "懂 SDLC，银行或金融背景",
        "colloquial": "财富管理那边要个项目经理，管交付进度和预算的，银行做过最好",
        "vague": "财富管理 项目经理",
    },
    "45b1f2bb419c": {  # 中级java开发工程师（保险产品方向）
        "standard": "中级 Java 开发工程师，3 年以上，Spring Boot 与 Spring Cloud，熟悉 JVM 调优，"
                    "保险业务中后台系统，有 AI 辅助开发实践",
        "colloquial": "中级 Java，三年以上，Spring 全家桶熟，能搞保险中后台那种，会用 Cursor 之类 AI 工具加分",
        "vague": "中级 Java 保险",
    },
    "8fea634251ff": {  # 中级java开发工程师（营销与客服方向）
        "standard": "中级 Java 开发工程师（营销与客服方向），3 年以上，Spring Boot 微服务，"
                    "高并发场景性能优化与服务治理，AI 辅助编码实践",
        "colloquial": "中级 Java 开发，做营销和客服方向的，微服务高并发要能扛",
        "vague": "中级 Java 营销",
    },
    "e3e601454e56": {  # GFMT 全栈开发工程师
        "standard": "全栈开发工程师，Java 与 Spring Boot 后端，Web Components 或 Angular/React 前端，"
                    "微服务，Openshift 与 Jenkins CI/CD，SQL 数据处理",
        "colloquial": "全栈工程师，后端 Java Spring Boot，前端也要能写，会用 Openshift 和 Jenkins 那套",
        "vague": "全栈开发",
    },
    "22598a07bc97": {  # GFMT 全栈开发工程师（同族）
        "standard": "GFMT 全栈开发工程师，Java（Spring Boot）与前端 Web Components，微服务与 REST 集成，"
                    "CI/CD 与 DevOps，SQL 数据工程，银行金融背景优先",
        "colloquial": "GFMT 那边招全栈，前后端都要，金融背景优先",
        "vague": "GFMT 全栈",
    },
    "1487987234ea": {  # MOT 全栈开发工程师（后端为主）
        "standard": "MOT 全栈开发工程师，后端为主，Java Spring Boot，RESTful 与 GraphQL，MongoDB 与 PostgreSQL，"
                    "Docker/Kubernetes，Kafka 消息队列，金融数据安全",
        "colloquial": "后端强的全栈，Java Spring Boot，数据库和消息队列都要用过，能上 K8s",
        "vague": "MOT 全栈 后端",
    },
    "02d0b947ae43": {  # WMT Avaloq 开发工程师
        "standard": "Avaloq 开发工程师，具备 Avaloq 开发与实施经验，理解 Avaloq 架构与数据模型，"
                    "有 Avaloq 与其他系统集成经验，熟悉金融监管合规",
        "colloquial": "要会 Avaloq 的，做过银行核心系统定制和集成那种，最好有认证",
        "vague": "Avaloq 开发",
    },
    "47231b919ab0": {  # IBGT Java 开发工程师
        "standard": "机构银行 Java 开发工程师，1-3 年经验，Java 与 Python，参与 GenAI 与机器学习集成，"
                    "CI/CD 流水线，云平台 Azure/AWS/GCP，金融服务业优先",
        "colloquial": "IBGT 那边要个一两年到三年的 Java，会点 Python，做过 GenAI 集成的更好",
        "vague": "IBGT Java 开发",
    },
    "4eedb699e7e6": {  # 工程小队负责人（财富管理/投资旅程）
        "standard": "工程小队负责人，全栈工程师出身，Java 与 ReactJS，分布式微服务与事件驱动架构，"
                    "带过一个或多个工程小队，有订单管理系统与执行管理系统经验，熟悉债券股票外汇等投资领域",
        "colloquial": "要个工程小队的技术负责人，全栈的，做过证券那套订单和执行系统的加分",
        "vague": "工程小队 负责人",
    },
    "0ae37c77f091": {  # 工程小队负责人（同族）
        "standard": "工程小队负责人，端到端负责一条业务线的全栈工程交付，Java/ReactJS/Python/Go 技术栈，"
                    "微服务与事件驱动系统，金融投资领域业务经验",
        "colloquial": "找工程小队的负责人，能端到端扛一条业务线，全栈技术栈",
        "vague": "工程小队负责人",
    },
    "0488e458fb42": {  # Avaloq 工程团队负责人
        "standard": "Avaloq 工程团队负责人，5 年以上 Avaloq 核心银行平台亲力亲为开发经验，"
                    "Avaloq 脚本与 PL/SQL，对象建模与工作流配置，带过工程团队，私人银行财富管理背景",
        "colloquial": "Avaloq 那边的工程负责人，五年以上 Avaloq 实操，还要能带团队",
        "vague": "Avaloq 团队负责人",
    },
    "d2a9ed317572": {  # WMT 工程团队负责人
        "standard": "WMT 工程团队负责人，Java 与 ReactJS 全栈，分布式微服务架构，"
                    "订单管理系统与执行管理系统经验，债券股票外汇等业务",
        "colloquial": "财富管理科技那边要个工程团队负责人，全栈的，做过 OMS/EMS 的优先",
        "vague": "WMT 工程团队负责人",
    },
    "a22bec082c49": {  # 全栈工程团队负责人（含移动端）
        "standard": "全栈工程团队负责人，Java 与 ReactJS Web 开发，iOS/Android Swift 或 Kotlin，"
                    "微服务与微前端，端到端旅程架构，带工程团队",
        "colloquial": "全栈的工程团队负责人，Web 和移动端都要懂，能带团队做端到端旅程",
        "vague": "全栈 工程团队负责人",
    },
    "3d240f976de5": {  # AI应用开发工程师（Agent方向）
        "standard": "AI 应用开发工程师（Agent 方向），5 年以上开发架构经验，至少 1 年 AI 应用开发，"
                    "有完整 Agent 产品从 0 到 1 落地经验，Java 与 Spring Boot 微服务，"
                    "熟悉 RAG 与 LangGraph/LangChain、MCP 与 Tool-Calling",
        "colloquial": "蚂蚁要个做 Agent 的，五年以上，真做过 Agent 产品从零到一的，LangChain 那套要熟",
        "vague": "Agent 应用开发",
    },
}


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def clip(text, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def alive_jd_ids() -> set[str]:
    connection = sqlite3.connect(f"file:{DEV / 'db' / 'recruit.sqlite3'}?mode=ro", uri=True)
    try:
        return {row[0] for row in
                connection.execute("SELECT j.id FROM jd j WHERE j.deleted_at IS NULL").fetchall()}
    finally:
        connection.close()


def new_jds() -> list[dict]:
    """未删除、且不在原 20 个里的 JD（按 pool 顺序，保证可复现）。"""
    pool = json.loads((SRC / "jd_pool_all.json").read_text(encoding="utf-8"))
    selected = {item["jd_id"] for item in
                json.loads((SRC / "jd_selected.json").read_text(encoding="utf-8"))}
    alive = alive_jd_ids()
    return [item for item in pool if item["jd_id"] in alive and item["jd_id"] not in selected]


def stage_prepare() -> None:
    OUT.mkdir(exist_ok=True)
    selected = json.loads((SRC / "jd_selected.json").read_text(encoding="utf-8"))
    added = new_jds()
    if len(added) != len(NEW_QUERIES):
        raise SystemExit(f"新增 JD 数({len(added)})与已作者化问法数({len(NEW_QUERIES)})不一致")
    for item in added:
        if item["alias"] not in NEW_QUERIES:
            raise SystemExit(f"缺少作者化问法：{item['alias']} {item['title']}")
    expanded = selected + added
    (OUT / "jd_selected.json").write_text(
        json.dumps(expanded, ensure_ascii=False, indent=1), encoding="utf-8")

    queries = json.loads((SRC / "queries.json").read_text(encoding="utf-8"))
    for offset, item in enumerate(added):
        jid = f"J{len(selected) + offset + 1:02d}"
        queries[jid] = NEW_QUERIES[item["alias"]]
    (OUT / "queries.json").write_text(json.dumps(queries, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    print(f"选样：原 {len(selected)} + 新增 {len(added)} = {len(expanded)} 个 JD，"
          f"查询 {len([k for k in queries if not k.startswith('_')]) * 3} 条")

    # 原判决标签按 J01–J20 的字母编号，字母未变 → 原样复制即可继续对齐。
    for path in sorted(SRC.glob("judge_out_batch*.json")):
        shutil.copy2(path, OUT / path.name)
    print(f"已复制原判决产物 {len(list(SRC.glob('judge_out_batch*.json')))} 份")

    original = json.loads((SRC / "search_results.json").read_text(encoding="utf-8"))
    asyncio.run(_search_new(expanded, queries, original))


async def _search_new(expanded: list[dict], queries: dict, original: dict) -> None:
    from pydantic import SecretStr

    from kerui_recruit.core.settings import Settings
    from kerui_recruit.encryption.service import EncryptionService
    from kerui_recruit.providers.factory import build_providers
    from kerui_recruit.search.contracts import CandidateFilters
    from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
    from kerui_recruit.search.service import HybridSearchService

    raw = json.loads((DEV / "config" / "settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(DEV / "config" / "encryption.key")).decrypt(
        raw["siliconflow_api_key"])
    settings = Settings(data_root=DEV, session_token="judge-eval-expand",
                        siliconflow_api_key=SecretStr(key))
    bundle = build_providers(settings)
    index = LanceDBSearchIndex(DEV / "search", vector_dimension=bundle.vector_dimension,
                               embedding_model=raw["siliconflow_embedding_model"])
    service = HybridSearchService(index=index, embedding_provider=bundle.embedding,
                                  reranker_provider=bundle.reranker)

    results = dict(original)
    diagnostics: dict[str, dict] = {}
    missing = [f"J{i:02d}" for i in range(21, len(expanded) + 1) if f"J{i:02d}" not in results]
    started = time.monotonic()
    for n, jid in enumerate(missing, 1):
        results[jid] = {}
        diagnostics[jid] = {}
        for phrasing in PHRASINGS:
            results[jid][phrasing] = {}
            diagnostics[jid][phrasing] = {}
            for mode in MODES:
                page = await service.search(queries[jid][phrasing], CandidateFilters(),
                                            limit=LIMIT, mode=mode, rewrite_enabled=False)
                results[jid][phrasing][mode] = [alias(hit.candidate_id) for hit in page.items[:TOP_K]]
                diagnostics[jid][phrasing][mode] = {
                    "returned": len(page.items),
                    "degraded": list(page.degraded_reasons),
                    "empty_reason": page.empty_reason,
                }
        print(f"[{n:>2}/{len(missing)}] {jid} 用时 {time.monotonic() - started:.0f}s", flush=True)

    (OUT / "search_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    (OUT / "search_diagnostics.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=1), encoding="utf-8")
    empties = sum(1 for j in diagnostics.values() for p in j.values()
                  for d in p.values() if d["returned"] == 0)
    print(f"检索完成：新增 {len(missing)} 个 JD × {len(PHRASINGS)} 问法 × {len(MODES)} 模式，"
          f"零结果={empties}")
    if bundle.http_client is not None:
        await bundle.http_client.aclose()


def load_candidates(connection: sqlite3.Connection) -> dict[str, dict]:
    rows = connection.execute(
        "SELECT c.id, r.parsed_data, c.total_years, c.highest_degree "
        "FROM candidate c JOIN resume_document d ON d.candidate_id=c.id "
        "JOIN resume_revision r ON r.document_id=d.id "
        "WHERE c.status='AVAILABLE' AND c.deleted_at IS NULL "
        "AND r.is_current=1 AND r.status='READY'"
    ).fetchall()
    found: dict[str, dict] = {}
    for cid, parsed, total_years, degree in rows:
        payload = parsed
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        payload = payload if isinstance(payload, dict) else {}
        if cid in found:
            continue
        found[cid] = {"parsed": payload, "total_years": total_years, "degree": degree}
    return found


def digest(entry: dict) -> str:
    """与 `judge_eval_build_input_2026_09_19.py:digest` **逐字一致**，否则新旧判决不可比。"""
    p = entry["parsed"]
    years = p.get("total_years") or entry["total_years"]
    degree = p.get("highest_degree") or entry["degree"]
    school = p.get("school") or ""
    parts = [f"画像：{clip(p.get('ai_profile_narrative') or p.get('ai_profile_summary'), NARRATIVE_CHARS)}"]

    head = f"基本情况：{years or '未知'} 年经验 / {degree or '学历未知'}"
    if school:
        head += f" / {clip(school, 24)}"
    if p.get("location"):
        head += f" / 现居 {p['location']}"
    if p.get("current_company") or p.get("current_title"):
        head += f" / 当前 {clip(p.get('current_company'), 20)} {clip(p.get('current_title'), 24)}"
    parts.append(head)

    skills = p.get("skills") or []
    if skills:
        parts.append(f"技能：{clip('、'.join(str(s) for s in skills), SKILL_CHARS)}")

    experiences = [e for e in (p.get("experiences") or []) if isinstance(e, dict)]
    if experiences:
        lines = []
        for exp in experiences[:MAX_EXPERIENCES]:
            period = f"{exp.get('start_date') or '?'}~{exp.get('end_date') or '至今'}"
            lines.append(f"  · {period} {clip(exp.get('company'), 22)} {clip(exp.get('title'), 26)}："
                         f"{clip(exp.get('summary'), EXP_SUMMARY_CHARS)}")
        parts.append("工作经历：\n" + "\n".join(lines))

    projects = [x for x in (p.get("projects") or []) if isinstance(x, dict)]
    if projects:
        lines = []
        for proj in projects[:MAX_PROJECTS]:
            lines.append(f"  · {clip(proj.get('name'), 30)}｜技术栈 {clip(proj.get('tech_stack'), 60)}｜"
                         f"{clip(proj.get('summary'), PROJ_SUMMARY_CHARS)}")
        parts.append("项目经历：\n" + "\n".join(lines))

    # 注：不再附加索引侧的年限/学历标注（读取需 pylance；判断依据以简历解析结果为准）
    return "\n".join(parts)


def stage_blind() -> None:
    results = json.loads((OUT / "search_results.json").read_text(encoding="utf-8"))
    jds = json.loads((OUT / "jd_selected.json").read_text(encoding="utf-8"))
    queries = json.loads((OUT / "queries.json").read_text(encoding="utf-8"))
    original_blinding = json.loads((SRC / "blinding_map.json").read_text(encoding="utf-8"))

    connection = sqlite3.connect(f"file:{DEV / 'db' / 'recruit.sqlite3'}?mode=ro", uri=True)
    universe = load_candidates(connection)
    connection.close()
    by_alias = {alias(cid): cid for cid in universe}

    blinding: dict[str, dict] = dict(original_blinding)
    batch: dict[str, dict] = {}
    batches: list[dict] = []
    added = 0
    for i, jd in enumerate(jds, 1):
        jid = f"J{i:02d}"
        if jid in original_blinding:
            continue
        added += 1
        lists = results[jid]
        union: list[str] = []
        seen: set[str] = set()
        for phrasing in PHRASINGS:
            for mode in MODES:
                for cand in lists[phrasing][mode]:
                    if cand not in seen:
                        seen.add(cand)
                        union.append(cand)
        rng = random.Random(jd["alias"])
        shuffled = union[:]
        rng.shuffle(shuffled)
        letters = {cand: chr(ord("A") + n) for n, cand in enumerate(shuffled)}
        entries = []
        for cand in shuffled:
            cid = by_alias.get(cand)
            if cid is None:
                entries.append({"label": letters[cand], "digest": "（该候选人已不在库中）"})
                continue
            entry = dict(universe[cid])
            entry["cid"] = cid
            entries.append({"label": letters[cand], "digest": digest(entry)})
        batch[jid] = {
            "jd_id": jid, "company": jd["company"], "title": jd["title"],
            "min_years": jd["min_years"], "highest_degree": jd["highest_degree"],
            "location": jd["location"], "jd_text": jd["text"], "candidates": entries,
        }
        blinding[jid] = {
            "jd_alias": jd["alias"], "company": jd["company"], "title": jd["title"],
            "letters": {letters[cand]: cand for cand in shuffled},
            "candidate_count": len(shuffled), "query_texts": queries[jid],
        }
        if len(batch) >= BATCH_SIZE:
            batches.append(batch)
            batch = {}
    if batch:
        batches.append(batch)

    for n, payload in enumerate(batches, 1):
        path = OUT / f"judge_input_new_batch{n}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"judge_input_new_batch{n}: JD={list(payload)} 字符≈{path.stat().st_size // 1000}K")

    (OUT / "blinding_map.json").write_text(json.dumps(blinding, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    sizes = [item["candidate_count"] for jid, item in blinding.items() if jid not in original_blinding]
    if sizes:
        print(f"新增 {added} 个 JD，盲测候选数 min/中位/max="
              f"{min(sizes)}/{sorted(sizes)[len(sizes)//2]}/{max(sizes)}")
    judged = [f"J{i:02d}" for i in range(1, len(jds) + 1)
              if f"J{i:02d}" not in original_blinding and f"J{i:02d}" not in _new_judged()]
    print(f"待判决：{judged}")


def _new_judged() -> set[str]:
    judged: set[str] = set()
    for path in sorted(OUT.glob("judge_out_new_batch*.json")):
        judged.update(json.loads(path.read_text(encoding="utf-8")))
    return judged


def stage_verify() -> None:
    jds = json.loads((OUT / "jd_selected.json").read_text(encoding="utf-8"))
    crippled = [(f"J{i:02d}", len(jd["text"])) for i, jd in enumerate(jds, 1)
                if len(jd["text"]) < 200]
    queries = {k: v for k, v in
               json.loads((OUT / "queries.json").read_text(encoding="utf-8")).items()
               if not k.startswith("_")}
    results = json.loads((OUT / "search_results.json").read_text(encoding="utf-8"))
    blinding = json.loads((OUT / "blinding_map.json").read_text(encoding="utf-8"))
    print(f"JD={len(jds)} 查询JD={len(queries)}（×3 问法={len(queries) * 3} 条）"
          f" 检索={len(results)} 盲测={len(blinding)}")
    print(f"原文<200 字符的 JD={crippled}")

    # 标签必须逐字符对齐：候选数超过 62 时 chr(ord('A')+n) 会溢到 DEL/C1 控制字符，
    # 判决方只要有一个字符被改写，该候选的分数就静默丢失。
    print("\n新增 JD 标签对齐：")
    misaligned = []
    for n in range(1, 5):
        input_path = OUT / f"judge_input_new_batch{n}.json"
        output_path = OUT / f"judge_out_new_batch{n}.json"
        if not input_path.exists() or not output_path.exists():
            print(f"  batch{n}: 输入或判决缺失")
            continue
        inputs = json.loads(input_path.read_text(encoding="utf-8"))
        outputs = json.loads(output_path.read_text(encoding="utf-8"))
        for jid, jd in inputs.items():
            labels_in = [item["label"] for item in jd["candidates"]]
            labels_out = [item["label"] for item in outputs.get(jid, {}).get("judgements", [])]
            labels_blind = list(blinding[jid]["letters"])
            same = set(labels_in) == set(labels_out) == set(labels_blind)
            print(f"  {jid} 输入={len(labels_in)} 判决={len(labels_out)} 映射={len(labels_blind)} "
                  f"集合一致={same} 顺序一致={labels_in == labels_out}")
            if not same or labels_in != labels_out:
                misaligned.append(jid)

    print(f"\n标签异常={misaligned}")

    original = json.loads((SRC / "blinding_map.json").read_text(encoding="utf-8"))
    old_judged: dict[str, dict] = {}
    for path in sorted(SRC.glob("judge_out_batch*.json")):
        old_judged.update(json.loads(path.read_text(encoding="utf-8")))
    print("\n原 20 个 JD 的映射/判决条数：")
    for jid in sorted(original, key=lambda value: int(value[1:])):
        mapping = len(original[jid]["letters"])
        judged = len(old_judged.get(jid, {}).get("judgements", []))
        flag = "OK" if mapping == judged else "不一致"
        print(f"  {jid} 映射={mapping} 判决={judged} {flag}")

    grades = _load_grades()
    print(f"\n已判决 JD={len(grades)} 个")
    missing = [f"J{i:02d}" for i in range(1, len(jds) + 1) if f"J{i:02d}" not in grades]
    print(f"未判决={missing}")
    counts = sorted(len(table) for table in grades.values())
    print(f"每个 JD 有分的候选数 min/中位/max={counts[0]}/{counts[len(counts) // 2]}/{counts[-1]}")


def _load_grades() -> dict[str, dict[str, int]]:
    blinding = json.loads((OUT / "blinding_map.json").read_text(encoding="utf-8"))
    judgements: dict[str, dict[str, dict]] = {}
    for path in sorted(list(OUT.glob("judge_out_batch*.json"))
                       + list(OUT.glob("judge_out_new_batch*.json"))):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for jid, body in payload.items():
            judgements[jid] = {item["label"]: item for item in body["judgements"]}
    grades: dict[str, dict[str, int]] = {}
    for jid, info in blinding.items():
        table = judgements.get(jid, {})
        grades[jid] = {candidate: int(table[letter]["grade"])
                       for letter, candidate in (info.get("letters") or {}).items()
                       if letter in table}
    return grades


def main() -> None:
    parser = argparse.ArgumentParser(description="分层问法集扩样（20 → 36 JD）")
    parser.add_argument("--stage", choices=("prepare", "blind", "verify"), required=True)
    options = parser.parse_args()
    if options.stage == "prepare":
        stage_prepare()
    elif options.stage == "blind":
        stage_blind()
    else:
        stage_verify()


if __name__ == "__main__":
    main()
