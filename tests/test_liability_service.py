from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from liability_determination.clock import FrozenClock
from liability_determination.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from liability_determination.liability import liability_level
from liability_determination.service import LiabilityDeterminationService


def materials(detail: str = "首次询问笔录") -> list[dict[str, str]]:
    return [
        {"category": "party_statement", "reference": "STMT-1", "title": "当事人陈述笔录", "detail": detail},
        {"category": "vehicle_trajectory", "reference": "TRAJ-1", "title": "车辆轨迹鉴定", "detail": "事发前 30 秒"},
        {"category": "scene_evidence", "reference": "SCENE-1", "title": "现场勘验照片", "detail": "刹车痕 12.4 米"},
        {"category": "regulation_basis", "reference": "LAW-22", "title": "道路交通安全法第二十二条", "detail": "安全驾驶义务"},
    ]


def payload(conclusion: str, shares: dict[str, str], mats=None) -> dict[str, object]:
    return {
        "conclusion": conclusion,
        "party_shares": [
            {"party_id": party_id, "share_percent": value, "basis": f"{party_id} 的认定依据"}
            for party_id, value in shares.items()
        ],
        "materials": mats if mats is not None else materials(),
    }


class LiabilityServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = LiabilityDeterminationService(self.connection, self.clock)
        for user_id, name, role in (
            ("police-a", "主办民警甲", "investigator"),
            ("police-b", "复核民警乙", "reviewer"),
            ("chief-1", "大队负责人丙", "chief"),
            ("auditor-1", "审计人员丁", "auditor"),
        ):
            self.service.create_user(user_id, name, role)

    def tearDown(self) -> None:
        self.connection.close()

    def _register(self, accident_id: str = "ACC-1", *, casualty: bool = False, anomaly: bool = False) -> None:
        self.service.register_accident("police-a", {
            "accident_id": accident_id,
            "occurred_at": "2026-09-20T08:30:00+08:00",
            "location": "北环快速路",
            "summary": "测试事故",
            "has_casualty": casualty,
            "evidence_chain_anomaly": anomaly,
        })
        self.service.add_party("police-a", accident_id, "P1", "张某", "driver")
        self.service.add_party("police-a", accident_id, "P2", "李某", "driver")

    def _draft(self, accident_id: str = "ACC-1", shares=None) -> dict:
        return self.service.create_draft(
            "police-a", accident_id,
            payload("后车追尾，全责。", shares or {"P1": "100", "P2": "0"}),
        )

    # ---------- 输入契约 ----------

    def test_shares_must_total_one_hundred(self) -> None:
        self._register()
        with self.assertRaises(ValidationFailed):
            self._draft(shares={"P1": "70", "P2": "20"})

    def test_submit_requires_all_four_material_categories(self) -> None:
        self._register()
        incomplete = payload("结论", {"P1": "100", "P2": "0"}, mats=materials()[:3])
        version = self.service.create_draft("police-a", "ACC-1", incomplete)
        self.assertEqual(version["state"], "draft")
        with self.assertRaises(ValidationFailed):
            self.service.submit_draft("police-a", "LD-ACC-1", "提交但缺材料")

    def test_liability_level_derivation(self) -> None:
        self.assertEqual(liability_level(Decimal("0"), [Decimal("100"), Decimal("0")]), "none")
        self.assertEqual(liability_level(Decimal("100"), [Decimal("100"), Decimal("0")]), "full")
        self.assertEqual(liability_level(Decimal("50"), [Decimal("50"), Decimal("50")]), "equal")
        self.assertEqual(liability_level(Decimal("70"), [Decimal("70"), Decimal("30")]), "primary")
        self.assertEqual(liability_level(Decimal("30"), [Decimal("70"), Decimal("30")]), "secondary")

    # ---------- 普通两级流程 ----------

    def test_submit_then_review_makes_version_effective(self) -> None:
        self._register()
        self._draft()
        submitted = self.service.submit_draft("police-a", "LD-ACC-1", "事实清楚")
        self.assertEqual(submitted["state"], "submitted")
        self.assertEqual(submitted["missing_levels"], ["review"])
        reviewed = self.service.review_draft("police-b", "LD-ACC-1", True, "同意")
        self.assertEqual(reviewed["state"], "effective")
        self.assertEqual(reviewed["missing_levels"], [])
        self.assertIsNotNone(reviewed["effective_at"])

    def test_duplicate_signatures_do_not_add_decisions(self) -> None:
        self._register()
        self._draft()
        self.service.submit_draft("police-a", "LD-ACC-1", "提交意见")
        first = self.service.review_draft("police-b", "LD-ACC-1", True, "同意")
        replay = self.service.review_draft("police-b", "LD-ACC-1", True, "又签一次")
        self.assertTrue(replay["already_signed"])
        self.assertEqual(first["signings"], replay["signings"])
        count = self.connection.execute(
            "SELECT count(*) FROM version_signings WHERE level='review'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_reviewer_cannot_be_submitting_investigator(self) -> None:
        self._register()
        self._draft()
        self.service.submit_draft("police-a", "LD-ACC-1", "提交")
        with self.assertRaises(Forbidden):
            self.service.review_draft("police-a", "LD-ACC-1", True, "自审")

    def test_role_separation(self) -> None:
        self._register()
        self._draft()
        with self.assertRaises(Forbidden):
            self.service.submit_draft("police-b", "LD-ACC-1", "复核员不能提交")
        self.service.submit_draft("police-a", "LD-ACC-1", "提交")
        with self.assertRaises(Forbidden):
            self.service.review_draft("police-a", "LD-ACC-1", True, "民警不能复核")

    # ---------- 终审 ----------

    def test_casualty_case_requires_chief_final_signature(self) -> None:
        self._register(casualty=True)
        self._draft()
        self.service.submit_draft("police-a", "LD-ACC-1", "提交")
        reviewed = self.service.review_draft("police-b", "LD-ACC-1", True, "同意，报终审")
        self.assertEqual(reviewed["state"], "signed")
        self.assertEqual(reviewed["required_levels"], ["submit", "review", "final"])
        self.assertEqual(reviewed["missing_levels"], ["final"])
        explanation = self.service.explanation("police-b", "LD-ACC-1")
        self.assertFalse(explanation["effective"])
        with self.assertRaises(InvalidState):
            self.service.service_document("police-b", "LD-ACC-1")
        finalized = self.service.final_sign("chief-1", "LD-ACC-1", "终审同意")
        self.assertEqual(finalized["state"], "effective")
        self.assertEqual(finalized["missing_levels"], [])

    def test_evidence_chain_anomaly_also_triggers_final_review(self) -> None:
        self._register(anomaly=True)
        self._draft()
        self.service.submit_draft("police-a", "LD-ACC-1", "提交")
        self.service.review_draft("police-b", "LD-ACC-1", True, "同意")
        detail = self.service.version_detail("auditor-1", "LD-ACC-1", 1)
        self.assertTrue(detail["final_required"])
        self.assertEqual(detail["state"], "signed")
        self.service.final_sign("chief-1", "LD-ACC-1", "终审同意")

    def test_final_signature_rejected_when_not_required(self) -> None:
        self._register()
        self._draft()
        self.service.submit_draft("police-a", "LD-ACC-1", "提交")
        self.service.review_draft("police-b", "LD-ACC-1", True, "同意")
        with self.assertRaises(InvalidState):
            self.service.final_sign("chief-1", "LD-ACC-1", "普通案件无需终审")

    def test_reviewer_and_chief_are_distinct_roles(self) -> None:
        self._register(casualty=True)
        self._draft()
        self.service.submit_draft("police-a", "LD-ACC-1", "提交")
        self.service.review_draft("police-b", "LD-ACC-1", True, "复核同意")
        # reviewer 角色无权终审，chief 角色无权复核
        with self.assertRaises(Forbidden):
            self.service.final_sign("police-b", "LD-ACC-1", "复核员终审")
        self.service.final_sign("chief-1", "LD-ACC-1", "终审同意")

    # ---------- 退回与修订 ----------

    def test_review_return_creates_new_version_and_invalidates_old_signings(self) -> None:
        self._register()
        self._draft()
        self.service.submit_draft("police-a", "LD-ACC-1", "提交意见")
        self.service.review_draft("police-b", "LD-ACC-1", False, "材料矛盾，退回")
        revised = payload("补充后结论", {"P1": "100", "P2": "0"}, materials("第二次询问笔录"))
        new_version = self.service.revise_draft(
            "police-a", "LD-ACC-1", revised,
            trigger_reason="review_returned", change_note="按退回意见修订",
        )
        self.assertEqual(new_version["version_no"], 2)
        self.assertEqual(new_version["state"], "draft")
        history = self.service.history("auditor-1", "LD-ACC-1")
        self.assertEqual(history["current_version_no"], 2)
        v1, v2 = history["versions"]
        self.assertEqual(v1["state"], "superseded")
        self.assertEqual(v2["state"], "draft")
        # 旧意见逐字保留，但全部标记失效
        old_signings = v1["signings"]
        self.assertTrue(all(s["status"] == "invalidated" for s in old_signings))
        self.assertEqual({s["opinion"] for s in old_signings}, {"提交意见", "材料矛盾，退回"})
        for signing in old_signings:
            self.assertIn("退回", signing["invalidated_reason"])
            self.assertIsNotNone(signing["invalidated_at"])

    def test_returned_revision_requires_review_returned_trigger(self) -> None:
        self._register()
        self._draft()
        self.service.submit_draft("police-a", "LD-ACC-1", "提交")
        self.service.review_draft("police-b", "LD-ACC-1", False, "退回")
        with self.assertRaises(ValidationFailed):
            self.service.revise_draft(
                "police-a", "LD-ACC-1", payload("结论", {"P1": "100", "P2": "0"}),
                trigger_reason="supplement_evidence",
            )

    def test_share_change_after_effective_invalidates_signings(self) -> None:
        self._register()
        self._draft()
        self.service.submit_draft("police-a", "LD-ACC-1", "v1 提交")
        self.service.review_draft("police-b", "LD-ACC-1", True, "v1 同意")
        doc1 = self.service.service_document("police-a", "LD-ACC-1")
        self.service.revise_draft(
            "police-a", "LD-ACC-1", payload("改定同等责任", {"P1": "50", "P2": "50"}),
            trigger_reason="liability_share_changed", change_note="新轨迹出现",
        )
        history = self.service.history("auditor-1", "LD-ACC-1")
        v1 = history["versions"][0]
        self.assertEqual(v1["state"], "superseded")
        self.assertTrue(all(s["status"] == "invalidated" for s in v1["signings"]))
        # 新版本未走完层级前不生效，旧送达文本不再是当前决定
        explanation = self.service.explanation("auditor-1", "LD-ACC-1")
        self.assertFalse(explanation["effective"])
        self.assertEqual(explanation["state"], "draft")
        self.assertIsNone(explanation["service_document"])
        # v1 的送达文本仍然留档可查
        retained = self.connection.execute(
            "SELECT document_no FROM service_documents WHERE version_id=?", (v1["version_id"],)
        ).fetchall()
        self.assertEqual([row[0] for row in retained], [doc1["document_no"]])
        self.service.submit_draft("police-a", "LD-ACC-1", "v2 提交")
        self.service.review_draft("police-b", "LD-ACC-1", True, "v2 同意")
        doc2 = self.service.service_document("police-a", "LD-ACC-1")
        self.assertNotEqual(doc1["document_id"], doc2["document_id"])
        self.assertIn("同等责任", doc2["document_text"])

    def test_supplement_evidence_requires_detected_change(self) -> None:
        self._register()
        self._draft()
        self.service.submit_draft("police-a", "LD-ACC-1", "提交")
        self.service.review_draft("police-b", "LD-ACC-1", True, "同意")
        same = payload("相同内容", {"P1": "100", "P2": "0"})
        with self.assertRaises(ValidationFailed):
            self.service.revise_draft(
                "police-a", "LD-ACC-1", same, trigger_reason="supplement_evidence"
            )

    def test_cannot_revise_while_submission_is_in_flight(self) -> None:
        self._register()
        self._draft()
        self.service.submit_draft("police-a", "LD-ACC-1", "提交")
        with self.assertRaises(InvalidState):
            self.service.revise_draft(
                "police-a", "LD-ACC-1", payload("结论", {"P1": "100", "P2": "0"}),
                trigger_reason="other",
            )

    def test_editorial_revision_without_share_or_material_change_keeps_signings(self) -> None:
        self._register()
        self._draft()
        self.service.submit_draft("police-a", "LD-ACC-1", "提交")
        self.service.review_draft("police-b", "LD-ACC-1", True, "同意")
        self.service.revise_draft(
            "police-a", "LD-ACC-1", payload("仅润色结论文字", {"P1": "100", "P2": "0"}),
            trigger_reason="other", change_note="文字润色",
        )
        v1 = self.service.version_detail("auditor-1", "LD-ACC-1", 1)
        self.assertTrue(all(s["status"] == "valid" for s in v1["signings"]))

    # ---------- 送达文本 ----------

    def test_document_only_for_effective_current_version_and_is_stable(self) -> None:
        self._register()
        self._draft()
        with self.assertRaises(InvalidState):
            self.service.service_document("police-a", "LD-ACC-1")
        self.service.submit_draft("police-a", "LD-ACC-1", "提交")
        with self.assertRaises(InvalidState):
            self.service.service_document("police-a", "LD-ACC-1")
        self.service.review_draft("police-b", "LD-ACC-1", True, "同意")
        first = self.service.service_document("police-a", "LD-ACC-1")
        second = self.service.service_document("auditor-1", "LD-ACC-1")
        self.assertTrue(second["already_generated"])
        self.assertEqual(first["document_id"], second["document_id"])
        self.assertEqual(first["document_text"], second["document_text"])
        self.assertEqual(first["content_sha256"], second["content_sha256"])
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM service_documents").fetchone()[0], 1
        )

    # ---------- 解释查询 ----------

    def test_explanation_covers_parties_materials_and_status_changes(self) -> None:
        self._register(casualty=True)
        self._draft()
        self.service.submit_draft("police-a", "LD-ACC-1", "提交")
        self.service.review_draft("police-b", "LD-ACC-1", True, "同意")
        self.service.final_sign("chief-1", "LD-ACC-1", "终审同意")
        view = self.service.explanation("auditor-1", "LD-ACC-1")
        self.assertTrue(view["effective"])
        parties = {item["party_id"]: item for item in view["party_liabilities"]}
        self.assertEqual(parties["P1"]["liability_level"], "full")
        self.assertEqual(parties["P1"]["liability_level_label"], "全部责任")
        self.assertEqual(parties["P2"]["liability_level_label"], "无责任")
        self.assertTrue(parties["P1"]["basis"])
        self.assertEqual(set(view["cited_materials"]),
                         {"party_statement", "vehicle_trajectory", "scene_evidence", "regulation_basis"})
        self.assertTrue(all(group["items"] for group in view["cited_materials"].values()))
        timeline_types = [event["event_type"] for event in view["status_timeline"]]
        self.assertEqual(timeline_types, [
            "draft.created", "version.submitted", "version.review_signed",
            "version.final_signed", "version.effective",
        ])
        # 每一条状态变化都可解释
        for event in view["status_timeline"]:
            self.assertTrue(event["label"])
            self.assertTrue(event["actor_id"])

    def test_history_preserves_every_opinion_forever(self) -> None:
        self._register()
        self._draft()
        self.service.submit_draft("police-a", "LD-ACC-1", "最初提交意见")
        self.service.review_draft("police-b", "LD-ACC-1", True, "最初复核意见")
        self.service.revise_draft(
            "police-a", "LD-ACC-1", payload("同等责任", {"P1": "50", "P2": "50"}),
            trigger_reason="liability_share_changed",
        )
        self.service.submit_draft("police-a", "LD-ACC-1", "第二次提交意见")
        self.service.review_draft("police-b", "LD-ACC-1", True, "第二次复核意见")
        history = self.service.history("auditor-1", "LD-ACC-1")
        opinions = [
            (v["version_no"], s["level"], s["opinion"], s["status"])
            for v in history["versions"] for s in v["signings"]
        ]
        self.assertEqual(opinions, [
            (1, "submit", "最初提交意见", "invalidated"),
            (1, "review", "最初复核意见", "invalidated"),
            (2, "submit", "第二次提交意见", "valid"),
            (2, "review", "第二次复核意见", "valid"),
        ])

    # ---------- 其他规则 ----------

    def test_one_draft_per_accident(self) -> None:
        self._register()
        self._draft()
        with self.assertRaises(Conflict):
            self._draft()

    def test_edit_draft_only_before_submission(self) -> None:
        self._register()
        self._draft()
        self.service.edit_draft(
            "police-a", "LD-ACC-1", payload("修改结论", {"P1": "100", "P2": "0"})
        )
        self.service.submit_draft("police-a", "LD-ACC-1", "提交")
        with self.assertRaises(InvalidState):
            self.service.edit_draft(
                "police-a", "LD-ACC-1", payload("不能改", {"P1": "100", "P2": "0"})
            )

    def test_auditor_cannot_write(self) -> None:
        self._register()
        with self.assertRaises(Forbidden):
            self.service.create_draft(
                "auditor-1", "ACC-1", payload("x", {"P1": "100", "P2": "0"})
            )


if __name__ == "__main__":
    unittest.main()
