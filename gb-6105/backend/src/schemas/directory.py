from pydantic import BaseModel, Field, field_validator

from src.domain.certificates import CODE


class CertificateInput(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    lift: str = Field(min_length=1, max_length=40)
    expires_on: str = Field(min_length=8, max_length=10)
    revoked: bool

    @field_validator("code", "lift")
    @classmethod
    def check_code(cls, value):
        if not CODE.fullmatch(value):
            raise ValueError("编号须为 1–40 位大写字母、数字或连字符")
        return value

    @field_validator("expires_on")
    @classmethod
    def check_date(cls, value):
        from datetime import date
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("失效日须用 YYYY-MM-DD") from exc
        return value


class RebuildInput(BaseModel):
    capacity: int = Field(strict=True, ge=1, le=16)


class RevokeInput(BaseModel):
    code: str = Field(min_length=1, max_length=40)

    @field_validator("code")
    @classmethod
    def check_code(cls, value):
        if not CODE.fullmatch(value):
            raise ValueError("编号须为 1–40 位大写字母、数字或连字符")
        return value


class ProbeInput(BaseModel):
    probes: list[str] = Field(min_length=1, max_length=100)
    review_date: str | None = None

    @field_validator("probes")
    @classmethod
    def check_probes(cls, probes):
        for value in probes:
            if not CODE.fullmatch(value):
                raise ValueError("编号须为 1–40 位大写字母、数字或连字符")
        if len(set(probes)) != len(probes):
            raise ValueError("同批点查编号不能重复")
        return probes

    @field_validator("review_date")
    @classmethod
    def check_date(cls, value):
        if value is None:
            return value
        from datetime import date
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("巡检日须用 YYYY-MM-DD") from exc
        return value


class CertificateEntry(BaseModel):
    code: str
    lift: str
    expires_on: str
    revoked: bool
    updated_seq: int
    updated_by: str
    updated_at: int


class VersionEntry(BaseModel):
    version: int
    status: str
    capacity: int
    applied_seq: int
    base_seq: int
    progress: int
    digest: str | None
    note: str | None
    created_at: int
    published_at: int | None


class DirectoryOverview(BaseModel):
    serving_version: int
    serving_capacity: int
    serving_digest: str | None
    serving_published_at: int | None
    certificate_count: int
    latest_seq: int
    rebuilding: dict | None
    versions: list[VersionEntry]


class ChangeEntry(BaseModel):
    seq: int
    code: str
    kind: str
    payload: dict
    actor: str
    created_at: int


class ProbeBatchEntry(BaseModel):
    id: int
    directory_version: int
    status: str
    review_date: str
    probes: list
    result: dict | None
    digest: str | None
    failure: str | None
    actor: str
    created_at: int
    finished_at: int | None
