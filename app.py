
import os, secrets, sqlite3
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file, abort
from werkzeug.security import generate_password_hash, check_password_hash
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.enums import TA_CENTER

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "demo-change-this-secret")
DB = os.getenv("DATABASE_PATH", "entlass_connect.db")
DEMO = os.getenv("DEMO_MODE", "true").lower() == "true"

STATUS = ["NEW","OFFERED","ACCEPTED","TO_PICKUP","PICKED_UP","TO_DESTINATION","ARRIVED","COMPLETED","CANCELLED"]
NEXT_STATUS = {
    "ACCEPTED":"TO_PICKUP", "TO_PICKUP":"PICKED_UP", "PICKED_UP":"TO_DESTINATION",
    "TO_DESTINATION":"ARRIVED", "ARRIVED":"COMPLETED"
}

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT,
      role TEXT NOT NULL, name TEXT, active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS drivers(
      id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, phone TEXT,
      capability TEXT NOT NULL, available INTEGER DEFAULT 1,
      lat REAL, lng REAL, last_seen TEXT,
      shift_active INTEGER DEFAULT 0, notification_enabled INTEGER DEFAULT 0, gps_enabled INTEGER DEFAULT 0, last_offer_at TEXT, shift_started_at TEXT
    );
    CREATE TABLE IF NOT EXISTS orders(
      id INTEGER PRIMARY KEY, order_no TEXT UNIQUE, patient_name TEXT, patient_ref TEXT,
      pickup TEXT, destination TEXT, transport_type TEXT, date TEXT, pickup_time TEXT,
      payer TEXT, insurance_no TEXT, approval TEXT, reason TEXT, notes TEXT,
      status TEXT, driver_id INTEGER, created_by INTEGER, created_at TEXT,
      accepted_at TEXT, completed_at TEXT, tracking_token TEXT UNIQUE,
      price_cents INTEGER DEFAULT 0, invoice_no TEXT,
      direction TEXT DEFAULT 'hinfahrt', treatment_facility TEXT,
      distance_km REAL DEFAULT 12.4, copay_cents INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS shift_logs(
      id INTEGER PRIMARY KEY, driver_id INTEGER NOT NULL, started_at TEXT NOT NULL,
      ended_at TEXT, break_minutes INTEGER DEFAULT 0, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY, order_id INTEGER, status TEXT, note TEXT, created_at TEXT,
      lat REAL, lng REAL
    );
    CREATE TABLE IF NOT EXISTS invoices(
      id INTEGER PRIMARY KEY, invoice_no TEXT UNIQUE, order_id INTEGER,
      payer TEXT, amount_cents INTEGER, pdf_path TEXT, created_at TEXT, sent_at TEXT
    );
    CREATE TABLE IF NOT EXISTS email_log(
      id INTEGER PRIMARY KEY, order_id INTEGER, recipient TEXT, subject TEXT,
      body TEXT, created_at TEXT, sent INTEGER DEFAULT 0
    );
    """)
    # Safe demo migrations for newly added form fields.
    for col, definition in [
        ("direction", "TEXT DEFAULT 'hinfahrt'"),
        ("treatment_facility", "TEXT"),
        ("distance_km", "REAL DEFAULT 12.4"),
        ("copay_cents", "INTEGER DEFAULT 0"),
        ("shift_active", "INTEGER DEFAULT 0"), ("notification_enabled", "INTEGER DEFAULT 0"),
        ("gps_enabled", "INTEGER DEFAULT 0"), ("last_offer_at", "TEXT"), ("shift_started_at", "TEXT")
    ]:
        try:
            c.execute(f"ALTER TABLE orders ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass

    # Demo users
    users = [
      ("admin","admin123","admin","Admin"),
      ("dispatcher","dispatch123","dispatcher","Dispatcher"),
      ("station","station123","hospital","Station / Krankenhaus"),
      ("pflege","pflege123","hospital","Pflegeheim"),
    ]
    for u in users:
        c.execute("INSERT OR IGNORE INTO users(username,password_hash,role,name) VALUES(?,?,?,?)",
                  (u[0], generate_password_hash(u[1]), u[2], u[3]))
    # 10 demo drivers: 2 Liege, 5 Rollstuhl, 3 Sitzend
    driver_specs = [
      ("Max Müller","+49 151 10000001","liege"),
      ("Anna Weber","+49 151 10000002","liege"),
      ("Peter Klein","+49 151 10000003","rollstuhl"),
      ("Sofia Becker","+49 151 10000004","rollstuhl"),
      ("Lukas Wagner","+49 151 10000005","rollstuhl"),
      ("Nina Fischer","+49 151 10000006","rollstuhl"),
      ("Jonas Hoffmann","+49 151 10000007","rollstuhl"),
      ("Laura Schmitt","+49 151 10000008","sitzend"),
      ("Tim Schneider","+49 151 10000009","sitzend"),
      ("Eva Bauer","+49 151 10000010","sitzend"),
    ]
    for name, phone, cap in driver_specs:
        username = "fahrer" + str(driver_specs.index((name,phone,cap))+1)
        c.execute("INSERT OR IGNORE INTO users(username,password_hash,role,name) VALUES(?,?,?,?)",
                  (username, generate_password_hash("fahrer123"), "driver", name))
        uid = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
        existing = c.execute("SELECT id FROM drivers WHERE user_id=?", (uid,)).fetchone()
        if not existing:
            # Frankfurt-area demo positions; drivers are intentionally close but not exact real positions.
            i = driver_specs.index((name,phone,cap))
            lat, lng = 50.11 + i*0.004, 8.68 + i*0.005
            c.execute("""INSERT INTO drivers(user_id,name,phone,capability,available,lat,lng,last_seen)
                         VALUES(?,?,?,?,0,?,?,?)""",
                      (uid,name,phone,cap,lat,lng,datetime.utcnow().isoformat()))
    c.commit(); c.close()

def login_required(roles=None):
    def deco(fn):
        from functools import wraps
        @wraps(fn)
        def wrapper(*a, **kw):
            if not session.get("user_id"): return redirect(url_for("login"))
            if roles and session.get("role") not in roles: abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco

def capability_ok(driver_cap, requested):
    return driver_cap == "liege" or (driver_cap == "rollstuhl" and requested in ("rollstuhl","sitzend")) or (driver_cap == "sitzend" and requested == "sitzend")

def distance_km(lat1,lng1,lat2,lng2):
    # Haversine
    from math import radians, sin, cos, sqrt, atan2
    if None in (lat1,lng1,lat2,lng2): return 9999
    R=6371
    p1,p2=radians(lat1),radians(lat2)
    dp=radians(lat2-lat1); dl=radians(lng2-lng1)
    a=sin(dp/2)**2+cos(p1)*cos(p2)*sin(dl/2)**2
    return 2*R*atan2(sqrt(a),sqrt(1-a))

def tariff_for(order):
    """
    Demo tariff basis.
    For 2026 Hessen, a published Hessen taxi/mietwagen agreement lists
    2.40 EUR base + 2.35 EUR per occupied km outside mandatory fare areas
    from 01.04.2026 (the exact payer/contract and tariff area must be validated).
    For rollstuhl/liege the demo uses configurable illustrative values until
    the specific transporter's Krankenkassenverträge are entered.
    """
    km = float(order["distance_km"] or 12.4)
    t = order["transport_type"]
    if t == "sitzend":
        return {"code":"510000", "label":"Sitzendkrankenfahrt – Grundpauschale", "base":2.40,
                "km_price":2.35, "km":km, "basis":"Hessen 2026 Demo-Basis"}
    if t == "rollstuhl":
        return {"code":"R-Demo", "label":"Rollstuhltransport – Demo-Tarif", "base":19.00,
                "km_price":2.20, "km":km, "basis":"Illustrativer Demo-Wert – Vertrag erforderlich"}
    return {"code":"L-Demo", "label":"Liegendtransport – Demo-Tarif", "base":49.00,
            "km_price":2.50, "km":km, "basis":"Illustrativer Demo-Wert – Vertrag erforderlich"}

def make_invoice(order):
    os.makedirs("invoices", exist_ok=True)
    inv = f"EC-{datetime.now():%Y%m%d}-{order['id']:05d}"
    path = os.path.abspath(f"invoices/{inv}.pdf")

    tariff = tariff_for(order)
    gross = round(tariff["base"] + tariff["km_price"] * tariff["km"], 2)
    # Statutory co-payment: 10% per trip, minimum 5 EUR, maximum 10 EUR,
    # not exceeding the actual fare. This is shown for demo billing only.
    copay = min(gross, max(5.00, min(10.00, gross * 0.10)))
    payer_amount = max(0, gross - copay)

    styles = getSampleStyleSheet()
    navy = colors.HexColor("#123b58")
    teal = colors.HexColor("#2e9a8b")
    green = colors.HexColor("#35b978")
    line = colors.HexColor("#d8e5e2")
    pale = colors.HexColor("#eef7f4")

    doc = SimpleDocTemplate(path, pagesize=A4, rightMargin=38, leftMargin=38,
                            topMargin=35, bottomMargin=35)

    story = []

    # Logo / company header
    logo_path = os.path.join(os.path.dirname(__file__), "static", "img", "logo.jpeg")
    try:
        from reportlab.platypus import Image
        logo = Image(logo_path, width=62, height=62)
        header = Table([[logo, Paragraph("<b>ENTLASS</b><br/><font color='#2e9a8b'><b>CONNECT</b></font><br/><font size='8'>DIGITAL. SICHER. GEMEINSAM.</font>", styles["Normal"]),
                        Paragraph("<b>KOSTENTRÄGERRECHNUNG</b><br/><font size='9'>Patiententransport · Demo</font>", styles["Normal"])]],
                       colWidths=[70, 230, 210])
        header.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"MIDDLE"),
                                    ("LINEBELOW",(0,0),(-1,-1),1,teal),
                                    ("BOTTOMPADDING",(0,0),(-1,-1),10)]))
        story.append(header)
    except Exception:
        story.append(Paragraph("ENTLASS-CONNECT", styles["Title"]))

    story += [
        Spacer(1, 15),
        Paragraph(f"<b>Rechnungsnummer:</b> {inv}", styles["Normal"]),
        Paragraph(f"<b>Rechnungsdatum:</b> {datetime.now():%d.%m.%Y}", styles["Normal"]),
        Paragraph(f"<b>Leistungsdatum:</b> {order['date']}", styles["Normal"]),
        Spacer(1, 12)
    ]

    recipient = Table([
        [Paragraph("<b>LEISTUNGSERBRINGER</b>", styles["Normal"]),
         Paragraph("<b>KOSTENTRÄGER</b>", styles["Normal"])],
        [Paragraph("ENTLASS-CONNECT Patiententransport<br/>Demo-Fahrdienst<br/>IK: DEMO-IK-123456789", styles["Normal"]),
         Paragraph(f"{order['payer'] or 'Krankenkasse'}<br/>Abrechnung Krankenbeförderung", styles["Normal"])]
    ], colWidths=[255,255])
    recipient.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),pale),("BOX",(0,0),(-1,-1),.6,line),
        ("INNERGRID",(0,0),(-1,-1),.4,line),("VALIGN",(0,0),(-1,-1),"TOP"),
        ("TOPPADDING",(0,0),(-1,-1),8),("BOTTOMPADDING",(0,0),(-1,-1),8)
    ]))
    story.append(recipient)
    story.append(Spacer(1, 15))

    patient = Table([
        ["Patient / Fallnummer", order["patient_ref"] or "—", "Auftrag", order["order_no"]],
        ["Patient", order["patient_name"] or "—", "Transportart", order["transport_type"].title()],
        ["Abholung", order["pickup"], "Ziel", order["destination"]],
        ["Fahrt", f"{order['date']} · {order['pickup_time']}", "Fahrtrichtung", (order["direction"] or "hinfahrt").replace("_"," ").title()],
        ["Versicherungs-Nr.", order["insurance_no"] or "—", "Genehmigung", order["approval"] or "—"]
    ], colWidths=[120,145,100,145])
    patient.setStyle(TableStyle([
        ("BOX",(0,0),(-1,-1),.6,line),("INNERGRID",(0,0),(-1,-1),.4,line),
        ("BACKGROUND",(0,0),(0,-1),pale),("BACKGROUND",(2,0),(2,-1),pale),
        ("FONTNAME",(0,0),(0,-1),"Helvetica-Bold"),("FONTNAME",(2,0),(2,-1),"Helvetica-Bold"),
        ("FONTSIZE",(0,0),(-1,-1),8),("VALIGN",(0,0),(-1,-1),"TOP"),
        ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6)
    ]))
    story.append(patient)
    story.append(Spacer(1, 18))

    positions = [
        ["Pos.", "Leistung", "Menge", "Einzelpreis", "Gesamt"],
        ["1", tariff["label"], "1", f"{tariff['base']:.2f} €", f"{tariff['base']:.2f} €"],
        ["2", f"Besetzt-km ({tariff['km']:.1f} km)", f"{tariff['km']:.1f}", f"{tariff['km_price']:.2f} €", f"{tariff['km_price']*tariff['km']:.2f} €"],
    ]
    pos = Table(positions, colWidths=[35,250,60,90,75])
    pos.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),navy),("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("BOX",(0,0),(-1,-1),.6,line),
        ("INNERGRID",(0,0),(-1,-1),.4,line),("ALIGN",(2,1),(-1,-1),"RIGHT"),
        ("FONTSIZE",(0,0),(-1,-1),8),("TOPPADDING",(0,0),(-1,-1),7),
        ("BOTTOMPADDING",(0,0),(-1,-1),7)
    ]))
    story.append(pos)
    story.append(Spacer(1, 10))

    totals = Table([
        ["Gesamt-Brutto", f"{gross:.2f} €"],
        ["Zuzahlung Versicherter", f"- {copay:.2f} €"],
        ["Rechnungsbetrag Kostenträger", f"{payer_amount:.2f} €"],
    ], colWidths=[390,120])
    totals.setStyle(TableStyle([
        ("ALIGN",(1,0),(1,-1),"RIGHT"),("BOX",(0,0),(-1,-1),.6,line),
        ("INNERGRID",(0,0),(-1,-1),.4,line),("BACKGROUND",(0,2),(-1,2),pale),
        ("FONTNAME",(0,2),(-1,2),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,-1),9),
        ("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7)
    ]))
    story.append(totals)
    story.append(Spacer(1, 15))

    story.append(Paragraph(
        f"<b>Tarifgrundlage:</b> {tariff['basis']}. "
        "Die konkrete Vergütung ist vor Produktiveinsatz anhand des jeweiligen "
        "Krankenkassen-/Leistungserbringervertrags und Abrechnungswegs zu hinterlegen.",
        styles["Normal"]))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "Zuzahlung: grundsätzlich 10 % je Fahrt, mindestens 5,00 € und höchstens 10,00 €, "
        "soweit keine Befreiung bzw. gesetzliche Ausnahme vorliegt.",
        styles["Normal"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph(
        "<b>DEMO-DOKUMENT</b> – Diese Rechnung dient ausschließlich der Funktionsdemonstration "
        "von ENTLASS-CONNECT und ist keine echte Abrechnung mit einer Krankenkasse.",
        styles["Normal"]))

    doc.build(story)

    c=db()
    c.execute("""INSERT OR REPLACE INTO invoices(invoice_no,order_id,payer,amount_cents,pdf_path,created_at)
                 VALUES(?,?,?,?,?,?)""",
              (inv,order["id"],order["payer"],round(payer_amount*100),path,datetime.utcnow().isoformat()))
    c.execute("UPDATE orders SET invoice_no=?,price_cents=?,copay_cents=? WHERE id=?",
              (inv,round(gross*100),round(copay*100),order["id"]))
    c.commit(); c.close()
    return inv,path

@app.context_processor
def inject():
    return {"session_user": session.get("name"), "session_role": session.get("role")}

@app.route("/")
def index():
    if session.get("user_id"): return redirect(url_for("dashboard"))
    return render_template("login.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        c=db(); u=c.execute("SELECT * FROM users WHERE username=? AND active=1",(request.form["username"],)).fetchone(); c.close()
        if u and check_password_hash(u["password_hash"],request.form["password"]):
            session.update(user_id=u["id"], role=u["role"], name=u["name"])
            if u["role"]=="driver": return redirect(url_for("driver"))
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="Benutzername oder Passwort ist falsch.")
    return render_template("login.html")

@app.get("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

@app.get("/dashboard")
@login_required(["admin","dispatcher","hospital"])
def dashboard():
    c=db()
    orders=c.execute("""SELECT o.*, d.name driver_name FROM orders o LEFT JOIN drivers d ON d.id=o.driver_id
                       ORDER BY o.id DESC LIMIT 50""").fetchall()
    drivers=c.execute("SELECT * FROM drivers ORDER BY capability,name").fetchall()
    alerts=c.execute("SELECT * FROM orders WHERE status IN ('PROBLEM') OR status='NEW' ORDER BY id DESC LIMIT 10").fetchall()
    invoices=c.execute("SELECT * FROM invoices ORDER BY id DESC LIMIT 10").fetchall()
    stats={
      "today": c.execute("SELECT COUNT(*) n FROM orders WHERE date=?",(datetime.now().date().isoformat(),)).fetchone()["n"],
      "open": c.execute("SELECT COUNT(*) n FROM orders WHERE status NOT IN ('COMPLETED','CANCELLED')").fetchone()["n"],
      "available": c.execute("SELECT COUNT(*) n FROM drivers WHERE available=1").fetchone()["n"],
      "completed": c.execute("SELECT COUNT(*) n FROM orders WHERE status='COMPLETED'").fetchone()["n"],
    }
    c.close()
    return render_template("dashboard.html",orders=orders,drivers=drivers,invoices=invoices,stats=stats,alerts=alerts)

@app.route("/orders/new", methods=["GET","POST"])
@login_required(["admin","dispatcher","hospital"])
def new_order():
    if request.method=="POST":
        c=db()
        data=request.form
        token=secrets.token_urlsafe(20)
        now=datetime.utcnow().isoformat()
        c.execute("""INSERT INTO orders(order_no,patient_name,patient_ref,pickup,destination,transport_type,date,pickup_time,
                     payer,insurance_no,approval,reason,notes,status,created_by,created_at,tracking_token,price_cents,
                     direction,treatment_facility,distance_km,copay_cents)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (f"AU{datetime.now():%y%m%d%H%M%S}",data.get("patient_name",""),data.get("patient_ref",""),
                   data["pickup"],data["destination"],data["transport_type"],data["date"],data["pickup_time"],
                   data.get("payer",""),data.get("insurance_no",""),data.get("approval","unknown"),
                   data.get("reason",""),data.get("notes",""),"NEW",session["user_id"],now,token,
                   0, data.get("direction","hinfahrt"),data.get("treatment_facility",""),
                   float(data.get("distance_km") or 12.4), 0))
        oid=c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        c.execute("INSERT INTO events(order_id,status,note,created_at) VALUES(?,?,?,?)",(oid,"NEW","Auftrag erstellt",now))
        c.commit()

        # Automatic demo dispatch: the system selects an eligible available driver.
        candidates=c.execute("SELECT * FROM drivers WHERE available=1 AND shift_active=1").fetchall()
        order_row=c.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
        eligible=[d for d in candidates if capability_ok(d["capability"],order_row["transport_type"])]
        if eligible:
            d=max(eligible, key=lambda x: driver_score(x,order_row)[0])
            c.execute("UPDATE orders SET driver_id=?,status='OFFERED' WHERE id=?",(d["id"],oid))
            c.execute("UPDATE drivers SET last_offer_at=? WHERE id=?",(datetime.utcnow().isoformat(),d["id"]))
            c.execute("INSERT INTO events(order_id,status,note,created_at) VALUES(?,?,?,?)",
                      (oid,"OFFERED",f"Automatisches Angebot an {d['name']} – 5 Minuten",datetime.utcnow().isoformat()))
        else:
            c.execute("INSERT INTO events(order_id,status,note,created_at) VALUES(?,?,?,?)",
                      (oid,"ALARM", "Kein passender Fahrer verfügbar – Dispatcher/Admin muss eingreifen.", datetime.utcnow().isoformat()))
        c.commit(); c.close()

        # Hospital users must not be redirected to the dispatcher-only /dispatch route.
        # They return to their dashboard and can see the current order status.
        if session.get("role") in ("admin", "dispatcher"):
            return redirect(url_for("dispatch", order_id=oid))
        return redirect(url_for("dashboard"))
    return render_template("new_order.html", today=datetime.now().date().isoformat())

