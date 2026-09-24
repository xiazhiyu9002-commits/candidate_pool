"""JD 导入链路的真实数据端到端体检（多段 / 表格型 / 混合中英 / 缩写 / 口语化）。

画像链路（``POST /api/jd/parse-constraints``）走「模型 ∪ 规则」，但 **JD 导入链路
（``POST /api/jd/import`` → AiJdParser）此前只跑模型**——本脚本用真实数据验证这一环：
JD 文本里明写的学历 / 年限 / 公司背景，是否真的落到了 ``parsed_data`` 里。

用法（会真实建 JD 并在结束后删除，``--keep`` 可保留）：
    python scripts/e2e_jd_parse_robustness_2026_09_20.py
    python scripts/e2e_jd_parse_robustness_2026_09_20.py --keep
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:43127"
TOKEN = "0" * 64

# (名称, 期望至少命中的 exact_constraints kind→alternatives, 期望 min_years)
CASES: list[tuple[str, str, dict[str, set[str]], float | None]] = [
    (
        "表格型",
        "岗位名称\tJava 后端开发工程师\n"
        "学历要求\t本科及以上\n"
        "工作年限\t5年以上\n"
        "目标公司\t字节或阿里\n"
        "技术要求\tJava、Spring Boot、MySQL、Redis、Kafka\n"
        "岗位职责\t负责交易系统的后端服务开发与性能优化\n",
        {"degree": {"本科"}, "company_history": {"字节", "阿里"}},
        5.0,
    ),
    (
        "多段带标题",
        "【岗位职责】\n"
        "1. 负责支付核心链路的服务设计与开发；\n"
        "2. 参与稳定性建设与容量规划。\n\n"
        "【任职要求】\n"
        "1. 本科及以上学历，计算机相关专业；\n"
        "2. 5 年以上后端开发经验；\n"
        "3. 有字节跳动或腾讯的工作经历；\n"
        "4. 精通 Java、Spring Boot 与微服务架构。\n",
        {"degree": {"本科"}, "company_history": {"字节", "腾讯"}},
        5.0,
    ),
    (
        "混合中英 + 缩写",
        "We are hiring a Senior Backend Engineer.\n"
        "Bachelor degree or above, 5+ years of backend experience.\n"
        "有 Alibaba 或 Tencent 背景优先。\n"
        "Must have: Java, K8s, CI/CD, Kafka. Nice to have: Go, Redis.\n",
        {"degree": {"本科"}},
        5.0,
    ),
    (
        "口语化",
        "招个后端，最好在字节待过，起码 3 年经验，本科以上。\n"
        "技术栈就是 Java 那套，K8s 懂点更好。\n",
        {"degree": {"本科"}, "company_history": {"字节"}},
        3.0,
    ),
]


def _request(method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"X-Kerui-Session": TOKEN, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def _wait_ready(revision_id: str, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, items = _request("GET", "/api/jd")
        for item in items or []:
            if item.get("revision_id") == revision_id and item.get("status") == "READY":
                return item
        time.sleep(1.0)
    raise TimeoutError(f"revision {revision_id} 未在 {timeout:.0f}s 内 READY")


def _covers(expected: set[str], actual: set[str]) -> bool:
    """公司名的「实质覆盖」判定：模型抄全称（字节跳动）、规则给简称（字节）都算命中。"""
    return all(any(e in a or a in e for a in actual) for e in expected)


def main() -> int:
    keep = "--keep" in sys.argv
    failures: list[str] = []
    created: list[str] = []

    for name, text, expect_kinds, expect_years in CASES:
        status, imported = _request("POST", "/api/jd/import", {
            "company": "边界测试公司", "title": f"[边界测试] {name}", "source_text": text,
        })
        if status != 202 or not isinstance(imported, dict):
            failures.append(f"[{name}] 导入失败：{status} {imported}")
            print(f"FAIL {name}: 导入失败 {status} {imported}")
            continue
        created.append(imported["jd_id"])
        try:
            item = _wait_ready(imported["revision_id"])
        except TimeoutError as error:
            failures.append(f"[{name}] {error}")
            print(f"FAIL {name}: {error}")
            continue

        parsed = item.get("parsed_data") or {}
        constraints = parsed.get("exact_constraints") or []
        kinds: dict[str, set[str]] = {}
        for constraint in constraints:
            if isinstance(constraint, dict):
                kinds.setdefault(str(constraint.get("kind")), set()).update(
                    constraint.get("alternatives") or [])

        problems = []
        for kind, expected in expect_kinds.items():
            if not _covers(expected, kinds.get(kind, set())):
                problems.append(f"{kind} 期望⊇{sorted(expected)} 实际{sorted(kinds.get(kind, set()))}")
        actual_years = parsed.get("min_years")
        if expect_years is not None and float(actual_years or 0) != expect_years:
            problems.append(f"min_years 期望{expect_years} 实际{actual_years}")

        print(f"{'OK ' if not problems else 'FAIL'} {name}")
        print(f"     constraints={[(c.get('kind'), c.get('strength'), c.get('alternatives')) for c in constraints]}")
        print(f"     min_years={actual_years} required_skills={(parsed.get('required_skills') or [])[:6]}")
        print(f"     business={parsed.get('business_directions')} career={parsed.get('career_directions')}")
        if problems:
            failures.append(f"[{name}] {'；'.join(problems)}")
            for problem in problems:
                print(f"     <- {problem}")

    if not keep:
        for jd_id in created:
            _request("DELETE", f"/api/jd/{jd_id}")
        print(f"\n已清理 {len(created)} 个测试 JD")
    else:
        print(f"\n保留 {len(created)} 个测试 JD：{created}")

    print(f"\n通过 {len(CASES) - len(failures)} / {len(CASES)}")
    for item in failures:
        print("  -", item)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
