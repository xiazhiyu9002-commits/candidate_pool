import sys
from pathlib import Path
from collections import Counter

sys.path.insert(0, "src")
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.db import models

DB = Path(r"C:\Users\Nl\Desktop\安装包+源码\candidate_pool\.dev-data\db\recruit.sqlite3")
engine = create_engine_for(DB)
Factory = sessionmaker(engine, expire_on_commit=False)

with Factory() as s:
    # OPEN 且当前 revision 的 JD
    jd_rows = s.execute(
        select(models.Jd, models.JdRevision)
        .join(models.JdRevision, models.JdRevision.jd_id == models.Jd.id)
        .where(models.Jd.status == "OPEN", models.Jd.deleted_at.is_(None),
               models.JdRevision.is_current.is_(True), models.JdRevision.status == "READY")
    ).all()
    print(f"OPEN 且 READY 的 JD 数量: {len(jd_rows)}")
    print("\n各 JD 的 title / direction / industry:")
    for jd, rev in jd_rows:
        pd = rev.parsed_data or {}
        print(f"  - {jd.title[:30]!r:35} direction={pd.get('direction')!r:10} industry={pd.get('industry')!r}")

    # 候选人 industry 取值集合（去重）
    cands = s.scalars(select(models.Candidate)).all()
    cind = set()
    for c in cands:
        rev = s.scalars(
            select(models.ResumeRevision)
            .where(models.ResumeRevision.document_id.in_([d.id for d in c.documents]))
            .where(models.ResumeRevision.is_current.is_(True))
        ).first()
        if rev is None:
            continue
        pd = rev.parsed_data or {}
        for k in ("industry", "current_industry", "longest_industry"):
            v = pd.get(k)
            if v and str(v).strip():
                cind.add(str(v).strip().casefold())

    # 行业精确匹配命中率
    print("\n=== 行业匹配（当前 _industry_match 精确字符串匹配）命中率 ===")
    hit = 0
    tot = 0
    for jd, rev in jd_rows:
        jind = (rev.parsed_data or {}).get("industry")
        if not jind:
            continue
        tot += 1
        if str(jind).strip().casefold() in cind:
            hit += 1
        else:
            print(f"   未命中: JD '{jind}' 不在候选人行业集合中")
    print(f"  JD 有行业 {tot} 个，其中能被候选人行业集合精确命中 {hit} 个")
    print(f"  候选人行业取值样例: {sorted(cind)[:20]}")
