from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Column:
    """A flattened export column for the org chart.

    ``sensitive`` marks fields that must be stripped from the client-facing
    export while remaining in the internal export.
    """

    key: str
    label: str
    sensitive: bool = False


# One flattened row per employee, ordered to match the internal Excel spec.
FLAT_COLUMNS: tuple[Column, ...] = (
    Column("company", "公司"),
    Column("top_department", "大部门"),
    Column("sub_department", "子部门/小组"),
    Column("name", "人员姓名"),
    Column("title", "岗位Title"),
    Column("job_level", "内部职级"),
    Column("report_to_name", "直接汇报人"),
    Column("subordinate_count", "下属数量"),
    Column("tenure_years", "司龄"),
    Column("business_module", "负责业务模块"),
    Column("status", "人员状态"),
    Column("intention", "跳槽意向", sensitive=True),
    Column("remark", "备注", sensitive=True),
    Column("contact", "联系方式", sensitive=True),
    Column("hc_status", "HC状态"),
    Column("hc_internal_note", "HC内部判断", sensitive=True),
    Column("team_size", "团队人数"),
    Column("business_direction", "业务方向"),
    Column("tech_stack", "技术栈"),
    Column("office_location", "办公地"),
    Column("leader_name", "小组负责人"),
    Column("leader_report_to_name", "小组负责人汇报给谁"),
)


def client_columns() -> list[Column]:
    return [column for column in FLAT_COLUMNS if not column.sensitive]