def driver_score(d, order):
    # Demo score: capability 40%, availability 25%, distance 35%.
    # Eligibility is enforced separately.
    dist=distance_km(d["lat"],d["lng"],50.1109,8.6821)
    dist_score=max(0.0, 100.0-min(dist,20.0)*5.0)
    capability_score=100.0 if d["capability"]==order["transport_type"] else (92.0 if d["capability"] in ("liege","rollstuhl") else 85.0)
    availability_score=100.0 if d["available"] and d["shift_active"] else 0.0
    score=round(capability_score*.40+availability_score*.25+dist_score*.35,1)
    return score,round(dist,1)

@app.get("/dispatch/<int:order_id>")
@login_required(["admin","dispatcher"])
def dispatch(order_id):
    c=db(); o=c.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
    drivers=c.execute("SELECT * FROM drivers WHERE available=1 AND shift_active=1").fetchall(); c.close()
    if not o: abort(404)
    ranked=[]
    for d in drivers:
        if capability_ok(d["capability"],o["transport_type"]):
            score,dist=driver_score(d,o)
            ranked.append((score,dist,d))
    ranked.sort(key=lambda x:x[0], reverse=True)
    return render_template("dispatch.html",order=o,ranked=ranked)

@app.post("/api/dispatch/<int:order_id>")
@login_required(["admin","dispatcher"])
def api_dispatch(order_id):
    c=db(); o=c.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
    if not o: return jsonify(error="not found"),404

    requested_driver_id = request.get_json(silent=True)
    requested_driver_id = requested_driver_id.get("driver_id") if requested_driver_id else None

    if requested_driver_id:
        d=c.execute("SELECT * FROM drivers WHERE id=?",(requested_driver_id,)).fetchone()
        if not d or not d["available"] or not capability_ok(d["capability"],o["transport_type"]):
            c.close()
            return jsonify(error="Dieser Fahrer ist nicht verfügbar oder nicht für die Transportart geeignet."),409
    else:
        candidates=c.execute("SELECT * FROM drivers WHERE available=1 AND shift_active=1").fetchall()
        eligible=[d for d in candidates if capability_ok(d["capability"],o["transport_type"])]
        if not eligible:
            c.close()
            return jsonify(error="Kein passender Fahrer verfügbar. Dispatcher/Admin wird informiert.", alarm=True),409
        d=max(eligible, key=lambda x: driver_score(x,o)[0])

    now=datetime.utcnow().isoformat()
    c.execute("UPDATE orders SET driver_id=?,status='OFFERED' WHERE id=?",(d["id"],order_id))
    c.execute("UPDATE drivers SET last_offer_at=? WHERE id=?",(now,d["id"]))
    c.execute("INSERT INTO events(order_id,status,note,created_at) VALUES(?,?,?,?)",
              (order_id,"OFFERED",f"Angebot an {d['name']} – 5 Minuten",now))
    c.commit(); c.close()
    return jsonify(driver=d["name"],expires_in_seconds=300)

