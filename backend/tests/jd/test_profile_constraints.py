"""JD 显式硬条件解析与评估测试。"""
from __future__ import annotations

import pytest

from kerui_recruit.jd.profile_constraints import (
    evaluate_exact_constraints,
    interpret_model_years,
    merge_requirements,
    merge_rule_constraints,
    normalize_constraints,
    parse_exact_constraints,
    parse_years_requirement,
    preference_ratio,
)
from kerui_recruit.jd.structured import ParsedJd
from kerui_recruit.match.policy import evaluate_pair


def test_parse_explicit_must_conditions():
    constraints = parse_exact_constraints("卡985，字节或阿里背景，LangGraph 优先")
    kinds = {(c.kind, c.strength) for c in constraints}
    assert ("school_level", "MUST") in kinds
    assert ("company_history", "MUST") in kinds
    assert ("skill", "PLUS") in kinds
    # 优先条件不得升级为 MUST。
    skill = next(c for c in constraints if c.kind == "skill")
    assert skill.strength == "PLUS"


def test_evaluate_must_school_and_company():
    constraints = parse_exact_constraints("卡985，字节或阿里背景")
    unmet, _ = evaluate_exact_constraints(constraints, {
        "school_level": "211",
        "current_company": "某公司",
        "experiences": [{"company": "某公司"}],
    })
    assert len(unmet) == 2

    unmet, _ = evaluate_exact_constraints(constraints, {
        "school_level": "985",
        "current_company": "阿里巴巴",
        "experiences": [],
    })
    assert unmet == []


def test_plus_is_not_hard_reject():
    constraints = parse_exact_constraints("LangGraph 优先")
    unmet, _ = evaluate_exact_constraints(constraints, {"skills": ["Python"]})
    assert unmet == []


def test_preference_ratio_counts_plus_hits_and_is_none_without_plus():
    """PLUS 命中率是它唯一的作用途径；没有 PLUS 项时返回 None（不参与加权归一）。"""
    constraints = parse_exact_constraints("LangGraph 优先，Rust 优先")
    assert preference_ratio(constraints, {"skills": ["LangGraph"]}) == 0.5
    assert preference_ratio(constraints, {"skills": ["LangGraph", "Rust"]}) == 1.0
    assert preference_ratio(constraints, {"skills": ["Java"]}) == 0.0
    assert preference_ratio([], {}) is None
    # 只有 MUST 的 JD 没有「优先项」这个维度。
    assert preference_ratio(parse_exact_constraints("卡985"), {"school_level": "985"}) is None


def test_evaluate_pair_reports_plus_preference_without_rejecting():
    """降级后的 skill 靠 preference 继续影响排序：一个都没命中也不淘汰。"""
    jd = {"exact_constraints": [
        {"kind": "skill", "operator": "OR", "alternatives": ["LangGraph"], "strength": "PLUS", "source": "inferred", "source_text": "LangGraph 优先"},
        {"kind": "skill", "operator": "OR", "alternatives": ["Rust"], "strength": "PLUS", "source": "inferred", "source_text": "Rust 优先"},
    ]}
    hit_once = evaluate_pair(jd, {"skills": ["LangGraph"]})
    assert hit_once.eligibility == "eligible"
    assert hit_once.preference == 0.5

    hit_none = evaluate_pair(jd, {"skills": ["Java"]})
    assert hit_none.eligibility == "eligible"
    assert hit_none.preference == 0.0

    assert evaluate_pair({}, {"skills": ["Java"]}).preference is None


def test_parse_years_requirement_handles_common_phrasings():
    """画像里的年限要能被抽出来（年限是独立硬窗口，不在 exact_constraints 的 kind 里）。"""
    assert parse_years_requirement("5年以上经验") == (True, 5.0)
    assert parse_years_requirement("至少 3 年工作经验") == (True, 3.0)
    assert parse_years_requirement("3-5 年经验") == (True, 3.0)
    assert parse_years_requirement("8 年左右") == (True, 8.0)
    assert parse_years_requirement("要求 10 年及以上") == (True, 10.0)
    assert parse_years_requirement("经验不限") == (True, None)
    assert parse_years_requirement("不限经验，熟悉 Java") == (True, None)


