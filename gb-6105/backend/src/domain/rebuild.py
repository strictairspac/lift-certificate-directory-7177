"""缩容影子重建的纯域逻辑。

数据库服务负责把状态持久化，本模块只负责：
- 从基线快照装载新容量目录；
- 按变更序号顺序追平补录 / 更正 / 撤销；
- 断点恢复（重复追平同一条变更保持幂等）；
- 切换前核对新目录与权威登记，防止漏证或已撤销证书恢复有效。
"""
from dataclasses import dataclass

from .certificates import Certificate
from .hash_directory import Directory


@dataclass(frozen=True)
class Change:
    seq: int
    kind: str  # register | correct | revoke
    certificate: Certificate | None  # revoke 也带撤销后的完整快照


class RebuildInconsistency(Exception):
    """切换前核对失败，新目录不能发布。"""


def apply_change(directory, change):
    if change.kind in ("register", "correct", "revoke"):
        directory.upsert(change.certificate)
        return
    raise RebuildInconsistency(f"未知变更类型 {change.kind}")


def catch_up(directory, changes, applied_seq):
    """按序号追平，跳过已应用的变更；返回新的已应用序号。"""
    for change in sorted(changes, key=lambda item: item.seq):
        if change.seq <= applied_seq:
            continue
        apply_change(directory, change)
        applied_seq = change.seq
    return applied_seq


def verify_then_publish(shadow, registry, *, allow_overflow=True):
    """切换前最后核对。

    - 不多证：影子里的每个编号都在权威登记中且内容一致；
    - 不漏证：权威登记中的每个编号都在影子里；
    - 撤销不复活：撤销标记与失效日逐字段一致；
    - 容量口径：超深溢出只允许显式登记。
    返回（核对差异列表, 溢出桶号列表）。无差异才允许发布。
    """
    shadow_records = shadow.records()
    differences = []
    for code, record in shadow_records.items():
        expected = registry.get(code)
        if expected is None:
            differences.append({"code": code, "reason": "新目录存在未登记证书"})
        elif record.snapshot() != expected.snapshot():
            differences.append({"code": code, "reason": "新目录内容与权威登记不一致"})
    for code in registry:
        if code not in shadow_records:
            differences.append({"code": code, "reason": "新目录漏装证书"})
    overflow = [bucket.number for bucket in shadow.overflow_buckets()]
    if overflow and not allow_overflow:
        differences.append({"code": None, "reason": f"深度封顶仍有溢出桶 {overflow}"})
    if differences:
        raise RebuildInconsistency(differences)
    return differences, overflow


def build_shadow(base_snapshot, target_capacity):
    """从空目录按新容量重建；基线只用于校验容量口径，不复制旧桶结构。"""
    if base_snapshot is not None and target_capacity >= base_snapshot["capacity"]:
        raise RebuildInconsistency("巡检中途只允许调小桶容量")
    return Directory(target_capacity)