@app.get("/driver")
@login_required(["driver"])
def driver():
    c=db(); d=c.execute("SELECT * FROM drivers WHERE user_id=?",(session["user_id"],)).fetchone()
    active=c.execute("""SELECT o.*,d.name driver_name FROM orders o JOIN drivers d ON d.id=o.driver_id
                        WHERE o.driver_id=? AND o.status NOT IN ('COMPLETED','CANCELLED')
                        ORDER BY o.id DESC LIMIT 1""",(d["id"],)).fetchone()
    offers=c.execute("""SELECT o.* FROM orders o WHERE o.driver_id=? AND o.status='OFFERED'
                        ORDER BY o.id DESC""",(d["id"],)).fetchall()
    c.close(); return render_template("driver.html",driver=d,active=active,offers=offers)

@app.post("/driver/availability")
@login_required(["driver"])
def driver_availability():
    val=1 if request.form.get("available")=="1" else 0
    now=datetime.utcnow().isoformat()
    c=db(); d=c.execute("SELECT * FROM drivers WHERE user_id=?",(session["user_id"],)).fetchone()
    if not d: abort(404)
    if val:
        c.execute("UPDATE drivers SET available=1,shift_active=1,shift_started_at=?,last_seen=? WHERE id=?",(now,now,d["id"]))
        c.execute("INSERT INTO shift_logs(driver_id,started_at,created_at) VALUES(?,?,?)",(d["id"],now,now))
    else:
        c.execute("UPDATE shift_logs SET ended_at=? WHERE driver_id=? AND ended_at IS NULL",(now,d["id"]))
        c.execute("UPDATE drivers SET available=0,shift_active=0,shift_started_at=NULL,last_seen=? WHERE id=?",(now,d["id"]))
    c.commit(); c.close()
    return redirect(url_for("driver"))

