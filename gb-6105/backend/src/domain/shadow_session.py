"""影子重建会话的纯内存编排。

ShadowSession 只持有状态与决策，不碰数据库：服务层把行锁内读出的
版本行、变更流、登记快照喂进来，再把它产出的新状态持久化。
这样中断恢复、重复发布、撤销不复活等口径可以在无数据库环境下单测。
"""
from dataclasses import dataclass

from .certificates import Certificate
from .hash_directory import Directory
from .rebuild import Change, catch_up, verify_then_publish


REBUILDING = "rebuilding"
SERVING = "serving"
ARCHIVED = "archived"
FAILED = "failed"


@dataclass
class ShadowState:
    version: int
    capacity: int
    base_seq: int
    applied_seq: int
    snapshot: dict | None
    progress: int = 0
    note: str | None = None

    @classmethod
    def initial(cls, version, capacity, base_seq):
        # 影子目录从空目录起步、重放整条变更流；base_seq 仅记录申请时旧版进度。
        return cls(version, capacity, base_seq, 0, None, 0)


class ShadowSession:
    """一次重建任务的可恢复状态机。"""

    def __init__(self, state: ShadowState, registry):
        self.state = state
        self.registry = dict(registry)
        self.directory = (
            Directory.from_snapshot(state.snapshot)
            if state.snapshot is not None else Directory(state.capacity)
        )

    def advance(self, changes):
        """追平一批变更并返回应持久化的快照；重复调用安全。"""
        before = self.state.applied_seq
        self.state.applied_seq = catch_up(self.directory, changes, self.state.applied_seq)
        self.state.snapshot = self.directory.snapshot()
        self.state.progress = min(99, self.state.progress + (1 if self.state.applied_seq > before else 0))
        return self.state

    def finalize(self, latest_seq):
        """收齐后核对并生成可发布快照；未收齐返回 None。"""
        if self.state.applied_seq < latest_seq:
            return None
        _, overflow = verify_then_publish(self.directory, self.registry)
        self.state.snapshot = self.directory.snapshot()
        self.state.progress = 100
        self.state.note = (
            f"新目录收齐至变更 #{latest_seq} 后一次性切换；深度封顶溢出桶 {overflow}"
            if overflow else f"新目录收齐至变更 #{latest_seq}，所有桶在容量内"
        )
        return self.state


def parse_change(row):
    return Change(
        seq=row["seq"], kind=row["kind"],
        certificate=Certificate.from_snapshot(row["payload"]),
    )
