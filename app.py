from flask import Flask, render_template, request, redirect, url_for, send_file, session
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime, date, timezone
from io import BytesIO
import pandas as pd
from functools import wraps

# -------------------------
# App & Auth setup
# -------------------------
app = Flask(__name__)
app.secret_key = "replace-this-with-a-long-random-string"  # REQUIRED for sessions

# Hardcoded credentials (simple demo auth)
HARD_USER = "admin"
HARD_PASS = "manish1994"

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("user") != HARD_USER:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "").strip()
        if u == HARD_USER and p == HARD_PASS:
            session["user"] = HARD_USER
            return redirect(url_for("index"))
        return render_template("login.html", error="Invalid username or password")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# -------------------------
# Firebase setup
# -------------------------
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# -------------------------
# Wallet helpers
# -------------------------
def _ensure_seed_wallets():
    wallets_ref = db.collection("wallets")
    if not list(wallets_ref.limit(1).stream()):
        now = datetime.now(timezone.utc)
        batch = db.batch()
        w1 = wallets_ref.document()
        w2 = wallets_ref.document()
        w3 = wallets_ref.document()
        batch.set(w1, {"name": "Wallet 1", "balance": 0.0, "is_primary": True,  "created_at": now})
        batch.set(w2, {"name": "Wallet 2", "balance": 0.0, "is_primary": False, "created_at": now})
        batch.set(w3, {"name": "Wallet 3", "balance": 0.0, "is_primary": False, "created_at": now})
        batch.commit()

def _get_wallets():
    _ensure_seed_wallets()
    wallets = []
    for doc in db.collection("wallets").order_by("created_at").stream():
        w = doc.to_dict()
        w["id"] = doc.id
        wallets.append(w)
    return wallets

def _get_primary_wallet():
    q = db.collection("wallets").where("is_primary", "==", True).limit(1).stream()
    primary = None
    for d in q:
        primary = d
        break
    if not primary:
        ws = _get_wallets()
        if ws:
            db.collection("wallets").document(ws[0]["id"]).update({"is_primary": True})
            primary = db.collection("wallets").document(ws[0]["id"]).get()
    return primary

def _adjust_wallet_balance(wallet_id: str, delta: float):
    db.collection("wallets").document(wallet_id).update({"balance": firestore.Increment(delta)})

# -------------------------
# Expense helpers
# -------------------------
def _fetch_expenses():
    ref = db.collection("expenses")
    try:
        docs = ref.order_by("date", direction=firestore.Query.DESCENDING).stream()
    except Exception:
        docs = ref.stream()
    items = []
    for d in docs:
        obj = d.to_dict()
        obj["id"] = d.id
        items.append(obj)
    return items

# -------------------------
# Routes
# -------------------------
@app.route("/")
@login_required
def index():
    wallets = _get_wallets()
    expenses = _fetch_expenses()
    total_spent = sum(float(x.get("amount", 0) or 0) for x in expenses)
    total_balance = sum(float(w.get("balance", 0) or 0) for w in wallets)
    primary_doc = _get_primary_wallet()
    primary_id = primary_doc.id if primary_doc else None
    wallet_map = {w["id"]: w.get("name", "") for w in wallets}

    return render_template(
        "index.html",
        wallets=wallets,
        expenses=expenses,
        total=total_spent,
        remaining=total_balance,
        primary_id=primary_id,
        wallet_map=wallet_map
    )

# ----- Wallet management -----
@app.route("/wallets/add", methods=["POST"])
@login_required
def add_wallet():
    name = request.form.get("name", "").strip() or "New Wallet"
    try:
        balance = float(request.form.get("balance", "0") or 0)
    except ValueError:
        balance = 0.0
    now = datetime.now(timezone.utc)
    db.collection("wallets").document().set({
        "name": name,
        "balance": balance,
        "is_primary": False,
        "created_at": now
    })
    return redirect(url_for("index"))

@app.route("/wallets/<id>/update", methods=["POST"])
@login_required
def update_wallet(id):
    name = request.form.get("name", "").strip()
    bal_str = request.form.get("balance", "").strip()
    update_data = {}
    if name:
        update_data["name"] = name
    if bal_str != "":
        try:
            update_data["balance"] = float(bal_str)
        except ValueError:
            pass
    if update_data:
        db.collection("wallets").document(id).update(update_data)
    return redirect(url_for("index"))

@app.route("/wallets/<id>/set-primary", methods=["POST"])
@login_required
def set_primary(id):
    batch = db.batch()
    for d in db.collection("wallets").where("is_primary", "==", True).stream():
        batch.update(db.collection("wallets").document(d.id), {"is_primary": False})
    batch.update(db.collection("wallets").document(id), {"is_primary": True})
    batch.commit()
    return redirect(url_for("index"))