@app.post("/driver/permissions")
@login_required(["driver"])
def driver_permissions():
    c=db(); c.execute("UPDATE drivers SET notification_enabled=?,gps_enabled=?,last_seen=? WHERE user_id=?",(1 if request.form.get("notifications") else 0,1 if request.form.get("gps") else 0,datetime.utcnow().isoformat(),session["user_id"])); c.commit(); c.close(); return redirect(url_for("driver"))

@app.post("/driver/problem/<int:order_id>")
@login_required(["driver"])
def driver_problem(order_id):
    note=request.form.get("problem","Sonstiges")
    c=db(); d=c.execute("SELECT * FROM drivers WHERE user_id=?",(session["user_id"],)).fetchone(); o=c.execute("SELECT * FROM orders WHERE id=? AND driver_id=?",(order_id,d["id"])).fetchone()
    if not o: abort(404)
    now=datetime.utcnow().isoformat(); c.execute("INSERT INTO events(order_id,status,note,created_at) VALUES(?,?,?,?)",(order_id,"PROBLEM",note,now)); c.execute("UPDATE orders SET status='PROBLEM' WHERE id=?",(order_id,)); c.commit(); c.close(); return redirect(url_for("driver"))

@app.get("/api/alerts")
@login_required(["admin","dispatcher"])
def alerts():
    c=db(); rows=c.execute("SELECT o.id,o.order_no,o.status,o.pickup,o.destination,o.transport_type FROM orders o WHERE o.status IN ('PROBLEM','NEW') OR (o.status='OFFERED' AND datetime(o.created_at) <= datetime('now','-5 minutes')) ORDER BY o.id DESC").fetchall(); c.close(); return jsonify(alerts=[dict(r) for r in rows])

