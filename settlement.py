#!/usr/bin/env python3
"""供应商在线换账期间:本地受理、核验与付款办理流程(演练)。

只读取 feeds/ 下各来源交付的资料,按约定规则给出办理结论,不实际划款。
各来源只对自己交付的事实负责:开户行确认账户归属、币种与停用,公司目录
确认人员身份与登记渠道;换账获准需要两类依据同时成立,任何单一来源都
不足以放款。控制账户不证明有权替账单上的法人收款。
"""

import argparse
import json
import sys
from pathlib import Path

FEED_NAMES = (
    "mandates",
    "bank_facts",
    "invoices",
    "factoring",
    "pay_receipts",
    "change_reviews",
)

INVOICE_FIELDS = ("id", "seller", "currency", "amount", "assigned_payee", "assignment_ref")

STATUS_LABELS = {
    "payable": "照付",
    "settled": "已付(按原指令跟进)",
    "in_flight": "在途待查(不得转投新账)",
    "pending_verify": "待核(先向原开户方查证)",
    "mispaid": "误付待追偿(不自动再次支付)",
}

PUBLIC_TRANSFER_LABELS = {"accepted": "受理中", "debited": "已扣款", "declined": "未执行"}
PUBLIC_STATUS_LABELS = {
    "payable": "待支付",
    "settled": "已完成",
    "in_flight": "处理中",
    "pending_verify": "待处理",
    "mispaid": "人工处理中",
}


# ---------------------------------------------------------------- 资料与时刻

def load_feeds(feeds_dir):
    root = Path(feeds_dir)
    return {
        name: json.loads((root / f"{name}.json").read_text(encoding="utf-8"))
        for name in FEED_NAMES
    }


def default_now(feeds):
    """当前时刻取各来源已交付资料中的最晚观察时刻。"""
    seen = []
    for row in feeds["mandates"]:
        seen.append(row["contact_since"])
    for row in feeds["bank_facts"]:
        seen.extend((row["effective"], row["received"]))
    for row in feeds["pay_receipts"]:
        seen.extend((row["occurred"], row["received"]))
    for row in feeds["change_reviews"]:
        seen.append(row["occurred"])
    return max(seen)


# ---------------------------------------------------------------- 换账核验

def _channel_problems(row, mandate, expected_role):
    problems = []
    if mandate["role"] != expected_role:
        problems.append(f"目录登记角色为 {mandate['role']}")
    if mandate["legal_entity"] != row["entity"]:
        problems.append("登记法人与换账主体不一致")
    if mandate["contact"] != row["contact"]:
        problems.append("操作渠道与目录登记不符")
    if mandate["contact_since"] > row["occurred"]:
        problems.append("操作时刻渠道尚未登记")
    if row["occurred"] > mandate["valid_to"]:
        problems.append("渠道登记已失效")
    return problems


def validate_changes(feeds, now):
    """核验换账请求。

    发起与复核须由不同人员经由已登记且仍有效的渠道分别进行;申请人不能
    复核自己的操作;发起后才登记(刚修改)的联系方式不能用于复核本次换账。
    """
    del now  # 核验只依赖各记录自身的时刻
    mandates = {m["id"]: m for m in feeds["mandates"]}
    grouped = {}
    for row in feeds["change_reviews"]:
        grouped.setdefault(row["change"], []).append(row)
    results = {}
    for change_id in sorted(grouped):
        rows = sorted(grouped[change_id], key=lambda r: (r["occurred"], r["id"]))
        request = next((r for r in rows if r["decision"] == "request"), None)
        requester = None
        request_time = None
        request_info = None
        if request is not None:
            mandate = mandates.get(request["representative"])
            problems = (
                ["代表人不在公司目录中"]
                if mandate is None
                else _channel_problems(request, mandate, "propose")
            )
            if mandate is not None:
                requester = mandate["person"]
            request_time = request["occurred"]
            request_info = {
                "id": request["id"],
                "ok": not problems,
                "problems": problems,
                "person": requester,
                "occurred": request["occurred"],
            }
        reviews = []
        valid_times = []
        for row in rows:
            if row["decision"] != "approve":
                continue
            mandate = mandates.get(row["representative"])
            if mandate is None:
                problems = ["代表人不在公司目录中"]
                person = None
            else:
                problems = _channel_problems(row, mandate, "review")
                person = mandate["person"]
                if requester is not None and person == requester:
                    problems.append("申请人不能复核自己的操作")
                if request_time is not None:
                    if row["occurred"] < request_time:
                        problems.append("复核早于发起")
                    if mandate["contact_since"] > request_time:
                        problems.append("复核渠道在换账发起后才登记,不能用于本次复核")
            if not problems:
                valid_times.append(row["occurred"])
            reviews.append({
                "id": row["id"],
                "ok": not problems,
                "problems": problems,
                "person": person,
                "occurred": row["occurred"],
            })
        approved = bool(request_info and request_info["ok"] and valid_times)
        results[change_id] = {
            "change": change_id,
            "account": rows[0]["account"],
            "entity": rows[0]["entity"],
            "currency": rows[0]["currency"],
            "request": request_info,
            "reviews": reviews,
            "approved": approved,
            "approved_at": min(valid_times) if approved else None,
        }
    return results


