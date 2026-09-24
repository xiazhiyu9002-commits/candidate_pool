"""画像双形态结构化契约测试：一致性校验 + 确定性回退 + 硬指标校验与定点重写。"""
from __future__ import annotations

import asyncio

from kerui_recruit.jd.profile import JdProfileGenerator, JdPointsPlan, JdProfilePair
from kerui_recruit.providers import profile_spec
from kerui_recruit.providers.profile_pair import (
    REGEN_TIMEOUT_SECONDS,
    REPAIR_BUDGET_SECONDS,
    ProfileFact,
    ProfilePair,
    ProfilePoint,
    build_profile_pair,
    pair_from_points,
    produce_pair_with_vet,
    reconcile_pair,
    repair_profile_points,
    repaired_profile_view,
    split_profile_clauses,
)


def test_pair_from_points_derives_narrative_facts_and_compact():
    # 模型只给要点时，整段按要点顺序拼接：逐字同源天然成立，facts/compact 一并派生。
    pair = pair_from_points([
        ProfilePoint(text="本科及以上", evidence_paths=["requirements[0]"]),
        ProfilePoint(text="主导过金融交易系统"),
        ProfilePoint(text="熟悉 Java、Spring"),
    ])

    assert pair.narrative == "本科及以上。主导过金融交易系统。熟悉 Java、Spring"
    assert [p.text for p in pair.points] == ["本科及以上", "主导过金融交易系统", "熟悉 Java、Spring"]
    assert [f.text for f in pair.facts] == [p.text for p in pair.points]
    assert pair.facts[0].evidence_paths == ["requirements[0]"]
    assert pair.compact == "本科及以上"
    assert pair.is_consistent()  # 派生结果必须自洽


def test_pair_from_points_drops_blank_points_and_handles_empty():
    assert [p.text for p in pair_from_points([ProfilePoint(text="  "), ProfilePoint(text="有效要点")]).points] == ["有效要点"]
    empty = pair_from_points([])
    assert empty.narrative == "" and empty.points == [] and empty.facts == []


def test_build_profile_pair_splits_lines_and_derives_compact():
    pair = build_profile_pair("7年后端研发\n主导交易系统\n精通 Java 微服务")
    assert pair.narrative.startswith("7年后端研发")
    assert [p.text for p in pair.points] == ["7年后端研发", "主导交易系统", "精通 Java 微服务"]
    assert pair.compact == "7年后端研发"


def test_split_profile_clauses_breaks_single_paragraph_into_points():
    # 单段人工画像也应拆成真正的多分点，且不新增事实。
    points = split_profile_clauses("7年后端经验，主导交易系统，精通Java微服务；负责团队管理")
    assert points == ["7年后端经验，主导交易系统，精通Java微服务", "负责团队管理"]
    # 枚举顿号不切分。
    assert split_profile_clauses("精通 Java、Python、Go") == ["精通 Java、Python、Go"]


def test_comma_is_not_a_point_boundary():
    """逗号是句内停顿，不切分点。

    按逗号切会让画像在界面上变成「一词一行」——历史数据里的碎片分点就是这么来的，
    因为确定性回退路径（`build_profile_pair`）用的正是 `split_profile_clauses`。
    """
    points = split_profile_clauses("3年后端开发经验，现任京东后端开发工程师。长期处于互联网行业。")

    assert points == ["3年后端开发经验，现任京东后端开发工程师", "长期处于互联网行业"]


