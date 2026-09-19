"""真实 API 验收（临时脚本，不入库、不提交密钥）。

隔离数据目录 + 真实 DeepSeek/SiliconFlow/Tavily 配置，从 `1/` 选两份真实 PDF，
验证：上传 -> 解析 -> AI 画像 -> 索引 -> keyword/vector/hybrid 三模式搜索。
关闭 IMAP/SMTP 自动同步与每日报告，避免干扰真实邮箱。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

BACKEND_SRC = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(BACKEND_SRC))

from pydantic import SecretStr
from fastapi.testclient import TestClient

from kerui_recruit.core.settings import Settings
from kerui_recruit.runtime import create_runtime_app

HEADERS = {"X-Kerui-Session": "real-api-token"}


def load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _secret(name: str) -> SecretStr | None:
    value = os.environ.get(name)
    return SecretStr(value) if value else None


def wait_task(client: TestClient, task_id: str, timeout: float = 300.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        resp = client.get(f"/api/tasks/{task_id}", headers=HEADERS)
        if resp.status_code == 200:
            body = resp.json()
            last = body
            if body["status"] in ("SUCCESS", "FAILED", "DEAD_LETTER"):
                return body
        time.sleep(1.5)
    raise TimeoutError(f"task {task_id} 未在 {timeout}s 内完成，最后状态: {last}")


def search_until_hit(client: TestClient, query: str, candidate_id: str, timeout: float = 120.0) -> list:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.post("/api/search/candidates",
                           json={"query": query, "mode": "hybrid", "limit": 20}, headers=HEADERS)
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            if any(item["candidate_id"] == candidate_id for item in items):
                return items
        time.sleep(2.0)
    return []


def main() -> None:
    load_env(Path(__file__).resolve().parent.parent / ".env")

    data_root = Path(tempfile.mkdtemp(prefix="kerui-real-api-"))
    settings = Settings(
        data_root=data_root,
        session_token=SecretStr("real-api-token"),
        deepseek_api_key=_secret("DEEPSEEK_API_KEY"),
        siliconflow_api_key=_secret("SILICONFLOW_API_KEY"),
        tavily_api_key=_secret("TAVILY_API_KEY"),
        imap_host=os.environ.get("IMAP_HOST"),
        imap_account=os.environ.get("IMAP_ACCOUNT"),
        imap_auth_code=_secret("IMAP_AUTH_CODE"),
        smtp_host=os.environ.get("SMTP_HOST"),
        smtp_account=os.environ.get("SMTP_ACCOUNT"),
        smtp_auth_code=_secret("SMTP_AUTH_CODE"),
        mail_auto_sync=False,
        daily_followup_enabled=False,
    )

    print(f"[配置] 数据目录: {data_root}")
    print(f"[配置] LLM: {settings.llm_enabled} | Embedding/Reranker: {settings.search_providers_enabled} | BD 搜索: {settings.bd_search_enabled} | 邮件自动同步: {settings.mail_auto_sync_enabled}")

    app = create_runtime_app(settings)

    with TestClient(app) as client:
        # 从 `1/` 选两份含中文姓名的 PDF（文件名形如「...】姓名 N年.pdf」）
        pdf_dir = Path(__file__).resolve().parent.parent / "1"
        pdfs = sorted(pdf_dir.glob("*.pdf"))
        chosen = []
        for pdf in pdfs:
            name = pdf.stem
            if "】" in name:
                tail = name.split("】")[-1].strip()
                candidate_name = tail.split(" ")[0].strip()
                if candidate_name:
                    chosen.append((pdf, candidate_name))
            if len(chosen) == 2:
                break
        if len(chosen) < 2:
            chosen = [(pdf, "") for pdf in pdfs[:2]]
        print(f"[样本] 选用: {[p.name for p, _ in chosen]}")

        imported = []
        for pdf, _ in chosen:
            resp = client.post("/api/resumes/import",
                               files={"file": (pdf.name, pdf.read_bytes(), "application/pdf")},
                               headers=HEADERS)
            assert resp.status_code == 202, f"导入失败 {pdf.name}: {resp.status_code} {resp.text}"
            body = resp.json()
            task = wait_task(client, body["task_id"])
            print(f"[导入] {pdf.name}: task={task['status']}, candidate={body.get('candidate_id')}")
            if task["status"] != "SUCCESS":
                print(f"  -> 失败信息: {task.get('error_message') or task.get('error_code')}")
            imported.append((pdf, body.get("candidate_id"), task))

        succeeded = [(pdf, cid) for pdf, cid, task in imported if task["status"] == "SUCCESS"]
        print(f"[解析] 成功 {len(succeeded)}/{len(imported)}")

        # 对每个成功解析的候选人，用姓名命中并检查画像
        for pdf, cid in succeeded:
            _, query_name = next((p, n) for p, n in chosen if p == pdf)
            items = search_until_hit(client, query_name, cid) if query_name else []
            hit = next((i for i in items if i["candidate_id"] == cid), None)
            parsed = (hit or {}).get("parsed_data") or {}
            profile = parsed.get("ai_profile_summary")
            source = parsed.get("ai_profile_source")
            stale = parsed.get("ai_profile_stale")
            print(f"[画像] {pdf.name}: name={parsed.get('name')!r} summary={'有' if profile else '无'} source={source} stale={stale}")
            if profile:
                print(f"       摘要(前80字): {profile[:80]}")

        # 三模式搜索验证（用第一个成功候选人的姓名）
        if succeeded:
            first_pdf, first_cid = succeeded[0]
            _, qname = next((p, n) for p, n in chosen if p == first_pdf)
            for mode in ("keyword", "vector", "hybrid"):
                resp = client.post("/api/search/candidates",
                                   json={"query": qname, "mode": mode, "limit": 20}, headers=HEADERS)
                body = resp.json()
                hit = any(i["candidate_id"] == first_cid for i in body.get("items", []))
                print(f"[搜索] mode={mode}: status={body.get('status')} items={len(body.get('items', []))} 命中目标={'是' if hit else '否'} degraded={body.get('degraded_reasons')}")

    print("\n[完成] 真实 API 验收脚本结束。")


if __name__ == "__main__":
    main()