# ---------------------------------------------------------------- 银行依据

def bank_view(feeds, now):
    """开户行事实:确认、停用与更正。更正一经知悉即撤销被更正的确认。"""
    known = [f for f in feeds["bank_facts"] if f["received"] <= now]
    corrections = {
        f["replaces"]: f for f in known if f["state"] == "corrected" and f["replaces"]
    }
    accounts = {}
    for f in known:
        acc = accounts.setdefault(f["account"], {
            "account": f["account"],
            "grants": [],
            "retired_at": None,
            "corrections": [],
        })
        if f["state"] == "confirmed":
            acc["grants"].append({
                "id": f["id"],
                "holder": f["holder"],
                "currencies": [c for c in f["currencies"].split(",") if c],
                "effective": f["effective"],
                "annulled": f["id"] in corrections,
            })
        elif f["state"] == "retired":
            acc["retired_at"] = (
                f["effective"]
                if acc["retired_at"] is None
                else min(acc["retired_at"], f["effective"])
            )
        elif f["state"] == "corrected":
            acc["corrections"].append({
                "id": f["id"],
                "holder": f["holder"],
                "replaces": f["replaces"],
                "received": f["received"],
            })
    return accounts


def account_valid_at(acc, holder, currency, moment):
    """按当前已知事实判断账户在 moment 是否为 holder 的 currency 有效账户。

    被更正的确认自始无效(账户从未属于该法人);停用只向前生效。
    """
    for grant in acc["grants"]:
        if grant["annulled"] or grant["holder"] != holder or currency not in grant["currencies"]:
            continue
        if grant["effective"] <= moment and (
            acc["retired_at"] is None or acc["retired_at"] > moment
        ):
            return grant
    return None


def legitimate_at(acc, changes, holder, currency, moment):
    """moment 时刻以该账户收款是否合法:银行依据与(如需)换账核准同时具备。"""
    if account_valid_at(acc, holder, currency, moment) is None:
        return False
    targets = [
        c for c in changes.values()
        if c["account"] == acc["account"] and c["entity"] == holder and c["currency"] == currency
    ]
    if not targets:
        return True
    return any(c["approved"] and c["approved_at"] <= moment for c in targets)


def collection_relationships(changes, accounts, now):
    """当前已核准收款关系:开户行确认 + 涉及换账流程的账户须复核通过。

    按法人、币种分别成立;一个币种通过不连带其他币种或其他法律主体。
    """
    rels = []
    for acc in accounts.values():
        if acc["retired_at"] is not None and acc["retired_at"] <= now:
            continue
        for grant in acc["grants"]:
            if grant["annulled"]:
                continue
            for currency in grant["currencies"]:
                targets = [
                    c for c in changes.values()
                    if c["account"] == acc["account"]
                    and c["entity"] == grant["holder"]
                    and c["currency"] == currency
                ]
                since = grant["effective"]
                basis = [grant["id"]]
                if targets:
                    ok = [c for c in targets if c["approved"] and c["approved_at"] <= now]
                    if not ok:
                        continue
                    since = max(since, min(c["approved_at"] for c in ok))
                    basis.extend(sorted(c["change"] for c in ok))
                if since <= now:
                    rels.append({
                        "entity": grant["holder"],
                        "currency": currency,
                        "account": acc["account"],
                        "since": since,
                        "basis": basis,
                    })
    rels.sort(key=lambda r: (r["entity"], r["currency"], -r["since"]))
    return rels


