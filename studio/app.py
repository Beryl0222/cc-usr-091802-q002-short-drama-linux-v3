"""领域外观：组装各服务，并把跨模块不变量放进同一事务。

最重要的跨模块事务是 *按市场撤权*：先撤授权与项目锁，再级联冻结/召回
该市场资产。两步在同一把仓储锁内完成，因此与"发布、拍摄、入账、改稿"
并发时，要么整组生效、要么整组不生效，不会出现撤权后仍有新版本漏网。
"""

from .events import EventLog
from .store import Store
from .asset import AssetService
from .ledger import LedgerService
from .licensing import LicensingService
from .schedule import ScheduleService
from .script import ScriptService
from .shoot import ShootService
from .trace import TraceService


class ProductionSystem:
    def __init__(self):
        self.store = Store()
        self.events = EventLog()
        self.licensing = LicensingService(self.store, self.events)
        self.scripts = ScriptService(self.store, self.events, self.licensing)
        self.schedule = ScheduleService(self.store, self.events, self.licensing)
        self.shoot = ShootService(self.store, self.events, self.licensing,
                                  self.scripts)
        self.assets = AssetService(self.store, self.events, self.licensing)
        self.ledger = LedgerService(self.store, self.events)
        self.trace = TraceService(self.store, self.events)

    # ---- 授权 / 立项 ---------------------------------------------------

    def register_ip(self, *args, **kwargs):
        return self.licensing.register_ip(*args, **kwargs)

    def create_grant(self, *args, **kwargs):
        return self.licensing.create_grant(*args, **kwargs)

    def create_project(self, *args, **kwargs):
        return self.licensing.create_project(*args, **kwargs)

    def revoke_market_rights(self, actor, reason, grant_ids=None,
                             ip_id=None, market=None):
        """撤权 + 资产级联，同一事务内原子完成。

        可直接给 ``grant_ids``；或给 ``ip_id`` + ``market`` 表示撤销该 IP
        在该市场当前全部有效授权（跨区撤权的常见入口）。
        """
        with self.store.tx():
            if not grant_ids:
                if not ip_id or not market:
                    from .errors import ValidationError
                    raise ValidationError("撤权需提供 grant_ids 或 ip_id+market")
                grant_ids = self.licensing.active_grant_ids(ip_id, market)
                if not grant_ids:
                    from .errors import ConflictError
                    raise ConflictError(
                        f"IP {ip_id} 在市场 {market} 没有可撤销的有效授权")
            revoked, affected = self.licensing.revoke_grants(
                grant_ids, actor, reason)
            cascade = []
            for project_id, m in affected:
                result = self.assets.block_market(project_id, m, reason, actor)
                cascade.append({"project_id": project_id, "market": m,
                                **result})
            return {"revoked_grant_ids": revoked,
                    "affected_projects": cascade}

    # ---- 剧本 ----------------------------------------------------------

    def add_localization_note(self, *args, **kwargs):
        return self.scripts.add_localization_note(*args, **kwargs)

    def add_plot_change(self, *args, **kwargs):
        return self.scripts.add_plot_change(*args, **kwargs)

    def create_script_version(self, *args, **kwargs):
        return self.scripts.create_version(*args, **kwargs)

    def get_version(self, *args, **kwargs):
        return self.scripts.get_version(*args, **kwargs)

    def submit_script(self, *args, **kwargs):
        return self.scripts.submit(*args, **kwargs)

    def approve_script(self, *args, **kwargs):
        return self.scripts.approve(*args, **kwargs)

    def reject_script(self, *args, **kwargs):
        return self.scripts.reject(*args, **kwargs)

    def withdraw_script(self, *args, **kwargs):
        return self.scripts.withdraw(*args, **kwargs)

    def release_script_to_market(self, *args, **kwargs):
        return self.scripts.release_for_shooting(*args, **kwargs)

    # ---- 排期 / 拍摄 ---------------------------------------------------

    def register_resource(self, *args, **kwargs):
        return self.schedule.register_resource(*args, **kwargs)

    def book(self, *args, **kwargs):
        return self.schedule.book(*args, **kwargs)

    def check_availability(self, *args, **kwargs):
        return self.schedule.check_availability(*args, **kwargs)

    def calendar(self, *args, **kwargs):
        return self.schedule.calendar(*args, **kwargs)

    def capture_shot(self, *args, **kwargs):
        return self.shoot.shoot(*args, **kwargs)

    # ---- 资产 / 发行 / 账目 --------------------------------------------

    def create_asset(self, *args, **kwargs):
        return self.assets.create_asset(*args, **kwargs)

    def lineage(self, *args, **kwargs):
        return self.assets.lineage(*args, **kwargs)

    def publish(self, *args, **kwargs):
        return self.assets.publish(*args, **kwargs)

    def post_receipt(self, *args, **kwargs):
        return self.ledger.post_receipt(*args, **kwargs)

    def market_account(self, *args, **kwargs):
        return self.ledger.market_account(*args, **kwargs)

    def all_accounts(self, *args, **kwargs):
        return self.ledger.all_accounts(*args, **kwargs)

    # ---- 反查 / 事件 ---------------------------------------------------

    def trace_shot(self, *args, **kwargs):
        return self.trace.trace_shot(*args, **kwargs)

    def trace_asset(self, *args, **kwargs):
        return self.trace.trace_asset(*args, **kwargs)

    def trace_release(self, *args, **kwargs):
        return self.trace.trace_release(*args, **kwargs)

    def trace_market(self, *args, **kwargs):
        return self.trace.trace_market(*args, **kwargs)

    def event_log(self, project_id=None):
        if project_id is None:
            return [e.to_dict() for e in self.events.all()]
        return [e.to_dict() for e in self.events.by_ref(project_id=project_id)]
