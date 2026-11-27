import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = {'mandates': ('id', 'person', 'legal_entity', 'role', 'contact', 'contact_since', 'valid_to'), 'bank_facts': ('id', 'account', 'holder', 'currencies', 'state', 'effective', 'received', 'replaces'), 'invoices': ('id', 'seller', 'currency', 'amount', 'assigned_payee', 'assignment_ref'), 'factoring': ('id', 'seller', 'payee', 'invoice', 'currency', 'valid_to'), 'pay_receipts': ('id', 'transfer', 'invoice', 'account', 'state', 'occurred', 'received'), 'change_reviews': ('id', 'change', 'representative', 'contact', 'account', 'entity', 'currency', 'decision', 'occurred')}
RELATIONS = [('pay_receipts', ('invoice',), 'invoices', ('id',)), ('pay_receipts', ('account',), 'bank_facts', ('account',)), ('change_reviews', ('representative', 'contact'), 'mandates', ('id', 'contact')), ('invoices', ('assignment_ref', 'assigned_payee', 'id', 'currency'), 'factoring', ('id', 'payee', 'invoice', 'currency'))]


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

    def test_cross_source_references(self):
        feeds = {
            name: json.loads((ROOT / "feeds" / (name + ".json")).read_text())
            for name in SCHEMA
        }
        for source, fields, target, target_fields in RELATIONS:
            with self.subTest(source=source, target=target, link=fields):
                keys = {tuple(row[key] for key in target_fields) for row in feeds[target]}
                for row in feeds[source]:
                    ref = tuple(row[key] for key in fields)
                    if None not in ref:
                        self.assertIn(ref, keys)

    def test_reference_from_service_directory(self):
        catalog = json.loads((ROOT / "feeds" / "index.json").read_text())
        feed, ref = catalog["sample_ref"].split("/")
        self.assertEqual(set(SCHEMA), set(catalog["sources"]))
        rows = json.loads((ROOT / "feeds" / (feed + ".json")).read_text())
        self.assertEqual(sum(row.get("id", row.get("ref")) == ref for row in rows), 1)
