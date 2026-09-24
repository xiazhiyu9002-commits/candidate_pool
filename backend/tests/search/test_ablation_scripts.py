"""阶段 3/4 消融脚本的管线验证：用假 provider 端到端跑一遍，确保脚本本身可用。

真实报告需要付费的 embedding / reranker 调用，不在单测里跑；这里只证明：
- ``--suite readside`` 真的会逐档切换旋钮、收集诊断、写报告，并在结束后恢复默认档位；
- ``--suite index`` 真的会用同一份快照构建 v8 与 v9 两个隔离索引并对比。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from kerui_recruit.search import lancedb_index as index_module
from kerui_recruit.search import service as service_module
from kerui_recruit.search.contracts import CandidateFilters, SearchChunk
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "retrieval_ablation_2026_09_20.py"

_MODULE = None


def _load_script():
    """始终返回同一个脚本模块实例。

    若每次返回新实例，测试就可能「把补丁打在一个实例上、却调用另一个实例」，
    于是真的去打远端 embedding 并读全量库（曾发生过一次）。
    """
    global _MODULE
    if _MODULE is None:
        spec = importlib.util.spec_from_file_location("retrieval_ablation_script", SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULE = module
    return _MODULE


class _Embedding:
    async def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]

    async def embed_query(self, text):
        return [1.0, 0.0]


class _Reranker:
    async def rerank(self, query, documents):
        return list(range(len(documents)))


class _Key:
    def decrypt(self, value):
        return "fake-key"


def _seed_index(root: Path, *, embedding_model: str) -> LanceDBSearchIndex:
    index = LanceDBSearchIndex(root, vector_dimension=2, embedding_model=embedding_model)
    index.upsert([
        SearchChunk("a-p", "a", "r-a", "Java 平台", (1.0, 0.0), 5, "MASTER", "上海", "AVAILABLE",
                    keyword_text="Java 平台", keyword_index_text="java 平台",
                    chunk_type="parent", kind="parent"),
        SearchChunk("b-p", "b", "r-b", "支付平台", (0.9, 0.1), 5, "MASTER", "上海", "AVAILABLE",
                    keyword_text="支付平台", keyword_index_text="支付 平台",
                    chunk_type="parent", kind="parent"),
    ])
    return index


def test_embed_in_batches_respects_batch_size_and_concurrency(monkeypatch) -> None:
    """分批 + 有界并发：单请求不超批大小，同时在飞数量不超并发上限，且顺序不乱。"""
    module = _load_script()
    monkeypatch.setattr(module, "EMBED_BATCH", 2)
    monkeypatch.setattr(module, "EMBED_CONCURRENCY", 3)
    in_flight = 0
    peak = 0

    class Slow:
        async def embed_documents(self, texts):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return [[float(text[1:])] for text in texts]

    texts = [f"x{index}" for index in range(7)]
    vectors = asyncio.run(module.embed_in_batches(Slow(), texts))
    assert [vector[0] for vector in vectors] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert peak <= 3
    assert peak > 1  # 确实并发了，而不是退化成串行


def test_load_queries_ignores_note_keys_and_expands_phrasings(tmp_path: Path) -> None:
    """查询集允许有 _note 这类说明性字符串键；多种问法要各自成条（20 JD × 3 = 60 条）。"""
    module = _load_script()
    path = tmp_path / "queries.json"
    path.write_text(json.dumps({
        "_note": "每个 JD 三种问法",
        "J01": {"vague": "AI 效能", "standard": "AI 研发效能工程师", "colloquial": "想做效能的人"},
        "J02": {"vague": "大模型"},
    }, ensure_ascii=False), encoding="utf-8")

    assert module.load_queries(path) == [("J01:vague", "AI 效能"), ("J02:vague", "大模型")]
    assert module.load_queries(path, ("vague", "standard")) == [
        ("J01:vague", "AI 效能"),
        ("J01:standard", "AI 研发效能工程师"),
        ("J02:vague", "大模型"),
    ]


def test_score_arm_follows_judge_metric_definitions() -> None:
    """指标必须与 judge_eval_metrics 同口径，否则门槛判据不可比。"""
    module = _load_script()
    grades = {"J01": {"a": 3, "b": 1, "c": 2}}

    summary, per_query = module.score_arm({"J01:vague": {"hybrid": ["a", "c", "b"]}}, grades)
    assert summary["ndcg@10"] == 1.0          # 与理想序一致
    assert summary["p@5"] == round(2 / 3, 4)  # a(3)、c(2) 强相关
    assert summary["hits@10"] == 1.0          # 强相关 {a, c} 全部被前 10 命中
    assert summary["top1_ge2"] == 1.0
    assert summary["queries"] == 1
    assert set(per_query) == {"J01:vague|hybrid"}

    worst, _ = module.score_arm({"J01:vague": {"hybrid": ["b", "c", "a"]}}, grades)
    assert 0 < worst["ndcg@10"] < 1.0
    assert worst["top1_ge2"] == 0.0
    assert worst["hits@10"] == 1.0            # 召回不降，只是排序变差

    comparison = module.compare_arms({"v9": (summary, per_query),
                                      "v8": module.score_arm({"J01:vague": {"hybrid": ["b", "c", "a"]}},
                                                             grades)},
                                     "v8")
    assert comparison["v9"]["wins"] == 1 and comparison["v9"]["losses"] == 0
    assert comparison["v9"]["mean_ndcg_delta"] > 0


def test_mean_by_mode_splits_modes_so_keyword_is_not_masked() -> None:
    """一次跑多模式时，混在一起的均值会掩盖单模式，必须再按模式分解一份。"""
    module = _load_script()
    grades = {"J01": {"a": 3, "c": 2, "b": 1}}
    _summary, per_query = module.score_arm({
        "J01:vague": {"keyword": ["a", "c", "b"], "hybrid": ["b", "c", "a"]},
    }, grades)

    by_mode = module.mean_by_mode(per_query)
    assert set(by_mode) == {"keyword", "hybrid"}
    assert by_mode["keyword"]["ndcg@10"] == 1.0
    assert by_mode["hybrid"]["ndcg@10"] < 1.0
    assert by_mode["keyword"]["queries"] == 1


def test_attach_metrics_requires_labels(tmp_path: Path) -> None:
    """没有标注就只输出排序差异，不能凭空造指标。"""
    module = _load_script()
    assert module.attach_metrics({"v8": {"top": {}}}, "v8", tmp_path) is None
    assert module.attach_metrics({"v8": {"top": {}}}, "v8", None) is None


def test_index_suite_reports_judge_metrics(tmp_path: Path, monkeypatch) -> None:
    """报告要带上判决式指标块，才能判发布门槛。

    注意：必须使用 ``_prepare_index_suite`` 返回的那个模块实例——它才是打了假 provider /
    假快照补丁的实例，用别处的实例会真的去打远端 embedding 并读全量库。
    """
    module = _prepare_index_suite(tmp_path, monkeypatch, _Embedding())
    judge = tmp_path / "judge"
    judge.mkdir()
    (judge / "blinding_map.json").write_text(json.dumps({
        "J01": {"letters": {"A": module.alias("c-0")}},
    }), encoding="utf-8")
    (judge / "judge_out_batch1.json").write_text(json.dumps({
        "J01": {"judgements": [{"label": "A", "grade": 3}]},
    }), encoding="utf-8")

    output = tmp_path / "index.json"
    asyncio.run(module.run_suite_index([("J01:vague", "Java 后端")], output, ("vector",), {},
                                       judge))

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["metrics"]["baseline"] == "v8"
    for version in ("v8", "v9"):
        assert report["metrics"]["arms"][version]["queries"] == 1
        assert report["metrics"]["arms"][version]["ndcg@10"] == 1.0
    assert "v9" in report["metrics"]["vs_baseline"]


def test_index_suite_never_sends_blank_text_or_loses_child_fields(tmp_path: Path,
                                                                 monkeypatch) -> None:
    """空文本绝不能进请求体（上游会以不可重试的 E_API_FORMAT 拒掉整个构建）。

    顺带锁住子片段的映射：词法/向量文本取片段自身，硬字段与 parent_id 继承父。
    """
    seen: list[str] = []

    class TextSpy:
        async def embed_documents(self, texts):
            seen.extend(texts)
            return [[1.0, 0.0] for _ in texts]

        async def embed_query(self, text):
            return [1.0, 0.0]

    snapshot = [
        {"revision_id": "r-empty", "candidate_id": "c-empty", "display_name": "", "data": {}},
        {"revision_id": "r-ok", "candidate_id": "c-ok", "display_name": "甲",
         "data": {"summary": "Java 后端",
                  "experiences": [{"company": "某公司", "title": "工程师", "summary": "支付清结算"}]}},
    ]
    module = _prepare_index_suite(tmp_path, monkeypatch, TextSpy(), snapshot=snapshot)
    output = tmp_path / "index.json"
    asyncio.run(module.run_suite_index([("Q1", "Java 后端")], output, ("vector",), {}))

    assert seen and all(text.strip() for text in seen)   # 没有空串 / 纯空白
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["chunk_counts"]["v8"] == report["chunk_counts"]["v9"] >= 2

    index = LanceDBSearchIndex(tmp_path / ".tmp-ablation" / "index-v9", vector_dimension=2,
                               embedding_model="ablation")
    rows = index.database.open_table(index.table_name).to_arrow().to_pylist()
    assert {row["candidate_id"] for row in rows} == {"c-ok"}      # 空候选人被整条跳过
    child = next(row for row in rows if row["chunk_type"] == "child")
    assert child["parent_id"] == "r-ok"
    assert child["keyword_text"] == child["vector_text"]          # 子片段词法文本取自身
    assert child["candidate_id"] == "c-ok"


def test_readside_suite_switches_configs_and_restores_defaults(tmp_path: Path, monkeypatch) -> None:
    module = _load_script()
    dev = tmp_path / "dev"
    _seed_index(dev / "search", embedding_model="fake-embed")
    monkeypatch.setattr(module, "DEV", dev)
    monkeypatch.setattr(module, "settings_and_keys",
                        lambda: ({"siliconflow_embedding_model": "fake-embed"}, _Key()))
    monkeypatch.setattr(module, "live_index_dimension", lambda: 2)
    # 单测不去碰真实库：学校别名组按空处理（生产口径由真实运行覆盖）。
    monkeypatch.setattr(module, "school_alias_groups", lambda: None)
    monkeypatch.setattr(module, "providers", lambda *args: (_Embedding(), _Reranker()))

    output = tmp_path / "readside.json"
    asyncio.run(module.run_suite_readside([("Q1", "Java 平台")], output,
                                          ("vector", "hybrid"), {"marker": "frozen"}))

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["frozen"] == {"marker": "frozen"}
    assert set(report["results"]) == {
        "baseline", "quota20", "quota40", "quota60",
        "fts1.25/vec1.0", "fts1.0/vec1.25",
        "coverage_tiebreak", "coverage_light",
    }
    for payload in report["results"].values():
        assert payload["top"]["Q1"].keys() == {"vector", "hybrid"}
        assert "p50" in payload["latency_summary"]
        assert isinstance(payload["kind_distribution"], dict)
    assert set(report["jaccard_vs_baseline"]) == set(report["results"]) - {"baseline"}

    # 档位必须在报告写完后恢复默认，否则会污染同进程内的后续调用。
    assert service_module.VECTOR_KIND_RECALL_QUOTA is None
    assert service_module.CONCEPT_COVERAGE_MODE == "off"
    assert index_module.FUSION_WEIGHT_BM25 == 1.0
    assert index_module.FUSION_WEIGHT_VECTOR == 1.0


class _RecordingReranker:
    def __init__(self) -> None:
        self.calls = 0

    async def rerank(self, query, documents):
        self.calls += 1
        return list(range(len(documents)))


def test_readside_suite_can_limit_configs_and_run_keyword_arm(tmp_path: Path, monkeypatch) -> None:
    """keyword 臂只走 FTS：补采第三模式时既不重复跑全部档位，也不应调用 reranker。"""
    module = _load_script()
    dev = tmp_path / "dev"
    _seed_index(dev / "search", embedding_model="fake-embed")
    monkeypatch.setattr(module, "DEV", dev)
    monkeypatch.setattr(module, "settings_and_keys",
                        lambda: ({"siliconflow_embedding_model": "fake-embed"}, _Key()))
    monkeypatch.setattr(module, "live_index_dimension", lambda: 2)
    monkeypatch.setattr(module, "school_alias_groups", lambda: None)
    reranker = _RecordingReranker()
    monkeypatch.setattr(module, "providers", lambda *args: (_Embedding(), reranker))

    output = tmp_path / "readside-keyword.json"
    asyncio.run(module.run_suite_readside([("Q1", "Java 平台")], output, ("keyword",),
                                          {"marker": "frozen"}, None, ("baseline",)))

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["configs"] == ["baseline"]
    assert set(report["results"]) == {"baseline"}
    assert set(report["results"]["baseline"]["top"]["Q1"]) == {"keyword"}
    assert reranker.calls == 0


def test_readside_suite_rejects_unknown_config(tmp_path: Path, monkeypatch) -> None:
    module = _load_script()
    dev = tmp_path / "dev"
    _seed_index(dev / "search", embedding_model="fake-embed")
    monkeypatch.setattr(module, "DEV", dev)
    monkeypatch.setattr(module, "settings_and_keys",
                        lambda: ({"siliconflow_embedding_model": "fake-embed"}, _Key()))
    monkeypatch.setattr(module, "live_index_dimension", lambda: 2)
    monkeypatch.setattr(module, "providers", lambda *args: (_Embedding(), _Reranker()))

    with pytest.raises(SystemExit, match="quota999"):
        asyncio.run(module.run_suite_readside([("Q1", "Java 平台")], tmp_path / "x.json",
                                              ("keyword",), {}, None, ("quota999",)))


def _prepare_index_suite(tmp_path: Path, monkeypatch, embedding, batch: int | None = None,
                         candidates: int = 1, candidate_batch: int | None = None,
                         snapshot: list[dict] | None = None):
    """把 index suite 的数据与 provider 依赖全部换成本地假件。"""
    module = _load_script()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "read_snapshot_rows", lambda: snapshot if snapshot is not None else [{
        "revision_id": f"r-{index}", "candidate_id": f"c-{index}", "display_name": f"甲{index}",
        "data": {
            "summary": "Java 后端",
            "skills": ["Java"],
            "experiences": [{"company": "某公司", "title": "工程师",
                             "tech_stack": ["Kafka"], "summary": "支付清结算"}],
        },
    } for index in range(candidates)])
    monkeypatch.setattr(module, "live_index_dimension", lambda: 2)
    monkeypatch.setattr(module, "settings_and_keys",
                        lambda: ({"siliconflow_embedding_model": "fake-embed"}, _Key()))
    monkeypatch.setattr(module, "providers", lambda *args: (embedding, _Reranker()))
    if batch is not None:
        monkeypatch.setattr(module, "EMBED_BATCH", batch)
    if candidate_batch is not None:
        monkeypatch.setattr(module, "CANDIDATE_BATCH", candidate_batch)
    return module


def test_index_suite_builds_v8_and_v9_from_one_snapshot(tmp_path: Path, monkeypatch) -> None:
    module = _prepare_index_suite(tmp_path, monkeypatch, _Embedding())

    output = tmp_path / "index.json"
    asyncio.run(module.run_suite_index([("Q1", "Java 后端")], output, ("vector",), {"marker": 1}))

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["frozen"] == {"marker": 1}
    assert set(report["results"]) == {"v8", "v9"}
    # 同一快照、同一 embedding：两臂的 chunk 数量一致（parent + profile_point + experience），
    # 差别只在 vector_text 口径。
    assert report["chunk_counts"]["v8"] == report["chunk_counts"]["v9"] == 3
    assert set(report["jaccard_v8_vs_v9"]) == {"v9"}
    # 文档口径分 v8/v9 两臂，但物理索引版本都是当前 chunk 10。
    for version in ("v8", "v9"):
        metadata = json.loads((tmp_path / ".tmp-ablation" / f"index-{version}"
                               / "candidate-index-metadata.json").read_text(encoding="utf-8"))
        assert metadata["chunk_version"] == "10"


def test_index_suite_embeds_in_batches_and_accumulates_windows(tmp_path: Path, monkeypatch) -> None:
    """数万条文本不能塞进单个请求，也不能全留在内存：分批嵌入 + 分批落库且必须累加。

    用 CANDIDATE_BATCH=1 把 2 个候选人拆成两个窗口，验证第二个窗口不会覆盖第一个窗口。
    """
    sizes: list[int] = []

    class BatchSpy:
        async def embed_documents(self, texts):
            sizes.append(len(texts))
            return [[1.0, 0.0] for _ in texts]

        async def embed_query(self, text):
            return [1.0, 0.0]

    module = _prepare_index_suite(tmp_path, monkeypatch, BatchSpy(), batch=1,
                                  candidates=2, candidate_batch=1)
    output = tmp_path / "index.json"
    asyncio.run(module.run_suite_index([("Q1", "Java 后端")], output, ("vector",), {}))

    # 每个候选人 3 条文本 × 2 个候选人 × 2 个版本；EMBED_BATCH=1 → 每次请求 1 条。
    assert sizes == [1] * 12
    report = json.loads(output.read_text(encoding="utf-8"))
    # 两个窗口累加而不是互相覆盖：每个版本都应有 2 个候选人的全部 chunk。
    assert report["chunk_counts"]["v8"] == report["chunk_counts"]["v9"] == 6
