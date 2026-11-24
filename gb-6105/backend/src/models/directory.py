from sqlalchemy import JSON, BigInteger, Boolean, Column, Integer, String, Table, Text
from .base import metadata


# 权威证书登记表：补录、更正、撤销都落在这里，是切换前核对的基准。
certificates = Table(
    "certificates", metadata,
    Column("code", String(40), primary_key=True),
    Column("lift", String(40), nullable=False),
    Column("expires_on", String(10), nullable=False),
    Column("revoked", Boolean, nullable=False),
    Column("updated_seq", BigInteger, nullable=False),
    Column("updated_by", String(48), nullable=False),
    Column("updated_at", BigInteger, nullable=False),
)

# 有序变更流：影子目录按 seq 追平，重建中断后从 applied_seq 继续。
certificate_changes = Table(
    "certificate_changes", metadata,
    Column("seq", BigInteger, primary_key=True, autoincrement=True),
    Column("code", String(40), nullable=False, index=True),
    Column("kind", String(12), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("actor", String(48), nullable=False),
    Column("created_at", BigInteger, nullable=False),
)

# 版本化目录：serving 同时只有一版；rebuilding 可断点续建；archived 只读。
directory_versions = Table(
    "directory_versions", metadata,
    Column("version", Integer, primary_key=True, autoincrement=True),
    Column("status", String(12), nullable=False, index=True),
    Column("capacity", Integer, nullable=False),
    Column("snapshot", JSON(none_as_null=True), nullable=True),
    Column("base_seq", BigInteger, nullable=False, server_default="0"),
    Column("applied_seq", BigInteger, nullable=False, server_default="0"),
    Column("digest", String(64), nullable=True),
    Column("progress", Integer, nullable=False, server_default="0"),
    Column("note", Text, nullable=True),
    Column("actor", String(48), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("published_at", BigInteger, nullable=True),
)

# 点查批次：批次开始时固定目录版本，整批结论与下载证据保持原样。
probe_batches = Table(
    "probe_batches", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("directory_version", Integer, nullable=False, index=True),
    Column("status", String(16), nullable=False, index=True),
    Column("review_date", String(10), nullable=False),
    Column("probes", JSON, nullable=False),
    Column("result", JSON(none_as_null=True), nullable=True),
    Column("digest", String(64), nullable=True),
    Column("failure", String(320), nullable=True),
    Column("actor", String(48), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("finished_at", BigInteger, nullable=True),
)