def test_repair_recomputes_only_provably_comma_split_points():
    """存量分点的修复必须**只在能证明被切碎时**才做。"""
    # ① 逗号切出来的碎片：有短分点、且拼不回原段 → 重算。
    assert repair_profile_points(
        "3年后端经验，现任京东后端开发工程师。",
        ["3年后端经验", "现任京东后端开发工程师"],
    ) == ["3年后端经验，现任京东后端开发工程师"]

    # ② 短但合法的整句分点：虽然很短，但拼起来就等于原段 → 不动（避免无意义的改写）。
    assert repair_profile_points("7年后端研发。主导交易系统", ["7年后端研发", "主导交易系统"]) is None

    # ③ 模型产出的「核心 3~5 条」子集：每条都是整句 → 不动（重算会把它扩成全部句子，是改语义）。
    assert repair_profile_points(
        "拥有 7 年电商后端研发经验，主导交易系统重构。精通 Java 微服务，负责 5 人团队管理。",
        ["拥有 7 年电商后端研发经验，主导交易系统重构", "精通 Java 微服务，负责 5 人团队管理"],
    ) is None

    # ④ 空输入不产生修复。
    assert repair_profile_points("", ["a"]) is None
    assert repair_profile_points("7年后端研发。", []) is None


def test_repaired_profile_view_rewrites_only_points_and_keeps_source():
    """展示视图只改 `ai_profile_points`，整体段落与原始证据字段原样保留。"""
    parsed = {
        "ai_profile_summary": "3年后端经验，现任京东后端开发工程师。",
        "ai_profile_points": [
            {"text": "3年后端经验", "evidence_paths": ["experiences[0].summary"]},
            {"text": "现任京东后端开发工程师", "evidence_paths": []},
        ],
        "skills": ["Java"],
    }

    view = repaired_profile_view(parsed)

    assert [p["text"] for p in view["ai_profile_points"]] == ["3年后端经验，现任京东后端开发工程师"]
    assert view["ai_profile_summary"] == parsed["ai_profile_summary"]
    assert view["skills"] == ["Java"]
    # 纯视图：原 dict 不被就地修改，落库内容保持碎片（由回填脚本单独处理）。
    assert len(parsed["ai_profile_points"]) == 2


def test_repaired_profile_view_is_identity_when_nothing_to_fix():
    parsed = {
        "ai_profile_summary": "7年后端研发。主导交易系统",
        "ai_profile_points": [{"text": "7年后端研发"}, {"text": "主导交易系统"}],
    }
    # 无需修复时直接返回同一个对象，调用方不必担心多余拷贝。
    assert repaired_profile_view(parsed) is parsed
    assert repaired_profile_view(None) is None


def test_consistent_pair_requires_facts_in_both_forms():
    pair = ProfilePair(
        facts=[ProfileFact(text="交易系统", evidence_paths=["projects[0].summary"])],
        narrative="负责交易系统后端研发",
        points=[ProfilePoint(text="负责交易系统后端研发")],
        compact="后端研发",
    )
    assert pair.is_consistent() is True


def test_fact_only_in_one_form_is_inconsistent():
    pair = ProfilePair(
        facts=[ProfileFact(text="交易系统", evidence_paths=["projects[0].summary"])],
        narrative="负责交易系统后端研发",
        points=[ProfilePoint(text="负责支付系统后端研发")],
        compact="后端研发",
    )
    assert pair.is_consistent() is False


def test_reconcile_pair_rebuilds_narrative_from_points_when_facts_are_rewritten():
    # 模型把 facts 写成 points 的改写：整体段落未逐字包含全部分点，按分点重建后应自洽。
    pair = ProfilePair(
        facts=[ProfileFact(text="张三具备5年后端研发经验的工程师")],
        narrative="张三具备5年后端研发经验的工程师",
        points=[
            ProfilePoint(text="5年后端研发经验", evidence_paths=["experiences[0].summary"]),
            ProfilePoint(text="负责交易系统"),
        ],
        compact="5年后端研发",
    )
    reconciled = reconcile_pair(pair)
    assert reconciled is not pair
    # 整体段落由分点顺序拼成，且必须是「整段无换行」。
    assert reconciled.narrative == "5年后端研发经验。负责交易系统"
    assert "\n" not in reconciled.narrative
    assert reconciled.is_consistent() is True
    assert reconciled.facts[0].evidence_paths == ["experiences[0].summary"]


