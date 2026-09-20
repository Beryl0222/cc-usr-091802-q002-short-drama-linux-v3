"""SQLite 存储：WAL + 外键 + 立即写事务，靠唯一约束兜底并发。

所有业务写操作都在 ``BEGIN IMMEDIATE`` 事务内完成“检查+写入”，
配合 (resource, minute)、幂等键、回执周期等唯一索引，保证跨线程并发安全。
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS ips (
    ip_id          TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    rightsholder   TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_id     TEXT PRIMARY KEY,
    ip_id          TEXT NOT NULL REFERENCES ips(ip_id),
    title          TEXT NOT NULL,
    -- 素材边界：允许使用的来源素材清单，如 novel#ch1-120 / character_design#01
    allowed_source_materials_json TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_markets (
    project_id     TEXT NOT NULL REFERENCES projects(project_id),
    market         TEXT NOT NULL,
    PRIMARY KEY (project_id, market)
);

CREATE TABLE IF NOT EXISTS licenses (
    license_id     TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(project_id),
    market         TEXT NOT NULL,
    language       TEXT NOT NULL,
    starts_at      TEXT NOT NULL,   -- UTC ISO
    expires_at     TEXT NOT NULL,   -- UTC ISO
    restricted_materials_json TEXT NOT NULL DEFAULT '[]', -- 该市场从项目素材边界中剔除的部分
    status         TEXT NOT NULL DEFAULT 'active',        -- active | revoked
    version        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    revoked_at     TEXT,
    revoke_reason  TEXT
);
-- 同一项目/市场/语言至多一份有效授权；续约走 renew，撤销后才可重新授予
CREATE UNIQUE INDEX IF NOT EXISTS ux_license_active
    ON licenses(project_id, market, language) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS script_versions (
    version_id     TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(project_id),
    seq            INTEGER NOT NULL,
    parent_version_id TEXT REFERENCES script_versions(version_id),
    based_on_version_id TEXT REFERENCES script_versions(version_id),
    content_hash   TEXT NOT NULL,
    scenes_json    TEXT NOT NULL,
    change_summary TEXT NOT NULL DEFAULT '',
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    locked_at      TEXT,             -- 三方审批汇合后固化
    license_snapshot_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(project_id, seq)
);

CREATE TABLE IF NOT EXISTS script_notes (
    note_id        TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(project_id),
    version_id     TEXT NOT NULL REFERENCES script_versions(version_id),
    author         TEXT NOT NULL,
    author_role    TEXT NOT NULL,    -- local_consultant 等
    category       TEXT NOT NULL,    -- cultural | compliance | plot
    body           TEXT NOT NULL,
    scene_ref      TEXT,             -- 意见可逐条挂到具体场景
    created_at     TEXT NOT NULL,
    resolved_in_version_id TEXT REFERENCES script_versions(version_id)
);

CREATE TABLE IF NOT EXISTS script_changes (
    change_id      TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(project_id),
    version_id     TEXT NOT NULL REFERENCES script_versions(version_id),
    scene_ref      TEXT,
    change_type    TEXT NOT NULL,    -- plot | localization | compliance_fix
    description    TEXT NOT NULL,
    source_note_id TEXT REFERENCES script_notes(note_id), -- 逐条关联本地化意见
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    version_id     TEXT NOT NULL REFERENCES script_versions(version_id),
    role           TEXT NOT NULL,    -- rights | compliance | production
    approver       TEXT NOT NULL,
    decision       TEXT NOT NULL,    -- approved | rejected
    comment        TEXT NOT NULL DEFAULT '',
    decided_at     TEXT NOT NULL,
    PRIMARY KEY (version_id, role)
);

CREATE TABLE IF NOT EXISTS teams (
    team_id        TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    timezone       TEXT NOT NULL     -- IANA，排期与反查事件线使用
);

CREATE TABLE IF NOT EXISTS sets (
    set_id         TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    timezone       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS talent (
    talent_id      TEXT PRIMARY KEY,
    name           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_teams (
    project_id     TEXT NOT NULL REFERENCES projects(project_id),
    team_id        TEXT NOT NULL REFERENCES teams(team_id),
    PRIMARY KEY (project_id, team_id)
);

CREATE TABLE IF NOT EXISTS schedule_bookings (
    booking_id     TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(project_id),
    scene_ref      TEXT NOT NULL,
    version_id     TEXT NOT NULL REFERENCES script_versions(version_id),
    status         TEXT NOT NULL DEFAULT 'tentative', -- tentative | confirmed | cancelled
    reason         TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    confirmed_at   TEXT
);

CREATE TABLE IF NOT EXISTS schedule_slots (
    slot_id        TEXT PRIMARY KEY,
    booking_id     TEXT NOT NULL REFERENCES schedule_bookings(booking_id),
    project_id     TEXT NOT NULL,
    resource_kind  TEXT NOT NULL,    -- set | talent | team
    resource_id    TEXT NOT NULL,
    start_minute   TEXT NOT NULL,    -- UTC YYYY-mm-ddTHH:MM
    end_minute     TEXT NOT NULL,
    holds_minutes  INTEGER NOT NULL DEFAULT 1
);

-- 分钟级占用表：同一资源同一分钟只能有一个生效档期，彻底杜绝区间重叠（含竞态）
CREATE TABLE IF NOT EXISTS slot_minutes (
    resource_kind  TEXT NOT NULL,
    resource_id    TEXT NOT NULL,
    minute         TEXT NOT NULL,
    slot_id        TEXT NOT NULL REFERENCES schedule_slots(slot_id),
    PRIMARY KEY (resource_kind, resource_id, minute)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS shots (
    shot_id        TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(project_id),
    booking_id     TEXT NOT NULL REFERENCES schedule_bookings(booking_id),
    scene_ref      TEXT NOT NULL,
    -- 拍摄瞬间钉住批准版本；之后剧本再改，本字段不动
    version_id     TEXT NOT NULL REFERENCES script_versions(version_id),
    version_seq    INTEGER NOT NULL,
    content_hash   TEXT NOT NULL,
    material_refs_json TEXT NOT NULL,
    taken_at       TEXT NOT NULL,
    team_id        TEXT REFERENCES teams(team_id),
    immutable      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id       TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(project_id),
    kind           TEXT NOT NULL,    -- rough_cut | subtitle | dub | market_format
    language       TEXT,
    market         TEXT,
    parent_asset_id TEXT REFERENCES assets(asset_id),
    version_id     TEXT NOT NULL REFERENCES script_versions(version_id),
    root_shot_ids_json  TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_asset_content ON assets(project_id, content_hash);

CREATE TABLE IF NOT EXISTS releases (
    release_id     TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(project_id),
    asset_id       TEXT NOT NULL REFERENCES assets(asset_id),
    market         TEXT NOT NULL,
    language       TEXT NOT NULL,
    platform       TEXT NOT NULL,
    scheduled_at   TEXT NOT NULL,
    released_at    TEXT,
    status         TEXT NOT NULL DEFAULT 'scheduled', -- scheduled | released | blocked
    blocked_reason TEXT NOT NULL DEFAULT '',
    license_id     TEXT REFERENCES licenses(license_id),
    license_terms_json TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_release_asset_platform
    ON releases(asset_id, platform);

CREATE TABLE IF NOT EXISTS release_receipts (
    receipt_id     TEXT PRIMARY KEY,
    release_id     TEXT NOT NULL REFERENCES releases(release_id),
    platform       TEXT NOT NULL,
    period_start   TEXT NOT NULL,
    period_end     TEXT NOT NULL,
    amount         TEXT NOT NULL,    -- 十进制字符串
    currency       TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_json   TEXT NOT NULL DEFAULT '{}',
    received_at    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'recorded'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_receipt_idempotency ON release_receipts(idempotency_key);
CREATE UNIQUE INDEX IF NOT EXISTS ux_receipt_period
    ON release_receipts(release_id, period_start, period_end);

CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id       TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(project_id),
    market         TEXT NOT NULL,
    release_id     TEXT NOT NULL REFERENCES releases(release_id),
    receipt_id     TEXT NOT NULL UNIQUE REFERENCES release_receipts(receipt_id),
    amount         TEXT NOT NULL,
    currency       TEXT NOT NULL,
    posted_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     TEXT,
    event_type     TEXT NOT NULL,
    actor          TEXT NOT NULL,
    occurred_at    TEXT NOT NULL,    -- UTC
    team_timezone  TEXT,             -- 事件发生时团队/资源本地时区
    local_time     TEXT,             -- 该时区下的本地时间，便于跨时区反查
    entity_type    TEXT,
    entity_id      TEXT,
    payload_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_project ON events(project_id, event_id);
CREATE INDEX IF NOT EXISTS ix_events_entity ON events(entity_type, entity_id);
"""


def connect(path, *, init=True):
    """打开一个到文件库的连接（每次调用独立连接，WAL 允许并发）。"""
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    if init:
        conn.executescript(SCHEMA)
    return conn


@contextmanager
def write_tx(conn):
    """BEGIN IMMEDIATE：进入即拿写锁，使“检查后写入”在并发下仍可串行化。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(text, default=None):
    if text is None or text == "":
        return default
    return json.loads(text)
