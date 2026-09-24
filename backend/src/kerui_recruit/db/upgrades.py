from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Connection


@dataclass(frozen=True, slots=True)
class Upgrade:
    from_version: int
    to_version: int
    apply: Callable[[Connection], None]


def _upgrade_v1_to_v2(connection: Connection) -> None:
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_task_status_updated ON task (status, updated_at)"
    )


def _upgrade_v2_to_v3(connection: Connection) -> None:
    existing = {
        row[1]
        for row in connection.exec_driver_sql("PRAGMA table_info(bd_lead)").fetchall()
    }
    if "confidence" not in existing:
        connection.exec_driver_sql("ALTER TABLE bd_lead ADD COLUMN confidence FLOAT")
    if "is_hiring" not in existing:
        connection.exec_driver_sql("ALTER TABLE bd_lead ADD COLUMN is_hiring BOOLEAN")
    if "session_id" not in existing:
        connection.exec_driver_sql("ALTER TABLE bd_lead ADD COLUMN session_id VARCHAR(36)")
    if "synthesized_json" not in existing:
        connection.exec_driver_sql("ALTER TABLE bd_lead ADD COLUMN synthesized_json JSON")
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_bd_lead_session_id ON bd_lead (session_id)"
    )


def _upgrade_v3_to_v4(connection: Connection) -> None:
    """引入可变面试轮次：stage_event 增加 round_no/round_name/result，并回填存量数据。"""
    existing = {
        row[1]
        for row in connection.exec_driver_sql("PRAGMA table_info(stage_event)").fetchall()
    }
    if "round_no" not in existing:
        connection.exec_driver_sql("ALTER TABLE stage_event ADD COLUMN round_no INTEGER")
    if "round_name" not in existing:
        connection.exec_driver_sql("ALTER TABLE stage_event ADD COLUMN round_name VARCHAR(64)")
    if "result" not in existing:
        connection.exec_driver_sql("ALTER TABLE stage_event ADD COLUMN result VARCHAR(16)")

    # 存量「初试/复试/终试」映射为推进次数，Offer/入职/拒绝映射为结果。
    connection.exec_driver_sql(
        "UPDATE stage_event SET round_no=0, round_name='简历筛选', result='推进' WHERE stage='已推荐'"
    )
    connection.exec_driver_sql(
        "UPDATE stage_event SET round_no=1, round_name='初试', result='推进' WHERE stage='初试'"
    )
    connection.exec_driver_sql(
        "UPDATE stage_event SET round_no=2, round_name='复试', result='推进' WHERE stage='复试'"
    )
    connection.exec_driver_sql(
        "UPDATE stage_event SET round_no=3, round_name='终试', result='推进' WHERE stage='终试'"
    )
    connection.exec_driver_sql(
        "UPDATE stage_event SET result='offer' WHERE stage='Offer'"
    )
    connection.exec_driver_sql(
        "UPDATE stage_event SET result='入职' WHERE stage='入职'"
    )
    connection.exec_driver_sql(
        "UPDATE stage_event SET result='淘汰' WHERE stage IN ('客户拒绝','岗位关闭')"
    )
    connection.exec_driver_sql(
        "UPDATE stage_event SET result='拒接' WHERE stage='候选人拒绝'"
    )

    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_stage_event_round_no ON stage_event (round_no)"
    )


def _upgrade_v4_to_v5(connection: Connection) -> None:
    """简历版本新增结构化失败原因字段，用于区分提取/OCR/结构化等失败。"""
    existing = {
        row[1]
        for row in connection.exec_driver_sql("PRAGMA table_info(resume_revision)").fetchall()
    }
    if "error_code" not in existing:
        connection.exec_driver_sql(
            "ALTER TABLE resume_revision ADD COLUMN error_code VARCHAR(80)"
        )
    if "error_message" not in existing:
        connection.exec_driver_sql(
            "ALTER TABLE resume_revision ADD COLUMN error_message TEXT"
        )


