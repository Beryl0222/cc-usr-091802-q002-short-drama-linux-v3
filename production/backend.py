"""聚合所有领域 mixin 的后端门面。"""

from .backend_asset import AssetMixin
from .backend_finance import FinanceMixin
from .backend_ip import BackendBase, IPMixin, LicenseMixin, ResourceMixin
from .backend_schedule import ScheduleMixin
from .backend_script import ScriptMixin


class Backend(
    BackendBase,
    ResourceMixin,
    IPMixin,
    LicenseMixin,
    ScriptMixin,
    ScheduleMixin,
    AssetMixin,
    FinanceMixin,
):
    """海外制片协作后端。

    用法::

        b = Backend("data/production.db")
        b.register_ip(...)
    """