def find_relationship(rels, entity, currency):
    """同一法人同一币种取最近核准的关系(新核准的换账优先)。"""
    candidates = [r for r in rels if r["entity"] == entity and r["currency"] == currency]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r["since"])


# ---------------------------------------------------------------- 支付指令

def transfer_states(feeds, now):
    """各支付指令的当前状态:受理与扣款是独立事实,declined 表示明确未执行。"""
    known = [r for r in feeds["pay_receipts"] if r["received"] <= now]
    grouped = {}
    for row in sorted(known, key=lambda r: (r["occurred"], r["id"])):
        grouped.setdefault(row["transfer"], []).append(row)
    states = {}
    for transfer, rows in grouped.items():
        last = rows[-1]
        states[transfer] = {
            "transfer": transfer,
            "invoice": last["invoice"],
            "account": last["account"],
            "state": last["state"],
            "updated": last["occurred"],
            "receipts": [
                {"id": r["id"], "state": r["state"], "occurred": r["occurred"]}
                for r in rows
            ],
        }
    return states


# ---------------------------------------------------------------- 收款人

def invoice_payee(invoice, factoring, now):
    """账单的法定收款人;有保理安排时校验委托覆盖本账单及币种且仍有效。"""
    payee = invoice["assigned_payee"] or invoice["seller"]
    problems = []
    ref = invoice["assignment_ref"]
    if ref:
        grant = factoring.get(ref)
        if grant is None:
            problems.append(f"保理委托 {ref} 不存在")
        else:
            if grant["invoice"] != invoice["id"]:
                problems.append(f"保理委托 {ref} 未覆盖本账单")
            if grant["seller"] != invoice["seller"]:
                problems.append(f"保理委托 {ref} 的出让方与账单卖方不符")
            if grant["payee"] != payee:
                problems.append(f"保理委托 {ref} 的收款人与账单不符")
            if grant["currency"] != invoice["currency"]:
                problems.append(f"保理委托 {ref} 币种与账单不符")
            if grant["valid_to"] < now:
                problems.append(f"保理委托 {ref} 已失效")
    return payee, problems


# ---------------------------------------------------------------- 发票处置

def disposition(invoice, changes, transfers, rels, accounts, factoring, now):
    payee, payee_problems = invoice_payee(invoice, factoring, now)
    mine = sorted(
        (t for t in transfers.values() if t["invoice"] == invoice["id"]),
        key=lambda t: t["updated"],
    )
    debited = [t for t in mine if t["state"] == "debited"]
    accepted = [t for t in mine if t["state"] == "accepted"]
    declined = [t for t in mine if t["state"] == "declined"]
    rel = find_relationship(rels, payee, invoice["currency"])
    base = {
        "invoice": invoice["id"],
        "payee": payee,
        "currency": invoice["currency"],
        "amount": invoice["amount"],
        "transfers": [t["transfer"] for t in mine],
    }
    if debited:
        current = debited[-1]
        acc = accounts.get(current["account"])
        debit_time = current["receipts"][-1]["occurred"]
        legit = acc is not None and legitimate_at(
            acc, changes, payee, invoice["currency"], debit_time
        )
        evidence = {
            "transfer": current["transfer"],
            "account": current["account"],
            "receipts": current["receipts"],
        }
        if legit:
            # 换账前合法已支付的款不列作误付;已扣款只能按原指令跟进。
            return {
                **base,
                "status": "settled",
                "detail": f"{current['transfer']} 已承诺扣款,只能按原指令跟进",
                "evidence": evidence,
            }
        return {
            **base,
            "status": "mispaid",
            "detail": (
                f"{current['transfer']} 确曾通过错误账户支付;保留供应商原单据,"
                "不自动再次支付"
            ),
            "evidence": evidence,
            "recovery": "pending",
            "repay": False,
        }
    if accepted:
        current = accepted[-1]
        return {
            **base,
            "status": "in_flight",
            "detail": (
                f"{current['transfer']} 受理待查,不得转投新账,"
                "向原支付方查询原去向"
            ),
            "evidence": {
                "transfer": current["transfer"],
                "account": current["account"],
                "receipts": current["receipts"],
            },
        }
    prefix = ""
    if declined:
        prefix = f"原指令 {declined[-1]['transfer']} 明确未执行;"
    if payee_problems:
        return {
            **base,
            "status": "pending_verify",
            "detail": prefix + "收款人依据不足:" + ";".join(payee_problems),
        }
    if rel:
        note = prefix + "可重新选账" if declined else "尚未发送,可选当前已核准账户"
        return {
            **base,
            "status": "payable",
            "account": rel["account"],
            "basis": rel["basis"],
            "detail": f"{note}:{payee}/{invoice['currency']} -> {rel['account']}",
        }
    return {
        **base,
        "status": "pending_verify",
        "detail": (
            f"{prefix}无当前有效的 {payee}/{invoice['currency']} 收款关系,"
            "先向原开户方查证"
        ),
    }


