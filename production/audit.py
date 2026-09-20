"""统一事件记录：所有状态变更落审计事件，反查时按团队时区回放。"""

from .clock import in_timezone, iso, iso_local, now_utc
from .db import dumps


def log_event(conn, event_type, actor, *, project_id=None, payload=None,
              timezone=None, entity_type=None, entity_id=None, at=None):
    at = at or now_utc()
    local_time = None
    if timezone:
        local_time = iso_local(in_timezone(at, timezone))
    cur = conn.execute(
        """INSERT INTO events (project_id, event_type, actor, occurred_at,
                               team_timezone, local_time, entity_type, entity_id, payload_json)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (project_id, event_type, actor, iso(at), timezone, local_time,
         entity_type, entity_id, dumps(payload or {})),
    )
    return cur.lastrowid
