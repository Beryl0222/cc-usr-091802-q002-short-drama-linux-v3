"""时间工具：内部一律存 UTC，排期展示按资源所在时区解释。"""

from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9+ 自带
    ZoneInfo = None


def now():
    """当前 UTC 时间（感知时区）。"""
    return datetime.now(timezone.utc)


def utc(dt):
    """把任意时间归一化为感知时区的 UTC。

    接受感知或朴素 datetime（朴素值按 UTC 解释）以及 ISO 字符串。
    """
    if isinstance(dt, str):
        parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        dt = parsed
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_iso(dt):
    """宽松解析 ISO 8601，失败抛 ValueError。"""
    return utc(dt)


def iso(dt):
    """稳定的 ISO 序列化（秒级精度、Z 结尾）。"""
    return utc(dt).isoformat(timespec="seconds").replace("+00:00", "Z")


def in_window(dt, start, end):
    """dt 是否落在闭开区间 [start, end) 内。"""
    moment = utc(dt)
    return utc(start) <= moment < utc(end)


def overlap(start_a, end_a, start_b, end_b):
    """两个区间是否重叠（半开区间）。"""
    return utc(start_a) < utc(end_b) and utc(start_b) < utc(end_a)


def zone(name):
    """返回 IANA 时区对象。"""
    if ZoneInfo is None:  # pragma: no cover
        raise RuntimeError("当前 Python 不支持 zoneinfo")
    return ZoneInfo(name)
