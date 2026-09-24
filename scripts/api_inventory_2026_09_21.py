"""后端 API 接口盘点：把真实注册进 app 的路由逐条导出成清单。

做法：按 `main.py` 的注册顺序 import 各 router，收集 `router.routes`，
因此清单与运行时注册的路由**同源**，不会因为手写文档而与实现漂移。

产物：
- `.tmp-api/api_inventory.json`：机器可读（method / path / name / summary / tags / 依赖项）
- `.tmp-api/api_inventory.md`：人读表格，按模块分组

用法：

    py -3.12 scripts/api_inventory_2026_09_21.py

只读、不联网、不需要服务实例。
"""
from __future__ import annotations

import importlib
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
OUT = ROOT / ".tmp-api"

# 与 main.py:create_app 的 include_router 顺序一致（有 services 时注册的全部 router）。
ROUTER_MODULES = (
    "kerui_recruit.api.resumes",
    "kerui_recruit.api.tasks",
    "kerui_recruit.api.search",
    "kerui_recruit.api.indexes",
    "kerui_recruit.api.jd",
    "kerui_recruit.api.match",
    "kerui_recruit.api.backup",
    "kerui_recruit.api.correction",
    "kerui_recruit.api.diagnostics",
    "kerui_recruit.api.mapping",
    "kerui_recruit.api.reminders",
    "kerui_recruit.api.bd_search",
    "kerui_recruit.api.bd_agent",
    "kerui_recruit.api.cases",
    "kerui_recruit.api.dashboard",
    "kerui_recruit.api.daily_followup",
    "kerui_recruit.api.settings",
    "kerui_recruit.api.ai_settings",
    "kerui_recruit.api.migration",
    "kerui_recruit.api.onboarding",
    "kerui_recruit.api.org",
    "kerui_recruit.api.duplicates",
    "kerui_recruit.api.schools",
    "kerui_recruit.api.backfill",
    "kerui_recruit.api.soft_delete",
)


def dependencies(endpoint) -> list[str]:
    """该接口声明的依赖名（Request / Services / 各类 Depends），用于说明它需不需要真实服务。"""
    names: list[str] = []
    for parameter in inspect.signature(endpoint).parameters.values():
        annotation = parameter.annotation
        name = getattr(annotation, "__name__", None) or str(annotation)
        names.append(f"{parameter.name}:{name}")
    return names


def collect() -> list[dict]:
    rows: list[dict] = []
    for module_name in ROUTER_MODULES:
        module = importlib.import_module(module_name)
        router = module.router
        for route in router.routes:
            methods = sorted(getattr(route, "methods", ()) or ())
            for method in methods:
                if method in {"HEAD", "OPTIONS"}:
                    continue
                endpoint = getattr(route, "endpoint", None)
                summary = (getattr(route, "summary", None)
                           or (inspect.getdoc(endpoint) or "").split("\n")[0])
                rows.append({
                    "module": module_name.rsplit(".", 1)[-1],
                    "method": method,
                    "path": route.path,
                    "name": getattr(route, "name", ""),
                    "status_code": getattr(route, "status_code", None),
                    "summary": summary,
                    "tags": list(getattr(route, "tags", ()) or ()),
                    "params": sorted(dependencies(endpoint)) if endpoint else [],
                })
    rows.sort(key=lambda item: (item["module"], item["path"], item["method"]))
    return rows


def main() -> None:
    rows = collect()
    OUT.mkdir(exist_ok=True)
    (OUT / "api_inventory.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = ["# 后端 API 接口清单（自动生成，来源：router 注册表）", ""]
    modules = {}
    for row in rows:
        modules.setdefault(row["module"], []).append(row)
    for module, items in modules.items():
        lines.append(f"## {module}（{len(items)}）")
        lines.append("")
        lines.append("| 方法 | 路径 | 函数 | 说明 |")
        lines.append("| --- | --- | --- | --- |")
        for item in items:
            lines.append(f"| {item['method']} | `{item['path']}` | `{item['name']}` | "
                         f"{item['summary']} |")
        lines.append("")
    (OUT / "api_inventory.md").write_text("\n".join(lines), encoding="utf-8")

    methods: dict[str, int] = {}
    for row in rows:
        methods[row["method"]] = methods.get(row["method"], 0) + 1
    print(f"接口总数={len(rows)} 模块={len(modules)} 方法分布={methods}")
    for module, items in modules.items():
        print(f"  {module:<14} {len(items):>3}")
    print(f"产物：{OUT / 'api_inventory.json'}、{OUT / 'api_inventory.md'}")


if __name__ == "__main__":
    main()
