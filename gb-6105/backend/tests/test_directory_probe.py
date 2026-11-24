import unittest
from datetime import date

from src.domain.certificates import Certificate
from src.domain.directory_probe import probe_batch
from src.domain.hash_directory import Directory


def cert(code, revoked=False, expires="2026-12-31"):
    return Certificate(code, f"L-{code}", date.fromisoformat(expires), revoked)


class ProbeBatchTest(unittest.TestCase):
    def test_pinned_snapshot_verdicts_and_immutability(self):
        directory = Directory(2)
        directory.add(cert("A"))
        directory.add(cert("B", expires="2026-11-20"))
        directory.add(cert("C", revoked=True))
        snapshot_v1 = directory.snapshot()

        outcome = probe_batch(snapshot_v1, ["A", "B", "C", "Z"], date(2026, 11, 24))
        verdicts = {row["项目"]: row["结论"] for row in outcome["rows"] if row["类别"] == "点查请求"}
        self.assertEqual(verdicts, {"A": "有效", "B": "已到期", "C": "已撤销", "Z": "未登记"})
        self.assertEqual(outcome["unavailable"], 3)

        # 之后目录发生撤销与切换，旧批次快照结论不变。
        directory.upsert(cert("A", revoked=True))
        snapshot_v2 = directory.snapshot()
        again = probe_batch(snapshot_v1, ["A"], date(2026, 11, 24))
        self.assertEqual(again["rows"][0]["结论"], "有效")
        switched = probe_batch(snapshot_v2, ["A"], date(2026, 11, 24))
        self.assertEqual(switched["rows"][0]["结论"], "已撤销")


if __name__ == "__main__":
    unittest.main()