def correction_warnings(accounts, transfers, now):
    """开户行更正的处置:未发送的相关指令立即停止,在途的仍从原支付方确认。"""
    warnings = []
    for acc in accounts.values():
        for corr in acc["corrections"]:
            if corr["received"] > now:
                continue
            warnings.append(
                f"开户行更正 {corr['id']}:{acc['account']} 从未属于拟收款法人,"
                "尚未发送的相关指令立即停止"
            )
            for t in transfers.values():
                if t["account"] == acc["account"] and t["state"] == "accepted":
                    warnings.append(
                        f"{t['transfer']} 在途且目的地经更正,仍须从原支付方确认,"
                        "不得转投新账"
                    )
    return warnings


def evaluate(feeds, now):
    factoring = {f["id"]: f for f in feeds["factoring"]}
    changes = validate_changes(feeds, now)
    accounts = bank_view(feeds, now)
    rels = collection_relationships(changes, accounts, now)
    transfers = transfer_states(feeds, now)
    dispositions = {
        inv["id"]: disposition(inv, changes, transfers, rels, accounts, factoring, now)
        for inv in feeds["invoices"]
    }
    return {
        "now": now,
        "changes": changes,
        "accounts": accounts,
        "relationships": rels,
        "transfers": transfers,
        "dispositions": dispositions,
        "warnings": correction_warnings(accounts, transfers, now),
    }


# ---------------------------------------------------------------- 视图

def supplier_view(feeds, result, person):
    """供应商维护人员视图:仅本人有权代表的法人及需补齐的身份或银行依据。"""
    now = result["now"]
    mandates = [
        m for m in feeds["mandates"]
        if m["person"] == person and m["contact_since"] <= now <= m["valid_to"]
    ]
    entities = []
    for ent in sorted({m["legal_entity"] for m in mandates}):
        roles = sorted({m["role"] for m in mandates if m["legal_entity"] == ent})
        rels = [r for r in result["relationships"] if r["entity"] == ent]
        todo = []
        for c in result["changes"].values():
            if c["entity"] != ent:
                continue
            acc = result["accounts"].get(c["account"])
            grant = (
                account_valid_at(acc, ent, c["currency"], now) if acc else None
            )
            if grant is None:
                todo.append(
                    f"换账 {c['change']} 的目标账户缺少开户行对 "
                    f"{ent}/{c['currency']} 的有效确认,需重新取得银行依据"
                )
            if not c["approved"]:
                todo.append(f"换账 {c['change']} 尚未取得有效复核")
            for rev in c["reviews"]:
                if not rev["ok"]:
                    todo.append(f"复核 {rev['id']} 无效:{';'.join(rev['problems'])}")
        if not todo:
            todo.append("无待补齐事项")
        entities.append({
            "entity": ent,
            "roles": roles,
            "relationships": rels,
            "todo": todo,
        })
    return {"person": person, "entities": entities}


