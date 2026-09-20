"""领域事件日志。

所有改变状态的业务动作都会追加一条不可变事件，争议反查时据此重建
"谁、在什么时间、依据什么批准/权利窗口做了什么"。事件只追加、不修改。
"""

from dataclasses import dataclass, field

from .clock import iso, now


@dataclass
class Event:
    seq: int
    at: object
    type: str
    actor: str
    refs: dict = field(default_factory=dict)
    detail: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "seq": self.seq,
            "at": iso(self.at),
            "type": self.type,
            "actor": self.actor,
            "refs": self.refs,
            "detail": self.detail,
        }


class EventLog:
    def __init__(self):
        self._events = []

    def append(self, type_, actor, refs=None, detail=None, at=None):
        event = Event(
            seq=len(self._events) + 1,
            at=at or now(),
            type=type_,
            actor=actor,
            refs=dict(refs or {}),
            detail=dict(detail or {}),
        )
        self._events.append(event)
        return event

    def all(self):
        return list(self._events)

    def by_ref(self, **criteria):
        """按 refs 精确过滤（如 project_id=...）。"""
        result = []
        for event in self._events:
            if all(event.refs.get(k) == v for k, v in criteria.items()):
                result.append(event)
        return result
