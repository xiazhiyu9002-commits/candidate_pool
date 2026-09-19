from kerui_recruit.search.degrees import DEGREE_ORDER, degrees_at_least, normalize_degree
from kerui_recruit.search.query import parse_query


def test_normalize_degree_canonical_values() -> None:
    assert normalize_degree("ASSOCIATE") == "ASSOCIATE"
    assert normalize_degree("BACHELOR") == "BACHELOR"
    assert normalize_degree("MASTER") == "MASTER"
    assert normalize_degree("DOCTORATE") == "DOCTORATE"


def test_normalize_degree_legacy_aliases() -> None:
    assert normalize_degree("COLLEGE") == "ASSOCIATE"
    assert normalize_degree("JUNIOR_COLLEGE") == "ASSOCIATE"
    assert normalize_degree("junior college") == "ASSOCIATE"
    assert normalize_degree("DOCTOR") == "DOCTORATE"
    assert normalize_degree("PHD") == "DOCTORATE"
    assert normalize_degree("phd") == "DOCTORATE"


def test_normalize_degree_chinese() -> None:
    assert normalize_degree("大专") == "ASSOCIATE"
    assert normalize_degree("专科") == "ASSOCIATE"
    assert normalize_degree("本科") == "BACHELOR"
    assert normalize_degree("学士") == "BACHELOR"
    assert normalize_degree("硕士") == "MASTER"
    assert normalize_degree("博士") == "DOCTORATE"
    assert normalize_degree("本科及以上") == "BACHELOR"


def test_normalize_degree_empty_or_unknown() -> None:
    assert normalize_degree("") is None
    assert normalize_degree(None) is None
    assert normalize_degree("   ") is None


def test_degrees_at_least_expands_minimum() -> None:
    assert degrees_at_least("DOCTOR") == ("DOCTORATE",)
    assert degrees_at_least("COLLEGE") == DEGREE_ORDER
    assert degrees_at_least("BACHELOR") == ("BACHELOR", "MASTER", "DOCTORATE")
    assert degrees_at_least(None) == ()


def test_parse_query_chinese_degree() -> None:
    assert parse_query("博士").filters.highest_degree == "DOCTORATE"
    assert parse_query("大专").filters.highest_degree == "ASSOCIATE"
    assert parse_query("本科及以上").filters.highest_degree == "BACHELOR"
