from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import School
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.schools.reference import SchoolReference, normalize_school_name, recompute_educations
from kerui_recruit.schools.seed import seed_schools


def test_seed_tags_overlap(tmp_path) -> None:
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session, session.begin():
        seed_schools(session)

    with factory() as session:
        def tags(name):
            return set(session.scalar(select(School).where(School.canonical_name == name)).tags)

        assert tags("北京大学") == {"985", "211", "双一流"}  # 985 三标签
        assert tags("北京交通大学") == {"211", "双一流"}  # 纯 211
        assert tags("中国科学院大学") == {"双一流"}  # 纯双一流


def test_normalize_school_name_handles_spaces_case_and_punct() -> None:
    assert normalize_school_name(" 北京 大学 ") == "北京大学"
    assert normalize_school_name("Tsinghua University") == "tsinghuauniversity"
    assert normalize_school_name("中国科学技术大学") == "中国科学技术大学"
    assert normalize_school_name("") is None
    assert normalize_school_name(None) is None


def test_school_alias_resolves_to_canonical_name(tmp_path) -> None:
    """「北大」「清华」等别名必须解析到标准名，供查询与索引共用同一套映射。"""
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session, session.begin():
        seed_schools(session)

    reference = SchoolReference(factory)
    assert reference.resolve("北大")["canonical_name"] == "北京大学"
    assert reference.resolve("清华")["canonical_name"] == "清华大学"
    assert reference.resolve("浙大")["canonical_name"] == "浙江大学"
    # 原名也能直接命中
    assert reference.resolve("北京大学")["canonical_name"] == "北京大学"
    # 未收录的学校不猜测
    assert reference.resolve("不存在的学校")["matched"] is False


def test_recompute_educations_clears_stale_school_attributes(tmp_path) -> None:
    """学校名变化后，旧 school_tags/qs_rank 必须按新学校重算，未收录则清除。"""
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session, session.begin():
        seed_schools(session)

    # 985 学校：重新解析得到标准标签。
    recomputed = recompute_educations(factory, [
        {"school": "北京大学", "degree": "硕士", "school_tags": ["985", "211", "双一流"], "qs_rank": 18},
    ])
    assert recomputed[0]["school_tags"] == ["985", "211", "双一流"]

    # 改成未收录学校：旧的 985 标签与 QS 排名必须消失，不能继承。
    recomputed = recompute_educations(factory, [
        {"school": "某普通职业技术学院", "degree": "大专", "school_tags": ["985", "211", "双一流"], "qs_rank": 18},
    ])
    assert recomputed[0].get("school_tags") in (None, [])
    assert "qs_rank" not in recomputed[0]


def test_alias_groups_include_builtin_and_overseas_aliases(tmp_path) -> None:
    """标准名/别名快照包含内置国内别名与内置海外别名，供词法模块共用。"""
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session, session.begin():
        seed_schools(session)

    groups = SchoolReference(factory).alias_groups()
    assert set(groups["北京大学"]) == {"北大", "PKU", "Peking University"}
    assert "MIT" in groups["麻省理工学院"]
    # 标准名作为 key，别名不含标准名本身。
    assert "北京大学" not in groups["北京大学"]