@app.post("/driver/accept/<int:order_id>")
@login_required(["driver"])
def driver_accept(order_id):
    c=db(); d=c.execute("SELECT * FROM drivers WHERE user_id=?",(session["user_id"],)).fetchone()
    o=c.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
    if not d or not o or not capability_ok(d["capability"],o["transport_type"]): abort(403)
    c.execute("UPDATE orders SET driver_id=?,status='ACCEPTED',accepted_at=? WHERE id=?",
              (d["id"],datetime.utcnow().isoformat(),order_id))
    c.execute("UPDATE drivers SET available=0 WHERE id=?",(d["id"],))
    c.execute("INSERT INTO events(order_id,status,note,created_at) VALUES(?,?,?,?)",(order_id,"ACCEPTED",f"Angenommen von {d['name']}",datetime.utcnow().isoformat()))
    c.commit(); c.close(); return redirect(url_for("driver"))

@app.post("/driver/status/<int:order_id>")
@login_required(["driver"])
def driver_status(order_id):
    c=db(); d=c.execute("SELECT * FROM drivers WHERE user_id=?",(session["user_id"],)).fetchone()
    o=c.execute("SELECT * FROM orders WHERE id=? AND driver_id=?",(order_id,d["id"])).fetchone()
    if not o: abort(404)
    ns=NEXT_STATUS.get(o["status"])
    if not ns: return redirect(url_for("driver"))
    now=datetime.utcnow().isoformat()
    c.execute("UPDATE orders SET status=?,completed_at=? WHERE id=?", (ns, now if ns=="COMPLETED" else o["completed_at"],order_id))
    c.execute("INSERT INTO events(order_id,status,note,created_at) VALUES(?,?,?,?)",(order_id,ns,"Status durch Fahrer aktualisiert",now))
    if ns=="COMPLETED":
        c.execute("UPDATE drivers SET available=1,last_seen=? WHERE id=?",(now,d["id"]))
    c.commit(); c.close()
    if ns=="COMPLETED":
        c=db(); order=c.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone(); c.close()
        inv,path=make_invoice(order)
    return redirect(url_for("driver"))

