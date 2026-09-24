"""改写 A/B 脚本的管线验证：生产 API 入口脚本必须能读真实查询集。

真实报告需要付费的 LLM/embedding 调用，不在单测里跑；这里只覆盖会让脚本在真数据上
直接崩掉的读取路径（``.tmp-judge/queries.json`` 里的 ``_note`` 说明键）。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "rewrite_ab_via_api_2026_09_20.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("rewrite_ab_api_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_queries_skips_note_keys_and_expands_phrasings(tmp_path: Path) -> None:
    module = _load_script()
    path = tmp_path / "queries.json"
    path.write_text(json.dumps({
        "_note": "每个 JD 三种问法，由评测者独立编写",
        "J01": {"vague": "AI 效能 全栈", "standard": "AI 研发效能工程师", "colloquial": "想做效能的人"},
        "J02": {"vague": "大模型 算法"},
    }, ensure_ascii=False), encoding="utf-8")

    assert module.load_queries(path) == [("J01:vague", "AI 效能 全栈"), ("J02:vague", "大模型 算法")]
    assert module.load_queries(path, ("vague", "standard", "colloquial")) == [
        ("J01:vague", "AI 效能 全栈"),
        ("J01:standard", "AI 研发效能工程师"),
        ("J01:colloquial", "想做效能的人"),
        ("J02:vague", "大模型 算法"),
    ]


def test_load_queries_accepts_list_shape(tmp_path: Path) -> None:
    module = _load_script()
    path = tmp_path / "queries.json"
    path.write_text(json.dumps([{"query": "Java 后端"}, {"query": "支付"}], ensure_ascii=False),
                    encoding="utf-8")
    assert module.load_queries(path) == [("01", "Java 后端"), ("02", "支付")]


def test_judge_metrics_scores_both_arms(tmp_path: Path) -> None:
    """两臂都要按同口径打分，且改写变好时被判为胜。"""
    module = _load_script()
    judge = tmp_path / "judge"
    judge.mkdir()
    (judge / "blinding_map.json").write_text(json.dumps({
        "J01": {"letters": {"A": module.alias("c0"), "B": module.alias("c1")}},
    }), encoding="utf-8")
    (judge / "judge_out_batch1.json").write_text(json.dumps({
        "J01": {"judgements": [{"label": "A", "grade": 3}, {"label": "B", "grade": 0}]},
    }), encoding="utf-8")

    good = [module.alias("c0"), module.alias("c1")]
    bad = [module.alias("c1"), module.alias("c0")]
    records = [{"query_key": "J01:vague", "mode": "hybrid",
                "rewrite_off": {"ids": bad}, "rewrite_on": {"ids": good}}]

    metrics = module._judge_metrics(records, judge)
    assert metrics["arms"]["rewrite_off"]["ndcg@10"] < metrics["arms"]["rewrite_on"]["ndcg@10"]
    assert metrics["arms"]["rewrite_on"]["top1_ge2"] == 1.0
    assert metrics["vs_baseline"]["rewrite_on"]["wins"] == 1
    assert metrics["vs_baseline"]["rewrite_on"]["losses"] == 0


def test_judge_metrics_requires_labels(tmp_path: Path) -> None:
    module = _load_script()
    assert module._judge_metrics([], tmp_path) is None
