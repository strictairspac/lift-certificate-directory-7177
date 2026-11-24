import json
from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

from migrations.v001_workbench import upgrade
from src.config.resources import engine
from src.config.settings import DEMO_PASSWORD
from src.constants.roles import Role
from src.domain.certificates import build_certificate
from src.domain.hash_directory import Directory
from src.domain.scenario import FIXTURES
from src.models.cases import cases
from src.models.directory import certificate_changes, certificates, directory_versions
from src.models.users import users
from src.services import directory as directory_service
from src.utils.passwords import hash_password
from src.utils.serial import content_digest


def insert_new(db, table, key, values):
    if db.dialect.name == "postgresql":
        stmt = pg_insert(table).values(**values).on_conflict_do_nothing(index_elements=[key])
    else:
        stmt = mysql_insert(table).values(**values).on_duplicate_key_update(**{key: table.c[key]})
    db.execute(stmt)


def setup_schema():
    with engine.begin() as db:
        upgrade(db)


def seed_directory(db):
    """把演示卷宗装入在线目录 V1：登记、变更流、目录快照三处一致。"""
    if db.scalar(select(func.count()).select_from(certificates)):
        return
    serving = directory_service._serving(db)
    directory = Directory.from_snapshot(serving["snapshot"])
    now_seed = [item for fixture in FIXTURES for item in fixture["payload"]["certificates"]]
    seen = {}
    for item in now_seed:
        seen[item["code"]] = item
    for code in sorted(seen):
        item = seen[code]
        certificate = build_certificate(
            item["code"], item["lift"], item["expires_on"], item["revoked"],
        )
        result = db.execute(certificate_changes.insert().values(
            code=certificate.code, kind="register", payload=certificate.snapshot(),
            actor="seed", created_at=serving["created_at"],
        ))
        seq = result.inserted_primary_key[0]
        insert_new(db, certificates, "code", {
            "code": certificate.code, "lift": certificate.lift,
            "expires_on": certificate.expires_on.isoformat(), "revoked": certificate.revoked,
            "updated_seq": seq, "updated_by": "seed", "updated_at": serving["created_at"],
        })
        directory.upsert(certificate)
    snapshot = directory.snapshot()
    db.execute(directory_versions.update().where(
        directory_versions.c.version == serving["version"]
    ).values(
        snapshot=snapshot, applied_seq=db.scalar(
            select(func.coalesce(func.max(certificate_changes.c.seq), 0))
        ), digest=content_digest(snapshot),
        note=f"初始目录装入 {len(seen)} 条演示证书，单桶容量 {serving['capacity']}",
    ))


def seed():
    with engine.begin() as db:
        existing = set(db.execute(select(users.c.username)).scalars())
        for role in Role:
            if role.value not in existing:
                insert_new(db, users, "username", {
                    "username": role.value, "role": role.value, "password_hash": hash_password(DEMO_PASSWORD),
                })
        for fixture in FIXTURES:
            insert_new(db, cases, "code", {**fixture, "created_by": "seed"})
        seed_directory(db)
        return {table.name: db.scalar(select(func.count()).select_from(table))
                for table in (users, cases, certificates)}


if __name__ == "__main__":
    setup_schema()
    print(json.dumps(seed(), sort_keys=True))
