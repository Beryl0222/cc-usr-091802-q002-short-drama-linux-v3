"""线程安全的内存仓储。

单把可重入锁串行化所有写事务；外观层每个公开方法在一个 ``store.tx()``
临界区内完成"读校验—改写—追加事件"，因此跨实体不变量不会被并发撕裂。
读接口也在锁内返回深拷贝，调用方拿到后无法在锁外改坏仓储状态。
"""

import copy
import threading


class Store:
    def __init__(self):
        self._lock = threading.RLock()
        self._tables = {
            "ips": {},
            "grants": {},
            "projects": {},
            "versions": {},
            "resources": {},
            "bookings": {},
            "shots": {},
            "assets": {},
            "releases": {},
            "ledger": {},
        }
        # 已入账回执幂等键：(project_id, idem_key)
        self.idem_keys = {}
        # release 唯一约束：(asset_id, market, platform) 只允许一条有效发布
        self.release_keys = {}

    @property
    def lock(self):
        return self._lock

    def tx(self):
        return self._lock

    def save(self, table, entity_id, entity):
        self._tables[table][entity_id] = entity

    def get(self, table, entity_id):
        return self._tables[table].get(entity_id)

    def require(self, table, entity_id, what):
        entity = self._tables[table].get(entity_id)
        if entity is None:
            from .errors import NotFoundError

            raise NotFoundError(
                f"{what}不存在: {entity_id}",
                details={"id": entity_id, "kind": table},
            )
        return entity

    def list(self, table, predicate=None):
        items = self._tables[table].values()
        if predicate is not None:
            items = [e for e in items if predicate(e)]
        return [copy.deepcopy(e) for e in items]

    def snapshot(self, entity):
        return copy.deepcopy(entity)
