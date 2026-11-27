import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import settlement


def load():
    return settlement.load_feeds(ROOT / "feeds")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.feeds = load()
        cls.now = settlement.default_now(cls.feeds)
        cls.result = settlement.evaluate(cls.feeds, cls.now)


class ChangeValidation(Base):
    def test_default_now_is_latest_observation(self):
        self.assertEqual(self.now, 66)

    def test_self_review_rejected(self):
        reviews = {r["id"]: r for r in self.result["changes"]["cr-pay-1"]["reviews"]}
        self.assertFalse(reviews["ar-2"]["ok"])
        self.assertIn("申请人不能复核自己的操作", reviews["ar-2"]["problems"])

    def test_fresh_channel_cannot_review(self):
        reviews = {r["id"]: r for r in self.result["changes"]["cr-pay-1"]["reviews"]}
        self.assertFalse(reviews["ar-3"]["ok"])
        self.assertTrue(any("发起后才登记" in p for p in reviews["ar-3"]["problems"]))

    def test_valid_review_approves(self):
        c1 = self.result["changes"]["cr-pay-1"]
        c2 = self.result["changes"]["cr-pay-2"]
        self.assertTrue(c1["approved"])
        self.assertEqual(c1["approved_at"], 29)
        self.assertTrue(c2["approved"])
        self.assertEqual(c2["approved_at"], 39)


class BankBasis(Base):
    def test_correction_annuls_confirmation(self):
        acc = self.result["accounts"]["acct-new"]
        self.assertTrue(all(g["annulled"] for g in acc["grants"]))
        self.assertIsNone(
            settlement.account_valid_at(acc, "wholesale-a", "MXN", 30)
        )

    def test_retirement_only_forward(self):
        acc = self.result["accounts"]["acct-old"]
        self.assertIsNotNone(
            settlement.account_valid_at(acc, "wholesale-a", "MXN", 38)
        )
        self.assertIsNone(
            settlement.account_valid_at(acc, "wholesale-a", "MXN", 45)
        )

    def test_current_relationships(self):
        got = {
            (r["entity"], r["currency"], r["account"])
            for r in self.result["relationships"]
        }
        self.assertEqual(
            got,
            {("fund-b", "USD", "acct-fund"), ("wholesale-a", "USD", "acct-us")},
        )

    def test_no_cascade_across_currencies(self):
        # USD 换账获准不连带 MXN;acct-new 被更正后 wholesale-a 没有 MXN 关系
        self.assertIsNone(
            settlement.find_relationship(
                self.result["relationships"], "wholesale-a", "MXN"
            )
        )


class Dispositions(Base):
    def test_settled_invoice_follows_original_instruction(self):
        d = self.result["dispositions"]["i-4"]
        self.assertEqual(d["status"], "settled")
        self.assertEqual(d["evidence"]["transfer"], "pay-1")

    def test_forward_change_not_listed_as_mispaid(self):
        # acct-old 后来才停用,换账前合法已支付的 i-4 不得列作误付
        self.assertNotEqual(self.result["dispositions"]["i-4"]["status"], "mispaid")

    def test_declined_without_relationship_stays_pending(self):
        d = self.result["dispositions"]["i-5"]
        self.assertEqual(d["status"], "pending_verify")
        self.assertIn("pay-2", d["detail"])

    def test_payable_invoices(self):
        d6 = self.result["dispositions"]["i-6"]
        self.assertEqual(d6["status"], "payable")
        self.assertEqual(d6["account"], "acct-fund")
        d7 = self.result["dispositions"]["i-7"]
        self.assertEqual(d7["status"], "payable")
        self.assertEqual(d7["account"], "acct-us")
        self.assertIn("cr-pay-2", d7["basis"])

    def test_mispaid_invoice_keeps_evidence_and_no_repay(self):
        d = self.result["dispositions"]["i-8"]
        self.assertEqual(d["status"], "mispaid")
        self.assertFalse(d["repay"])
        self.assertEqual(d["recovery"], "pending")
        self.assertEqual(d["evidence"]["transfer"], "pay-3")
        self.assertEqual(
            [r["id"] for r in d["evidence"]["receipts"]], ["pr-5", "pr-6"]
        )

    def test_correction_warning_listed(self):
        text = "\n".join(self.result["warnings"])
        self.assertIn("bf-5", text)
        self.assertIn("acct-new", text)