def _add_column(connection: Connection, table: str, column: str, ddl: str) -> None:
    existing = {
        row[1]
        for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in existing:
        connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _upgrade_v5_to_v6(connection: Connection) -> None:
    """可变面试流程：新增轮次实例(case_round)与事件(case_event)，保守迁移旧 stage_event。

    旧「初试/复试/终试」只迁移为「进入面试轮」，不伪造通过/未通过结果；
    Offer 迁移为「已发放」事实，接受/拒绝状态未知。
    """
    _add_column(connection, "hiring_process", "version", "INTEGER NOT NULL DEFAULT 1")
    _add_column(connection, "hiring_process", "deleted_at", "DATETIME")
    _add_column(connection, "process_round", "round_type", "VARCHAR(32)")
    _add_column(connection, "candidate_job_case", "template_id", "VARCHAR(36)")

    # 面试阶段：先建 case_round 再建进入事件（复用 stage_event.id 作为稳定 round_id）。
    connection.exec_driver_sql(
        "INSERT OR IGNORE INTO case_round "
        "(id, case_id, round_no, round_name, round_type, sort_order, source, skipped, created_at, updated_at) "
        "SELECT id, case_id, COALESCE(round_no, 0), COALESCE(round_name, stage), NULL, "
        "COALESCE(round_no, 0), 'legacy', 0, created_at, updated_at "
        "FROM stage_event WHERE stage IN ('初试','复试','终试')"
    )
    connection.exec_driver_sql(
        "INSERT OR IGNORE INTO case_event "
        "(id, case_id, event_type, case_round_id, occurred_at, recorded_at, result, note, status, created_at, updated_at) "
        "SELECT id, case_id, 'INTERVIEW_ENTERED', id, created_at, created_at, NULL, note, 'active', created_at, updated_at "
        "FROM stage_event WHERE stage IN ('初试','复试','终试')"
    )

    connection.exec_driver_sql(
        "INSERT OR IGNORE INTO case_event "
        "(id, case_id, event_type, case_round_id, occurred_at, recorded_at, result, note, status, created_at, updated_at) "
        "SELECT id, case_id, 'RECOMMENDED', NULL, created_at, created_at, NULL, note, 'active', created_at, updated_at "
        "FROM stage_event WHERE stage='已推荐'"
    )
    connection.exec_driver_sql(
        "INSERT OR IGNORE INTO case_event "
        "(id, case_id, event_type, case_round_id, occurred_at, recorded_at, result, note, status, created_at, updated_at) "
        "SELECT id, case_id, 'OFFER', NULL, created_at, created_at, '已发放', note, 'active', created_at, updated_at "
        "FROM stage_event WHERE stage='Offer'"
    )
    connection.exec_driver_sql(
        "INSERT OR IGNORE INTO case_event "
        "(id, case_id, event_type, case_round_id, occurred_at, recorded_at, result, note, status, created_at, updated_at) "
        "SELECT id, case_id, 'ONBOARDED', NULL, created_at, created_at, NULL, note, 'active', created_at, updated_at "
        "FROM stage_event WHERE stage='入职'"
    )
    connection.exec_driver_sql(
        "INSERT OR IGNORE INTO case_event "
        "(id, case_id, event_type, case_round_id, occurred_at, recorded_at, result, note, status, created_at, updated_at) "
        "SELECT id, case_id, 'EXIT', NULL, created_at, created_at, NULL, note, 'active', created_at, updated_at "
        "FROM stage_event WHERE stage IN ('客户拒绝','候选人拒绝','岗位关闭')"
    )


def _upgrade_v6_to_v7(connection: Connection) -> None:
    _add_column(connection, "candidate", "workflow_previous_status", "VARCHAR(32)")
    _add_column(connection, "candidate_contact", "manual_fields", "JSON")
    for field in ("manual_overrides", "extraction_diagnostics", "review_data"):
        _add_column(connection, "resume_revision", field, "JSON")
    _add_column(connection, "candidate_job_case", "template_version", "INTEGER")
    _add_column(connection, "candidate_job_case", "template_snapshot", "JSON")
    _add_column(connection, "case_round", "definition_key", "VARCHAR(200)")
    _add_column(connection, "reminder", "case_id", "VARCHAR(36) REFERENCES candidate_job_case(id) ON DELETE CASCADE")
    _add_column(connection, "reminder", "paused_by_workflow", "BOOLEAN NOT NULL DEFAULT 0")
    # The old desktop sent unzoned datetime-local values. Preserve its displayed
    # wall clock; new rows are explicitly normalized to UTC by ReminderService.
    _add_column(connection, "reminder", "time_basis", "VARCHAR(24) NOT NULL DEFAULT 'LEGACY_SHANGHAI'")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_reminder_case_id ON reminder (case_id)")


def _upgrade_v7_to_v8(connection: Connection) -> None:
    """BD 线索增加岗位介绍字段：开放时间、薪资范围、职级、要求。"""
    _add_column(connection, "bd_lead", "posted_time", "VARCHAR(100)")
    _add_column(connection, "bd_lead", "salary_range", "VARCHAR(100)")
    _add_column(connection, "bd_lead", "level", "VARCHAR(100)")
    _add_column(connection, "bd_lead", "requirements", "JSON")


def _upgrade_v8_to_v9(connection: Connection) -> None:
    """移除匹配结果的「短名单」「排除」标记：存量统一回退为「未处理」。"""
    connection.exec_driver_sql(
        "UPDATE match_result SET status='未处理' WHERE status IN ('短名单','排除')"
    )


def _upgrade_v9_to_v10(connection: Connection) -> None:
    """mapping 人员绑定人才库：employee 增加候选人与加密电话。"""
    _add_column(connection, "employee", "candidate_id", "VARCHAR(36) REFERENCES candidate(id) ON DELETE SET NULL")
    _add_column(connection, "employee", "phone_encrypted", "TEXT")


def _upgrade_v10_to_v11(connection: Connection) -> None:
    """候选人联系方式增加不可逆规范化指纹，用于本地身份匹配。"""
    _add_column(connection, "candidate_contact", "phone_fingerprint", "VARCHAR(64)")
    _add_column(connection, "candidate_contact", "email_fingerprint", "VARCHAR(255)")
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_candidate_contact_phone_fingerprint "
        "ON candidate_contact (phone_fingerprint)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_candidate_contact_email_fingerprint "
        "ON candidate_contact (email_fingerprint)"
    )