def test_parse_years_requirement_handles_chinese_numerals_and_plus_form():
    """中文数字与「n 年+」「工作年限：n 年」在画像里同样常见，漏掉就等于年限没生效。"""
    assert parse_years_requirement("五年以上后端经验") == (True, 5.0)
    assert parse_years_requirement("十五年以上经验") == (True, 15.0)
    assert parse_years_requirement("工作年限：25 年") == (True, 25.0)
    assert parse_years_requirement("3 年+ 经验") == (True, 3.0)
    assert parse_years_requirement("应届生亦可") == (True, None)


def test_company_background_triggers_cover_common_phrasings():
    """公司背景的触发词要覆盖「有 X 经验 / 曾在 X 任职 / X 出身 / 来自 X」等常见写法。"""
    def companies(text: str) -> set[str]:
        return {a for c in parse_exact_constraints(text) if c.kind == "company_history"
                for a in c.alternatives}

    assert companies("具备阿里或字节背景") == {"阿里", "字节"}
    assert companies("有字节经验") == {"字节"}
    assert companies("曾在腾讯任职") == {"腾讯"}
    assert companies("阿里出身") == {"阿里"}
    assert companies("来自美团") == {"美团"}
    assert companies("百度、京东背景") == {"百度", "京东"}


def test_company_product_wording_is_not_a_company_background():
    """「阿里云 / 腾讯云」是产品不是雇主；泛化背景与技术栈同样不得产出公司约束。

    反向错误比漏抽更危险：一条 MUST 的公司约束不满足就把整库清空。
    """
    def companies(text: str) -> set[str]:
        return {a for c in parse_exact_constraints(text) if c.kind == "company_history"
                for a in c.alternatives}

    assert companies("有容器化经验，熟悉阿里云 ACK") == set()
    assert companies("熟悉腾讯云 COS") == set()
    # 「经验」说的是电商，不是阿里的经历（语句级切分保证不连坐）。
    assert companies("有电商经验，了解阿里") == set()
    assert companies("熟悉 Oracle 数据库") == set()
    assert companies("要求互联网或金融背景") == set()


def test_merge_requirements_unions_model_and_rule_results():
    """并集合并：模型空结果不能吞掉规则抽出的客观条件（这正是「换个语序就抽不出来」的根因）。"""
    rule = normalize_constraints([
        {"kind": "degree", "alternatives": ["本科"], "strength": "MUST",
         "source_text": "本科及以上学历"},
        {"kind": "company_history", "alternatives": ["阿里", "字节"], "strength": "MUST",
         "source_text": "具备阿里或字节背景"},
    ])
    # 模型什么都没抽到 → 规则结果原样保留。
    assert merge_requirements([], rule) == rule

    # 同 kind 取并集：模型只认出阿里、规则认出阿里+字节 → 字节不能被丢掉。
    model = normalize_constraints([
        {"kind": "company_history", "alternatives": ["阿里"], "strength": "PLUS",
         "source_text": "具备阿里背景"},
    ])
    merged = {c["kind"]: c for c in merge_requirements(model, rule)}
    assert set(merged["company_history"]["alternatives"]) == {"阿里", "字节"}
    # 强度取更强的一档（两条都带原文依据，MUST 保留淘汰力）。
    assert merged["company_history"]["strength"] == "MUST"
    assert merged["degree"]["strength"] == "MUST"


def test_english_company_names_are_extracted_but_ambiguous_ones_are_not():
    """无歧义英文公司名要能抽（外企 JD / 海归候选人），歧义的（Oracle/AWS）不能抽成公司。"""
    def companies(text: str) -> set[str]:
        return {a for c in parse_exact_constraints(text) if c.kind == "company_history"
                for a in c.alternatives}

    assert companies("有 ByteDance 经验") == {"字节"}
    assert companies("有 Alibaba 背景") == {"阿里"}
    assert companies("阿里或蚂蚁集团背景") == {"阿里", "蚂蚁"}
    # oracle / aws / google 常指数据库与云服务，不作公司抽取（判定阶段才用英文别名）。
    assert companies("有 AWS 使用经验") == set()
    assert companies("熟悉 Oracle 数据库") == set()


