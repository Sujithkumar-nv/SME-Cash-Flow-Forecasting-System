"""Cash flow forecast engine.

Pure Python: it takes plain lists/dicts (as read from MongoDB) and returns a
plain dict, so it can be tested without a database or a web server.

Method
------
* Sales income      : average per weekday over the last 8 weeks (fewer if the
                      history is shorter).
* Rent/Utilities/Salaries : same day of month, using the average of the last 3
                      months that have data.
* Inventory         : once a week on its usual weekday, using the average weekly
                      total of the last 8 weeks.
* Marketing and any other expense category : daily average over the last 90 days.
* Unpaid invoices   : added as income on due_date + that customer's average
                      historical payment delay (+ the what-if delay), never
                      earlier than tomorrow.

"Today" for the forecast is the date of the most recent transaction, so the
forecast always continues straight from the data you have.
"""
from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

MONTHLY_FIXED = {"rent", "utilities", "salaries", "salary"}
WEEKLY = {"inventory"}
DEFAULT_THRESHOLD = 50000.0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def inr(n: float) -> str:
    """Format a number with Indian digit grouping, e.g. 4800000 -> 48,00,000."""
    n = int(round(n))
    sign = "-" if n < 0 else ""
    s = str(abs(n))
    if len(s) <= 3:
        return sign + s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return sign + ",".join(parts + [tail])


def parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def to_float(value):
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def normalize_transactions(raw):
    """Keep only usable rows: valid date, type income/expense, numeric amount."""
    out = []
    for r in raw or []:
        d = parse_date(r.get("date"))
        t = str(r.get("type", "")).strip().lower()
        a = to_float(r.get("amount"))
        if d is None or t not in ("income", "expense") or a is None:
            continue
        out.append(
            {
                "date": d,
                "type": t,
                "amount": abs(a),
                "category": str(r.get("category") or "Other").strip() or "Other",
                "description": str(r.get("description") or ""),
            }
        )
    return out


def normalize_invoices(raw):
    out = []
    for r in raw or []:
        amount = to_float(r.get("amount"))
        if amount is None:
            continue
        out.append(
            {
                "invoice_id": r.get("invoice_id"),
                "customer": str(r.get("customer") or "Unknown").strip(),
                "invoice_date": parse_date(r.get("invoice_date")),
                "due_date": parse_date(r.get("due_date")),
                "paid_date": parse_date(r.get("paid_date")) if r.get("paid_date") else None,
                "amount": abs(amount),
                "status": str(r.get("status") or "").strip().lower(),
            }
        )
    return out


def is_unpaid(inv) -> bool:
    return inv["status"] != "paid" and inv["paid_date"] is None


def get_as_of(txns) -> date:
    return max((t["date"] for t in txns), default=date.today())


def current_balance(business, txns) -> float:
    opening = to_float((business or {}).get("opening_balance")) or 0.0
    return opening + sum(t["amount"] if t["type"] == "income" else -t["amount"] for t in txns)


def customer_delays(invoices):
    """Average (paid_date - due_date) in days per customer, plus the overall average."""
    per = defaultdict(list)
    for inv in invoices:
        if inv["paid_date"] and inv["due_date"]:
            per[inv["customer"]].append((inv["paid_date"] - inv["due_date"]).days)
    avg = {c: sum(v) / len(v) for c, v in per.items() if v}
    all_delays = [x for v in per.values() for x in v]
    overall = sum(all_delays) / len(all_delays) if all_delays else 0.0
    return avg, overall


def resolve_threshold(business, threshold):
    if threshold is not None:
        return float(threshold)
    t = to_float((business or {}).get("low_cash_threshold"))
    return t if t is not None else DEFAULT_THRESHOLD


