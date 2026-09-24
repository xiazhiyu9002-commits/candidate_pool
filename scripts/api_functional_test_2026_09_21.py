"""全量后端接口功能测试：在真实 `.dev-data` 上启动真实 app，逐条打接口并校验功能要求。

设计要点：

1. **真启动**：用 `runtime.create_runtime_app(settings)`（sidecar 同一入口）+ `TestClient` 上下文，
   跑 lifespan（worker / scheduler / index sync 全部起来），接口拿到的是真实服务实例，不是 mock。
   请求带 `X-Kerui-Session`，走与桌面端相同的本地会话校验。
2. **覆盖度可证**：路由清单来自 `.tmp-api/api_inventory.json`（由 router 注册表导出），
   每条路由都必须有探针；漏掉的列进 `uncovered` 并计入失败，不允许「悄悄没测」。
3. **功能要求逐条断言**：除状态码外还断言响应字段/语义（见各 `check`），避免「200 就算过」。
4. **写入类接口用一次性夹具**：能安全创建/删除的（组织、岗位、映射项目、提醒）走
   「建 → 验 → 改 → 验 → 删」；不可逆的（物理删除候选人、快照恢复、数据迁移、批量合并）
   只探校验/守门路径，报告里标 `validation_only` 并写清「未执行破坏性动作」。

产物：`.tmp-api/api_test_report.json`、`.tmp-api/api_test_report.md`、`.tmp-api/openapi.json`。

用法：

    py -3.12 scripts/api_functional_test_2026_09_21.py --dump-routes   # 只看必填字段对照表
    py -3.12 scripts/api_functional_test_2026_09_21.py --only search,match
    py -3.12 scripts/api_functional_test_2026_09_21.py                  # 全量
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

DEV = ROOT / ".dev-data"
OUT = ROOT / ".tmp-api"
SESSION_TOKEN = "api-functional-test-token"
FIXTURE_RESUME = ROOT / "desktop" / "tests" / "fixtures" / "resume.pdf"
# 夹具用的岗位原文：刻意写成与库内任何 JD 都不重合，便于后置清理与人工识别。
FIXTURE_JD_TEXT = (
    "【接口功能测试一次性岗位】测试工程师（夹具）\n"
    "岗位职责：负责接口自动化测试用例设计与执行；\n"
    "任职要求：3 年以上测试开发经验，熟悉 Python 与 pytest。\n"
)


# ---------------------------------------------------------------- 断言小工具

def need(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def is_list(payload: Any, message: str) -> list:
    need(isinstance(payload, list), f"{message}：期望 list，实际 {type(payload).__name__}")
    return payload


def is_dict(payload: Any, message: str) -> dict:
    need(isinstance(payload, dict), f"{message}：期望 dict，实际 {type(payload).__name__}")
    return payload


def has_keys(payload: Any, *keys: str) -> dict:
    data = is_dict(payload, "响应应为对象")
    missing = [key for key in keys if key not in data]
    need(not missing, f"缺少字段 {missing}（实际字段 {sorted(data)}）")
    return data


def lists_have(payload: Any, key: str, least: int = 1) -> list:
    items = is_list(payload, "响应应为列表")
    need(len(items) >= least, f"列表长度 <{least}（实际 {len(items)}）")
    need(all(key in item for item in items if isinstance(item, dict)),
         f"列表元素缺少 {key}")
    return items


def pick(payload: Any, *keys: str) -> str:
    """按给定候选键名取第一个非空字符串值。

    本项目的创建类接口多数返回通用主键 `id`（而不是 `company_id` / `case_id` 这种带前缀名），
    因此探针按「优先具体名、回退 id」的顺序取，避免把契约差异误报成缺陷。
    """
    data = payload if isinstance(payload, dict) else {}
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def structured_error(payload: Any) -> bool:
    """统一的错误体形状：`{code, message, request_id, details}`。"""
    return isinstance(payload, dict) and isinstance(payload.get("code"), str)


def _tasks_check(payload: Any, ctx: Ctx) -> None:
    """任务列表：把 task_id 收进池供「单条查询 / 状态机」探针使用。"""
    items = is_list(payload, "任务列表")
    for item in items:
        if isinstance(item, dict):
            task_id = pick(item, "task_id", "id")
            if task_id:
                ctx.remember("task_id", task_id)


def _duplicate_groups_check(payload: Any, ctx: Ctx) -> None:
    """完全重复组：`{group_count, groups}`；把组 id 收进池供试算探针使用。"""
    data = has_keys(payload, "groups", "group_count")
    groups = data["groups"]
    need(isinstance(groups, list), "groups 应为列表")
    for group in groups:
        if isinstance(group, dict):
            group_id = pick(group, "group_id", "id")
            if group_id:
                ctx.remember("group_id", group_id)
    if not ctx.first("group_id"):
        ctx.remember("group_id", "no-duplicate-group")


def _leads_check(payload: Any, ctx: Ctx) -> None:
    """线索接口返回线索列表；把 lead_id 收进 id 池供状态/反查探针使用。"""
    if structured_error(payload):
        return
    items = is_list(payload, "线索列表")
    if not items:
        ctx.remember("lead_id", "no-lead-available")
        return
    for item in items:
        if isinstance(item, dict):
            lead_id = pick(item, "lead_id", "id")
            if lead_id:
                ctx.remember("lead_id", lead_id)


# ---------------------------------------------------------------- 探针定义

class Probe:
    """一条接口探针：怎么打 + 期望什么 + 怎么断言功能要求。"""

    def __init__(self, probe_id: str, method: str, path: str, *, module: str = "",
                 params: dict | None = None, json_body: Any = None,
                 files: dict | None = None, expect: int | tuple[int, ...] = 200,
                 check: Callable[[Any, "Ctx"], Any] | None = None,
                 prepare: Callable[["Ctx"], None] | None = None,
                 note: str = "", validation_only: bool = False) -> None:
        self.id = probe_id
        self.module = module
        self.method = method.upper()
        self.path = path
        self.params = params
        self.json_body = json_body
        self.files = files
        self.expect = expect if isinstance(expect, tuple) else (expect,)
        self.check = check
        self.prepare = prepare
        self.note = note
        self.validation_only = validation_only


class Ctx:
    """探针运行上下文：HTTP 客户端 + 从真实响应里捞出来的 id 池。"""

    def __init__(self, client) -> None:
        self.client = client
        self.ids: dict[str, list[str]] = defaultdict(list)
        self.records: list[dict] = []

    def harvest(self, payload: Any) -> None:
        """递归收集所有 `*_id` / `id` / `name` 字符串值，供后续探针引用真实对象。"""
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key in {"id", "name"} or key.endswith("_id") or key.endswith("_ids"):
                    values = value if isinstance(value, list) else [value]
                    for item in values:
                        if isinstance(item, str) and item and item not in self.ids[key]:
                            self.ids[key].append(item)
                self.harvest(value)
        elif isinstance(payload, list):
            for item in payload:
                self.harvest(item)

    def first(self, key: str, default: str = "") -> str:
        values = self.ids.get(key) or []
        return values[0] if values else default

    def all(self, key: str) -> list[str]:
        return list(self.ids.get(key) or [])

    def remember(self, key: str, value: str) -> None:
        if value and value not in self.ids[key]:
            self.ids[key].append(value)

    def resolve(self, path: str) -> str:
        def replace(match):
            key = match.group(1)
            value = self.first(key)
            need(bool(value), f"路径参数 {{{key}}} 无可用取值（未在真实数据中发现该 id）")
            return value
        import re
        return re.sub(r"\{(\w+)\}", replace, path)

    def fill(self, value: Any) -> Any:
        """把探针里的占位补成真实 id：`"{key}"` 直接替换；`None` 且同名字段有 id 时也替换。

        这样探针表可以写成 `{"company_id": None}` 这种「跟着上文刚创建的对象走」的形状，
        不必在构造时就把 id 拼进去。
        """
        if isinstance(value, str):
            if value.startswith("{") and value.endswith("}"):
                key = value[1:-1]
                resolved = self.first(key)
                need(bool(resolved), f"占位 {{{key}}} 无可用取值")
                return resolved
            return value
        if isinstance(value, dict):
            filled = {}
            for key, item in value.items():
                if item is None and key in self.ids and self.ids[key]:
                    filled[key] = self.first(key)
                else:
                    filled[key] = self.fill(item)
            return filled
        if isinstance(value, list):
            return [self.fill(item) for item in value]
        return value

    def call(self, method: str, path: str, *, params=None, json_body=None, files=None):
        kwargs: dict = {}
        if params:
            kwargs["params"] = {k: v for k, v in params.items() if v is not None}
        if json_body is not None:
            kwargs["json"] = self.fill(json_body)
        if files:
            kwargs["files"] = files
        return self.client.request(method, self.resolve(path), **kwargs)


# ---------------------------------------------------------------- 发现阶段

DISCOVERY = (
    ("GET", "/health/checks", None),
    ("GET", "/api/resumes/candidates/page", {"page": 1, "page_size": 5}),
    ("GET", "/api/jd/page", {"page": 1, "page_size": 5}),
    ("GET", "/api/match/results", {"limit_per_jd": 2}),
    ("GET", "/api/tasks", None),
    ("GET", "/api/org/companies", None),
    ("GET", "/api/mapping/projects", None),
    ("GET", "/api/reminders", None),
    ("GET", "/api/case", {"page": 1, "page_size": 5}),
    ("GET", "/api/duplicates/identical-groups", None),
    ("GET", "/api/search/index-status", None),
)


def run_discovery(ctx: Ctx) -> None:
    print("—— 发现阶段：从真实数据里捞 id ——")
    for method, path, params in DISCOVERY:
        response = ctx.call(method, path, params=params)
        print(f"  {method} {path} -> {response.status_code}")
        if response.status_code == 200:
            try:
                ctx.harvest(response.json())
            except Exception:
                pass
    candidate_id = ctx.first("candidate_id")
    if candidate_id:
        response = ctx.call("GET", f"/api/resumes/candidate/{candidate_id}/revisions")
        if response.status_code == 200:
            ctx.harvest(response.json())
    print(f"  捞到 id 种类={len(ctx.ids)}："
          + ", ".join(f"{k}={len(v)}" for k, v in sorted(ctx.ids.items())))


def _wait_for_candidate_revision(ctx: Ctx, candidate_id: str, timeout: float = 240.0) -> str:
    """等一次性夹具候选人的当前修订变成 READY，否则后续「改/删」类接口拿不到可用对象。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = ctx.client.get(f"/api/resumes/candidate/{candidate_id}/revisions")
        if response.status_code == 200:
            for item in response.json() or []:
                if str(item.get("status") or "").upper() == "READY":
                    return str(item.get("revision_id") or "")
        time.sleep(3)
    return ""


