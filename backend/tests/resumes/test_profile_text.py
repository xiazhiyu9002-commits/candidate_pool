"""画像文本就地替换：只改总年限与年龄，绝不碰其他数字。"""
from __future__ import annotations

from kerui_recruit.resumes.profile_text import refresh_profile_text


def test_replaces_total_years_and_age_in_the_standard_wording():
    text = "罗亮，男，27岁，大连海事大学软件工程本科。4年后端开发经验，主导 RAG 系统。"
    updated = refresh_profile_text(text, total_years="5.3", age=28, previous_total_years="4.0")
    assert updated == "罗亮，男，28岁，大连海事大学软件工程本科。5年后端开发经验，主导 RAG 系统。"


def test_does_not_touch_years_of_a_single_role_or_calendar_years():
    """「近4年先后任职」是单段经历、2024年是年份，都不是总年限。"""
    text = "约10年后端开发经验。近4年先后任职野村、汇丰与摩根士丹利，2024年起转向交易系统。"
    updated = refresh_profile_text(text, total_years="11.2", age=None, previous_total_years="10.0")
    assert updated == "约11年后端开发经验。近4年先后任职野村、汇丰与摩根士丹利，2024年起转向交易系统。"


def test_falls_back_to_the_sole_years_number_when_it_matches_the_previous_value():
    """历史画像里有只写「4年」不带「经验」的，此时唯一的「N年」就是总年限。"""
    text = "吴煜晗，男，27岁，资深测试开发工程师，4年在途虎负责效能研发。"
    updated = refresh_profile_text(text, total_years="5.3", age=28, previous_total_years="4.0")
    assert updated == "吴煜晗，男，28岁，资深测试开发工程师，5年在途虎负责效能研发。"


def test_fallback_is_skipped_when_the_sole_number_is_not_the_previous_total():
    """唯一的「N年」与本次重算前的年限不符时，说明它不是总年限，不能改。"""
    text = "该候选人有 12 年在同一家公司任职的经历。"
    assert refresh_profile_text(text, total_years="5.3", age=None, previous_total_years="4.0") is None


def test_standard_wording_wins_and_other_years_are_left_alone():
    """有「…年经验」时只改那一处，子技能/单段经历的年限保持原样。"""
    text = "8年经验，其中3年带团队，2年海外。"
    updated = refresh_profile_text(text, total_years="9.2", age=None, previous_total_years="8.0")
    assert updated == "9年经验，其中3年带团队，2年海外。"


def test_fallback_is_skipped_when_there_are_several_years_numbers():
    """兜底只认「唯一的 N年」：出现多处又没有「…年经验」句式时一律不动，避免改错。"""
    text = "8年在同一家公司，其中3年带团队，2年海外。"
    assert refresh_profile_text(text, total_years="9.2", age=None, previous_total_years="8.0") is None


def test_returns_none_when_nothing_changes():
    text = "5年后端开发经验，28岁。"
    assert refresh_profile_text(text, total_years="5.3", age=28, previous_total_years="5.0") is None


def test_returns_none_for_empty_text():
    assert refresh_profile_text("", total_years="5.3", age=28) is None


def test_repeated_rolls_keep_updating_the_same_place():
    """连续滚动都改在同一处，且改到位后不再产生写入。"""
    text = "具备 11.5 年风控建模经验。"
    first = refresh_profile_text(text, total_years="11.5", age=None, previous_total_years="11.5")
    assert first == "具备 11年风控建模经验。"

    second = refresh_profile_text(first, total_years="12.4", age=None, previous_total_years="11.5")
    assert second == "具备 12年风控建模经验。"

    third = refresh_profile_text(second, total_years="12.4", age=None, previous_total_years="12.4")
    assert third is None


def test_fallback_still_works_on_the_next_roll():
    """兜底路径写进去的是整数，下一轮要能按「该整数 = 上轮年限」重新认出这一处。"""
    text = "李四，31岁，在途虎负责效能研发，7年。"
    first = refresh_profile_text(text, total_years="8.3", age=31, previous_total_years="7.0")
    assert first == "李四，31岁，在途虎负责效能研发，8年。"

    second = refresh_profile_text(first, total_years="9.1", age=32, previous_total_years="8.3")
    assert second == "李四，32岁，在途虎负责效能研发，9年。"
