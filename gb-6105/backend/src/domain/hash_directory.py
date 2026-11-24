from dataclasses import dataclass, field
from hashlib import sha256

from .certificates import Certificate

MAX_DEPTH = 12


def stable_hash(key):
    return int.from_bytes(sha256(key.encode("ascii")).digest()[:8], "big")


@dataclass
class Bucket:
    number: int
    depth: int
    records: dict[str, Certificate] = field(default_factory=dict)


class Directory:
    """可扩展哈希目录。容量固定，超深后允许溢出并显式报告。"""

    def __init__(self, capacity):
        self.capacity = capacity
        self.depth = 0
        self.slots = [Bucket(0, 0)]
        self.splits = 0

    def _bucket(self, key):
        mask = (1 << self.depth) - 1
        return self.slots[stable_hash(key) & mask]

    def _split(self, full):
        if full.depth == self.depth:
            self.slots = self.slots + self.slots
            self.depth += 1
        distinguishing_bit = 1 << full.depth
        full.depth += 1
        sibling = Bucket(full.number | distinguishing_bit, full.depth)
        for slot, candidate in enumerate(self.slots):
            if candidate is full and slot & distinguishing_bit:
                self.slots[slot] = sibling
        for key, record in list(full.records.items()):
            if stable_hash(key) & distinguishing_bit:
                sibling.records[key] = record
                del full.records[key]
        self.splits += 1

    def add(self, record):
        bucket = self._bucket(record.code)
        if record.code in bucket.records:
            raise ValueError(f"证书编号重复 {record.code}")
        while len(bucket.records) >= self.capacity and bucket.depth < MAX_DEPTH:
            self._split(bucket)
            bucket = self._bucket(record.code)
        bucket.records[record.code] = record

    def upsert(self, record):
        """影子目录追平用：已存在则原地更正，不存在则按容量规则插入。"""
        bucket = self._bucket(record.code)
        if record.code in bucket.records:
            bucket.records[record.code] = record
            return
        while len(bucket.records) >= self.capacity and bucket.depth < MAX_DEPTH:
            self._split(bucket)
            bucket = self._bucket(record.code)
        bucket.records[record.code] = record

    def locate(self, key):
        bucket = self._bucket(key)
        return bucket, bucket.records.get(key)

    def buckets(self):
        unique = {bucket.number: bucket for bucket in self.slots}
        return sorted(unique.values(), key=lambda bucket: bucket.number)

    def records(self):
        return {code: record for bucket in self.buckets() for code, record in bucket.records.items()}

    def overflow_buckets(self):
        return [bucket for bucket in self.buckets() if len(bucket.records) > self.capacity]

    def snapshot(self):
        """可重复的完整目录状态，作为目录版本的持久证据。"""
        unique = {id(bucket): bucket for bucket in self.slots}
        bucket_index = {identity: index for index, identity in enumerate(unique)}
        return {
            "capacity": self.capacity,
            "depth": self.depth,
            "splits": self.splits,
            "buckets": [
                {
                    "number": bucket.number,
                    "depth": bucket.depth,
                    "records": [record.snapshot() for record in bucket.records.values()],
                }
                for bucket in unique.values()
            ],
            "slots": [bucket_index[id(bucket)] for bucket in self.slots],
        }

    @classmethod
    def from_snapshot(cls, data):
        directory = cls(data["capacity"])
        directory.depth = data["depth"]
        directory.splits = data["splits"]
        buckets = []
        for item in data["buckets"]:
            bucket = Bucket(item["number"], item["depth"])
            for record in item["records"]:
                certificate = Certificate.from_snapshot(record)
                bucket.records[certificate.code] = certificate
            buckets.append(bucket)
        directory.slots = [buckets[index] for index in data["slots"]]
        return directory
