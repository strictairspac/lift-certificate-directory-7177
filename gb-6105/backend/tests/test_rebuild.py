import unittest
from datetime import date

from src.domain.certificates import Certificate
from src.domain.hash_directory import Directory
from src.domain.rebuild import Change, RebuildInconsistency, catch_up, verify_then_publish
from src.domain.shadow_session import REBUILDING, SERVING, ShadowSession, ShadowState


def cert(code, revoked=False, expires="2026-12-31", lift=None):
    return Certificate(code, lift or f"L-{code}", date.fromisoformat(expires), revoked)


def change(seq, code, kind="register", revoked=False):
    return Change(seq, kind, cert(code, revoked=revoked))


class CatchUpTest(unittest.TestCase):
    def test_applies_changes_in_order_and_skips_applied(self):
        shadow = Directory(2)
        applied = catch_up(shadow, [change(1, "A"), change(2, "B")], 0)
        self.assertEqual(applied, 2)
        # 断点恢复：重放同一批不产生重复。
        again = catch_up(shadow, [change(1, "A"), change(2, "B"), change(3, "C")], applied)
        self.assertEqual(again, 3)
        self.assertEqual(set(shadow.records()), {"A", "B", "C"})

    def test_revoke_cannot_revive(self):
        shadow = Directory(2)
        catch_up(shadow, [change(1, "A"), change(2, "A", kind="revoke", revoked=True)], 0)
        _, record = shadow.locate("A")
        self.assertTrue(record.revoked)
        registry = {"A": cert("A", revoked=True)}
        verify_then_publish(shadow, registry)  # 不抛异常即通过

    def test_verify_detects_missing_and_extra(self):
        shadow = Directory(2)
        catch_up(shadow, [change(1, "A"), change(2, "B")], 0)
        registry = {"A": cert("A"), "C": cert("C")}  # B 多证、C 漏证
        with self.assertRaises(RebuildInconsistency) as ctx:
            verify_then_publish(shadow, registry)
        reasons = {item["reason"] for item in ctx.exception.args[0]}
        self.assertTrue(any("漏装" in reason for reason in reasons))
        self.assertTrue(any("未登记" in reason for reason in reasons))

    def test_verify_detects_revived_revocation(self):
        shadow = Directory(2)
        catch_up(shadow, [change(1, "A", revoked=True)], 0)
        # 权威登记仍是撤销，但影子若被写成有效则必须拦住。
        shadow.upsert(cert("A", revoked=False))
        with self.assertRaises(RebuildInconsistency):
            verify_then_publish(shadow, {"A": cert("A", revoked=True)})


class ShadowSessionTest(unittest.TestCase):
    def _registry(self, codes, revoked=()):
        return {code: cert(code, revoked=code in revoked) for code in codes}

    def test_resume_after_interruption(self):
        registry = self._registry({"A", "B", "C", "D"})
        state = ShadowState.initial(version=2, capacity=1, base_seq=0)
        session = ShadowSession(state, registry)
        session.advance([change(1, "A"), change(2, "B")])  # 第一轮中断
        checkpoint = session.state.snapshot
        self.assertEqual(session.state.applied_seq, 2)

        # 新 worker 进程从检查点恢复。
        resumed = ShadowSession(ShadowState(
            2, 1, 0, 2, checkpoint, session.state.progress, None,
        ), registry)
        resumed.advance([change(1, "A"), change(2, "B"), change(3, "C"), change(4, "D")])
        self.assertEqual(resumed.state.applied_seq, 4)
        finalized = resumed.finalize(latest_seq=4)
        self.assertIsNotNone(finalized)
        self.assertEqual(finalized.progress, 100)
        self.assertEqual(set(Directory.from_snapshot(finalized.snapshot).records()), registry.keys())

    def test_finalize_waits_for_late_changes(self):
        registry = self._registry({"A"})
        session = ShadowSession(ShadowState.initial(2, 1, 0), registry)
        session.advance([change(1, "A")])
        # 重建期间又来一条变更：未收齐不许切换。
        self.assertIsNone(session.finalize(latest_seq=2))
        session.advance([change(2, "A", kind="correct", revoked=True)])
        registry["A"] = cert("A", revoked=True)
        session.registry = registry
        finalized = session.finalize(latest_seq=2)
        self.assertIsNotNone(finalized)

    def test_repeated_publish_attempt_keeps_single_version(self):
        registry = self._registry({"A"}, revoked={"A"})
        state = ShadowState.initial(version=2, capacity=1, base_seq=0)
        session = ShadowSession(state, registry)
        session.advance([change(1, "A", kind="revoke", revoked=True)])
        first = session.finalize(latest_seq=1)
        #模拟 worker 重试：同一状态再 finalize 一次，版本号与快照不变。
        second = session.finalize(latest_seq=1)
        self.assertEqual(first.snapshot, second.snapshot)
        self.assertEqual(session.state.version, 2)

    def test_shadow_uses_smaller_capacity(self):
        base = Directory(4)
        base.add(cert("A"))
        from src.domain.rebuild import build_shadow
        shadow = build_shadow(base.snapshot(), target_capacity=2)
        self.assertEqual(shadow.capacity, 2)
        with self.assertRaises(RebuildInconsistency):
            build_shadow(base.snapshot(), target_capacity=4)


if __name__ == "__main__":
    unittest.main()
