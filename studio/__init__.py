"""海外短剧制片协作后端（横琴海外短剧基地）。

领域划分：

- ``licensing``  来源 IP、市场授权窗口、项目立项锁定、按市场撤权
- ``script``     剧本版本链、本地化意见与剧情改动逐条关联、三方审批汇合
- ``schedule``   场景/演员/团队档期排期，冲突给出明确原因
- ``shoot``      镜头拍摄，来源锁定到已下发的剧本版本快照
- ``asset``      粗剪/字幕/配音/市场版式派生谱系与发布、撤权阻断
- ``ledger``     发行回执幂等入账与分市场账目
- ``trace``      权利争议反查（条款、批准人、素材、窗口、团队时区、事件）

所有跨实体不变量集中在 :class:`studio.app.ProductionSystem`（外观），仓储用
单把可重入锁串行化写操作，保证并发改稿、抢档、跨区撤权下账目一致。
"""

from .app import ProductionSystem
from .errors import (
    DomainError,
    NotFoundError,
    ConflictError,
    ValidationError,
    StateError,
)

__all__ = [
    "ProductionSystem",
    "DomainError",
    "NotFoundError",
    "ConflictError",
    "ValidationError",
    "StateError",
]
