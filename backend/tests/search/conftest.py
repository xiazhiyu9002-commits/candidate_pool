"""搜索测试隔离：每个用例后重置共享检索线程池与信号量。

超时放弃的原生调用会继续占用 worker，只重置信号量会让新用例拿到许可却排不到
worker，表现为本该返回 FTS 结果的用例直接 TIMEOUT。详见 reset_search_pool()。
"""
import pytest

from kerui_recruit.search import service as service_module
from kerui_recruit.search.service import reset_search_pool


@pytest.fixture(autouse=True)
def _reset_shared_search_pool():
    yield
    reset_search_pool()


@pytest.fixture(autouse=True)
def _fast_provider_pacing(monkeypatch):
    """默认关掉 TPM pacing 的稳态节流。

    pacing 是 service 实例级预算（生产 1.25s/次调用），多轮检索用例会被无谓拖慢并
    逼近 8s deadline。pacing 行为本身由
    test_service.py::test_provider_pacing_throttles_sustained_rate 专测。
    """
    monkeypatch.setattr(service_module, "_PROVIDER_MIN_INTERVAL", 0.001)