def test_reconcile_pair_keeps_narrative_containing_all_points():
    # facts 是改写（未逐字命中分点），但整体段落已包含全部分点：保留模型原文段落。
    pair = ProfilePair(
        facts=[ProfileFact(text="主导交易系统重构，并负责交易系统后端研发")],
        narrative="负责交易系统后端研发，主导交易系统重构",
        points=[ProfilePoint(text="负责交易系统后端研发"), ProfilePoint(text="主导交易系统重构")],
        compact="后端研发",
    )
    reconciled = reconcile_pair(pair)
    assert reconciled is not pair
    assert reconciled.narrative == pair.narrative
    assert [f.text for f in reconciled.facts] == ["负责交易系统后端研发", "主导交易系统重构"]
    assert reconciled.is_consistent() is True


def test_narrative_and_points_are_normalized_to_single_line():
    # 形态不变量：narrative 恒为整段无换行；分点自身也不得带换行。
    pair = ProfilePair(
        narrative="7年后端研发\n主导交易系统",
        points=[ProfilePoint(text="负责支付系统\n主导重构")],
    )
    assert pair.narrative == "7年后端研发。主导交易系统"
    assert pair.points[0].text == "负责支付系统。主导重构"


def test_reconcile_pair_falls_back_to_clause_split_without_points():
    pair = ProfilePair(narrative="7年后端研发。主导交易系统")
    reconciled = reconcile_pair(pair)
    assert [p.text for p in reconciled.points] == ["7年后端研发", "主导交易系统"]
    assert reconciled.is_consistent() is True


# ---------------------------------------------------------------------------
# 校验与定点重写：硬指标必须真的拦住违规产出
# ---------------------------------------------------------------------------

_FILLER = "负责交易系统后端研发与性能治理。"


def _fit(text: str, low: int, high: int) -> str:
    """用中性的合规句子把文本垫到字数区间内，便于单测只验证一项违规。"""
    body = text
    while len(body) < low:
        body += _FILLER
    assert low <= len(body) <= high, f"垫字后仍越界：{len(body)}"
    return body


def test_vet_profile_accepts_clean_profiles():
    candidate = _fit("约 9.5 年 Java 低延迟后端开发经验，现任摩根士丹利高级开发工程师。", 120, 160)
    assert profile_spec.vet_profile(candidate, side="candidate") == ()

    jd = _fit("本科及以上，具备大规模数据平台与数据仓库建设经验。", 80, 150)
    assert profile_spec.vet_profile(jd, side="jd") == ()


