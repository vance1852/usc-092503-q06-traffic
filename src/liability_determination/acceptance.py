"""责任认定完整产品流程的离线验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .service import LiabilityDeterminationService
from .storage import connect, inspect_schema


def _materials() -> list[dict[str, object]]:
    return [
        {"material_id": "stmt-a", "material_type": "statement", "title": "甲车驾驶人陈述",
         "summary": "承认通过路口时未注意让行", "source_ref": "询问笔录 2026-001"},
        {"material_id": "stmt-b", "material_type": "statement", "title": "乙车驾驶人陈述",
         "summary": "称自己正常通行，甲车突然出现", "source_ref": "询问笔录 2026-002"},
        {"material_id": "trajectory-ab", "material_type": "trajectory", "title": "两车行驶轨迹鉴定",
         "summary": "甲车进入路口前未减速，乙车制动痕迹 12 米", "source_ref": "轨迹鉴定书 TR-77"},
        {"material_id": "scene-1", "material_type": "scene", "title": "现场勘验笔录与照片",
         "summary": "碰撞点位于路口中央偏东，信号灯工作正常", "source_ref": "现场卷 SC-09"},
        {"material_id": "reg-let", "material_type": "regulation", "title": "道路交通安全法相关条款",
         "summary": "第四十四条：机动车通过交叉路口应减速慢行并让行", "source_ref": "法规摘录"},
    ]


def _content(ratios: dict[str, int], citations: list[str], note: str) -> dict[str, object]:
    findings = []
    for party_id, value in ratios.items():
        findings.append({
            "party_id": party_id,
            "responsibility_ratio": value,
            "finding": "primary" if value > 50 else ("equal" if value == 50 else "secondary"),
            "reasoning": f"{party_id} 责任比例 {value}% 的综合认定",
        })
    return {
        "basis_note": note,
        "party_findings": findings,
        "material_citations": [{"material_id": mid} for mid in citations],
    }


def run(workspace: Path | None = None) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="liability-determination-") as temporary:
        database = Path(temporary) / "liability.sqlite3"
        connection = connect(database)
        try:
            service = LiabilityDeterminationService(connection)
            service.create_user("officer-1", "主办民警", "investigator")
            service.create_user("reviewer-1", "复核人员", "reviewer")
            service.create_user("chief-1", "事故处理大队负责人", "chief")
            service.create_user("auditor-1", "审计人员", "auditor")

            # 涉人员伤亡案件：需要复核 + 负责人终审两级
            service.create_case("officer-1", {
                "case_id": "case-2026-009",
                "case_number": "2026-交事认-009",
                "title": "甲乙两车路口碰撞事故",
                "occurred_at": "2026-09-20T08:10:00+08:00",
                "location": "示例市示例大道与环城路路口",
                "casualties": True,
                "evidence_anomaly": False,
            })
            service.add_party("officer-1", "case-2026-009", {
                "party_id": "driver-a", "name": "张某", "kind": "driver", "contact": "13800000001"})
            service.add_party("officer-1", "case-2026-009", {
                "party_id": "driver-b", "name": "李某", "kind": "driver", "contact": "13800000002"})
            for material in _materials():
                service.add_material("officer-1", "case-2026-009", material)

            citations_v1 = ["stmt-a", "stmt-b", "trajectory-ab", "scene-1", "reg-let"]
            service.create_draft("officer-1", "case-2026-009", _content(
                {"driver-a": 70, "driver-b": 30}, citations_v1,
                "初版认定：依据陈述、轨迹、现场证据及法规条款综合认定。"))
            service.submit_draft("officer-1", "case-2026-009")
            after_review = service.sign("reviewer-1", "case-2026-009", "review", "agree", "事实清楚，同意")
            assert not after_review["version"]["effective"], "仅复核同意时不应生效"
            service.sign("chief-1", "case-2026-009", "final", "agree", "终审同意")
            v1 = service.get_version("auditor-1", "case-2026-009", 1)
            assert v1["version"]["effective"], "两级签署完成应当生效"
            document_v1_no = v1["document"]["document_no"]

            # 同一人重复签署不产生第二份决定
            service.sign("chief-1", "case-2026-009", "final", "agree", "再次确认")
            document_count = connection.execute("SELECT count(*) FROM determination_effects").fetchone()[0]
            assert document_count == 1, "重复签署不能生成第二份决定"

            # 补充证据并修改责任比例 → 修订为第 2 版
            service.add_material("officer-1", "case-2026-009", {
                "material_id": "witness-c", "material_type": "statement",
                "title": "目击证人陈述", "summary": "证人称乙车通过路口时车速明显偏快",
                "source_ref": "证人笔录 2026-003"})
            service.revise_draft("officer-1", "case-2026-009", _content(
                {"driver-a": 60, "driver-b": 40}, citations_v1 + ["witness-c"],
                "补充目击证人陈述后，调整双方责任比例。"), "补充证人陈述，乙车责任比例上调")
            service.submit_draft("officer-1", "case-2026-009")
            service.sign("reviewer-1", "case-2026-009", "review", "agree", "补充材料后同意")
            service.sign("chief-1", "case-2026-009", "final", "agree", "终审同意修订")
            current = service.get_determination("auditor-1", "case-2026-009")
            history = current["version_history"]
            old_version = service.get_version("auditor-1", "case-2026-009", 1)
            schema = inspect_schema(connection)
        finally:
            connection.close()

    checks = {
        "current_version_no": current["current_version_no"],
        "version_count": len(history),
        "current_effective": current["version"]["effective"],
        "v1_effect_kept_but_superseded": (
            old_version["document"]["document_no"] == document_v1_no
            and old_version["document"]["superseded_at"] is not None
        ),
        "v1_signatures_preserved": len(old_version["version"]["signatures"]) == 2,
        "v1_signatures_superseded": all(
            item["status"] == "superseded" for item in old_version["version"]["signatures"]
        ),
        "documents_total": document_count + 1,
        "timeline_events": len(current["timeline"]),
        "explains_parties": {item["party_id"]: item["responsibility_ratio"] for item in current["parties"]},
        "explains_materials": sum(1 for item in current["materials"] if item["cited"]),
    }
    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 基础结构检查失败")
    if not all(value is True or isinstance(value, (int, str, dict)) for value in checks.values()):
        raise RuntimeError(f"验收断言失败: {checks}")
    return {
        "status": "ok",
        "case_id": "case-2026-009",
        "document_no_v2": current["document"]["document_no"],
        "schema": schema,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行交通事故责任认定服务的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
