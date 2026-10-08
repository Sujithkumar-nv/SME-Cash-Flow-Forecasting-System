import os
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from csv_import import parse_transactions_csv
from forecast import (clean_invoices, clean_transactions, current_balance,
                      is_unpaid, last_data_date, run_forecast)
from insights import generate_insights

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
load_dotenv(BASE_DIR / ".env")
load_dotenv()

MAX_UPLOAD = 5 * 1024 * 1024  # 5 MB

app = FastAPI(title="CashCast")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

client = None


def get_db():
    global client
    uri = os.getenv("MONGO_URI", "").strip()
    if not uri:
        raise HTTPException(503, "MONGO_URI is not set. Add it to your .env file or Render environment.")
    if client is None:
        client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    return client[os.getenv("MONGO_DB", "cashflow")]


def load_data():
    db = get_db()
    business = db["business"].find_one({}, {"_id": 0}) or {}
    transactions = list(db["transactions"].find({}, {"_id": 0}))
    invoices = list(db["invoices"].find({}, {"_id": 0}))
    return business, transactions, invoices


@app.exception_handler(PyMongoError)
async def mongo_error(request: Request, exc: PyMongoError):
    return JSONResponse(
        status_code=503,
        content={"detail": "Cannot reach the database. Check MONGO_URI and that your Atlas IP access list allows this server."},
    )


def build_summary(business, transactions, invoices):
    txns = clean_transactions(transactions)
    invs = clean_invoices(invoices)
    as_of = last_data_date(txns)
    this_month = as_of.strftime("%Y-%m")

    monthly = defaultdict(lambda: {"income": 0, "expense": 0})
    for t in txns:
        monthly[t["date"].strftime("%Y-%m")][t["type"]] += t["amount"]
    monthly_totals = []
    for m in sorted(monthly)[-12:]:
        monthly_totals.append({"month": m,
                               "income": round(monthly[m]["income"], 2),
                               "expense": round(monthly[m]["expense"], 2)})

    # expenses by category, last 90 days
    cutoff = as_of - timedelta(days=89)
    by_category = defaultdict(float)
    for t in txns:
        if t["type"] == "expense" and t["date"] >= cutoff:
            by_category[t["category"]] += t["amount"]
    categories = [{"category": c, "amount": round(a, 2)}
                  for c, a in sorted(by_category.items(), key=lambda x: -x[1])]

    def invoice_totals(status):
        rows = [i for i in invs if i["status"] == status and is_unpaid(i)]
        return {"count": len(rows), "amount": round(sum(i["amount"] for i in rows), 2)}

    return {
        "business_name": business.get("name") or "Your business",
        "as_of": as_of.isoformat(),
        "current_balance": round(current_balance(business, txns), 2),
        "income_this_month": round(monthly[this_month]["income"], 2) if txns else 0,
        "expense_this_month": round(monthly[this_month]["expense"], 2) if txns else 0,
        "this_month": this_month,
        "monthly_totals": monthly_totals,
        "expense_by_category": categories,
        "expense_by_category_period": "last 90 days",
        "pending_invoices": invoice_totals("pending"),
        "overdue_invoices": invoice_totals("overdue"),
        "has_data": bool(txns),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/summary")
def summary():
    business, transactions, invoices = load_data()
    return build_summary(business, transactions, invoices)


@app.get("/forecast")
def forecast(days: int = Query(60, ge=1, le=365),
             sales_change_pct: float = Query(0, ge=-100, le=500),
             expense_change_pct: float = Query(0, ge=-100, le=500),
             delay_days: int = Query(0, ge=0, le=365),
             threshold: float = Query(None, ge=0)):
    business, transactions, invoices = load_data()
    return run_forecast(transactions, invoices, business, days=days,
                        sales_change_pct=sales_change_pct,
                        expense_change_pct=expense_change_pct,
                        delay_days=delay_days, threshold=threshold)


@app.get("/alerts")
def alerts(days: int = Query(60, ge=1, le=365), threshold: float = Query(None, ge=0)):
    business, transactions, invoices = load_data()
    result = run_forecast(transactions, invoices, business, days=days, threshold=threshold)
    return {"alerts": result["alerts"], "threshold": result["threshold"], "days": days}


@app.post("/upload")
def upload(file: UploadFile = File(...)):
    raw = file.file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "File is too large. Keep it under 5 MB.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "Could not read the file. Save it as a UTF-8 CSV and try again.")

    rows, error = parse_transactions_csv(text)
    if error:
        raise HTTPException(400, error)

    get_db()["transactions"].insert_many(rows)
    return {"added": len(rows), "message": f"Added {len(rows)} transactions."}


@app.get("/insights")
def insights():
    business, transactions, invoices = load_data()
    s = build_summary(business, transactions, invoices)
    fc = run_forecast(transactions, invoices, business, days=60)
    context = {
        "business": s["business_name"],
        "current_balance": s["current_balance"],
        "income_this_month": s["income_this_month"],
        "expense_this_month": s["expense_this_month"],
        "monthly_totals_recent": s["monthly_totals"][-6:],
        "top_expense_categories_last_90_days": s["expense_by_category"][:5],
        "forecast_days": 60,
        "forecast_lowest_balance": fc["lowest_balance"],
        "forecast_lowest_date": fc["lowest_date"],
        "low_cash_threshold": fc["threshold"],
        "cash_alerts": [a["message"] for a in fc["alerts"]],
        "pending_invoices": s["pending_invoices"],
        "overdue_invoices": s["overdue_invoices"],
    }
    return generate_insights(context)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
