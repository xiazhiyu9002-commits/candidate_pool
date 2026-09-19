from kerui_recruit.search.contracts import resolve_search_status


def test_status_success() -> None:
    assert resolve_search_status([object()], None, ()) == "success"


def test_status_degraded_with_results() -> None:
    assert resolve_search_status([object()], None, ("VECTOR_UNAVAILABLE",)) == "degraded"


def test_status_no_match() -> None:
    assert resolve_search_status([], "no_match", ()) == "no_match"


def test_status_index_not_ready() -> None:
    assert resolve_search_status([], "index_not_ready", ()) == "index_not_ready"


def test_status_service_error() -> None:
    assert resolve_search_status([], "service_error", ()) == "service_error"
    assert resolve_search_status([], None, ("SEARCH_UNAVAILABLE",)) == "service_error"