def test_negated_statement_becomes_exclude_not_must():
    """「不考虑阿里背景」必须判成 EXCLUDE。

    之前按默认 MUST 处理，会把排除项**反转成必须项**——JD 说不看阿里的人，
    系统却只推阿里的人，是最危险的方向。
    """
    constraints = parse_exact_constraints("不考虑阿里背景")
    assert len(constraints) == 1
    assert constraints[0].kind == "company_history"
    assert constraints[0].strength == "EXCLUDE"
    assert constraints[0].alternatives == ("阿里",)

    excluded = parse_exact_constraints("排除字节背景")[0]
    assert (excluded.strength, excluded.alternatives) == ("EXCLUDE", ("字节",))


def test_negated_threshold_is_a_requirement_not_an_exclusion():
    """「本科以下勿投」「非 985 勿投」否定的是**补集**，等价于「必须本科/985 及以上」。

    这类写法在中文 JD 里极常见。若因为句子里有「勿投/不考虑」就判成 EXCLUDE，
    会正好把要招的人全排除——比漏抽危险得多，所以否定判定必须区分作用域。
    """
    def strength_of(text: str, kind: str) -> str | None:
        return next((c.strength for c in parse_exact_constraints(text) if c.kind == kind), None)

    assert strength_of("本科以下勿投", "degree") == "PLUS"
    assert strength_of("非 985 勿投", "school_level") == "PLUS"
    assert strength_of("985/211 勿投", "school_level") == "PLUS"
    assert strength_of("211 以下不考虑", "school_level") == "PLUS"
    # 作用域正确的排除仍然要判成 EXCLUDE。
    assert strength_of("不考虑阿里背景", "company_history") == "EXCLUDE"


def test_industry_must_needs_explicit_wording():
    """行业要求必须写明「必须/硬性/及以上」才保留淘汰力。

    实测同一份 JD 两次导入，模型一次产出 ``industry MUST ['支付']``、一次不产出，而「支付」
    只出现在职责句「负责支付核心链路的服务设计与开发」里。随机出现的行业 MUST 会把整库清空，
    而用户口径是「泛化背景不卡」。
    """
    from_duty = normalize_constraints([
        {"kind": "industry", "alternatives": ["支付"], "strength": "MUST",
         "source_text": "负责支付核心链路的服务设计与开发"},
    ])
    assert from_duty[0]["strength"] == "PLUS"

    explicit = normalize_constraints([
        {"kind": "industry", "alternatives": ["支付"], "strength": "MUST",
         "source_text": "必须有支付行业经验"},
    ])
    assert explicit[0]["strength"] == "MUST"


def test_skill_extraction_stays_ascii_and_skips_company_sentences():
    """规则只抽 ASCII 技能词：`\\w` 在 Python 里也匹配中文，「Alibaba 或 Tencent 背景优先」
    会被整句当成技能词（噪声），进而在 AI 复核清单里显示成「加分：Alibaba 或 Tencent 背景」。"""
    def skills(text: str) -> set[str]:
        return {a for c in parse_exact_constraints(text) if c.kind == "skill"
                for a in c.alternatives}

    assert skills("有 Alibaba 或 Tencent 背景优先。") == set()
    assert skills("K8s 优先，CI/CD 必须") == {"K8s", "CI/CD"}
    assert skills("Java 优先") == {"Java"}


def test_merge_rule_constraints_fills_model_gaps_for_jd_import():
    """JD 导入链路：模型漏给的学历 / 年限 / 公司由规则补上（与画像链路同一口径）。

    实测同一份多段 JD 两次导入，模型一次给 degree、一次不给——明写的条件不能随模型抖动丢失。
    """
    merged, years = merge_rule_constraints(
        [], "【任职要求】1. 本科及以上学历；2. 5 年以上后端经验；3. 有字节或阿里背景。"
    )
    kinds = {c["kind"]: c for c in merged}
    assert kinds["degree"]["strength"] == "MUST"
    assert kinds["company_history"]["strength"] == "MUST"
    assert set(kinds["company_history"]["alternatives"]) == {"字节", "阿里"}
    assert years == 5.0