@app.post("/api/gps")
@login_required(["driver"])
def gps():
    payload=request.get_json(force=True); c=db()
    c.execute("UPDATE drivers SET lat=?,lng=?,last_seen=? WHERE user_id=?",
              (payload.get("lat"),payload.get("lng"),datetime.utcnow().isoformat(),session["user_id"]))
    c.commit(); c.close(); return jsonify(ok=True)

@app.get("/reports")
@login_required(["admin","dispatcher","hospital","driver"])
def reports():
    c=db()
    role=session["role"]
    driver_filter=request.args.get("driver_id", type=int)
    if role=="driver":
        d=c.execute("SELECT * FROM drivers WHERE user_id=?",(session["user_id"],)).fetchone()
        driver_filter=d["id"] if d else -1
    drivers=c.execute("SELECT * FROM drivers ORDER BY name").fetchall()
    logs_sql="SELECT sl.*, d.name driver_name, d.capability FROM shift_logs sl JOIN drivers d ON d.id=sl.driver_id WHERE 1=1"
    params=[]
    if driver_filter:
        logs_sql += " AND sl.driver_id=?"; params.append(driver_filter)
    logs_sql += " ORDER BY sl.started_at DESC LIMIT 200"
    logs=c.execute(logs_sql,params).fetchall()
    now=datetime.utcnow(); rows=[]; total_seconds=0
    for r in logs:
        try:
            start_dt=datetime.fromisoformat(r["started_at"])
            end_dt=datetime.fromisoformat(r["ended_at"]) if r["ended_at"] else now
            seconds=max(0,(end_dt-start_dt).total_seconds())-int(r["break_minutes"] or 0)*60
        except Exception:
            seconds=0
        total_seconds += seconds
        rows.append({"driver_name":r["driver_name"],"capability":r["capability"],"started_at":r["started_at"],"ended_at":r["ended_at"],"break_minutes":r["break_minutes"] or 0,"hours":round(seconds/3600,2),"status":"Offen" if not r["ended_at"] else "Abgeschlossen"})
    where=""; p2=[]
    if driver_filter:
        where=" WHERE driver_id=?"; p2=[driver_filter]
    order_stats=c.execute(f"SELECT COUNT(*) total, SUM(status='COMPLETED') completed, COALESCE(SUM(distance_km),0) km FROM orders{where}",p2).fetchone()
    c.close()
    return render_template("reports.html",drivers=drivers,rows=rows,total_hours=round(total_seconds/3600,2),order_stats=order_stats,driver_filter=driver_filter,role=role)

