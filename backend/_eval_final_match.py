import sys, httpx
from pathlib import Path
sys.path.insert(0, "src")
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.db.models import Jd, JdRevision, MatchResult

BASE = "http://127.0.0.1:43127"
H = {"X-Kerui-Session": "0" * 64}

e = create_engine_for(Path(r"C:\Users\Nl\Desktop\安装包+源码\candidate_pool\.dev-data\db\recruit.sqlite3"))
F = sessionmaker(e, expire_on_commit=False)

# 选两个带 industry + direction 的 JD
with F() as s:
    rows = s.execute(
        select(Jd, JdRevision).join(JdRevision, JdRevision.jd_id == Jd.id)
        .where(Jd.status == "OPEN", Jd.deleted_at.is_(None),
               JdRevision.is_current.is_(True), JdRevision.status == "READY")
    ).all()
    targets = [(j, r) for j, r in rows if (r.parsed_data or {}).get("industry")][:2]

for jd, rev in targets:
    pd = rev.parsed_data or {}
    print(f"\n=== {jd.title} | direction={pd.get('direction')} | industry={pd.get('industry')} ===")
    r = httpx.post(BASE + "/api/match/jd", headers=H,
                   json={"revision_id": rev.id, "limit": 5, "mode": "hybrid"}, timeout=180)
    d = r.json()
    run_id = d.get("run_id")
    print("  top5:")
    for it in d.get("items", []):
        print(f"    {it.get('name')} | score={it.get('score')}")
    if run_id:
        with F() as s:
            mrs = s.scalars(select(MatchResult).where(MatchResult.run_id == run_id)).all()
            for mr in mrs[:5]:
                bd = mr.score_breakdown or {}
                print(f"    breakdown {mr.candidate_id[:8]} dir={bd.get('direction')} industry={bd.get('industry')} keys={list(bd.keys())}")
