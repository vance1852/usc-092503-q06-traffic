"""责任认定的领域规则：责任比例、责任等级、层级要求与送达文本。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence


PARTY_KINDS = {
    "driver": "机动车驾驶人",
    "rider": "非机动车驾驶人",
    "pedestrian": "行人",
    "passenger": "乘车人",
    "vehicle_owner": "车辆所有人",
    "other": "其他当事人",
}

MATERIAL_CATEGORIES = {
    "party_statement": "当事人陈述",
    "vehicle_trajectory": "车辆轨迹",
    "scene_evidence": "现场证据",
    "regulation_basis": "法规依据",
}

TRIGGER_REASONS = {
    "supplement_evidence": "补充证据",
    "liability_share_changed": "修改责任比例",
    "review_returned": "复核退回后修订",
    "other": "其他修订",
}

LEVEL_LABELS = {
    "full": "全部责任",
    "primary": "主要责任",
    "equal": "同等责任",
    "secondary": "次要责任",
    "none": "无责任",
}

TOTAL_SHARE = Decimal("100")


class LiabilityValidationError(ValueError):
    """责任认定输入不满足领域契约。"""


def parse_share(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise LiabilityValidationError(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LiabilityValidationError(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise LiabilityValidationError(f"{field} 必须是有限数值")
    if result < 0 or result > TOTAL_SHARE:
        raise LiabilityValidationError(f"{field} 必须在 0 到 100 之间")
    return result.quantize(Decimal("0.01"))


def share_text(value: Decimal) -> str:
    """统一比例文本：去掉多余尾零但保留两位以内精度。"""
    return format(value.quantize(Decimal("0.01")).normalize(), "f")


def validate_party_shares(
    party_ids: Sequence[str], share_rows: Sequence[Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """校验每一方的责任比例：覆盖全部当事人、比例合法、合计 100、理由非空。"""
    if not share_rows:
        raise LiabilityValidationError("party_shares 不能为空")
    result: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(share_rows):
        party_id = str(row.get("party_id", "")).strip()
        if not party_id:
            raise LiabilityValidationError(f"party_shares[{index}].party_id 不能为空")
        if party_id not in set(party_ids):
            raise LiabilityValidationError(f"party_shares[{index}] 引用了未登记的当事人 {party_id}")
        basis = row.get("basis")
        if not isinstance(basis, str) or not basis.strip():
            raise LiabilityValidationError(f"party_shares[{index}].basis 必须说明认定理由")
        result[party_id] = {
            "share_percent": parse_share(row.get("share_percent"), f"party_shares[{index}].share_percent"),
            "basis": basis.strip(),
        }
    if set(result) != set(party_ids):
        missing = sorted(set(party_ids) - set(result))
        raise LiabilityValidationError(f"以下当事人缺少责任认定: {missing}")
    total = sum((item["share_percent"] for item in result.values()), Decimal(0))
    if total != TOTAL_SHARE:
        raise LiabilityValidationError(f"各方责任比例合计必须为 100，当前为 {share_text(total)}")
    if not any(item["share_percent"] > 0 for item in result.values()):
        raise LiabilityValidationError("至少一方应当承担责任")
    return result


def liability_level(share: Decimal, all_shares: Sequence[Decimal]) -> str:
    """按比例确定性推导中文责任等级。"""
    if share == 0:
        return "none"
    positive = [item for item in all_shares if item > 0]
    if len(positive) == 1:
        return "full"
    if all(item == positive[0] for item in positive):
        return "equal"
    if share == max(positive):
        return "primary"
    return "secondary"


def final_review_required(has_casualty: bool, evidence_chain_anomaly: bool) -> bool:
    """人员伤亡或证据链异常时，负责人必须终审。"""
    return has_casualty or evidence_chain_anomaly


@dataclass(frozen=True, slots=True)
class MaterialRef:
    category: str
    reference: str
    title: str
    detail: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], index: int) -> "MaterialRef":
        category = str(raw.get("category", "")).strip()
        if category not in MATERIAL_CATEGORIES:
            raise LiabilityValidationError(
                f"materials[{index}].category 必须是 {sorted(MATERIAL_CATEGORIES)} 之一"
            )
        reference = str(raw.get("reference", "")).strip()
        title = str(raw.get("title", "")).strip()
        if not reference:
            raise LiabilityValidationError(f"materials[{index}].reference 不能为空")
        if not title:
            raise LiabilityValidationError(f"materials[{index}].title 不能为空")
        detail = str(raw.get("detail") or "").strip()
        return cls(category=category, reference=reference, title=title, detail=detail)


def validate_materials(raw_materials: Any) -> tuple[MaterialRef, ...]:
    if not isinstance(raw_materials, list) or not raw_materials:
        raise LiabilityValidationError("materials 必须是非空数组")
    return tuple(MaterialRef.from_dict(item, index) for index, item in enumerate(raw_materials))


def ensure_material_coverage(categories: Iterable[str]) -> None:
    """提交前必须汇总齐当事人陈述、车辆轨迹、现场证据和法规依据四类材料。"""
    covered = set(categories)
    missing = [label for key, label in MATERIAL_CATEGORIES.items() if key not in covered]
    if missing:
        raise LiabilityValidationError(f"认定材料不完整，缺少: {'、'.join(missing)}")


def render_service_document(
    *,
    document_no: str,
    accident: Mapping[str, Any],
    version_no: int,
    conclusion: str,
    parties: Sequence[Mapping[str, Any]],
    shares: Mapping[str, Mapping[str, Any]],
    materials: Sequence[Mapping[str, Any]],
    effective_at: str,
) -> str:
    """生成字段顺序固定的送达文本，保证同一版本重复获取得到同一文本。"""
    lines: list[str] = [
        "道路交通事故责任认定书（送达文本）",
        f"文档编号：{document_no}",
        f"案件编号：{accident['accident_id']}",
        f"事故时间：{accident['occurred_at']}",
        f"事故地点：{accident['location']}",
        f"认定版本：第 {version_no} 版",
        f"生效时间：{effective_at}",
        "",
        "一、当事人责任",
    ]
    ordered = sorted(parties, key=lambda item: item["party_id"])
    all_shares = [shares[item["party_id"]]["share_percent"] for item in ordered]
    for index, party in enumerate(ordered, start=1):
        item = shares[party["party_id"]]
        level = liability_level(item["share_percent"], all_shares)
        kind_label = PARTY_KINDS.get(party["party_kind"], party["party_kind"])
        lines.append(
            f"{index}. {party['name']}（{kind_label}）：责任比例 {share_text(item['share_percent'])}%，{LEVEL_LABELS[level]}"
        )
        lines.append(f"   认定理由：{item['basis']}")
    lines.append("")
    lines.append("二、引用材料")
    ordered_materials = sorted(materials, key=lambda item: (item["category"], item["material_id"]))
    current_category: str | None = None
    for material in ordered_materials:
        if material["category"] != current_category:
            current_category = material["category"]
            lines.append(f"【{MATERIAL_CATEGORIES[current_category]}】")
        suffix = f"——{material['detail']}" if material["detail"] else ""
        lines.append(f"- {material['title']}（引用：{material['reference']}）{suffix}".rstrip())
    lines.append("")
    lines.append("三、认定结论")
    lines.append(conclusion)
    return "\n".join(lines)