def test_vet_profile_flags_each_hard_metric():
    # 长度：不足下限与超过上限都要报。
    assert "不足下限" in "".join(profile_spec.vet_profile("近 5 年 Java 后端经验。", side="candidate"))
    too_long = _fit("", 120, 160) + _FILLER * 3
    assert "超过上限" in "".join(profile_spec.vet_profile(too_long, side="candidate"))
    # 形态：换行/分点必须报。
    multiline = _fit("约 9.5 年 Java 后端经验。\n主导交易系统。", 120, 160)
    assert "换行" in "".join(profile_spec.vet_profile(multiline, side="candidate"))
    # 候选人侧学历词必须报；岗位侧的学历门槛不报。
    with_edu = _fit("约 9.5 年 Java 后端经验，本科及以上学历，硕士在读。", 120, 160)
    assert "学历" in "".join(profile_spec.vet_profile(with_edu, side="candidate"))
    assert profile_spec.vet_profile(with_edu, side="jd") == ()
    # 效能数字、过程计数、提升比例、奖项次数必须报，段位类数字放行。
    with_numbers = _fit("将核心接口响应时间从 250ms 降至 80ms，QPS 提升 3 倍，覆盖 300 张表，获银奖 1 次。", 120, 160)
    issues = "".join(profile_spec.vet_profile(with_numbers, side="candidate"))
    assert "不该写的数字" in issues
    segmented = _fit("独立负责业务数据体系，支撑百万级用户，带领 50 人团队。", 120, 160)
    assert profile_spec.vet_profile(segmented, side="candidate") == ()
    # 评价式收尾必须报。
    closing = _fit("约 9.5 年 Java 后端经验。", 120, 160) + "兼具技术架构与团队管理经验。"
    assert "评价式收尾" in "".join(profile_spec.vet_profile(closing, side="candidate"))
    # JD 侧技术栈写成通用要求必须报（措辞不固定：技术主线/技术栈/技术要求/技术方向都要兜住）。
    jd = _fit("本科及以上，5 年以上经验。技术主线为分布式系统设计与微服务架构。", 80, 150)
    assert "通用要求" in "".join(profile_spec.vet_profile(jd, side="jd"))
    jd_variant = _fit("本科及以上，5 年以上经验。技术栈为分布式系统、微服务与分库分表。", 80, 150)
    assert "通用要求" in "".join(profile_spec.vet_profile(jd_variant, side="jd"))
    # JD 侧没有依据的评价性修饰词要报（岗位画像一直在浪费字数写「资深」）。
    filler = _fit("资深交易系统开发工程师，本科及以上，5 年以上经验。", 80, 150)
    assert "修饰词" in "".join(profile_spec.vet_profile(filler, side="jd"))
    # 候选人侧的「资深架构师」是用户认可的身份表述，不在禁列。
    candidate_senior = _fit("约 9.5 年 Java 后端经验，现任汇量科技资深架构师。", 120, 160)
    assert profile_spec.vet_profile(candidate_senior, side="candidate") == ()


def test_render_rewrite_instruction_lists_issues_and_freezes_facts():
    instruction = profile_spec.render_rewrite_instruction(
        ("长度超出上限", "出现了学历词"), side="candidate", draft="约 9.5 年 Java 后端经验。")
    assert "1. 长度超出上限" in instruction
    assert "2. 出现了学历词" in instruction
    assert "保持事实" in instruction
    # 草稿必须放进指令（不能当 previous，否则「上一版画像」规则会让模型原样返回）。
    assert "约 9.5 年 Java 后端经验。" in instruction


def test_render_rewrite_instruction_switches_to_append_when_under_length():
    """不足下限时必须让模型**补句**，而不是删句。

    原先这条也走「只做删减」分支：`max(current - high, low - current)` 在「当前 47 字、
    区间 80~150」时算出 33，指令成了「删掉约 33 字」——而这份文本总共只有 47 字、
    目标是补 33 字。实测（2026-09-22 真机，岗位画像快速档）正文 47~62 字，
    定向重写怎么都修不动，根因就是这条反方向的指令。
    """
    instruction = profile_spec.render_rewrite_instruction(
        ("长度 47 字，不足下限 80 字，请补足到 80~150 字",), side="jd", draft="只写了半句的草稿。")
    assert "只做补充" in instruction
    assert "删掉" not in instruction
    assert "补足约 33 字" in instruction
    # 「补」不能变成「编」：必须限定只能补证据里已有、还没写进去的内容。
    assert "不得编造" in instruction


def test_render_rewrite_instruction_switches_to_delete_only_for_length():
    # 只超字数时改问「删掉多少字」：实测让模型重写它照样超，给它具体删减量它做得到。
    instruction = profile_spec.render_rewrite_instruction(
        ("长度 180 字，超过上限 160 字，请删句压到 120~160 字",), side="candidate", draft="草稿正文。")
    assert "只做删减" in instruction
    assert "整句删除信息量最低的句子" in instruction
    assert "含「优先」「加分」的句子不得删除" in instruction
    assert "需要删掉约 20 字" in instruction


