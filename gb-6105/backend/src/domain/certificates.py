import re
from dataclasses import dataclass
from datetime import date

CODE = re.compile(r"[A-Z0-9][A-Z0-9-]{0,39}")


@dataclass(frozen=True)
class Certificate:
    code: str
    lift: str
    expires_on: date
    revoked: bool

    def verdict(self, review_date):
        if self.revoked:
            return "已撤销"
        if review_date >= self.expires_on:
            return "已到期"
        return "有效"

    def snapshot(self):
        return {
            "code": self.code, "lift": self.lift,
            "expires_on": self.expires_on.isoformat(), "revoked": self.revoked,
        }

    @classmethod
    def from_snapshot(cls, data):
        return cls(
            code=data["code"], lift=data["lift"],
            expires_on=date.fromisoformat(data["expires_on"]), revoked=bool(data["revoked"]),
        )


def _code(value):
    if not isinstance(value, str) or not CODE.fullmatch(value):
        raise ValueError("编号须为 1–40 位大写字母、数字或连字符")
    return value


def _date(value):
    try:
        day = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("巡检日与失效日须用 YYYY-MM-DD") from exc
    if day.isoformat() != value:
        raise ValueError("巡检日与失效日须用 YYYY-MM-DD")
    return day


def build_certificate(code, lift, expires_on, revoked):
    if type(revoked) is not bool:
        raise ValueError("撤销标记须为布尔值")
    return Certificate(_code(code), _code(lift), _date(expires_on), revoked)


def parse(payload):
    certificates, probes = payload.get("certificates"), payload.get("probes")
    if not isinstance(certificates, list) or not 1 <= len(certificates) <= 400:
        raise ValueError("certificates 须有 1–400 条证书")
    if not isinstance(probes, list) or not 1 <= len(probes) <= 100:
        raise ValueError("probes 须有 1–100 条检证编号")
    result = []
    for item in certificates:
        if not isinstance(item, dict) or set(item) != {"code", "lift", "expires_on", "revoked"}:
            raise ValueError("证书须包含 code、lift、expires_on、revoked")
        result.append(build_certificate(
            item["code"], item["lift"], item["expires_on"], item["revoked"],
        ))
    if len({record.code for record in result}) != len(result):
        raise ValueError("证书档案含重复编号")
    return result, [_code(probe) for probe in probes], _date(payload.get("review_date"))


def validate(payload):
    parse(payload)
