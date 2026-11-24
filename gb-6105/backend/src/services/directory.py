"""在线版本化目录服务。

口径：
- 补录 / 更正 / 撤销先写权威登记和有序变更流，再同步当前 serving 目录，
  因此切换前点查走旧目录也能查到已入库的更正；
- 缩容重建在后台影子进行，按变更流追平，中断后从 applied_seq 续建；
- 只有新目录收齐全部变更并通过核对才一次性原子切换，旧版转 archived 只读；
- 点查批次开始时固定目录版本，整批复用同一版，证据下载后永不改写。
"""
from datetime import date
from time import time

from fastapi import HTTPException
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.domain import rebuild
from src.domain.certificates import _code, _date, Certificate, build_certificate
from src.domain.directory_probe import probe_batch
from src.domain.hash_directory import Directory
from src.domain.shadow_session import (
    ARCHIVED, REBUILDING, SERVING, ShadowSession, ShadowState, parse_change,
)
from src.models.directory import (
    certificate_changes, certificates, directory_versions, probe_batches,
)
from src.utils.serial import content_digest
from . import audit

DEFAULT_CAPACITY = 4
MAX_BATCH_PROBES = 100


# ---------- 基础读取 ----------

def _serving(db, lock=False):
    statement = select(directory_versions).where(directory_versions.c.status == SERVING)
    if lock:
        statement = statement.with_for_update()
    row = db.execute(statement).mappings().first()
    if row is None:
        raise HTTPException(503, "在线目录尚未初始化")
    return dict(row)


def _version(db, version, lock=False):
    statement = select(directory_versions).where(directory_versions.c.version == version)
    if lock:
        statement = statement.with_for_update()
    row = db.execute(statement).mappings().first()
    if row is None:
        raise HTTPException(404, "目录版本不存在")
    return dict(row)


def _registry_map(db, lock=False):
    statement = select(certificates)
    if lock:
        statement = statement.with_for_update()
    return {
        row["code"]: Certificate.from_snapshot({
            "code": row["code"], "lift": row["lift"],
            "expires_on": row["expires_on"], "revoked": row["revoked"],
        })
        for row in db.execute(statement).mappings()
    }


def _latest_seq(db):
    return db.scalar(select(func.coalesce(func.max(certificate_changes.c.seq), 0)))


def overview(db):
    serving = _serving(db)
    rebuilding = db.execute(
        select(directory_versions).where(directory_versions.c.status == REBUILDING)
        .order_by(directory_versions.c.version.desc())
    ).mappings().first()
    versions = [dict(row) for row in db.execute(
        select(directory_versions).order_by(directory_versions.c.version.desc())
    ).mappings()]
    cert_count = db.scalar(select(func.count()).select_from(certificates))
    return {
        "serving_version": serving["version"],
        "serving_capacity": serving["capacity"],
        "serving_digest": serving["digest"],
        "serving_published_at": serving["published_at"],
        "certificate_count": cert_count,
        "latest_seq": _latest_seq(db),
        "rebuilding": dict(rebuilding) if rebuilding else None,
        "versions": [_version_brief(row) for row in versions],
    }


def _version_brief(row):
    return {
        "version": row["version"], "status": row["status"], "capacity": row["capacity"],
        "applied_seq": row["applied_seq"], "base_seq": row["base_seq"],
        "progress": row["progress"], "digest": row["digest"], "note": row["note"],
        "created_at": row["created_at"], "published_at": row["published_at"],
    }


def list_versions(db):
    return [_version_brief(dict(row)) for row in db.execute(
        select(directory_versions).order_by(directory_versions.c.version.desc())
    ).mappings()]


def list_certificates(db):
    return [dict(row) for row in db.execute(
        select(certificates).order_by(certificates.c.code)
    ).mappings()]


def list_changes(db, limit=100):
    return [dict(row) for row in db.execute(
        select(certificate_changes).order_by(certificate_changes.c.seq.desc()).limit(limit)
    ).mappings()]


# ---------- 初始化 ----------

def bootstrap(db):
    """幂等建立第一版空目录，保证旧目录始终可读。"""
    existing = db.execute(
        select(directory_versions.c.version).where(directory_versions.c.status == SERVING)
    ).first()
    if existing is not None:
        return
    snapshot = Directory(DEFAULT_CAPACITY).snapshot()
    now = int(time())
    db.execute(insert(directory_versions).values(
        version=1, status=SERVING, capacity=DEFAULT_CAPACITY, snapshot=snapshot,
        base_seq=0, applied_seq=0, digest=content_digest(snapshot), progress=100,
        note="初始空目录，单桶容量 4", actor="system", created_at=now, published_at=now,
    ))