def test_issue_severity_prefers_smaller_overage():
    heavy = ("长度 200 字，超过上限 160 字，请删句压到 120~160 字",)
    light = ("长度 165 字，超过上限 160 字，请删句压到 120~160 字",)
    assert profile_spec.issue_severity(light) < profile_spec.issue_severity(heavy)
    assert profile_spec.issue_severity(()) < profile_spec.issue_severity(light)


def test_issue_severity_counts_shortfall_too():
    """不足下限也要算偏离量。

    只算超出量的话，47 字与 79 字都得到 `(1, 0)`，`produce_pair_with_vet` 里
    `fixed >= best` 成立 → **一次真的补进了 32 字的重写会被当成「未改善」丢掉**。
    """
    far = ("长度 47 字，不足下限 80 字，请补足到 80~150 字",)
    near = ("长度 75 字，不足下限 80 字，请补足到 80~150 字",)
    assert profile_spec.issue_severity(near) < profile_spec.issue_severity(far)
    assert profile_spec.issue_severity(()) < profile_spec.issue_severity(near)
    # 两侧对称：不足与超出的偏离量可比（75 vs 80 差 5，165 vs 160 也差 5）。
    over_near = ("长度 165 字，超过上限 160 字，请删句压到 120~160 字",)
    assert profile_spec.issue_severity(near) == profile_spec.issue_severity(over_near)


def test_produce_pair_with_vet_reports_stages_only_when_a_rewrite_happens():
    """阶段回调只在真的还有第二次调用时才报 `repair`。

    这正是界面需要的信号：最坏要等 150 秒，使用者要能分辨「快好了」与「刚开始第二次模型
    调用」——后者意味着还要再等一个完整轮次。多报一次 `repair` 就等于把这条信号作废。
    """
    good = ProfilePair(
        narrative=_fit("约 9.5 年 Java 低延迟后端开发经验，现任摩根士丹利高级开发工程师。", 120, 160),
        points=[ProfilePoint(text="约 9.5 年 Java 低延迟后端开发经验")],
    )
    bad = ProfilePair(narrative="约 9.5 年 Java 后端经验。", points=[ProfilePoint(text="约 9.5 年 Java 后端经验")])

    async def always_good(instruction, previous):
        return good

    async def good_then_bad_until_rewrite(instruction, previous):
        return good if instruction else bad

    # 一次通过：只报 draft，不报 repair。
    clean: list[str] = []
    asyncio.run(produce_pair_with_vet(
        always_good, side="candidate", on_stage=clean.append))
    assert clean == ["draft"]

    # 需要重写：draft → repair。
    dirty: list[str] = []
    asyncio.run(produce_pair_with_vet(
        good_then_bad_until_rewrite, side="candidate", on_stage=dirty.append))
    assert dirty == ["draft", "repair"]


def test_produce_pair_with_vet_works_without_a_stage_callback():
    """没传回调时行为零变化（批量回填等非交互链路就这么用）。"""
    good = ProfilePair(
        narrative=_fit("约 9.5 年 Java 低延迟后端开发经验，现任摩根士丹利高级开发工程师。", 120, 160),
        points=[ProfilePoint(text="约 9.5 年 Java 低延迟后端开发经验")],
    )

    async def produce(instruction, previous):
        return good

    assert asyncio.run(produce_pair_with_vet(produce, side="candidate")) is good


def test_produce_pair_with_vet_rewrites_once_with_reasons():
    calls: list[tuple[str | None, str | None]] = []
    bad = ProfilePair(narrative="约 9.5 年 Java 后端经验。", points=[ProfilePoint(text="约 9.5 年 Java 后端经验")])
    good = ProfilePair(
        narrative=_fit("约 9.5 年 Java 低延迟后端开发经验，现任摩根士丹利高级开发工程师。", 120, 160),
        points=[ProfilePoint(text="约 9.5 年 Java 低延迟后端开发经验")],
    )

    async def produce(instruction, previous):
        calls.append((instruction, previous))
        return good if instruction else bad

    result = asyncio.run(produce_pair_with_vet(produce, side="candidate"))
    assert result is good
    assert len(calls) == 2
    assert calls[0] == (None, None)
    # 第二次必须带上违规原因与上一次的原文，模型才知道改什么；草稿走指令、不走 previous。
    assert calls[1][0] is not None and "不足下限" in calls[1][0]
    assert bad.narrative in (calls[1][0] or "")
    assert calls[1][1] is None