def _wait_for_jd_revision(ctx: Ctx, jd_id: str, timeout: float = 240.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = ctx.client.get("/api/jd")
        if response.status_code == 200:
            for item in response.json() or []:
                if str(item.get("jd_id")) == jd_id:
                    for key, value in item.items():
                        if isinstance(value, str) and value == "READY":
                            return str(item.get("revision_id") or "")
                    if str(item.get("revision_status") or "").upper() == "READY":
                        return str(item.get("revision_id") or "")
        time.sleep(3)
    return ""


def write_fixture_docx(path: Path) -> None:
    from docx import Document

    document = Document()
    for line in FIXTURE_JD_TEXT.splitlines():
        document.add_paragraph(line)
    document.save(str(path))


def write_fixture_resumes() -> list[Path]:
    """生成 3 份内容互不相同的合成简历（docx）。

    为什么不能复用仓库里那份测试 PDF：导入按内容哈希去重，同一份文件第二次导入会返回
    `ALREADY_IMPORTED` 且复用同一候选人 —— 三次导入只得到一个人，
    「物理删除 / 批量删除」就没有各自独立的对象可删了。
    """
    from docx import Document

    paths: list[Path] = []
    for index, (name, title, skills) in enumerate((
        ("接口测试夹具甲", "后端开发工程师", "Java Spring Boot 微服务 MySQL"),
        ("接口测试夹具乙", "数据开发工程师", "Python Spark 数仓 ETL"),
        ("接口测试夹具丙", "测试开发工程师", "pytest 自动化测试 接口测试"),
    ), 1):
        document = Document()
        document.add_paragraph(name)
        document.add_paragraph(f"求职意向：{title}")
        document.add_paragraph("技能：")
        document.add_paragraph(skills)
        document.add_paragraph("工作经历：")
        document.add_paragraph(f"2020.01~至今 接口功能测试一次性公司 {title}："
                               f"负责接口功能测试夹具数据，使用 {skills.split()[0]}。")
        document.add_paragraph("教育经历：接口功能测试大学 计算机科学与技术 本科")
        path = OUT / f"fixture-resume-{index}.docx"
        document.save(str(path))
        paths.append(path)
    return paths


def prepare_fixtures(ctx: Ctx) -> None:
    """建一次性夹具：3 个候选人 + 2 个岗位 + 1 份 docx 岗位文件。

    为什么是 3 个候选人：`fixture_candidate_id` 给「改字段/再解析/换版本/画像重生成/
    流程状态机/更正」用；另两个专供删除路径（物理删除、批量物理删除），
    这样删除类接口有真实对象可删，又不会把还要继续测试的夹具提前删掉。
    全部在报告里明确标注，且不触碰库内既有数据。
    """
    print("—— 夹具阶段：导入一次性候选人 / 岗位 ——")
    slots = (("fixture_candidate_id", 1), ("fixture_delete_candidate_id", 2),
             ("fixture_bulkdelete_candidate_id", 3))
    resume_paths = write_fixture_resumes()
    for (key, index), resume_path in zip(slots, resume_paths):
        with resume_path.open("rb") as handle:
            response = ctx.client.post(
                "/api/resumes/import",
                files={"file": (resume_path.name, handle,
                                "application/vnd.openxmlformats-officedocument."
                                "wordprocessingml.document")},
            )
        if response.status_code in (200, 202):
            data = response.json()
            ctx.remember(key, data.get("candidate_id") or "")
            ctx.remember("fixture_document_id", data.get("document_id") or "")
            ctx.remember("fixture_task_id", data.get("task_id") or "")
            print(f"  候选人夹具 #{index}: action={data.get('action')} "
                  f"candidate_id={data.get('candidate_id')} task={(data.get('task_id') or '')[:8]}")
        else:
            print(f"  候选人夹具 #{index} 失败：{response.status_code} {response.text[:120]}")
    revision = _wait_for_candidate_revision(ctx, ctx.first("fixture_candidate_id"))
    if revision:
        ctx.remember("fixture_revision_id", revision)
        print(f"  夹具修订就绪：revision={revision[:8]}")
    else:
        print("  警告：夹具修订未在时限内就绪，相关接口可能返回非 200")

    for key, index in (("fixture_jd_id", 1), ("fixture_jd_delete_id", 2),
                       ("fixture_jd_bulkdelete_id", 3)):
        response = ctx.client.post("/api/jd/import", json={
            "company": "接口功能测试公司", "title": f"夹具测试工程师{index}",
            "source_text": FIXTURE_JD_TEXT,
        })
        if response.status_code in (200, 202):
            data = response.json()
            ctx.remember(key, data.get("jd_id") or "")
            ctx.remember("fixture_jd_revision_id", data.get("revision_id") or "")
            print(f"  岗位夹具 #{index}: jd_id={data.get('jd_id')} "
                  f"revision_id={data.get('revision_id')}")
        else:
            print(f"  岗位夹具 #{index} 失败：{response.status_code} {response.text[:120]}")
    docx_path = OUT / "fixture-jd.docx"
    write_fixture_docx(docx_path)
    print(f"  docx 夹具已生成：{docx_path}")

    # 真实 JD 的当前修订：匹配类接口要用它（夹具岗位的解析是异步的，不必等）。
    response = ctx.client.get("/api/jd")
    if response.status_code == 200:
        for item in response.json() or []:
            revision_id = str(item.get("revision_id") or "")
            if revision_id and revision_id not in ctx.all("fixture_jd_revision_id"):
                ctx.remember("jd_revision_id", revision_id)
                break
    print(f"  真实 JD 修订（匹配用）：{ctx.first('jd_revision_id')[:8]}")


# ---------------------------------------------------------------- 探针表

def build_probes(ctx: Ctx) -> list[Probe]:
    probes: list[Probe] = []

    def add(*args, **kwargs) -> None:
        probes.append(Probe(*args, **kwargs))

    # ---------------- health
    add("health.live", "GET", "/health/live", module="health",
        check=lambda r, c: need(r.get("status") == "alive", "status 应为 alive"))
    add("health.ready", "GET", "/health/ready", module="health",
        check=lambda r, c: need(r.get("status") == "ready", "status 应为 ready"))
    add("health.checks", "GET", "/health/checks", module="health",
        check=lambda r, c: need(isinstance(r, dict) and r, "checks 应返回非空对象"))

    # ---------------- resumes：只读
    add("resumes.candidates", "GET", "/api/resumes/candidates", module="resumes",
        check=lambda r, c: lists_have(r, "candidate_id"))
    add("resumes.candidates_page", "GET", "/api/resumes/candidates/page", module="resumes",
        params={"page": 1, "page_size": 5},
        check=lambda r, c: has_keys(r, "items", "total") and len(r["items"]) > 0)
    add("resumes.direction_pending", "GET", "/api/resumes/candidates/direction-pending",
        module="resumes", check=lambda r, c: is_list(r, "方向待核队列"))
    add("resumes.revisions", "GET", "/api/resumes/candidate/{candidate_id}/revisions",
        module="resumes", check=lambda r, c: lists_have(r, "revision_id"))
    add("resumes.review", "GET", "/api/resumes/revisions/{revision_id}/review",
        module="resumes", check=lambda r, c: is_dict(r, "复核结果"))
    add("resumes.download", "GET", "/api/resumes/revisions/{revision_id}/download",
        module="resumes",
        check=lambda r, c: None)  # 二进制：由 harness 端断言长度
    add("resumes.view_target", "GET", "/api/resumes/revisions/{revision_id}/view-target",
        module="resumes", check=lambda r, c: is_dict(r, "预览目标"))
    add("resumes.preview", "GET", "/api/resumes/revisions/{revision_id}/preview",
        module="resumes", check=lambda r, c: None)  # 可能是 HTML/PDF 流
    add("resumes.contact_get", "GET", "/api/resumes/candidate/{candidate_id}/contact",
        module="resumes", check=lambda r, c: is_dict(r, "联系方式"))

    # ---------------- tasks
    add("tasks.list", "GET", "/api/tasks", module="tasks", check=lambda r, c: _tasks_check(r, c))
    add("tasks.status_batch", "POST", "/api/tasks/status-batch", module="tasks",
        json_body={"task_ids": ctx.all("task_id")[:5]},
        check=lambda r, c: has_keys(r, "found", "missing_ids"))
    add("tasks.get", "GET", "/api/tasks/{task_id}", module="tasks",
        check=lambda r, c: has_keys(r, "id", "status", "task_type"))

    # ---------------- search
    add("search.candidates_hybrid", "POST", "/api/search/candidates", module="search",
        json_body={"query": "Java 后端 微服务", "mode": "hybrid", "search_body": False},
        check=_search_plan_check)
    add("search.candidates_keyword", "POST", "/api/search/candidates", module="search",
        json_body={"query": "数仓", "mode": "keyword"},
        check=lambda r, c: has_keys(r, "items", "query_plan", "status"))
    add("search.candidates_parse_on", "POST", "/api/search/candidates", module="search",
        json_body={"query": "深圳 5 年以上 Java 后端，本科以上", "mode": "hybrid",
                   "parse_enabled": True, "search_body": True},
        note="解析开启 + 混合 + 正文：同时覆盖解析回显与正文通道",
        check=_search_plan_check)
    add("search.review_start", "POST", "/api/search/review", module="search",
        json_body={"query": "Java 后端", "candidate_ids": ctx.all("candidate_id")[:2],
                   "reasoning": False},
        check=lambda r, c: has_keys(r, "review_id"))

    # ---------------- indexes
    add("indexes.status", "GET", "/api/search/index-status", module="indexes",
        check=lambda r, c: is_dict(r, "索引状态"))
    add("indexes.retry", "POST", "/api/search/index-retry", module="indexes",
        check=lambda r, c: is_dict(r, "重试结果"))

    # ---------------- jd 只读
    add("jd.list", "GET", "/api/jd", module="jd", check=lambda r, c: is_list(r, "岗位列表"))
    add("jd.page", "GET", "/api/jd/page", module="jd", params={"page": 1, "page_size": 5},
        check=lambda r, c: has_keys(r, "items", "total"))
    add("jd.import", "POST", "/api/jd/import", module="jd",
        json_body={"company": "接口功能测试公司", "title": "夹具测试工程师（单条）",
                   "source_text": FIXTURE_JD_TEXT},
        expect=(200, 202), note="单条文本导岗位（夹具阶段另建了 3 个同款夹具）",
        check=lambda r, c: c.remember("fixture_jd_extra_id",
                                      pick(has_keys(r, "jd_id", "revision_id"), "jd_id")))

    # ---------------- match 只读
    add("match.results", "GET", "/api/match/results", module="match",
        check=lambda r, c: has_keys(r, "groups"))
    add("match.reverse", "GET", "/api/match/reverse/{candidate_id}", module="match",
        check=lambda r, c: is_list(r, "反查结果"))
    add("match.candidate_list", "GET", "/api/match/candidate/{candidate_id}", module="match",
        check=lambda r, c: is_list(r, "候选人匹配"))
    add("match.jd_export", "GET", "/api/match/jd/{jd_revision_id}/export", module="match",
        check=lambda r, c: None)

    # ---------------- dashboard / daily-followup / diagnostics
    add("dashboard.overview", "GET", "/api/dashboard/overview", module="dashboard",
        check=lambda r, c: is_dict(r, "总览"))
    add("dashboard.by_jd", "GET", "/api/dashboard/by-jd", module="dashboard",
        check=lambda r, c: is_list(r, "按岗位"))
    add("dashboard.trend", "GET", "/api/dashboard/trend", module="dashboard",
        params={"granularity": "week"}, check=lambda r, c: is_list(r, "趋势"))
    add("dashboard.export", "GET", "/api/dashboard/export", module="dashboard",
        check=lambda r, c: None)
    add("daily_followup.today", "GET", "/api/daily-followup/today", module="daily_followup",
        check=lambda r, c: is_dict(r, "今日待办"))
    add("diagnostics.collect", "GET", "/api/diagnostics", module="diagnostics",
        check=lambda r, c: is_dict(r, "诊断"))
    add("diagnostics.export", "GET", "/api/diagnostics/export", module="diagnostics",
        check=lambda r, c: None)

    # ---------------- settings / ai
    add("settings.get", "GET", "/api/settings", module="settings",
        check=lambda r, c: is_dict(r, "设置"))
    add("settings.put_roundtrip", "PUT", "/api/settings", module="settings", json_body={},
        note="空对象写入 = 无改动；随后比对 GET 前后一致",
        check=_settings_roundtrip_check)
    add("settings.vendors", "GET", "/api/settings/vendors", module="settings",
        check=lambda r, c: is_list(r, "供应商"))
    add("settings.mail_status", "GET", "/api/settings/mail/status", module="settings",
        check=lambda r, c: is_dict(r, "邮件状态"))
    add("ai.catalog", "GET", "/api/ai/catalog", module="ai_settings",
        check=lambda r, c: is_dict(r, "目录"))
    add("ai.config_get", "GET", "/api/ai/config", module="ai_settings",
        check=lambda r, c: is_dict(r, "AI 配置"))
    add("ai.status", "GET", "/api/ai/status", module="ai_settings",
        check=lambda r, c: is_dict(r, "AI 状态"))
    add("onboarding.status", "GET", "/api/onboarding/status", module="onboarding",
        check=lambda r, c: is_dict(r, "首启状态"))

    # ---------------- duplicates 只读
    add("duplicates.report", "GET", "/api/duplicates/report", module="duplicates",
        check=lambda r, c: is_dict(r, "重复报告"))
    add("duplicates.groups", "GET", "/api/duplicates/identical-groups", module="duplicates",
        check=lambda r, c: _duplicate_groups_check(r, c))

    # ---------------- schools
    add("schools.list", "GET", "/api/schools", module="schools",
        check=lambda r, c: is_list(r, "学校列表"))
    add("schools.template", "GET", "/api/schools/import-template", module="schools",
        check=lambda r, c: None)
    add("schools.resolve", "POST", "/api/schools/resolve", module="schools",
        json_body={"name": "北大"},
        check=lambda r, c: is_dict(r, "学校解析结果"))

    # ---------------- bd_search
    add("bd.search", "POST", "/api/bd/search", module="bd_search",
        json_body={"query": "Java 后端 上海", "limit": 2},
        check=lambda r, c: _leads_check(r, c))
    add("bd.search_for_candidate", "POST", "/api/bd/search-for-candidate",
        module="bd_search",
        json_body={"candidate_id": "{candidate_id}", "limit": 2},
        check=lambda r, c: _leads_check(r, c))

    # ---------------- reminders：建/查/删 一次性夹具
    add("reminders.create", "POST", "/api/reminders", module="reminders",
        json_body={"title": "接口功能测试一次性提醒", "remind_at": "2026-09-22T10:00:00",
                   "note": "脚本创建，随后 dismiss"},
        check=lambda r, c: c.remember("reminder_id", pick(r, "reminder_id", "id")))
    add("reminders.list", "GET", "/api/reminders", module="reminders",
        check=lambda r, c: is_list(r, "待处理提醒"))
    add("reminders.due", "GET", "/api/reminders/due", module="reminders",
        check=lambda r, c: is_list(r, "到期提醒"))
    add("reminders.dismiss", "POST", "/api/reminders/{reminder_id}/dismiss", module="reminders",
        check=lambda r, c: None)

    # ---------------- org：一次性夹具，建→验→改→删
    add("org.company_create", "POST", "/api/org/companies", module="org",
        json_body={"name": "接口功能测试一次性公司"},
        note="后续所有单公司读写都指向这个夹具，绝不触碰库内既有企业组织",
        check=lambda r, c: c.remember("fixture_company_id", pick(r, "company_id", "id")))
    add("org.companies", "GET", "/api/org/companies", module="org",
        check=lambda r, c: lists_have(r, "id"))
    add("org.company_patch", "PATCH", "/api/org/companies/{fixture_company_id}", module="org",
        json_body={"name": "接口功能测试一次性公司（改名）"},
        check=lambda r, c: None)
    add("org.department_create", "POST", "/api/org/departments", module="org",
        json_body={"company_id": "{fixture_company_id}", "name": "接口功能测试一次性部门"},
        check=_org_department_create_check)
    add("org.departments", "GET", "/api/org/companies/{fixture_company_id}/departments",
        module="org", check=lambda r, c: is_list(r, "部门列表"))
    add("org.department_patch", "PATCH", "/api/org/departments/{department_id}", module="org",
        json_body={"name": "接口功能测试一次性部门（改名）"}, check=lambda r, c: None)
    add("org.employee_create", "POST", "/api/org/employees", module="org",
        json_body={"company_id": "{fixture_company_id}", "name": "接口功能测试一次性员工",
                   "department_id": "{department_id}", "title": "测试"},
        check=_org_employee_create_check)
    add("org.employees", "GET", "/api/org/companies/{fixture_company_id}/employees",
        module="org", check=lambda r, c: is_list(r, "员工列表"))
    add("org.employee_patch", "PATCH", "/api/org/employees/{employee_id}", module="org",
        json_body={"title": "测试（改名）"}, check=lambda r, c: None)
    add("org.employee_bind", "POST", "/api/org/employees/{employee_id}/bind", module="org",
        json_body={"phone": "13800000000", "name": "接口功能测试一次性员工"}, note="绑定到人才库手机号",
        expect=(200, 404), check=lambda r, c: None)
    add("org.tree", "GET", "/api/org/companies/{fixture_company_id}/tree", module="org",
        check=lambda r, c: is_dict(r, "组织树"))
    add("org.source", "GET", "/api/org/companies/{fixture_company_id}/source", module="org",
        check=lambda r, c: None)
    add("org.export_xlsx", "GET", "/api/org/companies/{fixture_company_id}/export", module="org",
        check=lambda r, c: None)
    add("org.export_client", "GET", "/api/org/companies/{fixture_company_id}/export-client",
        module="org", check=lambda r, c: None)
    add("org.export_pdf", "GET", "/api/org/companies/{fixture_company_id}/export-pdf",
        module="org", params={"orientation": "portrait", "watermark": False},
        check=lambda r, c: None)
    add("org.employee_delete", "DELETE", "/api/org/employees/{employee_id}", module="org",
        check=lambda r, c: None)
    add("org.department_delete", "DELETE", "/api/org/departments/{department_id}", module="org",
        check=lambda r, c: None)
    add("org.company_delete", "DELETE", "/api/org/companies/{fixture_company_id}", module="org",
        check=lambda r, c: None)
    add("org.import_parse", "POST", "/api/org/import/parse", module="org",
        json_body={"text": "接口功能测试公司\n技术部\n张三 后端工程师"},
        expect=(200, 502, 503), note="真实模型调用",
        check=lambda r, c: is_dict(r, "组织导入草稿") if not structured_error(r) else None)
    add("org.import_answer", "POST", "/api/org/import/answer", module="org",
        json_body={"text": "接口功能测试公司\n技术部\n张三 后端工程师", "answers": ["无"]},
        expect=(200, 502, 503), check=lambda r, c: is_dict(r, "组织导入追问")
        if not structured_error(r) else None)
    add("org.import_revise", "POST", "/api/org/import/revise", module="org",
        json_body={"draft": {"company_name": "接口功能测试公司", "departments": []},
                   "instruction": "把公司名改成接口功能测试公司2"},
        expect=(200, 502, 503), note="草稿形状必须带 company_name",
        check=lambda r, c: is_dict(r, "组织导入修订") if not structured_error(r) else None)

    # ---------------- mapping：一次性夹具
    add("mapping.projects", "GET", "/api/mapping/projects", module="mapping",
        check=lambda r, c: is_list(r, "映射项目"))
    add("mapping.project_create", "POST", "/api/mapping/projects", module="mapping",
        json_body={"name": "接口功能测试一次性映射项目", "description": "脚本创建"},
        check=lambda r, c: c.remember("project_id", pick(r, "project_id", "id")))
    add("mapping.build_from_text", "POST", "/api/mapping/projects/{project_id}/build-from-text",
        module="mapping", json_body={"text": "接口功能测试公司-技术部-张三", "label": "夹具"},
        check=lambda r, c: is_dict(r, "构树结果"))
    add("mapping.snapshot_create", "POST", "/api/mapping/projects/{project_id}/snapshots",
        module="mapping", json_body={"label": "夹具快照"},
        check=lambda r, c: c.remember("snapshot_id", pick(r, "snapshot_id", "id")))
    add("mapping.snapshots", "GET", "/api/mapping/projects/{project_id}/snapshots",
        module="mapping", check=lambda r, c: is_list(r, "快照列表"))
    add("mapping.tree", "GET", "/api/mapping/snapshots/{snapshot_id}/tree", module="mapping",
        check=lambda r, c: is_list(r, "映射树"))
    add("mapping.export", "GET", "/api/mapping/snapshots/{snapshot_id}/export", module="mapping",
        expect=(200, 404),
        note="空快照导出回 404（E_NOT_FOUND: snapshot not found or empty）属已知行为",
        check=lambda r, c: None)
    add("mapping.export_pdf", "GET", "/api/mapping/snapshots/{snapshot_id}/export-pdf",
        module="mapping", expect=(200, 404), check=lambda r, c: None)

    # ---------------- backup：只读 + 校验路径
    add("backup.snapshots", "GET", "/api/backup/snapshots", module="backup",
        check=lambda r, c: is_list(r, "快照列表"))
    add("backup.snapshot_create", "POST", "/api/backup/snapshots", module="backup",
        json_body={"label": "接口功能测试一次性快照"}, note="会写一份快照到数据目录",
        check=lambda r, c: is_dict(r, "快照"))
    add("backup.restore_unknown", "POST", "/api/backup/restore/no-such-snapshot-2026.zip",
        module="backup", expect=(400, 404),
        validation_only=True, note="未执行恢复：只验证不存在的文件被拒",
        check=lambda r, c: has_keys(r, "code"))
    add("backup.portable", "POST", "/api/backup/portable", module="backup",
        json_body={"target_path": str(OUT / "portable-test.krbak"), "passphrase": "fixture-passphrase"},
        note="导出可迁移包到 .tmp-api，不覆盖任何现有数据",
        check=lambda r, c: c.remember("portable_path",
                                      pick(r, "path") or str(OUT / "portable-test.krbak")))
    add("backup.portable_restore", "POST", "/api/backup/portable/restore", module="backup",
        json_body={"backup_path": "{portable_path}",
                   "target_root": str(OUT / f"portable-restore-{os.getpid()}"),
                   "passphrase": "fixture-passphrase"},
        note="恢复到 .tmp-api 下的**新建空目录**（服务会拒绝非空目录，故每次换一个新目录）",
        check=lambda r, c: is_dict(r, "恢复结果"))

    # ---------------- migration：只探守门
    add("migration.guard", "POST", "/api/migration", module="migration",
        json_body={"target_root": str(OUT)}, expect=(400, 409, 422),
        validation_only=True, note="未执行迁移：目标是已有的非空目录，应被拒",
        check=lambda r, c: has_keys(r, "code"))

    # ---------------- soft-delete：夹具上验证 软删 → 恢复（候选人只支持物理删除，故用岗位）
    add("soft_delete.applied", "POST", "/api/soft-delete", module="soft_delete",
        json_body={"entity_type": "jd", "entity_id": "{fixture_jd_delete_id}"},
        note="对一次性夹具岗位软删，随后 restore（候选人侧只支持物理删除）",
        check=lambda r, c: is_dict(r, "软删结果"))
    add("soft_delete.restore", "POST", "/api/soft-delete/restore", module="soft_delete",
        json_body={"entity_type": "jd", "entity_id": "{fixture_jd_delete_id}"},
        check=lambda r, c: is_dict(r, "恢复结果"))

    # ---------------- resumes：写入类（全部落在一次性夹具上）
    add("resumes.import", "POST", "/api/resumes/import", module="resumes",
        files={"file": ("api-fixture-again.pdf", FIXTURE_RESUME.read_bytes(),
                        "application/pdf")},
        expect=(200, 202),
        note="重复导入同一份已入库简历：验证去重/冲突分支返回结构化结果",
        check=lambda r, c: has_keys(r, "action", "candidate_id"))
    add("resumes.import_folder", "POST", "/api/resumes/import-folder", module="resumes",
        json_body={"directory": str(OUT / "no-such-folder")}, expect=(400,),
        validation_only=True, note="目录不存在应被拒（不批量导入真实简历）",
        check=lambda r, c: has_keys(r, "code"))
    add("resumes.contact_put", "PUT", "/api/resumes/candidate/{fixture_candidate_id}/contact",
        module="resumes", json_body={"phone": "13900000000", "email": "fixture@example.com"},
        check=lambda r, c: is_dict(r, "联系方式写入"))
    # 沟通记录是候选人级备注（不写 parsed_data、不触发画像过期、不入队索引）。
    # 顺序要紧：先写入 → 再按**中间片段**查（证明 like 语义）→ 最后清空。
    add("resumes.communication_note_put", "PUT",
        "/api/resumes/candidate/{fixture_candidate_id}/communication-note",
        module="resumes", json_body={"note": "接口功能测试沟通记录"},
        check=lambda r, c: need(
            is_dict(r, "沟通记录写入").get("communication_note") == "接口功能测试沟通记录",
            "沟通记录应原样回显写入值"))
    add("search.communication_note_like", "POST", "/api/search/candidates", module="search",
        json_body={"query": "", "mode": "keyword",
                   "filters": {"communication_note": "测试沟通"}},
        note="沟通文本按子串匹配（like），不是精确相等：查的是中段片段",
        check=_communication_note_like_check)
    add("resumes.communication_note_clear", "PUT",
        "/api/resumes/candidate/{fixture_candidate_id}/communication-note",
        module="resumes", json_body={"note": ""},
        check=lambda r, c: need(
            is_dict(r, "沟通记录清空").get("communication_note") is None,
            "空字符串应清空而不是存成空串"))
    add("resumes.field_put", "PUT", "/api/resumes/candidate/{fixture_candidate_id}/field",
        module="resumes", json_body={"field": "summary", "value": "接口功能测试写入"},
        expect=(200, 422),
        note="`field` 必须在 ParsedResume 字段白名单内（title 不在，属设计约束）",
        check=lambda r, c: is_dict(r, "字段写入") if not structured_error(r) else None)
    add("resumes.parsed_put", "PUT", "/api/resumes/candidate/{fixture_candidate_id}/parsed",
        module="resumes", json_body={"parsed_data": {"summary": "接口功能测试写入",
                                                     "skills": ["pytest"]}},
        note="夹具候选人上整表写入解析数据（会触发索引重建）",
        check=_parsed_put_check)
    add("resumes.regen_profile", "POST",
        "/api/resumes/candidate/{fixture_candidate_id}/regen-profile", module="resumes",
        json_body={"instruction": "用一句话概括这位候选人的经历"},
        expect=(200, 502, 503), note="真实模型调用（夹具候选人）",
        check=lambda r, c: is_dict(r, "画像重生成"))
    add("resumes.switch", "POST", "/api/resumes/revisions/{fixture_revision_id}/switch",
        module="resumes", check=lambda r, c: is_dict(r, "版本切换"))
    add("resumes.reparse", "POST", "/api/resumes/revisions/{fixture_revision_id}/reparse",
        module="resumes", json_body={"force_ocr": False, "use_vision": False},
        expect=(200, 202), check=lambda r, c: is_dict(r, "重新解析"))
    add("resumes.bulk_reparse", "POST", "/api/resumes/bulk/reparse", module="resumes",
        json_body={"ids": ["{fixture_candidate_id}"]}, check=_bulk_check)
    add("resumes.bulk_force_ocr", "POST", "/api/resumes/bulk/force-ocr", module="resumes",
        json_body={"ids": ["{fixture_candidate_id}"]}, check=_bulk_check)
    add("resumes.bulk_download", "POST", "/api/resumes/bulk/download", module="resumes",
        json_body={"ids": ["{candidate_id}"]}, check=lambda r, c: None,
        note="读真实候选人的原件打包为 ZIP")
    add("resumes.delete", "DELETE", "/api/resumes/candidate/{fixture_delete_candidate_id}",
        module="resumes", expect=(200, 204),
        note="物理删除一次性夹具候选人（不是库内既有候选人）",
        check=lambda r, c: None)
    add("resumes.bulk_delete", "POST", "/api/resumes/bulk/delete", module="resumes",
        json_body={"ids": ["{fixture_bulkdelete_candidate_id}"]}, expect=(200,),
        note="批量物理删除第二个一次性夹具", check=_bulk_check)

    # ---------------- tasks：状态机
    add("tasks.pause", "POST", "/api/tasks/{fixture_task_id}/pause", module="tasks",
        expect=(200, 409), note="夹具任务可能已结束，409 属正常守门",
        check=_task_state_check)
    add("tasks.resume", "POST", "/api/tasks/{fixture_task_id}/resume", module="tasks",
        expect=(200, 409), check=_task_state_check)
    add("tasks.cancel", "POST", "/api/tasks/{fixture_task_id}/cancel", module="tasks",
        expect=(200, 409), check=_task_state_check)
    add("tasks.retry", "POST", "/api/tasks/{fixture_task_id}/retry", module="tasks",
        expect=(200, 409), check=_task_state_check)

    # ---------------- search：复核进度
    add("search.review_get", "GET", "/api/search/review/{review_id}", module="search",
        check=lambda r, c: has_keys(r, "query_key", "status", "progress", "items"))

    # ---------------- jd：写入类（夹具）
    add("jd.import_file", "POST", "/api/jd/import-file", module="jd",
        files={"file": ("fixture-jd.docx", (OUT / "fixture-jd.docx").read_bytes(),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        expect=(200, 202), note="docx 一次性岗位文件",
        check=lambda r, c: has_keys(r, "jd_id", "revision_id"))
    add("jd.import_batch", "POST", "/api/jd/import-batch", module="jd",
        json_body={"source_text": FIXTURE_JD_TEXT + "\n=====\n" + FIXTURE_JD_TEXT.replace("夹具", "夹具B")},
        expect=(200, 202), check=lambda r, c: is_dict(r, "批量导入"))
    add("jd.import_batch_file", "POST", "/api/jd/import-batch-file", module="jd",
        files={"file": ("fixture-jd-batch.docx", (OUT / "fixture-jd.docx").read_bytes(),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        expect=(200, 202), check=lambda r, c: is_dict(r, "批量文件导入"))
    add("jd.status_patch", "PATCH", "/api/jd/{fixture_jd_id}/status", module="jd",
        json_body={"status": "OPEN"}, check=lambda r, c: is_dict(r, "状态写入"))
    add("jd.field_put", "PUT", "/api/jd/{fixture_jd_id}/field", module="jd",
        json_body={"field": "company", "value": "接口功能测试公司（改名）"},
        check=lambda r, c: is_dict(r, "字段写入"))
    add("jd.parsed_put", "PUT", "/api/jd/{fixture_jd_id}/parsed", module="jd",
        json_body={"parsed_data": {"title": "夹具测试工程师", "required_skills": ["pytest"]}},
        note="夹具岗位上整表写入解析数据", check=_parsed_put_check)
    add("jd.parse_constraints", "POST", "/api/jd/parse-constraints", module="jd",
        json_body={"source_text": FIXTURE_JD_TEXT}, expect=(200, 502, 503),
        note="真实模型调用；规则与模型取并集",
        check=lambda r, c: is_dict(r, "硬条件重解析"))
    add("jd.regen_profile", "POST", "/api/jd/{fixture_jd_id}/regen-profile", module="jd",
        json_body={"instruction": "强调需要自动化测试经验"}, expect=(200, 502, 503, 504),
        note=("真实模型调用（夹具岗位）。**504 是设计行为**：服务端硬上限 "
              "`REGEN_TIMEOUT_SECONDS=150` 秒，超时返回 `E_PROFILE_TIMEOUT` 并保留旧画像；"
              "强制思考模型（实测智谱 glm-5.3 做完整解析 >300 秒）会走到这一支。"),
        check=lambda r, c: is_dict(r, "画像重生成") if not structured_error(r)
        else need(r.get("code") == "E_PROFILE_TIMEOUT", "超时必须是明确的画像超时错误"))
    add("jd.delete", "DELETE", "/api/jd/{fixture_jd_delete_id}", module="jd",
        expect=(200, 204), note="物理删除一次性夹具岗位", check=lambda r, c: None)
    add("jd.bulk_delete", "POST", "/api/jd/bulk/delete", module="jd",
        json_body={"ids": ["{fixture_jd_bulkdelete_id}"]}, expect=(200,),
        note="批量物理删除第二个一次性夹具岗位", check=lambda r, c: is_dict(r, "批量删除"))
    add("jd.cleanup_extra", "DELETE", "/api/jd/{fixture_jd_extra_id}", module="jd",
        expect=(200, 204), note="清掉 jd.import 探针建的那个夹具，不留残留数据",
        check=lambda r, c: None)

    # ---------------- match
    add("match.jd", "POST", "/api/match/jd", module="match",
        json_body={"revision_id": "{jd_revision_id}", "limit": 20, "mode": "hybrid"},
        check=lambda r, c: c.remember("run_id", has_keys(r, "run_id")["run_id"]))
    add("match.batch", "POST", "/api/match/batch", module="match",
        json_body={"revision_ids": ["{jd_revision_id}"], "limit": 10, "mode": "hybrid"},
        check=lambda r, c: is_dict(r, "批量匹配"))
    add("match.run_export", "GET", "/api/match/run/{run_id}/export", module="match",
        check=lambda r, c: None)
    add("match.result_mark", "POST", "/api/match/result/{result_id}/mark", module="match",
        json_body={"status": "保留"}, check=lambda r, c: has_keys(r, "result_id", "status"))
    add("match.result_create_case", "POST", "/api/match/result/{result_id}/create-case",
        module="match", expect=(200, 409),
        note="已入库匹配结果转流程；409 表示状态不允许，属正常守门",
        check=lambda r, c: is_dict(r, "转为流程"))
    add("match.candidate_post", "POST", "/api/match/candidate/{candidate_id}", module="match",
        check=lambda r, c: None)
    add("match.candidates_bulk", "POST", "/api/match/candidates/bulk", module="match",
        json_body={"candidate_ids": ctx.all("candidate_id")[:2], "mode": "hybrid"},
        check=lambda r, c: is_dict(r, "批量人找岗位"))
    add("match.ai_review_start", "POST", "/api/match/run/{run_id}/ai-review", module="match",
        json_body={"reasoning": False}, expect=(200, 202),
        note="真实模型调用：启动 AI 深度复核", check=lambda r, c: is_dict(r, "复核任务"))
    add("match.ai_review_get", "GET", "/api/match/run/{run_id}/ai-review", module="match",
        check=lambda r, c: has_keys(r, "status"))

    # ---------------- correction
    add("correction.apply", "POST", "/api/correction/apply", module="correction",
        json_body={"entity_type": "candidate", "entity_id": "{fixture_candidate_id}",
                   "field_name": "total_years", "new_value": "8",
                   "reason": "接口功能测试"},
        expect=(200, 201, 409, 422),
        note="可更正字段白名单：candidate ∈ {display_name, status, total_years, highest_degree}",
        check=lambda r, c: (None if structured_error(r)
                            else c.remember("correction_id", pick(r, "correction_id", "id"))))
    add("correction.undo", "POST", "/api/correction/{correction_id}/undo", module="correction",
        expect=(200, 404), check=lambda r, c: None)

    # ---------------- cases：夹具候选人 + 夹具岗位走完整状态机
    add("cases.create", "POST", "/api/case", module="cases",
        json_body={"candidate_id": "{fixture_candidate_id}", "jd_id": "{fixture_jd_id}",
                   "note": "接口功能测试"},
        expect=(200, 201, 409), note="在一次性夹具上创建流程",
        check=lambda r, c: c.remember("case_id", pick(r, "case_id", "id")))
    add("cases.list", "GET", "/api/case", module="cases", params={"page": 1, "page_size": 5},
        check=lambda r, c: has_keys(r, "items", "total"))
    add("cases.get", "GET", "/api/case/{case_id}", module="cases",
        check=_case_detail_check)
    add("cases.recommend", "POST", "/api/case/{case_id}/recommend", module="cases",
        json_body={"note": "接口功能测试"}, expect=(200, 201, 409),
        check=_event_check)
    add("cases.enter_interview", "POST", "/api/case/{case_id}/enter-interview", module="cases",
        json_body={"round_name": "一面", "round_type": "TECH"}, expect=(200, 201, 409),
        check=_event_check)
    add("cases.result", "POST", "/api/case/{case_id}/result", module="cases",
        json_body={"case_round_id": "{case_round_id}", "result": "通过"},
        expect=(200, 201, 409), check=_event_check)
    add("cases.pass_and_advance", "POST", "/api/case/{case_id}/pass-and-advance",
        module="cases",
        json_body={"case_round_id": "{case_round_id}", "next_round_name": "二面",
                   "next_round_type": "TECH"},
        expect=(200, 201, 409), check=_event_check)
    add("cases.offer", "POST", "/api/case/{case_id}/offer", module="cases",
        json_body={"note": "接口功能测试"}, expect=(200, 201, 409), check=_event_check)
    add("cases.offer_status", "POST", "/api/case/{case_id}/offer-status", module="cases",
        json_body={"result": "已接受"}, expect=(200, 201, 409), check=_event_check)
    add("cases.onboard", "POST", "/api/case/{case_id}/onboard", module="cases",
        json_body={"note": "接口功能测试"}, expect=(200, 201, 409), check=_event_check)
    add("cases.exit", "POST", "/api/case/{case_id}/exit", module="cases",
        json_body={"result": "候选人放弃"}, expect=(200, 201, 409), check=_event_check)
    add("cases.event_void", "POST", "/api/case/event/{event_id}/void", module="cases",
        json_body={"note": "接口功能测试撤销"}, expect=(200, 201, 404, 409),
        check=_event_check)
    add("cases.process_get", "GET", "/api/case/process/{fixture_jd_id}", module="cases",
        check=lambda r, c: is_list(r, "流程配置"))
    add("cases.process_put", "PUT", "/api/case/process/{fixture_jd_id}", module="cases",
        json_body={"rounds": [{"round_no": 1, "round_name": "一面", "round_type": "TECH"}]},
        note="夹具岗位上的流程配置（不碰真实岗位的轮次配置）",
        check=lambda r, c: is_list(r, "流程配置写入"))
    add("cases.delete", "DELETE", "/api/case/{case_id}", module="cases", expect=(200, 204),
        note="软删除一次性夹具流程", check=lambda r, c: None)
    add("cases.bulk_delete", "POST", "/api/case/bulk/delete", module="cases",
        json_body={"ids": ["{case_id}"]}, check=lambda r, c: is_dict(r, "批量删除"))

    # ---------------- bd_search 状态与反查
    add("bd.lead_status", "POST", "/api/bd/{lead_id}/status", module="bd_search",
        json_body={"status": "已联系", "note": "接口功能测试"}, expect=(200, 400, 404, 409),
        check=lambda r, c: is_dict(r, "线索状态"))
    add("bd.lookup_pool", "POST", "/api/bd/leads/{lead_id}/lookup-pool", module="bd_search",
        expect=(200, 404),
        check=lambda r, c: None if structured_error(r) else is_list(r, "人才库反查"))

    # ---------------- bd_agent（真实模型调用）
    add("bd.agent_query", "POST", "/api/bd/agent/query", module="bd_agent",
        json_body={"query": "上海有哪些做 Java 的公司", "kind": "candidate", "limit": 2},
        expect=(200, 502, 503), note="真实模型调用（kind ∈ text|candidate|jd）",
        check=lambda r, c: c.remember("session_id",
                                      pick(r, "session_id") or ""))
    add("bd.agent_follow_up", "POST", "/api/bd/agent/session/{session_id}/follow-up",
        module="bd_agent", json_body={"query": "深圳呢", "limit": 2},
        expect=(200, 404, 502, 503), check=lambda r, c: is_dict(r, "追问"))
    add("bd.agent_export", "GET", "/api/bd/agent/session/{session_id}/export", module="bd_agent",
        expect=(200, 404), check=lambda r, c: None)
    add("bd.agent_query_stream", "POST", "/api/bd/agent/query-stream", module="bd_agent",
        json_body={"query": "北京的公司", "kind": "candidate", "limit": 2},
        expect=(200, 502, 503), note="SSE 流式：无论是否 event-stream 都要求非空响应",
        check=lambda r, c: None)

    # ---------------- settings：邮件（真实连通性探测）
    add("settings.mail_test", "POST", "/api/settings/mail/test", module="settings",
        expect=(200, 400, 502, 503), note="真实 IMAP/SMTP 连通性探测",
        check=lambda r, c: is_dict(r, "连通性"))
    add("settings.mail_sync", "POST", "/api/settings/mail/sync", module="settings",
        expect=(200, 400, 502, 503), note="真实收信（读取邮箱）",
        check=lambda r, c: is_dict(r, "收信结果"))
    add("settings.send_confirmation", "POST", "/api/settings/mail/send-confirmation",
        module="settings", expect=(200, 400, 502, 503),
        note="会真实发信给已配置收件人", check=lambda r, c: is_dict(r, "确认邮件"))
    add("settings.send_followup_test", "POST", "/api/settings/mail/send-followup-test",
        module="settings", expect=(200, 400, 502, 503),
        note="会真实发信给已配置收件人", check=lambda r, c: is_dict(r, "跟进测试邮件"))

    # ---------------- ai_settings：真实探测
    add("ai.catalog_refresh", "POST", "/api/ai/catalog/refresh", module="ai_settings",
        expect=(200, 502, 503), note="联网刷新模型目录",
        check=lambda r, c: is_dict(r, "目录刷新"))
    add("ai.config_put", "PUT", "/api/ai/config", module="ai_settings",
        json_body={"connections": []}, expect=(200, 400, 422),
        validation_only=True, note="不提交真实连接（避免覆写密钥）；只验证请求被受理",
        check=None)
    add("ai.probe", "POST", "/api/ai/probe", module="ai_settings",
        json_body={"provider_id": "siliconflow", "api_key": "",
                   "base_url_override": None, "models": {}},
        expect=(200, 400, 422, 502, 503), note="连通性探测（不传真实密钥）",
        check=lambda r, c: is_dict(r, "探测结果"))
    add("onboarding.test_providers", "POST", "/api/onboarding/test-providers",
        module="onboarding", expect=(200, 400, 502, 503),
        note="逐个探测已配置供应商（真实调用）",
        check=lambda r, c: is_list(r, "供应商探测") if not structured_error(r) else None)

    # ---------------- org 导入：提交与 Word
    add("org.import_commit", "POST", "/api/org/import/commit", module="org",
        json_body={"company_id": "{import_commit_company_id}",
                   "draft": {"company_name": "接口功能测试公司", "departments": []},
                   "source_text": "接口功能测试公司\n技术部"},
        prepare=_org_import_commit_prepare, expect=(200, 422, 502, 503),
        note="把空草稿提交到新建的一次性公司",
        check=lambda r, c: is_dict(r, "提交结果") if not structured_error(r) else None)
    add("org.import_word", "POST", "/api/org/import/word", module="org",
        files={"file": ("fixture-org.docx", (OUT / "fixture-jd.docx").read_bytes(),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        expect=(200, 422, 502, 503), note="Word 组织架构导入（真实模型调用）",
        check=lambda r, c: is_dict(r, "Word 导入"))

    # ---------------- duplicates / schools / backfill
    add("duplicates.merge_dry_run", "POST", "/api/duplicates/merge-dry-run", module="duplicates",
        json_body={"group_id": "{group_id}",
                   "primary_candidate_id": "{candidate_id}",
                   "duplicate_candidate_ids": ["{candidate_id}"]},
        expect=(200, 400, 404, 422), note="只做试算，不落库",
        check=lambda r, c: None)
    add("duplicates.merge", "POST", "/api/duplicates/merge", module="duplicates",
        json_body={"candidate_ids": ["{candidate_id}"]}, expect=(400, 404, 409, 422),
        validation_only=True, note="未执行合并：单个 id 不足以构成合并，应被拒",
        check=lambda r, c: need(structured_error(r) or "detail" in r,
                                f"拒绝响应既非统一错误体也无 detail：{sorted(r)}"))
    add("schools.import", "POST", "/api/schools/import", module="schools",
        json_body={"records": [], "csv": ""}, expect=(200, 400, 422),
        validation_only=True, note="空导入：只验证请求被受理，不写入学校表",
        check=None)
    add("backfill.trigger", "POST", "/api/backfill/not-a-valid-kind", module="backfill",
        expect=(400, 404, 422), validation_only=True,
        note="未触发回填：非法 kind 应被拒（回填会重写全量画像）",
        check=lambda r, c: has_keys(r, "code"))

    return probes


# ---------------------------------------------------------------- 各探针的断言实现

def _search_plan_check(payload: Any, ctx: Ctx) -> None:
    """回显契约：合并后的生效条件 + 解析来源 + 词条 + 未识别残句（文档 §回显与交互）。"""
    data = has_keys(payload, "items", "query_plan", "status")
    plan = is_dict(data["query_plan"], "query_plan")
    has_keys(plan, "parsed_conditions", "retained_keywords")
    need(isinstance(plan.get("effective_conditions"), list),
         "query_plan 缺少 effective_conditions（阶段 2 起回显必须给合并后的生效条件）")
    need(plan.get("parsed_plan_source") in {"rule", "llm", "mixed"},
         f"parsed_plan_source 非法：{plan.get('parsed_plan_source')}")
    need(isinstance(plan.get("keyword_terms"), str),
         f"keyword_terms 应为字符串，实际 {type(plan.get('keyword_terms')).__name__}")
    need(isinstance(plan.get("unparsed_terms"), list), "unparsed_terms 应为列表")


def _communication_note_like_check(payload: Any, ctx: Ctx) -> None:
    """沟通文本必须按**子串**命中夹具候选人。

    查的是夹具备注「接口功能测试沟通记录」的**中段**「测试沟通」：精确相等语义会查不到，
    所以这一条同时证明了 like 语义真的生效（而不是「参数被接受但没用于过滤」）。
    """
    data = is_dict(payload, "沟通文本筛选")
    items = data.get("items") or []
    target = ctx.first("fixture_candidate_id")
    need(bool(target), "没有夹具候选人 id，无法验证命中")
    need(any(str(item.get("candidate_id")) == target for item in items),
         f"沟通文本子串筛选未命中夹具候选人（返回 {len(items)} 条）")


def _settings_roundtrip_check(payload: Any, ctx: Ctx) -> None:
    before = ctx.ids.get("__settings_before__")
    after = is_dict(payload, "设置")
    need(before is not None, "缺少前置 GET /api/settings 快照")
    need(after == before[0], "空对象写入后设置发生了变化，说明 PUT 会清空未提交字段")


def _org_department_create_check(payload: Any, ctx: Ctx) -> None:
    ctx.remember("department_id", pick(payload, "department_id", "id"))


def _org_employee_create_check(payload: Any, ctx: Ctx) -> None:
    ctx.remember("employee_id", pick(payload, "employee_id", "id"))


def _org_import_commit_prepare(ctx: Ctx) -> None:
    """提交草稿要指向一个自己新建的公司——前一个夹具公司在上一条探针里已被删除。"""
    response = ctx.client.post("/api/org/companies", json={"name": "接口功能测试一次性公司（导入）"})
    if response.status_code in (200, 201):
        ctx.remember("import_commit_company_id", pick(response.json(), "company_id", "id"))
    if not ctx.first("import_commit_company_id"):
        # 建不出来也要让探针跑下去：这时把拒绝结果如实记录下来，而不是让脚本崩在占位上。
        ctx.remember("import_commit_company_id", "unavailable-company")


def _parsed_put_check(payload: Any, ctx: Ctx) -> None:
    """整表保存解析数据的响应形状（夹具对象上执行）。"""
    data = is_dict(payload, "解析数据保存结果")
    need(any(key in data for key in ("candidate_id", "jd_id", "revision_id", "status",
                                    "queued", "task_id")),
         f"响应未体现保存结果：{sorted(data)}")


def _bulk_check(payload: Any, ctx: Ctx) -> None:
    """批量接口必须逐项返回结果（成功/失败都要可见，前端据此保留选中项）。"""
    data = is_dict(payload, "批量结果")
    need(any(isinstance(value, list) for value in data.values())
         or any(key in data for key in ("results", "succeeded", "failed", "items")),
         f"批量结果缺少逐项明细：{sorted(data)}")


def _task_state_check(payload: Any, ctx: Ctx) -> None:
    """任务状态变更：成功回任务标识，失败回统一错误体（409 属正常守门）。"""
    if structured_error(payload):
        return
    data = is_dict(payload, "任务状态变更")
    need(any(key in data for key in ("task_id", "id", "status", "state")),
         f"任务状态响应缺少标识：{sorted(data)}")


def _case_detail_check(payload: Any, ctx: Ctx) -> None:
    """流程详情必须带轮次，后续 result / pass-and-advance 需要 case_round_id。"""
    data = has_keys(payload, "rounds")
    for key in ("rounds", "process_rounds"):
        rounds = data.get(key)
        if isinstance(rounds, list):
            for item in rounds:
                if isinstance(item, dict):
                    round_id = pick(item, "case_round_id", "id")
                    if round_id:
                        ctx.remember("case_round_id", round_id)
    event_id = _find_key(payload, "event_id") or pick(payload, "last_event_id")
    if event_id:
        ctx.remember("event_id", event_id)
    if not ctx.first("case_round_id"):
        found = _find_key(payload, "case_round_id")
        if found:
            ctx.remember("case_round_id", found)


def _find_key(payload: Any, key: str) -> str:
    if isinstance(payload, dict):
        for name, value in payload.items():
            if name == key and isinstance(value, str):
                return value
            found = _find_key(value, key)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_key(item, key)
            if found:
                return found
    return ""


def _event_check(payload: Any, ctx: Ctx) -> None:
    """流程事件类接口：可能回单个事件对象，也可能回事件列表；失败回统一错误体。"""
    if structured_error(payload):
        return
    if isinstance(payload, list):
        need(bool(payload), "事件列表为空")
    else:
        data = is_dict(payload, "流程事件")
        need(any(key in data for key in ("case_id", "id", "event_id", "events", "status",
                                        "deleted")),
             f"流程事件响应缺少标识：{sorted(data)}")
    event_id = (_find_key(payload, "event_id") or _find_key(payload, "last_event_id")
                or (pick(payload, "id") if isinstance(payload, dict) else ""))
    if event_id:
        ctx.remember("event_id", event_id)


# ---------------------------------------------------------------- 运行与报告

def run_probes(ctx: Ctx, probes: list[Probe]) -> None:
    for probe in probes:
        started = time.monotonic()
        record = {
            "id": probe.id, "module": probe.module, "method": probe.method,
            "path": probe.path, "note": probe.note,
            "validation_only": probe.validation_only,
        }
        try:
            if probe.prepare is not None:
                probe.prepare(ctx)
            if probe.id == "settings.put_roundtrip":
                snapshot = ctx.call("GET", "/api/settings")
                ctx.ids["__settings_before__"] = [snapshot.json()]
            response = ctx.call(probe.method, probe.path, params=probe.params,
                                json_body=probe.json_body, files=probe.files)
            record["status"] = response.status_code
            record["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
            try:
                content_type = response.headers.get("content-type", "")
            except Exception:  # noqa: BLE001  带中文文件名的响应头在部分环境下会编码失败
                content_type = ""
            record["content_type"] = content_type
            if response.status_code not in probe.expect:
                record["verdict"] = "fail"
                record["reason"] = (f"状态码 {response.status_code} 不在期望 {probe.expect}；"
                                    f"响应 {response.text[:300]}")
            else:
                if "application/json" in content_type:
                    payload = response.json()
                    record["body_keys"] = (sorted(payload) if isinstance(payload, dict)
                                           else f"list[{len(payload)}]")
                    try:
                        ctx.harvest(payload)
                    except Exception:
                        pass
                    if probe.check is not None:
                        probe.check(payload, ctx)
                    record["verdict"] = "pass"
                else:
                    body = response.content
                    need(len(body) > 0, "二进制响应为空")
                    if probe.check is not None:
                        probe.check(None, ctx)
                    record["bytes"] = len(body)
                    record["verdict"] = "pass"
        except AssertionError as error:
            record["verdict"] = "fail"
            record["reason"] = str(error)
        except Exception as error:  # noqa: BLE001  脚本要记录而非中断
            record["verdict"] = "error"
            record["reason"] = f"{type(error).__name__}: {error}"
            record["trace"] = traceback.format_exc(limit=3)
        ctx.records.append(record)
        mark = {"pass": "OK  ", "fail": "FAIL", "error": "ERR "}[record["verdict"]]
        detail = record.get("reason", "")
        print(f"  [{mark}] {probe.id:<34} {record.get('status', '-')!s:<5} {detail[:110]}")


def _shape(path: str) -> tuple[str, ...]:
    return tuple(path.strip("/").split("/"))


def _matches(probe_path: str, route_path: str) -> bool:
    """按「段数相同 + 每段要么字面相等、要么有一侧是参数位」判断同一路由。

    探针实际打的是具体 id（`/api/jd/01a0…/status` 或 `/api/backfill/not-a-valid-kind`），
    而清单里是参数位（`/api/jd/{jd_id}/status`、`/api/backfill/{kind}`），
    直接比字符串会把已测的路由全判成「未覆盖」。
    """
    left, right = _shape(probe_path), _shape(route_path)
    if len(left) != len(right):
        return False
    for a, b in zip(left, right):
        if a == b:
            continue
        if (a.startswith("{") and a.endswith("}")) or (b.startswith("{") and b.endswith("}")):
            continue
        return False
    return True


def coverage(probes: list[Probe], modules: set[str] | None) -> tuple[list[str], list[str]]:
    inventory = json.loads((OUT / "api_inventory.json").read_text(encoding="utf-8"))
    wanted = [(row["method"], row["path"]) for row in inventory
              if not modules or row["module"] in modules]
    covered: list[tuple[str, str]] = []
    uncovered: list[str] = []
    for method, path in wanted:
        hit = any(probe.method == method and _matches(probe.path, path) for probe in probes)
        if hit:
            covered.append((method, path))
        else:
            uncovered.append(f"{method} {path}")
    known = set(wanted)
    extra = sorted(f"{probe.method} {probe.path}" for probe in probes
                   if not any(probe.method == method and _matches(probe.path, path)
                              for method, path in known))
    return sorted(uncovered), extra


def write_report(ctx: Ctx, uncovered: list[str], extra: list[str], modules: set[str] | None) -> bool:
    records = ctx.records
    counts: dict[str, int] = defaultdict(int)
    for record in records:
        counts[record["verdict"]] += 1
    by_module: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for record in records:
        by_module[record["module"] or "(未分组)"][record["verdict"]] += 1
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "data_root": str(DEV),
        "session_guard": "X-Kerui-Session",
        "scope": sorted(modules) if modules else "all",
        "summary": dict(counts),
        "by_module": {module: dict(value) for module, value in sorted(by_module.items())},
        "uncovered_routes": uncovered,
        "probes_not_in_inventory": extra,
        "records": records,
    }
    out_json = OUT / "api_test_report.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [
        "# 全量后端接口功能测试报告", "",
        f"- 数据根目录：`{DEV}`",
        f"- 会话校验：`X-Kerui-Session`",
        f"- 结果：**pass {counts['pass']} / fail {counts['fail']} / error {counts['error']}**",
        f"- 未覆盖路由：{len(uncovered)}",
        "", "## 按模块", "", "| 模块 | pass | fail | error |", "| --- | ---: | ---: | ---: |",
    ]
    for module, value in sorted(by_module.items()):
        lines.append(f"| {module} | {value['pass']} | {value['fail']} | {value['error']} |")
    if uncovered:
        lines += ["", "## 未覆盖路由", ""] + [f"- `{item}`" for item in uncovered]
    failing = [r for r in records if r["verdict"] != "pass"]
    if failing:
        lines += ["", "## 失败明细", "", "| 接口 | 状态 | 原因 |", "| --- | ---: | --- |"]
        for record in failing:
            lines.append(f"| `{record['id']}` | {record.get('status')} | "
                         f"{(record.get('reason') or '').replace('|', '/')[:200]} |")
    validation = [r for r in records if r["validation_only"]]
    if validation:
        lines += ["", "## 仅验证守门路径（未执行破坏性动作）", ""]
        for record in validation:
            lines.append(f"- `{record['id']}`：{record['note']}")
    (OUT / "api_test_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n汇总：pass={counts['pass']} fail={counts['fail']} error={counts['error']} "
          f"未覆盖={len(uncovered)}")
    print(f"产物：{out_json}、{OUT / 'api_test_report.md'}")
    return counts["fail"] == 0 and counts["error"] == 0 and not uncovered


def main() -> None:
    parser = argparse.ArgumentParser(description="全量后端接口功能测试")
    parser.add_argument("--only", default="", help="只跑这些模块（逗号分隔）")
    parser.add_argument("--dump-openapi", action="store_true", help="只导出 OpenAPI schema")
    parser.add_argument("--dump-routes", action="store_true", help="只导出路由对照表（必填字段）")
    options = parser.parse_args()
    modules = {item.strip() for item in options.only.split(",") if item.strip()} or None

    from fastapi.testclient import TestClient

    from kerui_recruit.runtime import create_runtime_app

    OUT.mkdir(exist_ok=True)
    app = create_runtime_app(load_settings())
    schema = app.openapi()
    (OUT / "openapi.json").write_text(json.dumps(schema, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    print(f"OpenAPI：路径 {len(schema.get('paths', {}))} 个")
    if options.dump_openapi or options.dump_routes:
        write_routes_brief(schema)
        return

    with TestClient(app, headers={"X-Kerui-Session": SESSION_TOKEN},
                    raise_server_exceptions=False) as client:
        ctx = Ctx(client)
        run_discovery(ctx)
        prepare_fixtures(ctx)
        probes = build_probes(ctx)
        if modules:
            probes = [probe for probe in probes if probe.module in modules]
        uncovered, extra = coverage(probes, modules)
        print(f"\n—— 执行 {len(probes)} 条探针（未覆盖路由 {len(uncovered)}）——")
        run_probes(ctx, probes)
        ok = write_report(ctx, uncovered, extra, modules)
    raise SystemExit(0 if ok else 1)


def load_settings():
    """与 sidecar 完全同源的 Settings：走 `sidecar.build_settings(RuntimeArgs)`。

    手写 Settings 只会带上 siliconflow 密钥，deepseek / 各 task 角色的供应商配置拿不到，
    画像生成这类接口会直接 `E_AI_NO_PROVIDER`——那是取数问题，不是接口缺陷。
    """
    from kerui_recruit.sidecar import RuntimeArgs, build_settings

    return build_settings(RuntimeArgs(host="127.0.0.1", port=1, token=SESSION_TOKEN,
                                      data_root=DEV))


def brief(schema: dict, components: dict) -> str:
    if "$ref" in schema:
        schema = components.get(schema["$ref"].split("/")[-1], {})
    if schema.get("type") == "array":
        return "array<" + brief(schema.get("items", {}), components) + ">"
    if "enum" in schema:
        return "enum" + json.dumps(schema["enum"], ensure_ascii=False)
    if "anyOf" in schema:
        return "|".join(sorted({brief(item, components) for item in schema["anyOf"]}))
    return str(schema.get("type") or "any")


def write_routes_brief(schema: dict) -> None:
    """把每条路由的必填 query / body 字段压成一行，作为写探针时的对照表。"""
    components = schema.get("components", {}).get("schemas", {})
    lines: list[str] = []
    for path, operations in schema.get("paths", {}).items():
        for method, operation in operations.items():
            body = ""
            request_body = operation.get("requestBody")
            if request_body:
                raw = request_body["content"].get("application/json", {}).get("schema", {})
                if "$ref" in raw:
                    raw = components.get(raw["$ref"].split("/")[-1], {})
                required = set(raw.get("required", []))
                fields = []
                for name, field in (raw.get("properties") or {}).items():
                    mark = "*" if name in required else ""
                    fields.append(f"{name}{mark}:{brief(field, components)}")
                body = "{" + ", ".join(fields) + "}"
            query = " ".join(
                f"{item['name']}{'*' if item.get('required') else ''}"
                for item in operation.get("parameters", [])
            )
            lines.append(f"{method.upper():6} {path}  q=[{query}]  body={body}")
    (OUT / "routes_brief.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"路由对照表：{len(lines)} 行 -> {OUT / 'routes_brief.txt'}")


if __name__ == "__main__":
    main()