def ticket_view(result, invoice_id):
    """公共票据状态:只引用支付编号,不显示账户或身份确认材料。"""
    disp = result["dispositions"].get(invoice_id)
    if disp is None:
        return None
    transfers = [
        {
            "transfer": tid,
            "state": PUBLIC_TRANSFER_LABELS[result["transfers"][tid]["state"]],
        }
        for tid in disp["transfers"]
    ]
    return {
        "invoice": invoice_id,
        "status": PUBLIC_STATUS_LABELS[disp["status"]],
        "transfers": transfers,
    }


def submit_check(feeds, result, invoice):
    """合作社财务提交新合成发票前:适用哪个已核准收款关系、是否可提交。"""
    missing = [f for f in INVOICE_FIELDS if f not in invoice]
    if missing:
        return {"submittable": False, "problems": [f"缺少字段 {f}" for f in missing]}
    factoring = {f["id"]: f for f in feeds["factoring"]}
    payee, problems = invoice_payee(invoice, factoring, result["now"])
    rel = None if problems else find_relationship(
        result["relationships"], payee, invoice["currency"]
    )
    out = {"submittable": bool(rel), "payee": payee, "problems": problems}
    if rel:
        out["relationship"] = rel
    elif not problems:
        out["problems"] = [f"无当前已核准的 {payee}/{invoice['currency']} 收款关系"]
    return out


# ---------------------------------------------------------------- 文本输出

def render_changes(result):
    lines = [f"换账受理与核验(时刻 {result['now']}):"]
    for c in result["changes"].values():
        verdict = f"获准于 {c['approved_at']}" if c["approved"] else "未获准"
        lines.append(f"\n{c['change']}:{c['entity']} / {c['currency']} -> {c['account']}  [{verdict}]")
        req = c["request"]
        if req is None:
            lines.append("  发起:缺失")
        else:
            mark = "有效" if req["ok"] else "无效:" + ";".join(req["problems"])
            lines.append(f"  发起 {req['id']}({req['person']}@{req['occurred']}):{mark}")
        for rev in c["reviews"]:
            mark = "有效" if rev["ok"] else "无效:" + ";".join(rev["problems"])
            lines.append(f"  复核 {rev['id']}({rev['person']}@{rev['occurred']}):{mark}")
        acc = result["accounts"].get(c["account"])
        grant = (
            account_valid_at(acc, c["entity"], c["currency"], result["now"])
            if acc else None
        )
        if grant:
            lines.append(f"  银行依据:{grant['id']} 确认有效")
        else:
            lines.append("  银行依据:缺少开户行对该法人/币种的有效确认")
    return "\n".join(lines)


def render_accounts(result):
    lines = [f"当前已核准收款关系(时刻 {result['now']}):"]
    if result["relationships"]:
        for r in result["relationships"]:
            lines.append(
                f"  {r['entity']} / {r['currency']} -> {r['account']}"
                f"  自 {r['since']}  依据 {' + '.join(r['basis'])}"
            )
    else:
        lines.append("  (无)")
    notes = []
    for acc in result["accounts"].values():
        for grant in acc["grants"]:
            if grant["annulled"]:
                notes.append(
                    f"  {acc['account']}:确认 {grant['id']} 已被开户行更正撤销,"
                    "该账户从未属于拟收款法人"
                )
        if acc["retired_at"] is not None:
            notes.append(f"  {acc['account']}:自 {acc['retired_at']} 起停用")
    if notes:
        lines.append("账户动态:")
        lines.extend(notes)
    return "\n".join(lines)


def render_pay(result):
    order = ["payable", "pending_verify", "mispaid", "in_flight", "settled"]
    lines = [f"值班员付款办理视图(时刻 {result['now']},金额为分):"]
    for status in order:
        group = [d for d in result["dispositions"].values() if d["status"] == status]
        lines.append(f"\n【{STATUS_LABELS[status]}】")
        if not group:
            lines.append("  (无)")
        for d in sorted(group, key=lambda d: d["invoice"]):
            lines.append(
                f"  {d['invoice']}  {d['currency']} {d['amount']}  {d['detail']}"
            )
            ev = d.get("evidence")
            if ev and d["status"] == "mispaid":
                refs = ", ".join(
                    f"{r['id']}({r['state']}@{r['occurred']})" for r in ev["receipts"]
                )
                lines.append(f"      原转账证据:{ev['transfer']} -> {ev['account']};{refs}")
    if result["warnings"]:
        lines.append("\n提醒:")
        lines.extend(f"  {w}" for w in result["warnings"])
    return "\n".join(lines)