def test_produce_pair_with_vet_keeps_first_when_rewrite_does_not_improve():
    bad = ProfilePair(narrative="约 9.5 年 Java 后端经验，本科及以上学历。", points=[])
    worse = ProfilePair(narrative="本科及以上学历。", points=[])
    calls: list[str | None] = []

    async def produce(instruction, previous):
        calls.append(instruction)
        return bad if instruction is None else worse

    result = asyncio.run(produce_pair_with_vet(produce, side="candidate"))
    # 只重写一次，且不改善时保留首版（重写是护栏，不是无限重试）。
    assert result is bad
    assert len(calls) == 2


def test_produce_pair_with_vet_caps_total_calls_at_two():
    """总调用数上界是 2：默认 ``max_repairs=1``，且不再为「只超字数」额外放行一次。

    原先默认 2 且对仅超字数再放一次 → 最坏 3 次调用。每次调用都可能几十秒，
    而「重新生成画像」是交互式入口，所以上界必须收成 2（问题 #6）。
    """
    base = _fit("约 9.5 年 Java 后端经验。", 120, 160)
    drafts = [
        ProfilePair(narrative=base + _FILLER * 3, points=[]),  # 超 36 字
        ProfilePair(narrative=base + _FILLER * 2, points=[]),  # 超 17 字（有改善，但仍未达标）
    ]
    calls: list[str | None] = []

    async def produce(instruction, previous):
        calls.append(instruction)
        return drafts[len(calls) - 1]

    result = asyncio.run(produce_pair_with_vet(produce, side="candidate"))
    # 改善了一次就停手：不为了「再压几字」多花一次模型调用。
    assert result is drafts[1]
    assert len(calls) == 2
    assert calls[1] is not None


def test_produce_pair_with_vet_stops_when_budget_is_exhausted():
    """预算用尽就不再发起重写，直接返回当前版本——有界的产出好过一直转圈。"""
    bad = ProfilePair(narrative="约 9.5 年 Java 后端经验。", points=[])
    calls: list[str | None] = []

    async def produce(instruction, previous):
        calls.append(instruction)
        return bad

    result = asyncio.run(produce_pair_with_vet(produce, side="candidate", budget_seconds=0))
    assert result is bad
    assert calls == [None]  # 只跑了初稿，重写被预算拦住


def test_produce_pair_with_vet_without_budget_still_repairs():
    """``budget_seconds=None`` 表示不设预算（批量回填这类非交互链路可以放开）。"""
    bad = ProfilePair(narrative="约 9.5 年 Java 后端经验。", points=[])
    good = ProfilePair(
        narrative=_fit("约 9.5 年 Java 低延迟后端开发经验，现任摩根士丹利高级开发工程师。", 120, 160),
        points=[],
    )
    calls: list[str | None] = []

    async def produce(instruction, previous):
        calls.append(instruction)
        return good if instruction else bad

    result = asyncio.run(produce_pair_with_vet(produce, side="candidate", budget_seconds=None))
    assert result is good
    assert len(calls) == 2


def test_regen_timeout_exceeds_repair_budget():
    """接口超时必须**大于**重写预算，否则预算还没用尽接口就先超时，闸门形同不存在。"""
    assert REGEN_TIMEOUT_SECONDS > REPAIR_BUDGET_SECONDS


