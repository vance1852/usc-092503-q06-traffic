from __future__ import annotations

import json
import sqlite3
import unittest

from liability_determination.api import JsonApplication
from liability_determination.service import LiabilityDeterminationService

from tests.test_liability_service import CITATIONS, MATERIALS, PARTIES, case_raw, content


def body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class LiabilityApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(LiabilityDeterminationService(self.connection))
        for uid, name, role in (
            ("officer", "主办民警", "investigator"),
            ("reviewer", "复核人员", "reviewer"),
            ("chief", "负责人", "chief"),
        ):
            response = self.app.handle(
                "POST", "/users",
                body=body({"user_id": uid, "display_name": name, "role": role}))
            self.assertEqual(response.status, 201)

    def tearDown(self) -> None:
        self.connection.close()

    def _headers(self, actor: str) -> dict:
        return {"X-Actor-Id": actor}

    def _seed(self, case_id: str = "c1", **flags) -> None:
        response = self.app.handle(
            "POST", "/cases", headers=self._headers("officer"), body=body(case_raw(case_id, **flags)))
        self.assertEqual(response.status, 201)
        for party in PARTIES:
            response = self.app.handle(
                "POST", f"/cases/{case_id}/parties",
                headers=self._headers("officer"), body=body(party))
            self.assertEqual(response.status, 201)
        for material in MATERIALS:
            response = self.app.handle(
                "POST", f"/cases/{case_id}/materials",
                headers=self._headers("officer"), body=body(material))
            self.assertEqual(response.status, 201)

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_full_flow_through_http(self) -> None:
        self._seed()
        created = self.app.handle(
            "POST", "/cases/c1/draft",
            headers=self._headers("officer"), body=body(content({"pa": 70, "pb": 30})))
        self.assertEqual(created.status, 201)
        submitted = self.app.handle(
            "POST", "/cases/c1/submit", headers=self._headers("officer"), body=b"{}")
        self.assertEqual(submitted.status, 200)
        signed = self.app.handle(
            "POST", "/cases/c1/sign", headers=self._headers("reviewer"),
            body=body({"level": "review", "opinion": "agree", "comment": "同意"}))
        self.assertEqual(signed.status, 200)
        self.assertTrue(signed.body["version"]["effective"])

        report = self.app.handle("GET", "/determinations/c1", headers=self._headers("chief"))
        self.assertEqual(report.status, 200)
        self.assertEqual(report.body["document"]["document_no"], signed.body["document"]["document_no"])
        self.assertEqual(len(report.body["version_history"]), 1)

    def test_revise_and_fetch_old_version(self) -> None:
        self._seed("c2", casualties=True)
        self.app.handle(
            "POST", "/cases/c2/draft", headers=self._headers("officer"),
            body=body(content({"pa": 70, "pb": 30})))
        self.app.handle(
            "POST", "/cases/c2/submit", headers=self._headers("officer"), body=b"{}")
        self.app.handle(
            "POST", "/cases/c2/sign", headers=self._headers("reviewer"),
            body=body({"level": "review", "opinion": "agree"}))
        self.app.handle(
            "POST", "/cases/c2/sign", headers=self._headers("chief"),
            body=body({"level": "final", "opinion": "agree"}))
        revised = self.app.handle(
            "POST", "/cases/c2/revise", headers=self._headers("officer"),
            body=body({"content": content({"pa": 60, "pb": 40}), "change_summary": "调整比例"}))
        self.assertEqual(revised.status, 201)
        old = self.app.handle("GET", "/determinations/c2/1", headers=self._headers("reviewer"))
        self.assertEqual(old.status, 200)
        self.assertFalse(old.body["version"]["is_current"])
        self.assertIsNotNone(old.body["document"]["superseded_at"])

    def test_missing_actor_is_rejected(self) -> None:
        response = self.app.handle("GET", "/determinations/c1")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope", headers=self._headers("officer"))
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