# ---------- 补录 / 更正 / 撤销 ----------

def _append_change(db, kind, certificate, actor):
    result = db.execute(insert(certificate_changes).values(
        code=certificate.code, kind=kind, payload=certificate.snapshot(),
        actor=actor, created_at=int(time()),
    ))
    return result.inserted_primary_key[0]


def _sync_serving(db, serving_row, certificate):
    directory = Directory.from_snapshot(serving_row["snapshot"])
    directory.upsert(certificate)
    snapshot = directory.snapshot()
    db.execute(update(directory_versions).where(
        directory_versions.c.version == serving_row["version"]
    ).values(snapshot=snapshot, digest=content_digest(snapshot)))


def register_certificate(db, data, actor):
    serving = _serving(db, lock=True)
    existing = db.execute(
        select(certificates).where(certificates.c.code == data.code).with_for_update()
    ).mappings().first()
    certificate = build_certificate(
        data.code, data.lift, data.expires_on, data.revoked,
    )
    kind = "correct" if existing else "register"
    seq = _append_change(db, kind, certificate, actor)
    db.execute(_cert_upsert(db, certificate, seq, actor))
    _sync_serving(db, serving, certificate)
    audit.record(db, f"directory_{kind}", actor, "certificate", 0)
    return {"code": certificate.code, "kind": kind, "seq": seq,
            "serving_version": serving["version"]}


def revoke_certificate(db, code, actor):
    serving = _serving(db, lock=True)
    row = db.execute(
        select(certificates).where(certificates.c.code == code).with_for_update()
    ).mappings().first()
    if row is None:
        raise HTTPException(404, f"证书 {code} 未登记，无法撤销")
    certificate = Certificate(
        code=row["code"], lift=row["lift"], expires_on=date.fromisoformat(row["expires_on"]),
        revoked=True,
    )
    seq = _append_change(db, "revoke", certificate, actor)
    db.execute(_cert_upsert(db, certificate, seq, actor))
    _sync_serving(db, serving, certificate)
    audit.record(db, "directory_revoke", actor, "certificate", 0)
    return {"code": code, "kind": "revoke", "seq": seq, "serving_version": serving["version"]}


def _cert_upsert(db, certificate, seq, actor):
    values = {
        "code": certificate.code, "lift": certificate.lift,
        "expires_on": certificate.expires_on.isoformat(), "revoked": certificate.revoked,
        "updated_seq": seq, "updated_by": actor, "updated_at": int(time()),
    }
    if db.dialect.name == "postgresql":
        return pg_insert(certificates).values(**values).on_conflict_do_update(
            index_elements=["code"], set_=values,
        )
    return mysql_insert(certificates).values(**values).on_duplicate_key_update(**values)


# ---------- 缩容重建 ----------

def request_rebuild(db, target_capacity, actor):
    serving = _serving(db, lock=True)
    active = db.execute(
        select(directory_versions.c.version).where(directory_versions.c.status == REBUILDING)
        .with_for_update(skip_locked=True)
    ).first()
    if active is not None:
        raise HTTPException(409, "已有影子重建在进行，收齐切换后才能再次调小容量")
    if type(target_capacity) is not int or not 1 <= target_capacity <= 16:
        raise HTTPException(422, "目标单桶容量须为 1–16")
    if target_capacity >= serving["capacity"]:
        raise HTTPException(422, f"巡检中途只允许调小容量，当前为 {serving['capacity']}")
    now = int(time())
    result = db.execute(insert(directory_versions).values(
        status=REBUILDING, capacity=target_capacity, snapshot=None,
        base_seq=serving["applied_seq"], applied_seq=0,
        progress=0, actor=actor, created_at=now,
        note=f"申请把单桶容量从 {serving['capacity']} 调小到 {target_capacity}，旧版 V{serving['version']} 继续服务",
    ))
    version = result.inserted_primary_key[0]
    audit.record(db, "directory_rebuild_request", actor, "directory_version", version)
    return _version(db, version)


