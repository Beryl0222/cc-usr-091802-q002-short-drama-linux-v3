"""标识生成与时间工具。

排期以 UTC 存储，比较时再转团队时区；时间槽按整分钟归一来比较档期。
"""

import re
import uuid
from datetime import datetime, timezone

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def check_id(value, field="id"):
    if not isinstance(value, str) or not ID_RE.match(value):
        from .errors import ValidationError

        raise ValidationError(f"{field} 必须是 1-64 位小写字母数字 ._- 标识", {"field": field, "value": value})
    return value


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    """统一以 ...+00:00 形式序列化带时区时间（转 UTC）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def iso_local(dt):
    """保留当前时区偏移序列化（用于展示团队/资源本地时间）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def parse_dt(value):
    """解析 ISO-8601；无时区视为 UTC，统一返回 aware datetime。"""
    if isinstance(value, datetime):
        dt = value
    else:
        if not isinstance(value, str):
            from .errors import ValidationError

            raise ValidationError(f"无法解析时间 {value!r}")
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            from .errors import ValidationError

            raise ValidationError(f"无法解析时间 {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def slot(dt):
    """档期归一到 UTC 整分钟，构成 (resource, start_minute) 唯一约束。"""
    dt = parse_dt(dt)
    return dt.strftime("%Y-%m-%dT%H:%M")


_TZ_CACHE = {}


def validate_timezone(name):
    """校验 IANA 时区名（排期/反查事件线需要）。"""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if name not in _TZ_CACHE:
        try:
            _TZ_CACHE[name] = ZoneInfo(name)
        except ZoneInfoNotFoundError:
            from .errors import ValidationError

            raise ValidationError(f"未知 IANA 时区 {name!r}", {"field": "timezone", "value": name})
    return name


def in_timezone(dt, tzname):
    from zoneinfo import ZoneInfo

    return parse_dt(dt).astimezone(ZoneInfo(tzname))


def normalize_language(code):
    if not isinstance(code, str) or not re.match(r"^[a-z]{2,3}(-[A-Z][a-zA-Z]{1,7})?$", code):
        from .errors import ValidationError

        raise ValidationError(f"语言码需形如 en / pt-BR，收到 {code!r}", {"field": "language", "value": code})
    return code