@app.route("/wallets/<id>/delete", methods=["POST"])
@login_required
def delete_wallet(id):
    all_wallets = _get_wallets()
    if len(all_wallets) <= 1:
        return redirect(url_for("index"))
    wallet_doc = db.collection("wallets").document(id).get()
    if wallet_doc.exists and wallet_doc.to_dict().get("is_primary"):
        for w in all_wallets:
            if w["id"] != id:
                db.collection("wallets").document(w["id"]).update({"is_primary": True})
                break
    db.collection("wallets").document(id).delete()
    return redirect(url_for("index"))

# ----- Expenses -----
@app.route("/add", methods=["POST"])
@login_required
def add_expense():
    description = request.form["description"].strip()
    amount = float(request.form["amount"])
    category = request.form["category"].strip()
    date_str = request.form.get("date", "").strip()
    wallet_id = request.form.get("wallet_id", "").strip() or None

    if date_str:
        dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    else:
        today = date.today()
        dt = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)

    if wallet_id:
        wdoc = db.collection("wallets").document(wallet_id).get()
        if not wdoc.exists:
            wallet_id = None

    if not wallet_id:
        primary = _get_primary_wallet()
        if not primary:
            return redirect(url_for("index"))
        wallet_id = primary.id

    exp_ref = db.collection("expenses").document()
    batch = db.batch()
    batch.set(exp_ref, {
        "description": description,
        "amount": amount,
        "category": category,
        "date": dt,
        "wallet_id": wallet_id
    })
    batch.update(db.collection("wallets").document(wallet_id), {"balance": firestore.Increment(-amount)})
    batch.commit()

    return redirect(url_for("index"))

@app.route("/edit/<id>", methods=["GET", "POST"])
@login_required
def edit_expense(id):
    exp_ref = db.collection("expenses").document(id)
    snap = exp_ref.get()
    if not snap.exists:
        return redirect(url_for("index"))
    original = snap.to_dict()

    if request.method == "POST":
        description = request.form["description"].strip()
        amount_new = float(request.form["amount"])
        category = request.form["category"].strip()
        date_str = request.form.get("date", "").strip()
        wallet_target = request.form.get("wallet_id") or original.get("wallet_id")

        if date_str:
            dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
        else:
            today = date.today()
            dt = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)

        amount_old = float(original.get("amount", 0) or 0)
        wallet_old = original.get("wallet_id")

        batch = db.batch()

        if wallet_target != wallet_old:
            if wallet_old:
                batch.update(db.collection("wallets").document(wallet_old), {"balance": firestore.Increment(+amount_old)})
            if wallet_target:
                batch.update(db.collection("wallets").document(wallet_target), {"balance": firestore.Increment(-amount_new)})
        else:
            delta = amount_old - amount_new  # positive -> refund, negative -> more charge
            if wallet_old:
                batch.update(db.collection("wallets").document(wallet_old), {"balance": firestore.Increment(delta)})

        batch.update(exp_ref, {
            "description": description,
            "amount": amount_new,
            "category": category,
            "date": dt,
            "wallet_id": wallet_target
        })
        batch.commit()
        return redirect(url_for("index"))

    wallets = _get_wallets()
    date_value = ""
    if isinstance(original.get("date"), datetime):
        date_value = original["date"].date().isoformat()

    return render_template("edit.html", expense={"id": id, **original}, date_value=date_value, wallets=wallets)

@app.route("/delete/<id>", methods=["POST"])
@login_required
def delete_expense(id):
    exp_ref = db.collection("expenses").document(id)
    snap = exp_ref.get()
    if snap.exists:
        data = snap.to_dict()
        amount = float(data.get("amount", 0) or 0)
        wid = data.get("wallet_id")
        batch = db.batch()
        batch.delete(exp_ref)
        if wid:
            batch.update(db.collection("wallets").document(wid), {"balance": firestore.Increment(+amount)})
        batch.commit()
    return redirect(url_for("index"))

# ----- Export -----
@app.route("/export")
@login_required
def export_expenses():
    expenses = _fetch_expenses()

    # FIX: build wallet lookup once (name by id)
    wallet_lookup = {w["id"]: w.get("name", "") for w in _get_wallets()}

    rows = []
    for e in expenses:
        dt = e.get("date")
        if isinstance(dt, datetime):
            dt = dt.astimezone(timezone.utc).date()
        elif isinstance(dt, str):
            try:
                dt = datetime.fromisoformat(dt).date()
            except Exception:
                dt = None
        wid = e.get("wallet_id", "")
        rows.append({
            "Description": e.get("description", ""),
            "Category": e.get("category", ""),
            "Amount": float(e.get("amount", 0) or 0),
            "Date": dt,
            "Wallet": wallet_lookup.get(wid, wid)  # prefer name, fall back to id
        })

    df = pd.DataFrame(rows, columns=["Description", "Category", "Amount", "Date", "Wallet"])
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Expenses", index=False)
    output.seek(0)
    today = date.today().isoformat()
    return send_file(
        output,
        as_attachment=True,
        download_name=f"expenses_{today}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

if __name__ == "__main__":
    app.run(debug=True)