def service_rebuild(db):
    """worker 调用：推进一个重建任务。返回是否处理了任务。"""
    task = db.execute(
        select(directory_versions).where(directory_versions.c.status == REBUILDING)
        .order_by(directory_versions.c.version).limit(1).with_for_update(skip_locked=True)
    ).mappings().first()
    if task is None:
        return False
    task = dict(task)
    try:
        registry = _registry_map(db)
        latest_seq = _latest_seq(db)
        changes = [parse_change(dict(row)) for row in db.execute(
            select(certificate_changes).where(certificate_changes.c.seq > task["applied_seq"])
            .order_by(certificate_changes.c.seq)
        ).mappings()]
        session = ShadowSession(ShadowState(
            version=task["version"], capacity=task["capacity"],
            base_seq=task["base_seq"], applied_seq=task["applied_seq"],
            snapshot=task["snapshot"], progress=task["progress"], note=task["note"],
        ), registry)
        session.advance(changes)
        finalized = session.finalize(latest_seq)
        if finalized is None:
            # 还有重建期间新入库的变更未追平，保存进度，旧版继续服务。
            db.execute(update(directory_versions).where(
                directory_versions.c.version == task["version"]
            ).values(
                snapshot=session.state.snapshot, applied_seq=session.state.applied_seq,
                progress=session.state.progress,
            ))
            return True
        _publish(db, task, session, latest_seq)
    except rebuild.RebuildInconsistency as exc:
        db.execute(update(directory_versions).where(
            directory_versions.c.version == task["version"]
        ).values(status=FAILED, note=f"切换核对未通过：{exc}"))
        audit.record(db, "directory_rebuild_failed", "worker", "directory_version", task["version"])
    return True


def _publish(db, task, session, latest_seq):
    """原子切换：同一事务内新版 serving、旧版 archived，重试不重复发布。"""
    serving = _serving(db, lock=True)
    snapshot = session.state.snapshot
    now = int(time())
    db.execute(update(directory_versions).where(
        directory_versions.c.version == task["version"],
        directory_versions.c.status == REBUILDING,
    ).values(
        status=SERVING, snapshot=snapshot, applied_seq=latest_seq, progress=100,
        digest=content_digest(snapshot), note=session.state.note, published_at=now,
    ))
    db.execute(update(directory_versions).where(
        directory_versions.c.version == serving["version"]
    ).values(status=ARCHIVED, note=(serving["note"] or "") + f"；V{task['version']} 收齐后切换，本版封存只读"))
    audit.record(db, "directory_publish", "worker", "directory_version", task["version"])


# ---------- 版本固定点查 ----------

def create_probe_batch(db, probes, review_date, actor):
    if not isinstance(probes, list) or not 1 <= len(probes) <= MAX_BATCH_PROBES:
        raise HTTPException(422, f"点查编号须有 1–{MAX_BATCH_PROBES} 条")
    codes = []
    for code in probes:
        try:
            codes.append(_code(code))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    if len(set(codes)) != len(codes):
        raise HTTPException(422, "同批点查编号不能重复")
    if review_date is None:
        review_date = date.today().isoformat()
    try:
        review_day = _date(review_date)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    serving = _serving(db)  # 批次开始时固定版本，整批不再换版
    now = int(time())
    result = db.execute(insert(probe_batches).values(
        directory_version=serving["version"], status="done",
        review_date=review_day.isoformat(), probes=codes, created_at=now, finished_at=now,
    ))
    batch_id = result.inserted_primary_key[0]
    outcome = probe_batch(serving["snapshot"], codes, review_day)
    result_payload = {
        "directory_version": serving["version"],
        "directory_digest": serving["digest"],
        "directory_capacity": serving["capacity"],
        "review_date": review_day.isoformat(),
        "summary": (
            f"点查 {len(codes)} 条，使用目录 V{serving['version']}（容量 {serving['capacity']}，"
            f"证据摘要 {serving['digest'][:12]}），异常 {outcome['unavailable']} 条。"
        ),
        "metrics": outcome["metrics"],
        "rows": outcome["rows"],
    }
    digest = content_digest(result_payload)
    db.execute(update(probe_batches).where(probe_batches.c.id == batch_id).values(
        result=result_payload, digest=digest,
    ))
    audit.record(db, "directory_probe", actor, "probe_batch", batch_id)
    return get_probe_batch(db, batch_id)


def list_probe_batches(db):
    return [dict(row) for row in db.execute(
        select(probe_batches).order_by(probe_batches.c.id.desc()).limit(100)
    ).mappings()]


def get_probe_batch(db, batch_id):
    row = db.execute(select(probe_batches).where(probe_batches.c.id == batch_id)).mappings().first()
    if row is None:
        raise HTTPException(404, "点查批次不存在")
    return dict(row)
