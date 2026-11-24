"""无数据库的全流程口径演练。

用与 services/directory.py 相同的域原语，在内存里复现服务层编排，
覆盖验收叙述的每一条：
1. 巡检中途调小桶容量；
2. 重建期间照常补录、撤销、点查；
3. 切换前点查走旧目录，但查得到已入库更正；
4. 新目录收齐重建期间全部更正才一次性切换；
5. 不漏证、已撤销证书不恢复有效；
6. 同一批点查用同一版目录；
7. 重建中断可恢复，重试不重复发布；
8. 旧批次桶证据与结论保持原样。
"""
import unittest
from datetime import date

from src.domain.certificates import Certificate
from src.domain.directory_probe import probe_batch
from src.domain.hash_directory import Directory
from src.domain.rebuild import Change, RebuildInconsistency
from src.domain.shadow_session import ShadowSession, ShadowState


def cert(code, revoked=False, expires="2026-12-31"):
    return Certificate(code, f"L-{code}", date.fromisoformat(expires), revoked)


class InMemoryDirectoryService:
    """镜像 services/directory.py 的事务口径，去掉 SQL。"""

    def __init__(self):
        self.registry = {}
        self.changes = []
        self.versions = {1: Directory(4)}
        self.sessions = {}
        self.serving = 1
        self.archived = set()
        self.published = []
        self._seq = 0
        for code in ("YT-01", "YT-02", "YT-03", "YT-04", "YT-05"):
            self.register(cert(code, revoked=(code == "YT-05")))

    def register(self, certificate, kind=None):
        kind = kind or ("correct" if certificate.code in self.registry else "register")
        self._seq += 1
        self.changes.append(Change(self._seq, kind, certificate))
        self.registry[certificate.code] = certificate
        self.versions[self.serving].upsert(certificate)  # 同步旧目录
        return self._seq

    def revoke(self, code):
        record = self.registry[code]
        revoked = Certificate(record.code, record.lift, record.expires_on, True)
        return self.register(revoked, kind="revoke")

    def start_rebuild(self, capacity):
        assert capacity < self.versions[self.serving].capacity
        version = max(self.versions) + 1
        self.sessions[version] = ShadowSession(
            ShadowState.initial(version, capacity, self._seq), self.registry,
        )
        return version

    def advance_rebuild(self, version, crash_after=None):
        session = self.sessions[version]
        pending = [item for item in self.changes if item.seq > session.state.applied_seq]
        if crash_after is not None:
            pending = pending[:crash_after]
        session.advance(pending)
        return session.state.applied_seq

    def publish(self, version):
        session = self.sessions[version]
        session.registry = dict(self.registry)  # 以最新权威登记为核对基准
        finalized = session.finalize(self._seq)
        if finalized is None:
            return False
        # 原子切换 + 幂等：已发布过就不重复。
        if version in self.published:
            return False
        shadow = Directory.from_snapshot(finalized.snapshot)
        # 切换前核对（服务层在 worker 内做同一检查）。
        if set(shadow.records()) != set(self.registry):
            raise RebuildInconsistency("漏证或多证")
        for code, record in shadow.records().items():
            if record.snapshot() != self.registry[code].snapshot():
                raise RebuildInconsistency(f"{code} 内容不一致，撤销可能复活")
        self.archived.add(self.serving)
        self.versions[self.serving] = Directory.from_snapshot(
            self.versions[self.serving].snapshot(),
        )  # 封存即冻结快照，后续变更不再影响旧版证据
        self.versions[version] = shadow
        self.serving = version
        self.published.append(version)
        return True

    def probe(self, codes, review_date=date(2026, 11, 24), pinned=None):
        version = pinned or self.serving
        snapshot = Directory.from_snapshot(self.versions[version].snapshot()).snapshot()
        return version, probe_batch(snapshot, codes, review_date)


