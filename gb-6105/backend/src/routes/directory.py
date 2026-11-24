from fastapi import APIRouter, Depends, Response

from src.config.resources import engine
from src.controllers.access import identity, operator
from src.schemas.directory import (
    CertificateEntry, CertificateInput, ChangeEntry, DirectoryOverview,
    ProbeBatchEntry, ProbeInput, RebuildInput, RevokeInput, VersionEntry,
)
from src.services import directory
from src.utils.serial import canonical_json

router = APIRouter(prefix="/directory", tags=["Directory"])


@router.get("/overview", response_model=DirectoryOverview, dependencies=[Depends(identity)])
def overview():
    with engine.connect() as db:
        return directory.overview(db)


@router.get("/versions", response_model=list[VersionEntry], dependencies=[Depends(identity)])
def versions():
    with engine.connect() as db:
        return directory.list_versions(db)


@router.get("/certificates", response_model=list[CertificateEntry], dependencies=[Depends(identity)])
def certificates():
    with engine.connect() as db:
        return directory.list_certificates(db)


@router.get("/changes", response_model=list[ChangeEntry], dependencies=[Depends(identity)])
def changes():
    with engine.connect() as db:
        return directory.list_changes(db)


@router.post("/certificates", response_model=dict, status_code=201)
def register(form: CertificateInput, user=Depends(operator)):
    with engine.begin() as db:
        result = directory.register_certificate(db, form, user["username"])
    _wake()
    return result


@router.post("/revoke", response_model=dict)
def revoke(form: RevokeInput, user=Depends(operator)):
    with engine.begin() as db:
        result = directory.revoke_certificate(db, form.code, user["username"])
    _wake()
    return result


@router.post("/rebuild", response_model=VersionEntry, status_code=202)
def rebuild(form: RebuildInput, user=Depends(operator)):
    with engine.begin() as db:
        entry = directory.request_rebuild(db, form.capacity, user["username"])
    _wake()
    return entry


@router.post("/probes", response_model=ProbeBatchEntry, status_code=201)
def probes(form: ProbeInput, user=Depends(identity)):
    with engine.begin() as db:
        return directory.create_probe_batch(
            db, form.probes, form.review_date, user["username"],
        )


@router.get("/probes", response_model=list[ProbeBatchEntry], dependencies=[Depends(identity)])
def probe_list():
    with engine.connect() as db:
        return directory.list_probe_batches(db)


@router.get("/probes/{batch_id}", response_model=ProbeBatchEntry, dependencies=[Depends(identity)])
def probe_detail(batch_id: int):
    with engine.connect() as db:
        return directory.get_probe_batch(db, batch_id)


@router.get("/probes/{batch_id}/export", dependencies=[Depends(identity)])
def probe_export(batch_id: int):
    with engine.connect() as db:
        batch = directory.get_probe_batch(db, batch_id)
    return Response(canonical_json(batch), media_type="application/json", headers={
        "Content-Disposition": f"attachment; filename=probe-evidence-{batch_id}.json",
    })


def _wake():
    from redis.exceptions import RedisError
    try:
        from src.config.resources import cache
        cache.lpush("workbench:wake", "directory")
        cache.ltrim("workbench:wake", 0, 99)
    except RedisError:
        # 数据库里的重建状态是持久队列，worker 轮询也能捡到。
        pass