@app.get("/track/<token>")
def track(token):
    c=db(); o=c.execute("""SELECT o.*,d.name driver_name,d.lat,d.lng FROM orders o
                           LEFT JOIN drivers d ON d.id=o.driver_id WHERE o.tracking_token=?""",(token,)).fetchone()
    events=c.execute("SELECT * FROM events WHERE order_id=? ORDER BY id",(o["id"],)).fetchall() if o else []
    c.close()
    if not o: abort(404)
    return render_template("track.html",order=o,events=events)

@app.get("/invoice/<int:order_id>")
@login_required(["admin","dispatcher"])
def invoice(order_id):
    c=db(); o=c.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone(); c.close()
    if not o: abort(404)
    if not o["invoice_no"]: make_invoice(o); c=db(); o=c.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone(); c.close()
    c=db(); inv=c.execute("SELECT * FROM invoices WHERE order_id=?",(order_id,)).fetchone(); c.close()
    return send_file(inv["pdf_path"],as_attachment=True,download_name=f"{inv['invoice_no']}.pdf")

@app.get("/api/orders")
@login_required()
def api_orders():
    c=db(); rows=c.execute("SELECT order_no,status,transport_type,pickup,destination,date,pickup_time,driver_id FROM orders ORDER BY id DESC LIMIT 100").fetchall(); c.close()
    return jsonify([dict(x) for x in rows])

@app.get("/health")
def health(): return {"status":"ok","app":"ENTLASS-CONNECT"}

init_db()
if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT",5000)),debug=True)
