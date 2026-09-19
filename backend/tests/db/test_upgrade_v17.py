import json

from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Blob, Candidate, Jd, JdRevision, ResumeDocument, ResumeRevision
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.db.upgrades import _upgrade_v16_to_v17


def test_v16_to_v17_strips_direction_keys(tmp_path) -> None:
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session, session.begin():
        candidate = Candidate(display_name="张三")
        blob = Blob(content_sha256="a" * 64, suffix=".pdf", size_bytes=1, storage_path="x")
        doc = ResumeDocument(candidate=candidate)
        revision = ResumeRevision(document=doc, blob=blob, content_sha256="a" * 64,
                                  original_filename="a.pdf", status="READY", is_current=True,
                                  parsed_data={"name": "张三", "tech_direction": ["Java"], "direction_profile": {}})
        jd = Jd(company="C", title="后端", status="OPEN")
        jd_revision = JdRevision(jd=jd, source_text="x", status="READY", is_current=True,
                                 parsed_data={"title": "后端", "business_direction": ["金融"], "direction_boost": 0.5})
        session.add_all([candidate, blob, doc, revision, jd, jd_revision])
        session.flush()
        rid, jid = revision.id, jd_revision.id

    with engine.connect() as conn:
        _upgrade_v16_to_v17(conn)
        conn.commit()

    with factory() as session:
        resume = session.get(ResumeRevision, rid).parsed_data
        jd_data = session.get(JdRevision, jid).parsed_data
    assert "tech_direction" not in resume
    assert "direction_profile" not in resume
    assert resume["name"] == "张三"
    assert "business_direction" not in jd_data
    assert "direction_boost" not in jd_data
    assert jd_data["title"] == "后端"
