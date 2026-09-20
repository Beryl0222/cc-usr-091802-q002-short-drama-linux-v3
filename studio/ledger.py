"""发行回执幂等入账与分市场账目。

每条回执有两道去重键：

- ``(project_id, idem_key)``：调用方提供的稳定幂等键（网络重试安全）；
- ``(release_id, receipt_no)``：平台回执单号本身（迟到的重复回执安全）。

任一键命中都只返回首次入账的条目，不产生第二笔账。撤权召回后的发布
冻结入账，避免撤回市场再出现新款项；历史账目保留。
"""

from decimal import Decimal

from .errors import ConflictError, StateError, ValidationError
from .models import RELEASE_RECALLED, RELEASE_RELEASED, ledger_entry


class LedgerService:
    def __init__(self, store, events):
        self.store = store
        self.events = events

    def post_receipt(self, project_id, release_id, receipt_no, amount,
                     currency, period, idem_key, actor):
        if not receipt_no:
            raise ValidationError("回执单号不能为空")
        if not idem_key:
            raise ValidationError("幂等键不能为空")
        if not currency:
            raise ValidationError("币种不能为空")
        if not period:
            raise ValidationError("结算期不能为空")
        try:
            value = Decimal(str(amount))
        except Exception:
            raise ValidationError(f"金额非法: {amount}")
        with self.store.tx():
            release = self.store.require("releases", release_id, "发布记录")
            if release["project_id"] != project_id:
                raise ConflictError("发布记录不属于该项目")
            if release["status"] == RELEASE_RECALLED:
                raise StateError(
                    "该市场发布已随撤权召回，账目冻结，不能再入账",
                    details={"release_id": release_id,
                             "market": release["market"]},
                )
            if release["status"] != RELEASE_RELEASED:
                raise StateError("发布记录不在已发行状态，不能入账")

            existing = self.store.idem_keys.get((project_id, idem_key))
            matched_by = "idem_key"
            if existing is None:
                for e in self.store.list(
                        "ledger", lambda e: e["release_id"] == release_id
                        and e["receipt_no"] == receipt_no):
                    existing = e["entry_id"]
                    matched_by = "receipt_no"
                    break
            if existing is not None:
                first = self.store.require("ledger", existing, "账目")
                self.events.append(
                    "ledger.receipt_deduplicated", actor,
                    {"project_id": project_id, "entry_id": existing,
                     "release_id": release_id},
                    {"receipt_no": receipt_no, "matched_by": matched_by,
                     "idem_key": idem_key},
                )
                result = self.store.snapshot(first)
                result["deduplicated"] = True
                result["matched_by"] = matched_by
                return result

            entry_id = f"led-{len(self.store.list('ledger')) + 1}-{receipt_no}"[:64]
            if self.store.get("ledger", entry_id) is not None:
                entry_id = f"led-{receipt_no}-{abs(hash(idem_key)) % 10**8}"
            entity = ledger_entry(entry_id, project_id, release["market"],
                                  release_id, release["platform"], receipt_no,
                                  value, currency, period, idem_key, actor)
            self.store.save("ledger", entry_id, entity)
            self.store.idem_keys[(project_id, idem_key)] = entry_id
            release["receipt_entry_ids"].append(entry_id)
            self.store.save("releases", release_id, release)
            self.events.append(
                "ledger.receipt_posted", actor,
                {"project_id": project_id, "market": release["market"],
                 "release_id": release_id, "entry_id": entry_id},
                {"receipt_no": receipt_no, "amount": str(value),
                 "currency": currency, "period": period},
            )
            result = self.store.snapshot(entity)
            result["deduplicated"] = False
            return result

    def market_account(self, project_id, market):
        """单市场账目：按币种汇总，并冻结标记与撤回市场一致。"""
        with self.store.tx():
            entries = self.store.list(
                "ledger", lambda e: e["project_id"] == project_id
                and e["market"] == market)
            totals = {}
            for e in entries:
                bucket = totals.setdefault(
                    e["currency"], {"currency": e["currency"], "amount": "0",
                                    "entry_count": 0})
                bucket["amount"] = str(Decimal(bucket["amount"])
                                      + Decimal(e["amount"]))
                bucket["entry_count"] += 1
            project_ = self.store.require("projects", project_id, "项目")
            lock = project_["markets"].get(market)
            return {
                "project_id": project_id,
                "market": market,
                "frozen": bool(lock and lock["status"] != "active"),
                "entry_count": len(entries),
                "totals": sorted(totals.values(), key=lambda t: t["currency"]),
                "entries": entries,
            }

    def all_accounts(self, project_id):
        """跨市场账目视图；每个市场独立汇总，撤权市场冻结且不影响他市。"""
        with self.store.tx():
            project_ = self.store.require("projects", project_id, "项目")
            return {m: self.market_account(project_id, m)
                    for m in sorted(project_["markets"].keys())}