def test_merge_rule_constraints_keeps_model_constraints_and_skips_absent_years():
    """规则与模型结果并集；文本里没有年限时回传 None，由调用方保留模型给的值。"""
    model = [{"kind": "skill", "operator": "OR", "alternatives": ["Java"],
              "strength": "PLUS", "source": "inferred", "source_text": "熟悉 Java"}]
    merged, years = merge_rule_constraints(model, "本科及以上学历，熟悉 Java")
    kinds = {c["kind"] for c in merged}
    assert "degree" in kinds and "skill" in kinds
    assert years is None


def test_company_hit_resolves_full_name_back_to_its_alias_group():
    """约束里是模型抄的原文全称（字节跳动），候选人简历只写简称（字节）也必须命中。

    只做 canonical → 别名的单向展开会漏判，而漏判在 MUST 下等同于误拒。
    """
    def passed(company: str, candidate_company: str) -> bool:
        jd = {"exact_constraints": [
            {"kind": "company_history", "operator": "OR", "alternatives": [company],
             "strength": "MUST", "source": "inferred", "source_text": f"{company}背景"},
        ]}
        return evaluate_pair(jd, {"current_company": candidate_company}).eligibility != "rejected"

    assert passed("字节跳动", "字节")
    assert passed("阿里巴巴", "阿里")
    assert passed("字节", "字节跳动")
    assert passed("Tencent", "腾讯")
    # 不相干的公司仍然不命中。
    assert not passed("字节跳动", "某互联网公司")


def test_exclude_constraint_actually_rejects_hitting_candidate():
    """EXCLUDE 必须真正拒人。

    此前 ``evaluate_exact_constraints`` 只分派 MUST / PLUS，EXCLUDE 既不拒人也不加分——
    「不考虑外包背景」被解析出来却毫无作用（模型产出 EXCLUDE、AI 复核清单标「排除」，
    确定性资格层完全忽略）。
    """
    jd = {"exact_constraints": [
        {"kind": "company_history", "operator": "OR", "alternatives": ["外包"],
         "strength": "EXCLUDE", "source": "manual", "source_text": "不考虑外包背景"},
    ]}
    hit = evaluate_pair(jd, {"current_company": "某外包服务有限公司"})
    assert hit.eligibility == "rejected"
    assert any("exclude_constraint" in reason for reason in hit.hard_reasons)

    # 没命中排除项 → 不受影响。
    miss = evaluate_pair(jd, {"current_company": "某互联网公司"})
    assert miss.eligibility == "eligible"


def test_exclude_does_not_touch_plus_or_must_alternatives():
    """排除项与准入项同 kind 时互不干扰：不能把 EXCLUDE 的 alternatives 并进 MUST 的 OR 组。"""
    jd = {"exact_constraints": [
        {"kind": "company_history", "operator": "OR", "alternatives": ["阿里"],
         "strength": "MUST", "source": "manual", "source_text": "必须阿里背景"},
        {"kind": "company_history", "operator": "OR", "alternatives": ["外包"],
         "strength": "EXCLUDE", "source": "manual", "source_text": "不考虑外包背景"},
    ]}
    # 阿里背景 + 没待过外包 → 通过。
    assert evaluate_pair(jd, {"current_company": "阿里巴巴"}).eligibility == "eligible"
    # 阿里背景但待过外包 → 被排除项拒掉。
    rejected = evaluate_pair(jd, {"experiences": [{"company": "阿里巴巴"}, {"company": "某外包公司"}]})
    assert rejected.eligibility == "rejected"