def render_supplier(view):
    lines = [f"供应商维护视图:{view['person']}"]
    if not view["entities"]:
        lines.append("  无有权代表的法人")
    for ent in view["entities"]:
        lines.append(f"\n{ent['entity']}(登记角色:{','.join(ent['roles'])})")
        if ent["relationships"]:
            for r in ent["relationships"]:
                lines.append(f"  已核准:{r['currency']} -> {r['account']}(自 {r['since']})")
        else:
            lines.append("  已核准收款关系:无")
        for item in ent["todo"]:
            lines.append(f"  待办:{item}")
    return "\n".join(lines)


def render_ticket(ticket):
    lines = [f"票据 {ticket['invoice']}:{ticket['status']}"]
    for t in ticket["transfers"]:
        lines.append(f"  支付 {t['transfer']}:{t['state']}")
    return "\n".join(lines)


def render_submit(check):
    lines = []
    if check["submittable"]:
        rel = check["relationship"]
        lines.append(
            f"可提交:适用 {rel['entity']} / {rel['currency']} -> {rel['account']}"
            f"(依据 {' + '.join(rel['basis'])})"
        )
    else:
        lines.append("不可提交:" + ";".join(check["problems"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- 入口

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="供应商在线换账期间:本地受理、核验与付款办理流程(演练,不实际划款)"
    )
    parser.add_argument("--feeds", default="feeds", help="资料目录(默认 feeds)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--at", type=int, default=None,
                       help="评估时刻(时间起点后的分钟数,默认取资料最晚观察时刻)")
        p.add_argument("--json", action="store_true", help="输出 JSON")

    p_changes = sub.add_parser("changes", help="换账请求的受理与核验结论")
    add_common(p_changes)
    p_accounts = sub.add_parser("accounts", help="当前已核准的收款关系及账户依据")
    add_common(p_accounts)
    p_pay = sub.add_parser("pay", help="值班员付款办理视图")
    add_common(p_pay)
    p_submit = sub.add_parser("submit", help="合作社财务提交新合成发票前核查")
    add_common(p_submit)
    p_submit.add_argument("--invoice", help="发票 JSON 串")
    p_submit.add_argument("--invoice-file", help="发票 JSON 文件路径")
    p_supplier = sub.add_parser("supplier", help="供应商维护人员视图")
    add_common(p_supplier)
    p_supplier.add_argument("--person", required=True, help="人员编号,如 u30")
    p_ticket = sub.add_parser("ticket", help="公共票据状态(只引用支付编号)")
    add_common(p_ticket)
    p_ticket.add_argument("--invoice", required=True, help="发票编号,如 i-5")

    args = parser.parse_args(argv)
    feeds = load_feeds(args.feeds)
    now = args.at if args.at is not None else default_now(feeds)
    result = evaluate(feeds, now)

    if args.command == "changes":
        out = result["changes"]
        text = render_changes(result)
    elif args.command == "accounts":
        out = {"relationships": result["relationships"], "accounts": result["accounts"]}
        text = render_accounts(result)
    elif args.command == "pay":
        out = {"dispositions": result["dispositions"], "warnings": result["warnings"]}
        text = render_pay(result)
    elif args.command == "submit":
        if args.invoice_file:
            invoice = json.loads(Path(args.invoice_file).read_text(encoding="utf-8"))
        elif args.invoice:
            invoice = json.loads(args.invoice)
        else:
            parser.error("submit 需要 --invoice 或 --invoice-file")
        out = submit_check(feeds, result, invoice)
        text = render_submit(out)
    elif args.command == "supplier":
        out = supplier_view(feeds, result, args.person)
        text = render_supplier(out)
    elif args.command == "ticket":
        out = ticket_view(result, args.invoice)
        if out is None:
            print(f"未知票据:{args.invoice}", file=sys.stderr)
            return 2
        text = render_ticket(out)
    else:  # pragma: no cover
        parser.error(f"未知命令 {args.command}")

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
