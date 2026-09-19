from sqlalchemy import select

from kerui_recruit.db.models import IndexSyncRecord


def projection_is_current(entity_type: str, entity_id):
    """Legacy entities without an outbox row remain compatible; pending writes do not."""
    return ~select(IndexSyncRecord.id).where(
        IndexSyncRecord.entity_type == entity_type,
        IndexSyncRecord.entity_id == entity_id,
        IndexSyncRecord.requested_version > IndexSyncRecord.applied_version,
    ).exists()