class FullScenarioTest(unittest.TestCase):
    def test_shrink_rebuild_with_live_corrections_and_pinned_probes(self):
        svc = InMemoryDirectoryService()

        # 批次 A：重建前在 V1 上点查，固定证据。
        v_before, batch_before = svc.probe(["YT-01", "YT-05", "YT-99"])
        self.assertEqual(v_before, 1)
        before_verdicts = {row["项目"]: row["结论"] for row in batch_before["rows"]
                           if row["类别"] == "点查请求"}
        self.assertEqual(before_verdicts, {"YT-01": "有效", "YT-05": "已撤销", "YT-99": "未登记"})

        # 巡检中途调小容量 4 -> 2，影子重建开始。
        shadow_version = svc.start_rebuild(2)
        self.assertEqual(svc.serving, 1)  # 旧版继续服务

        # 重建期间照常补录、撤销，旧目录同步可见。
        svc.register(cert("YT-06"))
        svc.register(cert("YT-01", expires="2026-11-01"))  # 更正为已到期
        svc.revoke("YT-02")
        _, mid = svc.probe(["YT-01", "YT-02", "YT-06"])
        mid_verdicts = {row["项目"]: row["结论"] for row in mid["rows"]
                        if row["类别"] == "点查请求"}
        self.assertEqual(mid_verdicts, {"YT-01": "已到期", "YT-02": "已撤销", "YT-06": "有效"})

        # 第一轮重建追了 2 条就中断（变更流共 8 条：初始 5 + 期间 3）。
        applied = svc.advance_rebuild(shadow_version, crash_after=2)
        self.assertEqual(applied, 2)
        self.assertFalse(svc.publish(shadow_version))  # 未收齐，不能切换

        # 恢复后从 applied_seq 续建，重放不重复。
        svc.advance_rebuild(shadow_version)
        self.assertTrue(svc.publish(shadow_version))
        self.assertEqual(svc.serving, shadow_version)
        self.assertIn(1, svc.archived)

        # 重试发布不产生新版本、不重复发布。
        self.assertFalse(svc.publish(shadow_version))
        self.assertEqual(svc.published, [shadow_version])

        # 新目录不漏证、撤销不复活、更正生效。
        new_dir = svc.versions[shadow_version]
        self.assertEqual(set(new_dir.records()), set(svc.registry))
        self.assertTrue(new_dir.records()["YT-02"].revoked)
        self.assertTrue(new_dir.records()["YT-05"].revoked)
        self.assertEqual(new_dir.capacity, 2)

        # 切换后点查走新版；旧批次 A 的固化证据（创建时落库的 JSON）原样可查。
        v_after, batch_after = svc.probe(["YT-01", "YT-02", "YT-06"])
        self.assertEqual(v_after, shadow_version)
        after_verdicts = {row["项目"]: row["结论"] for row in batch_after["rows"]
                          if row["类别"] == "点查请求"}
        self.assertEqual(after_verdicts, {"YT-01": "已到期", "YT-02": "已撤销", "YT-06": "有效"})

        # 批次 A 的证据在创建时已固化：YT-01 当时有效，结论不被后续更正/切换改写。
        stored_verdicts = {row["项目"]: row["结论"] for row in batch_before["rows"]
                           if row["类别"] == "点查请求"}
        self.assertEqual(stored_verdicts, {"YT-01": "有效", "YT-05": "已撤销", "YT-99": "未登记"})

        # 按封存的 V1 重新点查仍可寻址到同一桶号（旧版本结构冻结，仅内容含其生命周期内更正）。
        _, replay_old = svc.probe(["YT-01", "YT-05", "YT-99"], pinned=1)
        old_layout = {row["项目"]: row["桶号"] for row in replay_old["rows"]
                      if row["类别"] == "点查请求"}
        before_layout = {row["项目"]: row["桶号"] for row in batch_before["rows"]
                         if row["类别"] == "点查请求"}
        self.assertEqual(old_layout, before_layout)

    def test_same_batch_uses_one_version_even_if_switch_midway(self):
        svc = InMemoryDirectoryService()
        shadow_version = svc.start_rebuild(2)
        # 批次开始时固定 V1。
        pinned, first = svc.probe(["YT-03"], pinned=1)
        svc.advance_rebuild(shadow_version)
        svc.publish(shadow_version)
        # 同批复用固定版本，不跟随切换。
        _, same_batch = svc.probe(["YT-03"], pinned=pinned)
        self.assertEqual(
          same_batch["rows"][0], first["rows"][0],
        )
        _, switched = svc.probe(["YT-03"])
        self.assertEqual(switched["rows"][0]["结论"], first["rows"][0]["结论"])


if __name__ == "__main__":
    unittest.main()
