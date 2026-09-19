import httpx, json
from pathlib import Path
import sys
sys.path.insert(0, "src")
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.db.models import MatchResult

BASE = "http://127.0.0.1:43127"
H = {"X-Kerui-Session": "0" * 64}

# 匹配一个 Java 后端 JD
rev_id = "01a08a1d-6840-761b-94f1-3f0c9465250b"  # Java 后端与大数据开发工程师
r = httpx.post(BASE + "/api/match/jd", headers=H,
               json={"revision_id": rev_id, "limit": 8, "mode": "hybrid"}, timeout=180)
d = r.json()
print("match status:", d.get("status"), "| run_id:", d.get("run_id"), "| degraded:", d.get("degraded_reasons"))
print("返回候选人（top 8）：")
for it in d.get("items", []):
    print(f"  {it.get('name')} | score={it.get('score')} | matched={it.get('matched_skills')} | missing={it.get('missing_skills')}")

# 查数据库 score_breakdown 里的 direction/industry
run_id = d.get("run_id")
if run_id:
    engine = create_engine_for(Path(r"C:\Users\Nl\Desktop\安装包+源码\candidate_pool\.dev-data\db\recruit.sqlite3"))
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        rows = session.scalars(select(MatchResult).where(MatchResult.run_id == run_id)).all()
        print(f"\n该 run 的 score_breakdown（direction/industry 分量）：")
        for row in rows:
            bd = row.score_breakdown or {}
            name = ""
            print(f"  candidate={row.candidate_id} | direction={bd.get('direction')} | industry={bd.get('industry')} | total={row.total_score} | keys={list(bd.keys())}")
