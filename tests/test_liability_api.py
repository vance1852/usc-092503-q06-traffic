from __future__ import annotations

import json
import sqlite3
import unittest

from liability_determination.api import JsonApplication
from liability_determination.service import LiabilityDeterminationService


def materials() -> list[dict[str, str]]:
    return [
        {"category": "party_statement", "reference": "STMT-1", "title": "陈述笔录", "detail": ""},
        {"category": "vehicle_trajectory", "reference": "TRAJ-1", "title": "轨迹鉴定", "detail": ""},
        {"category": "scene_evidence", "reference": "SCENE-1", "title": "现场照片", "detail": ""},
        {"category": "regulation_basis", "reference": "LAW-22", "title": "道交法第二十二条", "detail": ""},
    ]


def draft_payload() -> dict[str, object]:
    return {
        "conclusion": "后车全责",
        "party_shares": [
            {"party_id": "P1", "share_percent": "100", "basis": "未保持安全距离"},
            {"party_id": "P2", "share_percent": "0", "basis": "正常行驶"},
        ],
        "materials": materials(),
    }


class LiabilityApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(LiabilityDeterminationService(self.connection))
        for uid, role in (("police-a", "investigator"), ("police-b", "reviewer"), ("auditor-1", "auditor")):
            self.app.handle("POST", "/users", body=json.dumps(
                {"user_id": uid, "display_name": uid, "role": role}
            ).encode())

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, payload: dict, actor: str = "police-a"):
        return self.app.handle(
            "POST", path, headers={"X-Actor-Id": actor},
            body=json.dumps(payload, ensure_ascii=False).encode(),
        )

    def _get(self, path: str, actor: str = "police-a"):
        return self.app.handle("GET", path, headers={"X-Actor-Id": actor})

    def _prepare(self) -> None:
        self._post("/accidents", {
            "accident_id": "ACC-1", "occurred_at": "2026-09-20T08:30:00+08:00",
            "location": "北环快速路", "summary": "追尾",
        })
        self._post("/accidents/ACC-1/parties", {"party_id": "P1", "name": "张某", "party_kind": "driver"})
        self._post("/accidents/ACC-1/parties", {"party_id": "P2", "name": "李某", "party_kind": "driver"})
        self._post("/accidents/ACC-1/drafts", draft_payload())

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_missing_actor_header(self) -> None:
        response = self.app.handle("GET", "/accidents/ACC-1")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_full_signing_flow_over_http(self) -> None:
        self._prepare()
        submitted = self._post("/drafts/LD-ACC-1/submit", {"opinion": "提交"})
        self.assertEqual(submitted.status, 200)
        self.assertEqual(submitted.body["state"], "submitted")
        reviewed = self._post("/drafts/LD-ACC-1/review", {"approve": True, "opinion": "同意"}, actor="police-b")
        self.assertEqual(reviewed.status, 200)
        self.assertEqual(reviewed.body["state"], "effective")
        document = self._post("/drafts/LD-ACC-1/document", {})
        self.assertEqual(document.status, 200)
        self.assertIn("道路交通事故责任认定书", document.body["document_text"])

    def test_review_reject_then_revise_flow(self) -> None:
        self._prepare()
        self._post("/drafts/LD-ACC-1/submit", {"opinion": "提交"})
        returned = self._post(
            "/drafts/LD-ACC-1/review", {"approve": False, "opinion": "退回补证"}, actor="police-b"
        )
        self.assertEqual(returned.body["state"], "returned")
        body = draft_payload()
        body["materials"][0]["detail"] = "补充第二次询问笔录"
        revised = self._post("/drafts/LD-ACC-1/revise", {
            "trigger_reason": "review_returned", "change_note": "补证", **body,
        })
        self.assertEqual(revised.status, 201)
        self.assertEqual(revised.body["version_no"], 2)

    def test_explanation_route(self) -> None:
        self._prepare()
        response = self._get("/drafts/LD-ACC-1/explanation", actor="auditor-1")
        self.assertEqual(response.status, 200)
        self.assertEqual(len(response.body["party_liabilities"]), 2)
        self.assertEqual(len(response.body["cited_materials"]), 4)
        self.assertIn("status_timeline", response.body)

    def test_history_route(self) -> None:
        self._prepare()
        response = self._get("/drafts/LD-ACC-1/history", actor="auditor-1")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["current_version_no"], 1)
        self.assertEqual(response.body["versions"][0]["state"], "draft")

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope")
        self.assertEqual(response.status, 404)

    def test_bad_json(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)


if __name__ == "__main__":
    unittest.main()