# --------------------------------------------------------------------------
# forecast
# --------------------------------------------------------------------------
def run_forecast(
    transactions,
    invoices,
    business,
    days=60,
    sales_change_pct=0.0,
    expense_change_pct=0.0,
    delay_days=0,
    threshold=None,
):
    days = max(1, min(int(days), 365))
    txns = normalize_transactions(transactions)
    invs = normalize_invoices(invoices)
    business = business or {}

    as_of = get_as_of(txns)
    start = as_of + timedelta(days=1)
    end = as_of + timedelta(days=days)
    start_balance = current_balance(business, txns)
    thr = resolve_threshold(business, threshold)

    first_date = min((t["date"] for t in txns), default=as_of)
    history_days = (as_of - first_date).days + 1

    # ---- sales income: average per weekday over the last N weeks -------------
    weeks = max(1, min(8, history_days // 7))
    win_start = as_of - timedelta(days=weeks * 7 - 1)
    sales_rows = [t for t in txns if t["type"] == "income" and t["category"].lower() == "sales"]
    if not sales_rows:  # no category called Sales: use all income rows
        sales_rows = [t for t in txns if t["type"] == "income"]
    sales_by_wd = defaultdict(float)
    for t in sales_rows:
        if t["date"] >= win_start:
            sales_by_wd[t["date"].weekday()] += t["amount"]
    sales_avg = {wd: total / weeks for wd, total in sales_by_wd.items()}

    # ---- expenses -------------------------------------------------------------
    expense_rows = [t for t in txns if t["type"] == "expense"]

    # monthly fixed (rent / utilities / salaries): same day each month
    monthly = {}  # category -> {(y, m): [total, last_day_of_month_seen]}
    for t in expense_rows:
        cat = t["category"].lower()
        if cat in MONTHLY_FIXED:
            slot = monthly.setdefault(t["category"], {}).setdefault((t["date"].year, t["date"].month), [0.0, 0])
            slot[0] += t["amount"]
            slot[1] = max(slot[1], t["date"].day) if slot[1] else t["date"].day
    fixed_events = defaultdict(float)  # date -> amount
    for cat, months in monthly.items():
        keys = sorted(months)
        last3 = keys[-3:]
        avg_amount = sum(months[k][0] for k in last3) / len(last3)
        pay_day = months[keys[-1]][1]
        y, m = start.year, start.month
        while (y, m) <= (end.year, end.month):
            if (y, m) not in months:  # not already paid this month
                d = date(y, m, min(pay_day, calendar.monthrange(y, m)[1]))
                if start <= d <= end:
                    fixed_events[d] += avg_amount
            m += 1
            if m > 12:
                y, m = y + 1, 1

    # weekly (inventory): usual weekday, average of last N weeks
    weekly_events = defaultdict(float)
    for cat in {t["category"] for t in expense_rows if t["category"].lower() in WEEKLY}:
        rows = [t for t in expense_rows if t["category"] == cat and t["date"] >= win_start]
        if not rows:
            continue
        weekly_amount = sum(t["amount"] for t in rows) / weeks
        usual_wd = Counter(t["date"].weekday() for t in rows).most_common(1)[0][0]
        d = start
        while d <= end:
            if d.weekday() == usual_wd:
                weekly_events[d] += weekly_amount
            d += timedelta(days=1)

    # daily average over last 90 days (marketing and any other category)
    win90_days = max(1, min(90, history_days))
    win90_start = as_of - timedelta(days=win90_days - 1)
    other_total = sum(
        t["amount"]
        for t in expense_rows
        if t["date"] >= win90_start
        and t["category"].lower() not in MONTHLY_FIXED
        and t["category"].lower() not in WEEKLY
    )
    daily_other = other_total / win90_days

    # ---- unpaid invoices -------------------------------------------------------
    delay_by_customer, overall_delay = customer_delays(invs)
    invoice_income = defaultdict(float)
    for inv in invs:
        if not is_unpaid(inv) or inv["due_date"] is None:
            continue
        avg_delay = delay_by_customer.get(inv["customer"], overall_delay)
        expected = inv["due_date"] + timedelta(days=int(round(avg_delay)) + int(delay_days))
        if expected < start:
            expected = start
        if expected <= end:
            invoice_income[expected] += inv["amount"]

    # ---- assemble day by day ----------------------------------------------------
    sales_factor = 1 + float(sales_change_pct) / 100.0
    expense_factor = 1 + float(expense_change_pct) / 100.0
    rows, balance = [], start_balance
    for i in range(days):
        d = start + timedelta(days=i)
        income = sales_avg.get(d.weekday(), 0.0) * sales_factor + invoice_income.get(d, 0.0)
        expense = (fixed_events.get(d, 0.0) + weekly_events.get(d, 0.0) + daily_other) * expense_factor
        balance += income - expense
        rows.append(
            {
                "date": d.isoformat(),
                "income": round(income, 2),
                "expense": round(expense, 2),
                "balance": round(balance, 2),
            }
        )

    lowest = min(rows, key=lambda r: r["balance"]) if rows else None
    alerts = build_alerts(rows, start_balance, as_of, thr)

    return {
        "as_of": as_of.isoformat(),
        "days_forecast": days,
        "start_balance": round(start_balance, 2),
        "threshold": thr,
        "days": rows,
        "lowest_balance": lowest["balance"] if lowest else round(start_balance, 2),
        "lowest_date": lowest["date"] if lowest else as_of.isoformat(),
        "alerts": alerts,
        "scenario": {
            "sales_change_pct": float(sales_change_pct),
            "expense_change_pct": float(expense_change_pct),
            "delay_days": int(delay_days),
        },
    }


def build_alerts(rows, start_balance, as_of, threshold):
    if start_balance < threshold:
        return [
            {
                "date": as_of.isoformat(),
                "balance": round(start_balance, 2),
                "threshold": threshold,
                "message": f"Cash is already below \u20b9{inr(threshold)} (balance \u20b9{inr(start_balance)})",
            }
        ]
    for r in rows:
        if r["balance"] < threshold:
            return [
                {
                    "date": r["date"],
                    "balance": r["balance"],
                    "threshold": threshold,
                    "message": f"Cash may fall below \u20b9{inr(threshold)} on {r['date']}",
                }
            ]
    return []
