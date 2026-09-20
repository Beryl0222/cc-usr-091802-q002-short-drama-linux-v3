"""发行回执、分市场账目与争议反查。

回执通过 idempotency_key（平台回执号）与 (发行单, 结算周期) 两个唯一约束防重；
ledger 行又对 receipt_id 唯一。平台重复推送或迟到重传时，第二次请求只返回
原回执（duplicate=true），绝不二次入账。账目始终按市场/币种汇总，与回执可对账。
"""

import re
import sqlite3
from decimal import Decimal, InvalidOperation

from .audit import log_event
from .clock import iso, new_id, now_utc
from .db import dumps, loads
from .errors import ConflictState, NotFound, ValidationError

# 争议反查关心的制作事件，按发生顺序回放
TRACE_EVENT_TYPES = (
    "ip.registered", "project.created", "license.granted", "license.renewed",
    "license.revoked", "script.draft_created", "script.note_added",
    "script.change_added", "script.approval_submitted", "script.locked",
    "team.assigned", "schedule.confirmed", "schedule.cancelled",
    "shot.recorded", "asset.created", "release.scheduled", "release.published",
)


class FinanceMixin:
    def record_receipt(self, release_id, platform, period_start, period_end,
                       amount, currency, idempotency_key, *, payload=None,
                       actor="platform"):
        """登记发行回执并单次入账；重复/迟到回执幂等返回。"""
        currency = self._normalize_currency(currency)
        amount_dec = self._normalize_amount(amount)
        p_start, p_end = self._normalize_period(period_start, period_end)
        if not idempotency_key or not isinstance(idempotency_key, str):
            raise ValidationError("idempotency_key（平台回执唯一号）不能为空")
        with self.tx() as conn:
            release = self._must(conn, "releases", "release_id", release_id, "发行单")
            if release["platform"] != platform:
                raise ValidationError(
                    "回执平台与发行单平台不一致",
                    {"receipt_platform": platform,
                     "release_platform": release["platform"]})
            if release["status"] != "released":
                raise ConflictState(
                    "只有已上线版本可登记发行回执",
                    {"release_id": release_id, "status": release["status"]})
            receipt_id = new_id("rcp")
            try:
                conn.execute(
                    """INSERT INTO release_receipts (receipt_id, release_id, platform,
                         period_start, period_end, amount, currency, idempotency_key,
                         payload_json, received_at, status)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (receipt_id, release_id, platform, p_start, p_end,
                     str(amount_dec), currency, idempotency_key,
                     dumps(payload or {}), iso(now_utc()), "recorded"))
            except sqlite3.IntegrityError:
                # 唯一约束兜底：重复推送/同周期重传 → 找到原回执，幂等返回不入账
                existing = conn.execute(
                    "SELECT * FROM release_receipts WHERE idempotency_key=?",
                    (idempotency_key,)).fetchone()
                if existing is None:
                    existing = conn.execute(
                        "SELECT * FROM release_receipts WHERE release_id=? "
                        "AND period_start=? AND period_end=?",
                        (release_id, p_start, p_end)).fetchone()
                if existing is None:
                    raise ConflictState("回执写入冲突但找不到既有记录")
                result = self._receipt_dict(existing)
                result["duplicate"] = True
                result["posted"] = False
                return result
            entry_id = new_id("led")
            conn.execute(
                """INSERT INTO ledger_entries (entry_id, project_id, market,
                     release_id, receipt_id, amount, currency, posted_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (entry_id, release["project_id"], release["market"], release_id,
                 receipt_id, str(amount_dec), currency, iso(now_utc())))
            log_event(conn, "receipt.recorded", actor, project_id=release["project_id"],
                      entity_type="receipt", entity_id=receipt_id,
                      payload={"release_id": release_id, "market": release["market"],
                               "period_start": p_start, "period_end": p_end,
                               "amount": str(amount_dec), "currency": currency,
                               "idempotency_key": idempotency_key,
                               "ledger_entry_id": entry_id})
            row = conn.execute("SELECT * FROM release_receipts WHERE receipt_id=?",
                               (receipt_id,)).fetchone()
            result = self._receipt_dict(row)
            result["duplicate"] = False
            result["posted"] = True
            result["ledger_entry_id"] = entry_id
            return result

    def market_accounts(self, project_id):
        """按市场/币种汇总账目，并用回执金额对账，保证两边一致。"""
        with self.conn() as conn:
            self._project(conn, project_id)
            ledger_rows = conn.execute(
                """SELECT market, currency, COUNT(*) AS entries,
                          SUM(amount) AS total
                   FROM ledger_entries WHERE project_id=?
                   GROUP BY market, currency ORDER BY market, currency""",
                (project_id,)).fetchall()
            receipt_rows = conn.execute(
                """SELECT r.market, rc.currency, COUNT(*) AS cnt,
                          SUM(rc.amount) AS total
                   FROM release_receipts rc JOIN releases r ON r.release_id=rc.release_id
                   WHERE r.project_id=?
                   GROUP BY r.market, rc.currency""",
                (project_id,)).fetchall()
            receipt_map = {(r["market"], r["currency"]): r for r in receipt_rows}
            accounts = []
            balanced = True
            for row in ledger_rows:
                key = (row["market"], row["currency"])
                rec = receipt_map.get(key)
                ledger_total = Decimal(row["total"])
                receipt_total = Decimal(rec["total"]) if rec else Decimal("0")
                match = ledger_total == receipt_total and (
                    rec is not None and rec["cnt"] == row["entries"])
                balanced = balanced and match
                accounts.append({
                    "market": row["market"], "currency": row["currency"],
                    "ledger_entries": row["entries"],
                    "receipt_count": rec["cnt"] if rec else 0,
                    "total": str(ledger_total.quantize(Decimal("0.01"))),
                    "receipt_total": str(receipt_total.quantize(Decimal("0.01"))),
                    "balanced": match,
                })
            return {"project_id": project_id, "accounts": accounts,
                    "balanced": balanced}

    def list_receipts(self, release_id):
        with self.conn() as conn:
            self._must(conn, "releases", "release_id", release_id, "发行单")
            return [self._receipt_dict(r) for r in conn.execute(
                "SELECT * FROM release_receipts WHERE release_id=? ORDER BY received_at",
                (release_id,))]

    # ---------------- 争议反查 ----------------
    def dispute_trace(self, release_id):
        """上线版本出现权利争议时，沿授权条款/团队时区/制作事件完整反查。"""
        with self.conn() as conn:
            r = self._must(conn, "releases", "release_id", release_id, "发行单")
            project = self._project(conn, r["project_id"])
            ip = conn.execute("SELECT * FROM ips WHERE ip_id=?",
                              (project["ip_id"],)).fetchone()
            asset = conn.execute("SELECT * FROM assets WHERE asset_id=?",
                                 (r["asset_id"],)).fetchone()

            # 1) 谱系与镜头来源
            chain, root, shots = self._trace_lineage(conn, r["asset_id"])

            # 2) 被采用的剧情版本与三方批准人
            version = conn.execute("SELECT * FROM script_versions WHERE version_id=?",
                                   (root["version_id"],)).fetchone()
            version_body = loads(version["scenes_json"])
            approvals = [dict(row) for row in conn.execute(
                "SELECT role, approver, decision, comment, decided_at FROM approvals "
                "WHERE version_id=? ORDER BY role", (version["version_id"],))]

            # 3) 授权：锁定时快照条款 + 当前状态 + 权利窗口
            snapshot = loads(version["license_snapshot_json"], [])
            current_licenses = [self._license_dict(row) for row in conn.execute(
                "SELECT * FROM licenses WHERE project_id=? ORDER BY market, language",
                (r["project_id"],))]

            # 4) 素材边界
            boundary = loads(project["allowed_source_materials_json"])

            # 5) 团队时区
            teams = [dict(row) for row in conn.execute(
                """SELECT t.team_id, t.name, t.timezone FROM teams t
                   JOIN project_teams pt ON pt.team_id=t.team_id
                   WHERE pt.project_id=?""", (r["project_id"],))]

            # 6) 制作事件线（UTC + 各事件发生时的团队本地时间）
            events = [dict(row) for row in conn.execute(
                f"""SELECT event_id, event_type, actor, occurred_at, team_timezone,
                           local_time, entity_type, entity_id, payload_json
                    FROM events WHERE project_id=?
                      AND event_type IN ({','.join('?' for _ in TRACE_EVENT_TYPES)})
                    ORDER BY event_id""",
                (r["project_id"], *TRACE_EVENT_TYPES))]
            for e in events:
                e["payload"] = loads(e.pop("payload_json"))

            return {
                "release_id": release_id,
                "status": r["status"],
                "market": r["market"], "language": r["language"],
                "platform": r["platform"],
                "scheduled_at": r["scheduled_at"], "released_at": r["released_at"],
                "blocked_reason": r["blocked_reason"],
                "rights_window_at_release": loads(r["license_terms_json"]),
                "source_ip": {"ip_id": ip["ip_id"], "title": ip["title"],
                              "rightsholder": ip["rightsholder"]},
                "project": {"project_id": project["project_id"],
                            "title": project["title"],
                            "allowed_source_materials": boundary},
                "adopted_script": {
                    "version_id": version["version_id"], "seq": version["seq"],
                    "content_hash": version["content_hash"],
                    "locked_at": version["locked_at"],
                    "scenes": version_body["scenes"],
                    "target_markets": version_body["target_markets"],
                    "created_by": version["created_by"],
                    "approvals": approvals,
                },
                "license_snapshot_at_lock": snapshot,
                "licenses_current": current_licenses,
                "asset_lineage": {"root_asset_id": root["asset_id"],
                                  "chain": chain, "root_shots": shots},
                "teams_timezones": teams,
                "production_timeline": events,
            }

    def list_events(self, project_id, event_type=None):
        with self.conn() as conn:
            self._project(conn, project_id)
            sql = "SELECT * FROM events WHERE project_id=? "
            params = [project_id]
            if event_type:
                sql += "AND event_type=? "
                params.append(event_type)
            sql += "ORDER BY event_id"
            out = []
            for row in conn.execute(sql, params):
                d = dict(row)
                d["payload"] = loads(d.pop("payload_json"))
                out.append(d)
            return out

    # ---------------- 工具 ----------------
    def _trace_lineage(self, conn, asset_id):
        chain, cur, seen = [], conn.execute(
            "SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone(), set()
        while True:
            chain.append(self._asset_dict(conn, cur))
            if cur["asset_id"] in seen:
                raise ConflictState("资产谱系存在环，数据异常")
            seen.add(cur["asset_id"])
            if cur["parent_asset_id"] is None:
                break
            cur = conn.execute("SELECT * FROM assets WHERE asset_id=?",
                               (cur["parent_asset_id"],)).fetchone()
        root = chain[-1]
        shots = []
        for s in conn.execute(
                "SELECT * FROM shots WHERE shot_id IN (SELECT value FROM json_each(?)) "
                "ORDER BY taken_at", (dumps(root["root_shot_ids"]),)):
            d = dict(s)
            d["material_refs"] = loads(d.pop("material_refs_json"))
            shots.append(d)
        return list(reversed(chain)), root, shots

    @staticmethod
    def _normalize_currency(code):
        if not isinstance(code, str) or not re.match(r"^[A-Z]{3}$", code):
            raise ValidationError("currency 需为 ISO-4217 三字母码，如 USD",
                                  {"value": code})
        return code

    @staticmethod
    def _normalize_amount(value):
        try:
            dec = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValidationError(f"金额无法解析: {value!r}")
        if dec < 0:
            raise ValidationError("回执金额不能为负（冲正请走专门流程）")
        # 定点两位，避免二进制浮点误差
        return dec.quantize(Decimal("0.01"))

    @staticmethod
    def _normalize_period(start, end):
        from .clock import parse_dt

        p_start, p_end = parse_dt(start), parse_dt(end)
        if p_start > p_end:
            raise ValidationError("结算周期开始不能晚于结束")
        return iso(p_start), iso(p_end)

    @staticmethod
    def _receipt_dict(row):
        d = dict(row)
        d["payload"] = loads(d.pop("payload_json"), {})
        return d
