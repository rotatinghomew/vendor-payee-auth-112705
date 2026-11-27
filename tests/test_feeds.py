import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = {'mandates': ('id', 'person', 'legal_entity', 'role', 'contact', 'contact_since', 'valid_to'), 'bank_facts': ('id', 'account', 'holder', 'currencies', 'state', 'effective', 'received', 'replaces'), 'invoices': ('id', 'seller', 'currency', 'amount', 'assigned_payee', 'assignment_ref'), 'factoring': ('id', 'seller', 'payee', 'invoice', 'currency', 'valid_to'), 'pay_receipts': ('id', 'transfer', 'invoice', 'account', 'state', 'occurred', 'received'), 'change_reviews': ('id', 'change', 'representative', 'contact', 'account', 'entity', 'currency', 'decision', 'occurred')}


class FixtureContract(unittest.TestCase):
    """检查联调资料的字段和公开引用。"""

    def test_feed_shapes(self):
        for name, fields in SCHEMA.items():
            with self.subTest(feed=name):
                rows = json.loads((ROOT / "feeds" / (name + ".json")).read_text())
                self.assertTrue(rows)
                self.assertTrue(all(set(row) == set(fields) for row in rows))
                for row in rows:
                    for field in set(row) & {"occurred", "received", "trusted_time"}:
                        self.assertIs(type(row[field]), int)
                    if "occurred" in row and "received" in row:
                        self.assertLessEqual(row["occurred"], row["received"])

    def test_reference_from_service_directory(self):
        catalog = json.loads((ROOT / "feeds" / "index.json").read_text())
        feed, ref = catalog["sample_ref"].split("/")
        self.assertEqual(set(SCHEMA), set(catalog["sources"]))
        rows = json.loads((ROOT / "feeds" / (feed + ".json")).read_text())
        self.assertEqual(sum(row.get("id", row.get("ref")) == ref for row in rows), 1)
