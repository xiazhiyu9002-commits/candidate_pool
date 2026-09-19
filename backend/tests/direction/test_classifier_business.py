"""业务方向判定单测：领域专属证据、跨域/通用词不触发、「最近 + 最长」口径与上限。

这些用例直接来自 1721 份真实简历上的实测误判（广告平台命中「汽车」、OTA/保险命中「订单」、
大型机测试命中「通信」），用于防止词表回退到过宽状态。
"""
from __future__ import annotations

from kerui_recruit.direction.classifier import classify_business_directions, _business_scores
from kerui_recruit.direction.policy import MAX_BUSINESS_DIRECTIONS


def test_generic_system_nouns_do_not_trigger_any_business_direction():
    """订单/商品/会员/核心系统/账务/合规/健康/设备管理在各行业通用，不得单独定位业务方向。"""
    generic = (
        "负责订单系统、商品管理、会员体系与核心系统的开发；"
        "处理账务流水、合规校验、健康检查与设备管理平台"
    )
    assert classify_business_directions({"projects": [{"summary": generic}]}) == ()


def test_client_industry_mention_is_not_own_business():
    """服务方「顺带提到客户行业」不得定位业务方向。

    只对**泛化罗列**成立：纯关键词分类器无法区分「在银行做系统」与「给银行做系统」，
    因此对银行/保险这类**领域专属**词，命中即算该领域经验（招聘上通常也确实有价值）；
    真正要挡住的是「订单/汽车/通信」这种在各行业都会顺手提到的词。
    """
    # 广告平台服务汽车客户 → 只有营销，不因提到「汽车」而算汽车行业
    assert classify_business_directions({
        "projects": [{"summary": "为汽车客户提供广告投放与广告系统建设"}],
    }) == ("MARKETING",)
    # 泛化的客户行业罗列不构成任何业务方向（汽车/通信均已从词表剔除）
    assert classify_business_directions({
        "projects": [{"summary": "服务的客户覆盖汽车、通信等多个行业"}],
    }) == ()
    # 已知边界：领域专属词（银行）命中即算，不做「自营 vs 服务方」区分
    assert "BANKING" in classify_business_directions({
        "projects": [{"summary": "银行信贷系统开发"}],
    })


def test_domain_specific_terms_still_trigger():
    assert classify_business_directions({
        "projects": [{"summary": "寿险保单理赔与承保流程系统"}],
    }) == ("INSURANCE",)
    assert classify_business_directions({
        "projects": [{"summary": "车联网与智能座舱系统开发"}],
    }) == ("AUTOMOTIVE",)
    assert classify_business_directions({
        "projects": [{"summary": "运营商核心网与基站管理系统"}],
    }) == ("TELECOM_CHIP",)
    assert classify_business_directions({
        "projects": [{"summary": "仓储 WMS 与运配调度系统"}],
    }) == ("LOGISTICS",)


def test_multi_domain_projects_capped_at_two():
    result = classify_business_directions({
        "projects": [
            {"summary": "寿险保单理赔系统"},
            {"summary": "仓储物流与运配调度"},
            {"summary": "游戏关卡与手游发行"},
        ],
    })
    assert len(result) == MAX_BUSINESS_DIRECTIONS
    assert set(result) <= {"INSURANCE", "LOGISTICS", "GAMING"}


def test_recent_and_longest_experiences_supply_both_slots():
    """「最近一段」与「任职时长最长的一段」各出 1 个，合计不超过 2 个。"""
    result = classify_business_directions({
        "experiences": [
            {"summary": "寿险理赔系统", "start_date": "2015.01", "end_date": "2021.12"},
            {"summary": "仓储物流系统", "start_date": "2022.01", "end_date": "至今"},
        ],
    })
    assert set(result) == {"INSURANCE", "LOGISTICS"}


def test_experience_slot_deduped_when_recent_is_also_longest():
    result = classify_business_directions({
        "experiences": [
            {"summary": "寿险理赔与承保系统", "start_date": "2015.01", "end_date": "至今"},
        ],
    })
    assert result == ("INSURANCE",)


def test_projects_fill_remaining_slot_when_experience_evidence_is_thin():
    """经历证据不足 2 个时用项目业务场景补齐（项目仍是主要证据来源）。"""
    result = classify_business_directions({
        "experiences": [{"summary": "团队管理与技术规划", "start_date": "2020.01", "end_date": "至今"}],
        "projects": [{"summary": "跨境电商商城与店铺运营平台"}],
    })
    assert "ECOMMERCE" in result


def test_no_evidence_returns_empty():
    assert classify_business_directions({}) == ()
    assert classify_business_directions({"summary": "后端工程师", "skills": ["Java"]}) == ()


def test_business_scores_rank_by_evidence_strength():
    ranked = _business_scores("保险 寿险 理赔 保单 承保 与 仓储物流".casefold())
    assert ranked[0][1] == "INSURANCE"
    assert ranked[0][0] > ranked[1][0]
