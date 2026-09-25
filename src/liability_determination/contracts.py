"""案件、证据材料与责任认定草案的严格数据契约。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


PARTY_KINDS = {"driver", "pedestrian", "cyclist", "vehicle_owner", "other"}
MATERIAL_TYPES = {"statement", "trajectory", "scene", "regulation"}
FINDINGS = {"full", "primary", "equal", "secondary", "minor", "none"}

FINDING_LABELS = {
    "full": "全部责任",
    "primary": "主要责任",
    "equal": "同等责任",
    "secondary": "次要责任",
    "minor": "轻微责任",
    "none": "无责任",
}
MATERIAL_TYPE_LABELS = {
    "statement": "当事人陈述",
    "trajectory": "车辆轨迹",
    "scene": "现场证据",
    "regulation": "法规依据",
}


class ValidationError(ValueError):
    """输入不能满足领域契约。"""


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{path} 必须是对象")
    return value


def _sequence(value: object, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationError(f"{path} 必须是数组")
    return value


def text(value: object, path: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{path} 必须是非空字符串")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationError(f"{path} 不能超过 {maximum} 个字符")
    return result


def optional_text(value: object, path: str, maximum: int = 256) -> str | None:
    if value is None:
        return None
    return text(value, path, maximum)


def ratio(value: object, path: str) -> int:
    # 责任比例以整数百分点存储，杜绝 33.3 这类无法闭合的比例
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{path} 必须是 0 到 100 的整数百分点")
    if not 0 <= value <= 100:
        raise ValidationError(f"{path} 必须在 0 到 100 之间")
    return value


def parse_case(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "case_id": text(raw.get("case_id"), "case_id", 64),
        "case_number": text(raw.get("case_number"), "case_number", 64),
        "title": text(raw.get("title"), "title", 200),
        "occurred_at": text(raw.get("occurred_at"), "occurred_at", 40),
        "location": text(raw.get("location"), "location", 256),
        "casualties": bool(raw.get("casualties", False)),
        "evidence_anomaly": bool(raw.get("evidence_anomaly", False)),
    }


def parse_party(raw: Mapping[str, Any]) -> dict[str, Any]:
    kind = text(raw.get("kind"), "kind", 32)
    if kind not in PARTY_KINDS:
        raise ValidationError("kind 必须是 driver、pedestrian、cyclist、vehicle_owner 或 other")
    return {
        "party_id": text(raw.get("party_id"), "party_id", 64),
        "name": text(raw.get("name"), "name", 100),
        "kind": kind,
        "contact": optional_text(raw.get("contact"), "contact", 100),
    }


def parse_material(raw: Mapping[str, Any]) -> dict[str, Any]:
    material_type = text(raw.get("material_type"), "material_type", 32)
    if material_type not in MATERIAL_TYPES:
        raise ValidationError("material_type 必须是 statement、trajectory、scene 或 regulation")
    return {
        "material_id": text(raw.get("material_id"), "material_id", 64),
        "material_type": material_type,
        "title": text(raw.get("title"), "title", 200),
        "summary": text(raw.get("summary"), "summary", 4000),
        "source_ref": optional_text(raw.get("source_ref"), "source_ref", 200),
    }


def parse_party_finding(raw: object) -> dict[str, Any]:
    data = _mapping(raw, "party_findings")
    finding = text(data.get("finding"), "finding", 32)
    if finding not in FINDINGS:
        raise ValidationError("finding 必须是 full、primary、equal、secondary、minor 或 none")
    result = {
        "party_id": text(data.get("party_id"), "party_id", 64),
        "responsibility_ratio": ratio(data.get("responsibility_ratio"), "responsibility_ratio"),
        "finding": finding,
        "reasoning": text(data.get("reasoning"), "reasoning", 4000),
    }
    # 无责任必须是 0%；被判有责任必须大于 0%，避免比例与定性自相矛盾
    if finding == "none" and result["responsibility_ratio"] != 0:
        raise ValidationError("finding 为 none 时 responsibility_ratio 必须为 0")
    if finding != "none" and result["responsibility_ratio"] == 0:
        raise ValidationError("有责任定性的 responsibility_ratio 必须大于 0")
    return result


def parse_material_citation(raw: object) -> dict[str, Any]:
    data = _mapping(raw, "material_citations")
    return {
        "material_id": text(data.get("material_id"), "material_id", 64),
        "cited_note": (data.get("cited_note", "") or "").strip()[:1000],
    }


def parse_version_content(raw: Mapping[str, Any]) -> dict[str, Any]:
    basis_note = text(raw.get("basis_note"), "basis_note", 4000)
    findings = [parse_party_finding(item) for item in _sequence(raw.get("party_findings"), "party_findings")]
    citations = [parse_material_citation(item) for item in _sequence(raw.get("material_citations"), "material_citations")]
    if not findings:
        raise ValidationError("party_findings 至少要认定一方")
    party_ids = [item["party_id"] for item in findings]
    if len(set(party_ids)) != len(party_ids):
        raise ValidationError("party_findings 中当事方重复")
    material_ids = [item["material_id"] for item in citations]
    if len(set(material_ids)) != len(material_ids):
        raise ValidationError("material_citations 中证据材料重复")
    total = sum(item["responsibility_ratio"] for item in findings)
    if total != 100:
        raise ValidationError(f"各方责任比例之和必须为 100，当前为 {total}")
    return {"basis_note": basis_note, "party_findings": findings, "material_citations": citations}


def decimal_ratio_text(value: int) -> str:
    """供文本与摘要使用的百分比表示。"""
    return f"{Decimal(value).quantize(Decimal('1'))}%"