class AsOfEvaluation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.feeds = load()
        cls.result = settlement.evaluate(cls.feeds, 50)  # 更正送达之前

    def test_before_correction_new_account_usable(self):
        d = self.result["dispositions"]["i-5"]
        self.assertEqual(d["status"], "payable")
        self.assertEqual(d["account"], "acct-new")

    def test_in_flight_cannot_redirect(self):
        for inv in ("i-4", "i-8"):
            d = self.result["dispositions"][inv]
            self.assertEqual(d["status"], "in_flight")
            self.assertIn("不得转投新账", d["detail"])
            self.assertNotIn("account", {k: v for k, v in d.items() if k != "evidence"})


class Views(Base):
    def test_ticket_references_payment_numbers_only(self):
        ticket = settlement.ticket_view(self.result, "i-5")
        raw = json.dumps(ticket, ensure_ascii=False)
        self.assertIn("pay-2", raw)
        for leaked in ("acct", "u30", "u41", "c-1", "c-2", "ma-", "bf-"):
            self.assertNotIn(leaked, raw)

    def test_ticket_public_labels(self):
        ticket = settlement.ticket_view(self.result, "i-8")
        self.assertEqual(ticket["status"], "人工处理中")
        self.assertEqual(ticket["transfers"], [{"transfer": "pay-3", "state": "已扣款"}])

    def test_supplier_view_scope(self):
        view = settlement.supplier_view(self.feeds, self.result, "u30")
        self.assertEqual([e["entity"] for e in view["entities"]], ["wholesale-a"])
        raw = json.dumps(view, ensure_ascii=False)
        self.assertIn("cr-pay-1", raw)  # 缺银行依据的待办
        self.assertNotIn("wholesale-other", raw)  # 不泄露更正后的归属
        self.assertNotIn("fund-b", raw)

    def test_supplier_view_other_entity(self):
        view = settlement.supplier_view(self.feeds, self.result, "u65")
        self.assertEqual([e["entity"] for e in view["entities"]], ["fund-b"])
        self.assertEqual(view["entities"][0]["todo"], ["无待补齐事项"])

    def test_submit_usd_ok(self):
        invoice = {
            "id": "i-9", "seller": "wholesale-a", "currency": "USD",
            "amount": 1000, "assigned_payee": "wholesale-a", "assignment_ref": None,
        }
        check = settlement.submit_check(self.feeds, self.result, invoice)
        self.assertTrue(check["submittable"])
        self.assertEqual(check["relationship"]["account"], "acct-us")

    def test_submit_mxn_blocked(self):
        invoice = {
            "id": "i-10", "seller": "wholesale-a", "currency": "MXN",
            "amount": 1000, "assigned_payee": "wholesale-a", "assignment_ref": None,
        }
        self.assertFalse(settlement.submit_check(self.feeds, self.result, invoice)["submittable"])

    def test_submit_factoring_must_cover_invoice(self):
        invoice = {
            "id": "i-11", "seller": "wholesale-a", "currency": "USD",
            "amount": 1000, "assigned_payee": "fund-b", "assignment_ref": "fa-1",
        }
        check = settlement.submit_check(self.feeds, self.result, invoice)
        self.assertFalse(check["submittable"])
        self.assertTrue(any("未覆盖本账单" in p for p in check["problems"]))

    def test_submit_factored_shape_ok(self):
        invoice = dict(next(i for i in self.feeds["invoices"] if i["id"] == "i-6"))
        check = settlement.submit_check(self.feeds, self.result, invoice)
        self.assertTrue(check["submittable"])
        self.assertEqual(check["relationship"]["account"], "acct-fund")


if __name__ == "__main__":
    unittest.main()