def _upgrade_v11_to_v12(connection: Connection) -> None:
    """公司保存组织导入的原始文本，供平台预览（不参与导出）。"""
    _add_column(connection, "company", "source_text", "TEXT")


def _upgrade_v12_to_v13(connection: Connection) -> None:
    """JD 版本新增方向相关 JSON 列；索引同步新增 requested_mode（FULL/METADATA）。"""
    _add_column(connection, "jd_revision", "review_data", "JSON")
    _add_column(connection, "jd_revision", "manual_overrides", "JSON")
    _add_column(connection, "index_sync", "requested_mode", "VARCHAR(16) NOT NULL DEFAULT 'FULL'")


def _upgrade_v13_to_v14(connection: Connection) -> None:
    """邮件游标记录 IMAP UIDVALIDITY，用于检测 QQ 等邮箱删除邮件后的 UID 重排。"""
    _add_column(connection, "mail_cursor", "uidvalidity", "INTEGER")


def _upgrade_v14_to_v15(connection: Connection) -> None:
    """候选人物理删除与流程快照：candidate_job_case.candidate_id 改为可空并 SET NULL，
    新增候选人快照列。SQLite 需重建表以变更外键与可空性。"""
    connection.exec_driver_sql(
        """
        CREATE TABLE candidate_job_case_new (
            id VARCHAR(36) NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            candidate_id VARCHAR(36),
            jd_id VARCHAR(36) NOT NULL,
            stage VARCHAR(24) NOT NULL,
            template_id VARCHAR(36),
            template_version INTEGER,
            template_snapshot JSON,
            note TEXT,
            deleted_at DATETIME,
            candidate_name_snapshot VARCHAR(200),
            candidate_phone_snapshot_encrypted TEXT,
            candidate_email_snapshot_encrypted TEXT,
            candidate_profile_snapshot JSON,
            candidate_deleted_at DATETIME,
            PRIMARY KEY (id),
            CONSTRAINT ck_case_stage CHECK (stage IN ('待评估','待联系','已联系','有意向','已推荐','初试','复试','终试','Offer','入职','客户拒绝','候选人拒绝','暂缓','岗位关闭')),
            FOREIGN KEY(candidate_id) REFERENCES candidate (id) ON DELETE SET NULL,
            FOREIGN KEY(jd_id) REFERENCES jd (id) ON DELETE CASCADE,
            FOREIGN KEY(template_id) REFERENCES hiring_process (id) ON DELETE SET NULL
        )
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO candidate_job_case_new
            (id, created_at, updated_at, candidate_id, jd_id, stage, template_id,
             template_version, template_snapshot, note, deleted_at)
        SELECT id, created_at, updated_at, candidate_id, jd_id, stage, template_id,
               template_version, template_snapshot, note, deleted_at
        FROM candidate_job_case
        """
    )
    connection.exec_driver_sql("DROP TABLE candidate_job_case")
    connection.exec_driver_sql("ALTER TABLE candidate_job_case_new RENAME TO candidate_job_case")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_case_candidate ON candidate_job_case (candidate_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_case_jd ON candidate_job_case (jd_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_case_stage ON candidate_job_case (stage)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_candidate_job_case_template_id ON candidate_job_case (template_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_candidate_job_case_deleted_at ON candidate_job_case (deleted_at)")


def _upgrade_v15_to_v16(connection: Connection) -> None:
    """匹配记录保存本次使用的搜索模式（keyword/vector/hybrid），便于追踪。"""
    _add_column(connection, "match_run", "mode", "VARCHAR(16)")


_DIRECTION_KEYS = ("tech_direction", "business_direction", "direction_profile",
                   "direction_boost", "direction_score")


def _upgrade_v16_to_v17(connection: Connection) -> None:
    """清除存量简历/JD JSON 中已废弃的方向字段（保留组织架构 Department.business_direction）。"""
    for table, column in (("resume_revision", "parsed_data"), ("resume_revision", "review_data"),
                          ("resume_revision", "manual_overrides"),
                          ("jd_revision", "parsed_data"), ("jd_revision", "review_data"),
                          ("jd_revision", "manual_overrides")):
        rows = connection.exec_driver_sql(
            f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL"
        ).fetchall()
        for row_id, raw in rows:
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            changed = False
            for key in _DIRECTION_KEYS:
                if key in data:
                    data.pop(key, None)
                    changed = True
            if changed:
                connection.exec_driver_sql(
                    f"UPDATE {table} SET {column} = ? WHERE id = ?",
                    (json.dumps(data, ensure_ascii=False), row_id),
                )


def _upgrade_v17_to_v18(connection: Connection) -> None:
    """岗位物理删除与流程快照：candidate_job_case.jd_id 改为可空并 SET NULL，
    新增岗位快照列。SQLite 需重建表以变更外键与可空性。

    与 v14→v15（候选人侧）对称：此前岗位删除依赖 ``ON DELETE CASCADE`` 连流程一起删，
    现在改为「先写岗位快照、再解除关联」，流程、轮次与面试记录全部保留。
    """
    connection.exec_driver_sql(
        """
        CREATE TABLE candidate_job_case_new (
            id VARCHAR(36) NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            candidate_id VARCHAR(36),
            jd_id VARCHAR(36),
            stage VARCHAR(24) NOT NULL,
            template_id VARCHAR(36),
            template_version INTEGER,
            template_snapshot JSON,
            note TEXT,
            deleted_at DATETIME,
            candidate_name_snapshot VARCHAR(200),
            candidate_phone_snapshot_encrypted TEXT,
            candidate_email_snapshot_encrypted TEXT,
            candidate_profile_snapshot JSON,
            candidate_deleted_at DATETIME,
            jd_title_snapshot VARCHAR(200),
            jd_company_snapshot VARCHAR(200),
            jd_profile_snapshot JSON,
            jd_deleted_at DATETIME,
            PRIMARY KEY (id),
            CONSTRAINT ck_case_stage CHECK (stage IN ('待评估','待联系','已联系','有意向','已推荐','初试','复试','终试','Offer','入职','客户拒绝','候选人拒绝','暂缓','岗位关闭')),
            FOREIGN KEY(candidate_id) REFERENCES candidate (id) ON DELETE SET NULL,
            FOREIGN KEY(jd_id) REFERENCES jd (id) ON DELETE SET NULL,
            FOREIGN KEY(template_id) REFERENCES hiring_process (id) ON DELETE SET NULL
        )
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO candidate_job_case_new
            (id, created_at, updated_at, candidate_id, jd_id, stage, template_id,
             template_version, template_snapshot, note, deleted_at,
             candidate_name_snapshot, candidate_phone_snapshot_encrypted,
             candidate_email_snapshot_encrypted, candidate_profile_snapshot,
             candidate_deleted_at)
        SELECT id, created_at, updated_at, candidate_id, jd_id, stage, template_id,
               template_version, template_snapshot, note, deleted_at,
               candidate_name_snapshot, candidate_phone_snapshot_encrypted,
               candidate_email_snapshot_encrypted, candidate_profile_snapshot,
               candidate_deleted_at
        FROM candidate_job_case
        """
    )
    connection.exec_driver_sql("DROP TABLE candidate_job_case")
    connection.exec_driver_sql("ALTER TABLE candidate_job_case_new RENAME TO candidate_job_case")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_case_candidate ON candidate_job_case (candidate_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_case_jd ON candidate_job_case (jd_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_case_stage ON candidate_job_case (stage)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_candidate_job_case_template_id ON candidate_job_case (template_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_candidate_job_case_deleted_at ON candidate_job_case (deleted_at)")


def _upgrade_v18_to_v19(connection: Connection) -> None:
    """搜索复核结论保存判据依据（来源、一致性兜底降级等），仅用于排查，不进界面。"""
    _add_column(connection, "search_review", "basis", "JSON")


def _upgrade_v19_to_v20(connection: Connection) -> None:
    """候选人新增「沟通记录」：单条自由文本，用于存偏软性信息，不进画像与索引。"""
    _add_column(connection, "candidate", "communication_note", "TEXT")


def _upgrade_v20_to_v21(connection: Connection) -> None:
    """新增表 ``work_years_regen_queue``（工作年限滚动带来的画像重生队列）。

    纯新增表、无存量数据迁移：``Base.metadata.create_all`` 已在升级循环前建出该表
    及其索引，因此这里不需要任何 DDL。保留本步骤是为了满足「每个版本都必须有
    升级步骤」的约束（见 ``migrate._apply_schema_version`` 与
    ``tests/db/test_migrate.py::test_schema_version_matches_last_upgrade``）。
    """
    _ = connection


def _upgrade_v21_to_v22(connection: Connection) -> None:
    """删除 ``work_years_regen_queue``：画像不再整段重生。

    v21 引入了「年限取整变化 → 调用模型整段重生画像」的待办队列；后来改为**只就地替换
    画像里的年限/年龄两个数字**（见 ``resumes/profile_text``），不再需要重生，因此该表
    与整套队列机制一并删除。已建过该表的库在这里物理清理，避免留下无人使用的空表。
    """
    connection.exec_driver_sql("DROP TABLE IF EXISTS work_years_regen_queue")


DEFAULT_UPGRADES = (
    Upgrade(1, 2, _upgrade_v1_to_v2),
    Upgrade(2, 3, _upgrade_v2_to_v3),
    Upgrade(3, 4, _upgrade_v3_to_v4),
    Upgrade(4, 5, _upgrade_v4_to_v5),
    Upgrade(5, 6, _upgrade_v5_to_v6),
    Upgrade(6, 7, _upgrade_v6_to_v7),
    Upgrade(7, 8, _upgrade_v7_to_v8),
    Upgrade(8, 9, _upgrade_v8_to_v9),
    Upgrade(9, 10, _upgrade_v9_to_v10),
    Upgrade(10, 11, _upgrade_v10_to_v11),
    Upgrade(11, 12, _upgrade_v11_to_v12),
    Upgrade(12, 13, _upgrade_v12_to_v13),
    Upgrade(13, 14, _upgrade_v13_to_v14),
    Upgrade(14, 15, _upgrade_v14_to_v15),
    Upgrade(15, 16, _upgrade_v15_to_v16),
    Upgrade(16, 17, _upgrade_v16_to_v17),
    Upgrade(17, 18, _upgrade_v17_to_v18),
    Upgrade(18, 19, _upgrade_v18_to_v19),
    Upgrade(19, 20, _upgrade_v19_to_v20),
    Upgrade(20, 21, _upgrade_v20_to_v21),
    Upgrade(21, 22, _upgrade_v21_to_v22),
)