def test_merge_requirements_resolves_must_vs_exclude_contradiction():
    """模型说「必须阿里」、规则说「排除阿里」时不能让两者并存。

    并存会让准入 OR 组与排除项的交集为空——谁都不满足，整库被拒。
    以排除为准：被排除的备选从准入门槛里摘掉，摘空了就整条丢掉。
    """
    model = normalize_constraints([
        {"kind": "company_history", "alternatives": ["阿里"], "strength": "MUST",
         "source_text": "必须阿里背景"},
    ])
    rule = normalize_constraints([
        {"kind": "company_history", "alternatives": ["阿里"], "strength": "EXCLUDE",
         "source_text": "不考虑阿里背景"},
    ])
    merged = merge_requirements(model, rule)
    assert [c for c in merged if c["strength"] in ("MUST", "PLUS")] == []
    assert [c["alternatives"] for c in merged if c["strength"] == "EXCLUDE"] == [["阿里"]]

    # 准入组里还有别的备选时，只摘掉被排除的那个。
    mixed = merge_requirements(
        normalize_constraints([
            {"kind": "company_history", "alternatives": ["字节", "阿里"], "strength": "MUST",
             "source_text": "字节或阿里背景"},
        ]),
        rule,
    )
    assert {a for c in mixed if c["strength"] == "MUST" for a in c["alternatives"]} == {"字节"}


def test_merge_requirements_keeps_exclude_separate():
    """EXCLUDE 语义与 MUST/PLUS 相反，不能并进 OR 组（并了就等于把排除项变成准入项）。"""
    rule = normalize_constraints([
        {"kind": "company_history", "alternatives": ["阿里"], "strength": "MUST", "source_text": "阿里背景"},
    ])
    model = normalize_constraints([
        {"kind": "company_history", "alternatives": ["外包"], "strength": "EXCLUDE",
         "source_text": "不考虑外包背景"},
    ])
    merged = merge_requirements(model, rule)
    exclude = [c for c in merged if c["strength"] == "EXCLUDE"]
    assert len(exclude) == 1 and exclude[0]["alternatives"] == ["外包"]
    assert {a for c in merged if c["strength"] == "MUST" for a in c["alternatives"]} == {"阿里"}


def test_parse_years_requirement_does_not_guess_when_absent_or_ambiguous():
    """没提年限要回 (False, None)——调用方据此保留原值；年份区间不能被误当成经验年数。"""
    assert parse_years_requirement(None) == (False, None)
    assert parse_years_requirement("") == (False, None)
    assert parse_years_requirement("熟悉 Java 与微服务") == (False, None)
    # 「2020-2023 年」是年份区间，不是 2020 年经验（否则 min_years 越界，保存直接 422）。
    assert parse_years_requirement("2020-2023 年负责交易系统") == (False, None)


def test_interpret_model_years_translates_unlimited_and_unstated():
    """模型契约里 0 = 经验不限，**不能直接落库**——_years_window(0) 会算出 1~3 年窗口。"""
    assert interpret_model_years(5) == (True, 5.0)
    assert interpret_model_years(3.5) == (True, 3.5)
    assert interpret_model_years(0) == (True, None)
    assert interpret_model_years(None) == (False, None)


def test_evaluate_pair_rejects_unmet_must_constraint():
    decision = evaluate_pair(
        {"direction": "BACKEND", "exact_constraints": [
            {"kind": "school_level", "operator": "OR", "alternatives": ["985"], "strength": "MUST", "source": "manual", "source_text": "卡985"},
        ]},
        {"direction": "BACKEND", "school_level": "普本", "skills": ["Java"]},
    )
    assert decision.eligibility == "rejected"
    assert any("exact_constraint" in r for r in decision.hard_reasons)


def test_evaluate_pair_passes_when_must_constraint_met():
    decision = evaluate_pair(
        {"direction": "BACKEND", "exact_constraints": [
            {"kind": "school_level", "operator": "OR", "alternatives": ["985"], "strength": "MUST", "source": "manual", "source_text": "卡985"},
        ]},
        {"direction": "BACKEND", "school_level": "985", "skills": ["Java"]},
    )
    assert decision.eligibility != "rejected"


def test_985_priority_stays_plus_even_with_must_elsewhere():
    # 「必须熟悉Java」里的「必须」不应把「985优先」升级为 MUST。
    constraints = parse_exact_constraints("985优先，必须熟悉Java")
    school = next(c for c in constraints if c.kind == "school_level")
    assert school.strength == "PLUS"
    assert school.alternatives == ("985",)


