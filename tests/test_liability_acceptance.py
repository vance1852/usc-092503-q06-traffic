from __future__ import annotations

import unittest

from liability_determination.acceptance import run


class LiabilityAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(None)
        self.assertEqual(result["status"], "ok")
        checks = result["checks"]
        self.assertEqual(checks["current_version_no"], 2)
        self.assertTrue(checks["current_effective"])
        self.assertEqual(checks["version_count"], 2)
        self.assertEqual(checks["documents_total"], 2)
        self.assertTrue(checks["v1_effect_kept_but_superseded"])
        self.assertTrue(checks["v1_signatures_preserved"])
        self.assertTrue(checks["v1_signatures_superseded"])
        self.assertEqual(checks["explains_parties"], {"driver-a": 60, "driver-b": 40})
        self.assertEqual(checks["explains_materials"], 6)
        self.assertEqual(result["schema"]["missing_tables"], [])


if __name__ == "__main__":
    unittest.main()
