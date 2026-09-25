"""责任认定决定与送达文本的确定性生成。

送达文本只有在版本走完所需签署层级、版本生效时生成，同一版本只生成一份，
因此文本内容必须完全由该版本的不可变数据决定。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from .contracts import FINDING_LABELS, MATERIAL_TYPE_LABELS


# 与“当事人陈述、车辆轨迹、现场证据、法规依据”的汇总顺序一致
MATERIAL_TYPE_ORDER = ("statement", "trajectory", "scene", "regulation")
PARTY_KIND_LABELS = {
    "driver": "机动车驾驶人",
    "pedestrian": "行人",
    "cyclist": "非机动车驾驶人",
    "vehicle_owner": "车辆所有人",
    "other": "其他当事方",
}


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def document_number(case_id: str, version_no: int, content_sha256: str) -> str:
    digest = hashlib.sha256(f"{case_id}|{version_no}|{content_sha256}".encode("utf-8")).hexdigest()[:10].upper()
    return f"LD-{case_id}-V{version_no}-{digest}"


def build_document_text(
    *,
    case: Mapping[str, Any],
    version_no: int,
    basis_note: str,
    party_findings: Sequence[Mapping[str, Any]],
    materials: Sequence[Mapping[str, Any]],
    signatures: Sequence[Mapping[str, Any]],
    effective_at: str,
) -> str:
    """根据生效版本的快照数据拼装《道路交通事故责任认定书》送达文本。"""

    lines: list[str] = []
    lines.append("道路交通事故责任认定书")
    lines.append("=" * 28)
    lines.append(f"案件编号：{case['case_number']}")
    lines.append(f"案件名称：{case['title']}")
    lines.append(f"事故时间：{case['occurred_at']}")
    lines.append(f"事故地点：{case['location']}")
    lines.append(f"认定版本：第 {version_no} 版")
    lines.append("")
    lines.append("一、事故与认定经过")
    lines.append(basis_note)
    lines.append("")
    lines.append("二、各方责任认定")
    for item in party_findings:
        lines.append(
            f"  {item['name']}（{PARTY_KIND_LABELS[item['kind']]}）：{FINDING_LABELS[item['finding']]}，"
            f"责任比例 {item['responsibility_ratio']}%。"
        )
        lines.append(f"    认定理由：{item['reasoning']}")
    lines.append("")
    lines.append("三、引用材料")
    order = {kind: index for index, kind in enumerate(MATERIAL_TYPE_ORDER)}
    ordered_materials = sorted(
        materials, key=lambda item: (order.get(item["material_type"], len(order)), item["material_id"])
    )
    for material in ordered_materials:
        ref = f"（来源：{material['source_ref']}）" if material.get("source_ref") else ""
        lines.append(
            f"  [{MATERIAL_TYPE_LABELS[material['material_type']]}] {material['title']}{ref}：{material['summary']}"
        )
    lines.append("")
    lines.append("四、签署意见")
    level_label = {"review": "复核人", "final": "负责人终审"}
    opinion_label = {"agree": "同意", "reject": "不同意"}
    for signature in signatures:
        comment = f"——{signature['comment']}" if signature.get("comment") else ""
        lines.append(
            f"  {level_label[signature['level']]} {signature['signer_id']}："
            f"{opinion_label[signature['opinion']]}（{signature['signed_at']}）{comment}"
        )
    lines.append("")
    lines.append(f"本认定自 {effective_at} 起生效并送达。")
    return "\n".join(lines)