def test_company_background_priority_is_plus():
    constraints = parse_exact_constraints("字节或阿里背景优先")
    company = next(c for c in constraints if c.kind == "company_history")
    assert company.strength == "PLUS"
    assert company.alternatives == ("字节", "阿里")


def test_local_must_marker_still_rejects():
    # 局部「卡」仍是 MUST；局部「必须」出现在同一语句时也是 MUST。
    constraints = parse_exact_constraints("卡985，必须熟悉Java")
    school = next(c for c in constraints if c.kind == "school_level")
    assert school.strength == "MUST"


def test_plus_985_and_company_do_not_reject_nonmatching_candidate():
    constraints = parse_exact_constraints("985优先，字节或阿里背景优先")
    unmet, _ = evaluate_exact_constraints(constraints, {
        "school_level": "普本",
        "current_company": "某公司",
        "experiences": [{"company": "某公司"}],
    })
    # PLUS 条件不硬拒。
    assert unmet == []


# ---- 学校档次 OR 合并 + 学历门槛维度拆分（2026-09-20 需求）----


def test_multiple_school_levels_merge_into_one_or_constraint():
    """「必须 985 或 211」是一条 OR 约束；拆成两条独立 MUST 会把 OR 判成 AND、误拒 211 的人。"""
    constraints = parse_exact_constraints("必须985或211")
    school = [c for c in constraints if c.kind == "school_level"]
    assert len(school) == 1
    assert school[0].operator == "OR"
    assert set(school[0].alternatives) == {"985", "211"}
    assert school[0].strength == "MUST"
    # 211 本科的候选人必须通过（旧实现会被判「未满足 985」而误拒）。
    unmet, _ = evaluate_exact_constraints(constraints, {
        "highest_degree": "BACHELOR", "school_level": "211",
        "educations": [{"degree": "BACHELOR", "school_tags": ["211"]}],
    })
    assert unmet == []


def test_degree_is_its_own_kind_not_school_level():
    """「必须硕士及以上」是学历门槛；写进 school_level 会拿「硕士」比学校标签 → 全库误拒。"""
    constraints = parse_exact_constraints("必须硕士及以上")
    kinds = {(c.kind, c.strength) for c in constraints}
    assert kinds == {("degree", "MUST")}
    degree = constraints[0]
    assert degree.alternatives == ("硕士",)

    # 本科候选人不满足、硕士候选人满足。
    assert evaluate_exact_constraints(constraints, {"highest_degree": "BACHELOR"})[0] != []
    assert evaluate_exact_constraints(constraints, {"highest_degree": "MASTER"})[0] == []


def test_school_with_degree_threshold_produces_two_constraints():
    """「985 本科及以上」= 学校档次 + 学历两条独立约束（AND）。"""
    constraints = parse_exact_constraints("985本科及以上")
    kinds = {c.kind for c in constraints}
    assert kinds == {"school_level", "degree"}
    assert all(c.strength == "MUST" for c in constraints)

    # 宽松口径：任一段是 985 + 最高学历达标 → 通过（本科普通、硕士 985 也算过）。
    unmet, _ = evaluate_exact_constraints(constraints, {
        "highest_degree": "MASTER", "school_level": "普通",
        "educations": [{"degree": "BACHELOR", "school_tags": ["普通"]},
                       {"degree": "MASTER", "school_tags": ["985"]}],
    })
    assert unmet == []

    # 最高学历达标但没有任何一段 985 → 不通过。
    unmet, _ = evaluate_exact_constraints(constraints, {
        "highest_degree": "MASTER", "school_level": "普通",
        "educations": [{"degree": "MASTER", "school_tags": ["普通"]}],
    })
    assert len(unmet) == 1

    # 有 985 但最高学历不足 → 不通过。
    unmet, _ = evaluate_exact_constraints(constraints, {
        "highest_degree": "ASSOCIATE", "school_level": "985",
        "educations": [{"degree": "ASSOCIATE", "school_tags": ["985"]}],
    })
    assert len(unmet) == 1


def test_threshold_word_with_priority_stays_plus():
    """「本科以上优先」带门槛词也仍是 PLUS：优先词先于门槛词判定。"""
    constraints = parse_exact_constraints("本科以上优先")
    degree = next(c for c in constraints if c.kind == "degree")
    assert degree.strength == "PLUS"


