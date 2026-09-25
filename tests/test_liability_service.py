from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from liability_determination.clock import FrozenClock
from liability_determination.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from liability_determination.service import LiabilityDeterminationService


def case_raw(case_id: str, *, casualties: bool = False, anomaly: bool = False) -> dict:
    return {
        "case_id": case_id,
        "case_number": f"{case_id}-no",
        "title": "路口碰撞事故",
        "occurred_at": "2026-09-20T08:10:00+08:00",
        "location": "示例大道与环城路路口",
        "casualties": casualties,
        "evidence_anomaly": anomaly,
    }


PARTIES = [
    {"party_id": "pa", "name": "张某", "kind": "driver"},
    {"party_id": "pb", "name": "李某", "kind": "driver"},
]

MATERIALS = [
    {"material_id": "stmt-a", "material_type": "statement", "title": "甲车陈述", "summary": "甲车陈述内容"},
    {"material_id": "stmt-b", "material_type": "statement", "title": "乙车陈述", "summary": "乙车陈述内容"},
    {"material_id": "traj", "material_type": "trajectory", "title": "轨迹鉴定", "summary": "轨迹内容"},
    {"material_id": "scene", "material_type": "scene", "title": "现场勘验", "summary": "现场内容"},
    {"material_id": "reg", "material_type": "regulation", "title": "道交法条款", "summary": "第四十四条"},
]
CITATIONS = [item["material_id"] for item in MATERIALS]


def finding_for(value: int) -> str:
    if value == 100:
        return "full"
    if value == 0:
        return "none"
    if value > 50:
        return "primary"
    if value == 50:
        return "equal"
    return "secondary"


def content(ratios: dict[str, int], *, citations=None, note: str = "综合认定") -> dict:
    return {
        "basis_note": note,
        "party_findings": [
            {"party_id": pid, "responsibility_ratio": value, "finding": finding_for(value),
             "reasoning": f"{pid} 责任 {value}%"}
            for pid, value in ratios.items()
        ],
        "material_citations": [{"material_id": mid} for mid in (citations or CITATIONS)],
    }


class LiabilityServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 25, 2, 0, tzinfo=timezone.utc))
        self.service = LiabilityDeterminationService(self.connection, self.clock)
        self.service.create_user("officer", "主办民警", "investigator")
        self.service.create_user("reviewer", "复核人员", "reviewer")
        self.service.create_user("chief", "负责人", "chief")
        self.service.create_user("auditor", "审计员", "auditor")

    def tearDown(self) -> None:
        self.connection.close()

    def _seed_case(self, case_id: str = "c1", *, casualties=False, anomaly=False) -> None:
        self.service.create_case("officer", case_raw(case_id, casualties=casualties, anomaly=anomaly))
        for party in PARTIES:
            self.service.add_party("officer", case_id, party)
        for material in MATERIALS:
            self.service.add_material("officer", case_id, material)

    def _flow_to_effective(self, case_id: str = "c1", *, casualties=False, anomaly=False) -> dict:
        self._seed_case(case_id, casualties=casualties, anomaly=anomaly)
        self.service.create_draft("officer", case_id, content({"pa": 70, "pb": 30}))
        self.service.submit_draft("officer", case_id)
        self.service.sign("reviewer", case_id, "review", "agree", "同意")
        if casualties or anomaly:
            self.service.sign("chief", case_id, "final", "agree", "终审同意")
        return self.service.get_determination("auditor", case_id)

    # ----- 基础校验 -----

    def test_ratios_must_sum_to_100(self) -> None:
        self._seed_case()
        with self.assertRaisesRegex(ValidationFailed, "100"):
            self.service.create_draft("officer", "c1", content({"pa": 70, "pb": 20}))

    def test_must_cover_all_parties_and_material_categories(self) -> None:
        self._seed_case()
        only_a = {
            "basis_note": "x",
            "party_findings": [
                {"party_id": "pa", "responsibility_ratio": 100, "finding": "full", "reasoning": "x"}],
            "material_citations": [{"material_id": mid} for mid in CITATIONS],
        }
        with self.assertRaisesRegex(ValidationFailed, "全部当事方"):
            self.service.create_draft("officer", "c1", only_a)
        no_regulation = content({"pa": 70, "pb": 30}, citations=["stmt-a", "stmt-b", "traj", "scene"])
        with self.assertRaisesRegex(ValidationFailed, "法规依据"):
            self.service.create_draft("officer", "c1", no_regulation)

    def test_finding_and_ratio_must_align(self) -> None:
        self._seed_case()
        raw = content({"pa": 0, "pb": 100})
        raw["party_findings"][0]["finding"] = "minor"
        with self.assertRaisesRegex(ValidationFailed, "必须大于 0"):
            self.service.create_draft("officer", "c1", raw)

    def test_party_cannot_be_added_after_draft(self) -> None:
        self._seed_case()
        self.service.create_draft("officer", "c1", content({"pa": 70, "pb": 30}))
        with self.assertRaises(InvalidState):
            self.service.add_party("officer", "c1", {"party_id": "pc", "name": "王某", "kind": "pedestrian"})

    # ----- 签署层级与生效 -----

    def test_simple_case_effective_after_review_only(self) -> None:
        result = self._flow_to_effective()
        self.assertEqual(result["required_levels"], ["review"])
        self.assertTrue(result["version"]["effective"])
        self.assertEqual(result["version"]["status"], "effective")
        self.assertIsNotNone(result["document"])
        text = result["document"]["document_text"]
        self.assertIn("道路交通事故责任认定书", text)
        self.assertIn("机动车驾驶人", text)
        self.assertIn("主要责任，责任比例 70%", text)
        # 引用材料按 陈述→轨迹→现场→法规 的叙事顺序排列
        self.assertTrue(
            text.index("当事人陈述") < text.index("车辆轨迹")
            < text.index("现场证据") < text.index("法规依据")
        )

    def test_casualty_case_needs_chief_final_review(self) -> None:
        self._seed_case("c2", casualties=True)
        self.service.create_draft("officer", "c2", content({"pa": 70, "pb": 30}))
        self.service.submit_draft("officer", "c2")
        reviewed = self.service.sign("reviewer", "c2", "review", "agree")
        self.assertFalse(reviewed["version"]["effective"])
        self.assertEqual(reviewed["version"]["pending_levels"], ["final"])
        self.assertIsNone(reviewed["document"])
        finalized = self.service.sign("chief", "c2", "final", "agree")
        self.assertTrue(finalized["version"]["effective"])

    def test_evidence_anomaly_also_requires_final_review(self) -> None:
        result = self._flow_to_effective("c3", anomaly=True)
        self.assertEqual(result["required_levels"], ["review", "final"])
        self.assertTrue(result["version"]["effective"])

    def test_chief_cannot_sign_case_without_final_requirement(self) -> None:
        self._seed_case()
        self.service.create_draft("officer", "c1", content({"pa": 70, "pb": 30}))
        self.service.submit_draft("officer", "c1")
        with self.assertRaisesRegex(InvalidState, "终审"):
            self.service.sign("chief", "c1", "final", "agree")

    def test_final_cannot_precede_review(self) -> None:
        self._seed_case("c2", casualties=True)
        self.service.create_draft("officer", "c2", content({"pa": 70, "pb": 30}))
        self.service.submit_draft("officer", "c2")
        with self.assertRaisesRegex(InvalidState, "前置层级"):
            self.service.sign("chief", "c2", "final", "agree")

    def test_author_cannot_sign_own_submission(self) -> None:
        self._seed_case()
        self.service.create_draft("officer", "c1", content({"pa": 70, "pb": 30}))
        self.service.submit_draft("officer", "c1")
        with self.assertRaises(Forbidden):
            self.service.sign("officer", "c1", "review", "agree")

    def test_role_separation(self) -> None:
        self._seed_case("c2", casualties=True)
        self.service.create_draft("officer", "c2", content({"pa": 70, "pb": 30}))
        self.service.submit_draft("officer", "c2")
        with self.assertRaises(Forbidden):
            self.service.sign("chief", "c2", "review", "agree")
        with self.assertRaises(Forbidden):
            self.service.sign("reviewer", "c2", "final", "agree")

    # ----- 驳回与修订 -----

    def test_rejection_blocks_effect_and_allows_revision(self) -> None:
        self._seed_case()
        self.service.create_draft("officer", "c1", content({"pa": 70, "pb": 30}))
        self.service.submit_draft("officer", "c1")
        rejected = self.service.sign("reviewer", "c1", "review", "reject", "比例不当")
        self.assertEqual(rejected["version"]["status"], "rejected")
        self.assertFalse(rejected["version"]["effective"])
        with self.assertRaises(Conflict):  # 意见已形成，不能把不同意改成同意
            self.service.sign("reviewer", "c1", "review", "agree")
        revised = self.service.revise_draft(
            "officer", "c1", content({"pa": 50, "pb": 50}), "按复核意见调整为同等责任")
        self.assertEqual(revised["current_version_no"], 2)
        self.assertEqual(revised["version"]["status"], "draft")

    def test_revision_supersedes_signatures_but_keeps_opinions(self) -> None:
        result = self._flow_to_effective("c2", casualties=True)
        doc_v1 = result["document"]["document_no"]
        self.clock.advance(minutes=30)
        self.service.add_material("officer", "c2", {
            "material_id": "wit", "material_type": "statement", "title": "证人陈述", "summary": "乙车偏快"})
        new_content = content({"pa": 60, "pb": 40}, citations=CITATIONS + ["wit"], note="补充证人后修订")
        self.service.revise_draft("officer", "c2", new_content, "补充证人陈述并重分比例")
        old = self.service.get_version("auditor", "c2", 1)
        self.assertEqual([item["status"] for item in old["version"]["signatures"]], ["superseded", "superseded"])
        # 曾经的意见、签署人和时间都保留
        self.assertEqual(
            [(item["level"], item["opinion"], item["signer_id"]) for item in old["version"]["signatures"]],
            [("review", "agree", "reviewer"), ("final", "agree", "chief")],
        )
        self.assertIsNotNone(old["version"]["signatures"][0]["superseded_at"])
        # 旧送达文本保留，但标记被替代
        self.assertEqual(old["document"]["document_no"], doc_v1)
        self.assertIsNotNone(old["document"]["superseded_at"])
        # 新草稿尚未完成签署，不生效、无新送达文本
        current = self.service.get_determination("auditor", "c2")
        self.assertFalse(current["version"]["effective"])
        self.assertIsNone(current["document"])

    def test_ratio_change_requires_revision_not_draft_edit(self) -> None:
        self._flow_to_effective()
        with self.assertRaises(InvalidState):
            self.service.save_draft("officer", "c1", content({"pa": 60, "pb": 40}))
        with self.assertRaises(InvalidState):  # 已生效版本不能重新提交
            self.service.submit_draft("officer", "c1")

    def test_revision_requires_change_summary(self) -> None:
        self._flow_to_effective()
        with self.assertRaises(ValidationFailed):
            self.service.revise_draft("officer", "c1", content({"pa": 60, "pb": 40}), "  ")

    def test_revision_restarts_full_signature_chain(self) -> None:
        self._flow_to_effective("c2", casualties=True)
        self.service.revise_draft(
            "officer", "c2", content({"pa": 60, "pb": 40}), "调整比例")
        self.service.submit_draft("officer", "c2")
        self.service.sign("reviewer", "c2", "review", "agree")
        pending = self.service.get_determination("auditor", "c2")
        self.assertFalse(pending["version"]["effective"])
        self.service.sign("chief", "c2", "final", "agree")
        done = self.service.get_determination("auditor", "c2")
        self.assertTrue(done["version"]["effective"])
        self.assertEqual(done["current_version_no"], 2)
        self.assertEqual(len(done["version_history"]), 2)

    def test_revision_blocked_while_signing_in_progress(self) -> None:
        self._seed_case("c2", casualties=True)
        self.service.create_draft("officer", "c2", content({"pa": 70, "pb": 30}))
        self.service.submit_draft("officer", "c2")
        self.service.sign("reviewer", "c2", "review", "agree")
        with self.assertRaisesRegex(InvalidState, "签署流程"):
            self.service.revise_draft("officer", "c2", content({"pa": 60, "pb": 40}), "提前修订")

    # ----- 幂等 -----

    def test_duplicate_signature_creates_no_second_decision(self) -> None:
        result = self._flow_to_effective()
        doc_no = result["document"]["document_no"]
        again = self.service.sign("reviewer", "c1", "review", "agree", "重复点击")
        self.assertEqual(again["document"]["document_no"], doc_no)
        count = self.connection.execute("SELECT count(*) FROM determination_effects").fetchone()[0]
        self.assertEqual(count, 1)
        signatures = self.connection.execute(
            "SELECT count(*) FROM version_signatures WHERE level='review'"
        ).fetchone()[0]
        self.assertEqual(signatures, 1)

    def test_duplicate_signature_with_changed_opinion_rejected(self) -> None:
        self._seed_case()
        self.service.create_draft("officer", "c1", content({"pa": 70, "pb": 30}))
        self.service.submit_draft("officer", "c1")
        self.service.sign("reviewer", "c1", "review", "agree")
        with self.assertRaises(Conflict):
            self.service.sign("reviewer", "c1", "review", "reject")

    def test_signed_level_cannot_be_taken_by_another_person(self) -> None:
        self.service.create_user("reviewer-2", "第二名复核人员", "reviewer")
        self._seed_case()
        self.service.create_draft("officer", "c1", content({"pa": 70, "pb": 30}))
        self.service.submit_draft("officer", "c1")
        self.service.sign("reviewer", "c1", "review", "agree")
        with self.assertRaises(Conflict):
            self.service.sign("reviewer-2", "c1", "review", "agree")

    # ----- 草稿编辑 -----

    def test_save_draft_updates_content_before_submit(self) -> None:
        self._seed_case()
        self.service.create_draft("officer", "c1", content({"pa": 70, "pb": 30}, note="初稿"))
        self.service.save_draft("officer", "c1", content({"pa": 60, "pb": 40}, note="改稿"))
        view = self.service.get_determination("auditor", "c1")
        ratios = {item["party_id"]: item["responsibility_ratio"] for item in view["parties"]}
        self.assertEqual(ratios, {"pa": 60, "pb": 40})
        self.assertEqual(len(view["version_history"]), 1)

    # ----- 查询解释 -----

    def test_report_explains_parties_materials_and_status_changes(self) -> None:
        result = self._flow_to_effective("c2", casualties=True)
        parties = {item["party_id"]: item for item in result["parties"]}
        self.assertEqual(parties["pa"]["responsibility_ratio"], 70)
        self.assertEqual(parties["pa"]["finding"], "primary")
        self.assertIn("责任 70%", parties["pa"]["reasoning"])
        cited = {item["material_id"]: item for item in result["materials"] if item["cited"]}
        self.assertEqual(set(cited), set(CITATIONS))
        self.assertEqual(cited["reg"]["material_type"], "regulation")
        types = [event["type"] for event in result["timeline"]]
        self.assertEqual(types, [
            "version_created", "submitted", "signed", "signed", "effective"])
        levels = [(event["level"], event["opinion"]) for event in result["timeline"] if event["type"] == "signed"]
        self.assertEqual(levels, [("review", "agree"), ("final", "agree")])

    def test_history_retains_every_version_with_its_documents(self) -> None:
        self._flow_to_effective("c2", casualties=True)
        self.service.revise_draft("officer", "c2", content({"pa": 60, "pb": 40}), "v2")
        self.service.submit_draft("officer", "c2")
        self.service.sign("reviewer", "c2", "review", "agree")
        self.service.sign("chief", "c2", "final", "reject", "仍有疑点")
        current = self.service.get_determination("auditor", "c2")
        self.assertEqual(current["version"]["status"], "rejected")
        history = current["version_history"]
        self.assertEqual([item["version_no"] for item in history], [1, 2])
        self.assertTrue(history[0]["effective"])
        self.assertFalse(history[0]["is_current"])
        self.assertFalse(history[1]["effective"])
        self.assertTrue(history[1]["is_current"])
        self.assertIsNotNone(history[0]["effect"]["superseded_at"])

    def test_audit_events_record_full_trail(self) -> None:
        self._flow_to_effective()
        events = self.service.audit_events("auditor", "c1")
        kinds = {event["event_type"] for event in events}
        self.assertIn("draft.created", kinds)
        self.assertIn("version.submitted", kinds)
        self.assertIn("version.signed", kinds)
        self.assertIn("determination.effective", kinds)


if __name__ == "__main__":
    unittest.main()
