import json
import sys
from pathlib import Path

sys.path.insert(0, "src")
from sqlalchemy import select, func
from sqlalchemy.orm import sessionmaker
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.db import models

DB = Path(r"C:\Users\Nl\Desktop\安装包+源码\candidate_pool\.dev-data\db\recruit.sqlite3")
engine = create_engine_for(DB)
Factory = sessionmaker(engine, expire_on_commit=False)

print("=" * 70)
print("真实数据测评：关键落库数据检查")
print("=" * 70)

with Factory() as s:
    # 1. JD revision 表：parsed_data 里 direction / industry 是否落库
    print("\n[1] JD parsed_data.direction / industry 落库情况")
    revs = s.scalars(select(models.JdRevision)).all()
    total = len(revs)
    has_dir = sum(1 for r in revs if (r.parsed_data or {}).get("direction"))
    has_ind = sum(1 for r in revs if (r.parsed_data or {}).get("industry"))
    print(f"  JD revision 总数: {total}")
    print(f"  含 direction: {has_dir} 个")
    print(f"  含 industry:  {has_ind} 个")

    # 状态分布
    from collections import Counter
    status = Counter(r.status for r in revs)
    print(f"  状态分布: {dict(status)}")

    # 抽样看几个 direction 值
    dirs = Counter((r.parsed_data or {}).get("direction") for r in revs)
    print(f"  direction 取值分布: {dict(dirs)}")

    # 2. 候选人 direction / industry 分布（当前 ResumeRevision.parsed_data）
    print("\n[2] 候选人（当前简历 revision）direction / industry 落库情况")
    cands = s.scalars(select(models.Candidate)).all()
    ctotal = len(cands)
    cdirs = Counter()
    cinds = Counter()
    c_has_dir = 0
    c_has_ind = 0
    for c in cands:
        rev = s.scalars(
            select(models.ResumeRevision)
            .where(models.ResumeRevision.document_id.in_(
                [d.id for d in c.documents]
            ))
            .where(models.ResumeRevision.is_current.is_(True))
        ).first()
        if rev is None:
            continue
        pd = rev.parsed_data or {}
        d = pd.get("direction")
        ind = pd.get("industry")
        if d:
            c_has_dir += 1
            cdirs[d] += 1
        if ind:
            c_has_ind += 1
            cinds[ind] += 1
    print(f"  候选人总数: {ctotal}")
    print(f"  含 direction: {c_has_dir}")
    print(f"  direction 取值分布: {dict(cdirs)}")
    print(f"  含 industry: {c_has_ind}")
    print(f"  industry 取值(前10): {dict(list(cinds.most_common(10)))}")