def test_generic_background_does_not_produce_company_constraint():
    """泛化背景（互联网/金融/大厂）不产出 company_history，只有点名公司才产出。"""
    assert [c for c in parse_exact_constraints("互联网背景优先") if c.kind == "company_history"] == []
    assert [c for c in parse_exact_constraints("金融背景") if c.kind == "company_history"] == []
    named = parse_exact_constraints("必须有字节或阿里巴巴背景")
    company = next(c for c in named if c.kind == "company_history")
    assert set(company.alternatives) == {"字节", "阿里"}


def test_normalize_constraints_rejects_invalid_and_unbacked_must():
    """非法取值整条丢弃；无原文依据的 MUST 降级；
    skill / other_keyword 即使写了「必须」且有依据，也不允许具备淘汰力（只做 PLUS）。"""
    normalized = normalize_constraints([
        {"kind": "school_level", "alternatives": ["985"], "strength": "MUST", "source_text": "卡 985"},
        {"kind": "skill", "alternatives": ["Java"], "strength": "MUST"},
        {"kind": "能力", "alternatives": ["沟通"], "strength": "MUST", "source_text": "沟通"},
        {"kind": "skill", "alternatives": ["Go"], "strength": "MAYBE", "source_text": "Go"},
        {"kind": "skill", "alternatives": [], "strength": "PLUS"},
        {"kind": "skill", "alternatives": ["Rust"], "strength": "MUST", "source_text": "必须熟悉 Rust"},
        {"kind": "other_keyword", "alternatives": ["5年以上"], "strength": "MUST", "source_text": "5年以上"},
        {"kind": "industry", "alternatives": ["支付"], "strength": "MUST", "source_text": "必须有支付行业经验"},
    ])

    assert [(c["kind"], c["strength"]) for c in normalized] == [
        ("school_level", "MUST"),
        ("skill", "PLUS"),
        ("skill", "PLUS"),
        ("other_keyword", "PLUS"),
        ("industry", "MUST"),
    ]
    assert normalized[0]["source"] == "inferred"


def test_parsed_jd_normalizes_model_produced_constraints():
    """AI 产出的硬条件与人工编辑共用同一套规整：非法丢弃、无依据的 MUST 降级。"""
    parsed = ParsedJd(title="后端工程师", exact_constraints=[
        {"kind": "school_level", "alternatives": ["985"], "strength": "MUST"},
        {"kind": "无法识别", "alternatives": ["x"], "strength": "MUST", "source_text": "x"},
    ])

    assert len(parsed.exact_constraints) == 1
    assert parsed.exact_constraints[0].kind == "school_level"
    assert parsed.exact_constraints[0].strength == "PLUS"


class _RecordingJdClient:
    """只记录提示词并返回固定结构的 JD 任务客户端。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete_json(self, messages, response_model, **kwargs):
        self.prompts.append(messages[-1]["content"])
        return response_model(title="后端工程师", exact_constraints=[
            {"kind": "school_level", "alternatives": ["985"], "strength": "MUST"},
            {"kind": "skill", "alternatives": ["Java"], "strength": "MUST", "source_text": "必须熟悉 Java"},
        ])


@pytest.mark.asyncio
async def test_ai_jd_parser_asks_for_constraints_with_the_shared_spec():
    """解析期就要产出硬条件（零额外调用），口径来自共享常量而非另抄一份。"""
    from kerui_recruit.providers.generation_tasks import AiJdParser

    client = _RecordingJdClient()
    parsed = await AiJdParser(client).parse_jd("必须熟悉 Java")

    prompt = client.prompts[0]
    assert "exact_constraints" in prompt
    assert "只有原文明确写" in prompt
    assert "985 优先，必须熟悉 Java" in prompt
    # 无原文依据的 MUST 已降级为 PLUS；技能类即使有依据也只做软条件。
    assert [(c.kind, c.strength) for c in parsed.exact_constraints] == [
        ("school_level", "PLUS"),
        ("skill", "PLUS"),
    ]