def test_jd_generic_mainline_is_replaced_with_required_skills():
    # 模型对 JD 原文有强照抄倾向：技术栈写成通用要求时，用 JD 的具体技术名词确定性替换。
    generator = JdProfileGenerator(llm=None)
    pair = JdProfilePair(**pair_from_points([
        ProfilePoint(text="本科及以上，5 年以上经验"),
        ProfilePoint(text="技术栈为分布式系统、微服务架构。"),
    ]).model_dump())
    fixed = generator._replace_generic_mainline(
        pair, {"required_skills": ["Java or ReactJS or Python or Go", "分布式系统设计", "Java", "Spring"]})
    # OR 并列与通用要求都要跳过，只留具体技术名词；措辞改用「技术栈为」。
    assert "技术栈为Java、Spring。" in fixed.narrative
    assert "分布式系统" not in fixed.narrative
    # 只改命中那条要点，其余要点原样保留（不再整段重拆）。
    assert fixed.points[0].text == "本科及以上，5 年以上经验"

    # required_skills 只有通用要求时，退回 must_skill_groups 的 alternatives。
    from_groups = generator._replace_generic_mainline(
        pair, {"required_skills": ["分布式系统设计"],
               "must_skill_groups": [{"alternatives": ["LangGraph", "CrewAI"]}]})
    assert "技术栈为LangGraph、CrewAI。" in from_groups.narrative

    # 挑不出具体技术名词时保持原样，不自行编造技术栈。
    unchanged = generator._replace_generic_mainline(pair, {"required_skills": ["分布式系统设计"]})
    assert unchanged.narrative == pair.narrative


def test_jd_generic_tech_is_dropped_when_no_concrete_skill_exists():
    # 长一点的画像里，通用技术栈句应当被直接删掉，而不是留一句「技术栈为分库分表」这种怪句。
    generator = JdProfileGenerator(llm=None)
    base = _fit("本科及以上，5 年以上经验，具备大规模数据平台建设与团队管理经验。", 80, 130)
    pair = JdProfilePair(**pair_from_points([
        ProfilePoint(text=base),
        ProfilePoint(text="技术栈为分布式系统、微服务架构。"),
    ]).model_dump())
    dropped = generator._replace_generic_mainline(pair, {"required_skills": ["分布式系统设计"]})
    assert "分布式系统" not in dropped.narrative
    assert profile_spec.vet_profile(dropped.narrative, side="jd") == ()

    # 删了会掉出字数下限时保持原样（宁可留通用要求，也不能让画像过短）。
    short = JdProfilePair(**pair_from_points([
        ProfilePoint(text="本科及以上"),
        ProfilePoint(text="技术栈为分布式系统、微服务架构。"),
    ]).model_dump())
    assert generator._replace_generic_mainline(short, {"required_skills": []}).narrative == short.narrative


def test_jd_produce_pair_derives_narrative_from_points():
    """模型只给要点与硬条件：整段 / facts / compact 全部本地派生，硬条件仍来自同一次调用。"""
    from kerui_recruit.jd.structured import ExactConstraint

    class _PointsLLM:
        async def complete_json(self, messages, model, **kwargs):
            assert model is JdPointsPlan
            return JdPointsPlan(
                points=[ProfilePoint(text="本科及以上"),
                        ProfilePoint(text="主导过金融交易系统，熟悉 Java、Spring")],
                exact_constraints=[ExactConstraint(
                    kind="skill", alternatives=["Java"], strength="MUST", source="inferred",
                    source_text="必须熟悉 Java")],
            )

    pair = asyncio.run(JdProfileGenerator(_PointsLLM())._produce_pair({"title": "后端工程师"}))

    assert pair.narrative == "本科及以上。主导过金融交易系统，熟悉 Java、Spring"
    assert [p.text for p in pair.points] == ["本科及以上", "主导过金融交易系统，熟悉 Java、Spring"]
    assert pair.compact == "本科及以上"
    assert pair.is_consistent()
    assert [c.alternatives for c in pair.exact_constraints] == [["Java"]]
    assert [c.kind for c in pair.exact_constraints] == ["skill"]
