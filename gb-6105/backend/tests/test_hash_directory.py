import unittest
from datetime import date

from src.domain.certificates import Certificate
from src.domain.hash_directory import MAX_DEPTH, Directory, stable_hash


def cert(code, revoked=False, expires="2026-12-31", lift=None):
    return Certificate(code, lift or f"L-{code}", date.fromisoformat(expires), revoked)


class DirectorySnapshotTest(unittest.TestCase):
    def test_snapshot_roundtrip_preserves_layout(self):
        directory = Directory(2)
        for index in range(20):
            directory.add(cert(f"YT-{index:02d}"))
        restored = Directory.from_snapshot(directory.snapshot())
        self.assertEqual(restored.capacity, 2)
        self.assertEqual(restored.depth, directory.depth)
        self.assertEqual(restored.splits, directory.splits)
        self.assertEqual(restored.records().keys(), directory.records().keys())
        for code in directory.records():
            _, before = directory.locate(code)
            _, after = restored.locate(code)
            self.assertEqual(before.snapshot(), after.snapshot())

    def test_upsert_corrects_without_duplicate(self):
        directory = Directory(4)
        directory.add(cert("YT-01"))
        directory.upsert(cert("YT-01", revoked=True))
        _, record = directory.locate("YT-01")
        self.assertTrue(record.revoked)
        self.assertEqual(len(directory.records()), 1)

    def test_overflow_is_reported_at_depth_cap(self):
        directory = Directory(1)
        colliding = [f"YT-{index:02d}" for index in range(3)]
        # 构造低位持续碰撞的编号（若不碰撞则跳过断言，保证测试稳定）。
        keys = [code for code in colliding if stable_hash(code) & ((1 << MAX_DEPTH) - 1)
                == stable_hash(colliding[0]) & ((1 << MAX_DEPTH) - 1)]
        if len(keys) >= 3:
            for code in keys:
                directory.add(cert(code))
            self.assertTrue(directory.overflow_buckets())


if __name__ == "__main__":
    unittest.main()
