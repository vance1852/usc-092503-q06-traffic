"""责任认定草案、分级签署、版本历史与送达文本的离线验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .service import LiabilityDeterminationService
from .storage import connect, inspect_schema


def _materials(statement: str) -> list[dict[str, str]]:
    return [
        {"category": "party_statement", "reference": "STMT-001", "title": "当事人陈述笔录", "detail": statement},
        {"category": "vehicle_trajectory", "reference": "TRAJ-001", "title": "车辆行驶轨迹鉴定", "detail": "事发前 30 秒轨迹"},
        {"category": "scene_evidence", "reference": "SCENE-001", "title": "现场勘验图与照片", "detail": "刹车痕 12.4 米"},
        {"category": "regulation_basis", "reference": "LAW-ROAD-22", "title": "道路交通安全法第二十二条", "detail": "安全驾驶义务"},
    ]


def _payload(conclusion: str, shares: dict[str, str]) -> dict[str, object]:
    return {
        "conclusion": conclusion,
        "party_shares": [
            {"party_id": party_id, "share_percent": value, "basis": f"{party_id} 责任认定依据"}
            for party_id, value in shares.items()
        ],
        "materials": _materials("首次询问笔录"),
    }


def run(workspace: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="liability-determination-") as temporary:
        database = Path(temporary) / "liability.sqlite3"
        connection = connect(database)
        try:
            service = LiabilityDeterminationService(connection)
            service.create_user("police-a", "主办民警甲", "investigator")
            service.create_user("police-b", "复核民警乙", "reviewer")
            service.create_user("chief-1", "大队负责人丙", "chief")
            service.create_user("auditor-1", "审计人员丁", "auditor")

            # 案件一：无人身伤亡，主办提交 + 复核签署即生效。
            service.register_accident("police-a", {
                "accident_id": "ACC-PLAIN",
                "occurred_at": "2026-09-20T08:30:00+08:00",
                "location": "北环快速路东向西 3 公里处",
                "summary": "两车追尾，仅车损。",
                "has_casualty": False,
            })
            service.add_party("police-a", "ACC-PLAIN", "P1", "张某", "driver")
            service.add_party("police-a", "ACC-PLAIN", "P2", "李某", "driver")
            service.create_draft("police-a", "ACC-PLAIN", _payload("追尾事故，后车全责。", {"P1": "100", "P2": "0"}))
            service.submit_draft("police-a", "LD-ACC-PLAIN", "事实清楚，建议后车全部责任。")
            service.review_draft("police-b", "LD-ACC-PLAIN", True, "证据链完整，同意主办意见。")
            document = service.service_document("police-a", "LD-ACC-PLAIN")
            replay = service.service_document("police-a", "LD-ACC-PLAIN")
            if not replay["already_generated"] or replay["document_id"] != document["document_id"]:
                raise RuntimeError("重复生成送达文本必须返回同一份决定")

            # 案件二：有人伤，需复核 + 负责人终审；先退回再修订。
            service.register_accident("police-a", {
                "accident_id": "ACC-INJURY",
                "occurred_at": "2026-09-21T19:05:00+08:00",
                "location": "建设路与和平路交叉口",
                "summary": "机动车与电动自行车碰撞，骑车人受伤。",
                "has_casualty": True,
            })
            service.add_party("police-a", "ACC-INJURY", "D1", "王某", "driver")
            service.add_party("police-a", "ACC-INJURY", "R1", "赵某", "rider")
            service.create_draft("police-a", "ACC-INJURY", _payload("初步判断主次责任。", {"D1": "70", "R1": "30"}))
            service.submit_draft("police-a", "LD-ACC-INJURY", "提交初步认定。")
            service.review_draft("police-b", "LD-ACC-INJURY", False, "伤者伤情鉴定未附，退回补充。")
            revised_payload = _payload("补充伤情鉴定后维持主次责任比例。", {"D1": "70", "R1": "30"})
            revised_payload["materials"][0]["detail"] = "第二次询问笔录，补充伤情鉴定意见"
            service.revise_draft(
                "police-a", "LD-ACC-INJURY", revised_payload,
                trigger_reason="review_returned", change_note="按复核意见补充伤情鉴定",
            )
            service.submit_draft("police-a", "LD-ACC-INJURY", "已补充材料，重新提交。")
            service.review_draft("police-b", "LD-ACC-INJURY", True, "材料齐备，同意。")
            before_final = service.explanation("police-b", "LD-ACC-INJURY")
            if before_final["effective"] or before_final["missing_levels"] != ["final"]:
                raise RuntimeError("伤亡案件在负责人终审前不应生效")
            service.final_sign("chief-1", "LD-ACC-INJURY", "同意终审意见。")

            # 案件一生效后修改责任比例：新版本生效需重新走完层级，旧签署失效但保留。
            changed = _payload("调取新轨迹后改定同等责任。", {"P1": "50", "P2": "50"})
            service.revise_draft(
                "police-a", "LD-ACC-PLAIN", changed,
                trigger_reason="liability_share_changed", change_note="新轨迹证据显示前车有变道行为",
            )
            service.submit_draft("police-a", "LD-ACC-PLAIN", "按新轨迹重新提交。")
            service.review_draft("police-b", "LD-ACC-PLAIN", True, "认可新轨迹，同意同等责任。")
            service.service_document("police-a", "LD-ACC-PLAIN")

            explanation = service.explanation("auditor-1", "LD-ACC-PLAIN")
            history = service.history("auditor-1", "LD-ACC-PLAIN")
            injury = service.explanation("auditor-1", "LD-ACC-INJURY")
            schema = inspect_schema(connection)
        finally:
            connection.close()
    invalidated = [
        signing
        for version in history["versions"]
        for signing in version["signings"]
        if signing["status"] == "invalidated"
    ]
    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 基础结构检查失败")
    if not invalidated or any(signing["opinion"] == "" for signing in invalidated):
        raise RuntimeError("旧签署必须保留原始意见并标记失效原因")
    return {
        "status": "ok",
        "plain_case": {
            "current_version_no": explanation["current_version_no"],
            "state": explanation["state"],
            "shares": [(item["name"], item["share_percent"], item["liability_level_label"])
                       for item in explanation["party_liabilities"]],
            "document_no": explanation["service_document"]["document_no"],
        },
        "injury_case": {
            "current_version_no": injury["current_version_no"],
            "state": injury["state"],
            "required_levels": injury["required_levels"],
            "timeline_events": len(injury["status_timeline"]),
        },
        "history_versions": len(history["versions"]),
        "invalidated_signings": len(invalidated),
        "schema": schema,
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
