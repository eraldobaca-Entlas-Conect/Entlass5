import os
import secrets
import sqlite3

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None

from datetime import datetime, timedelta

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    jsonify,
    send_file,
    abort
)

from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)

from reportlab.lib.pagesizes import A4

from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle
)

from reportlab.lib import colors

from reportlab.lib.styles import (
    getSampleStyleSheet
)

from reportlab.lib.enums import TA_CENTER

from notifications import send_family_tracking_email


# =========================================================
# APPLICATION
# =========================================================

app = Flask(__name__)

app.secret_key = os.getenv(
    "SECRET_KEY",
    "demo-change-this-secret"
)


# =========================================================
# SESSION CONFIGURATION
# =========================================================

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,

    SESSION_COOKIE_SAMESITE=os.getenv(
        "SESSION_COOKIE_SAMESITE",
        "Lax"
    ),

    SESSION_COOKIE_SECURE=os.getenv(
        "SESSION_COOKIE_SECURE",
        "true"
    ).lower() == "true",

    SESSION_COOKIE_PATH="/",
)


# =========================================================
# DATABASE CONFIGURATION
# =========================================================

DB = os.getenv(
    "DATABASE_PATH",
    "entlass_connect.db"
)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    ""
)

USE_POSTGRES = bool(
    DATABASE_URL
)

DEMO = os.getenv(
    "DEMO_MODE",
    "true"
).lower() == "true"


# =========================================================
# STATUS
# =========================================================

STATUS = [
    "NEW",
    "OFFERED",
    "ACCEPTED",
    "TO_PICKUP",
    "PICKED_UP",
    "TO_DESTINATION",
    "ARRIVED",
    "COMPLETED",
    "CANCELLED"
]


NEXT_STATUS = {
    "ACCEPTED": "TO_PICKUP",
    "TO_PICKUP": "PICKED_UP",
    "PICKED_UP": "TO_DESTINATION",
    "TO_DESTINATION": "ARRIVED",
    "ARRIVED": "COMPLETED"
}


# =========================================================
# OFFER TIMEOUT
# =========================================================

OFFER_TIMEOUT_MINUTES = 3


def _utc_now():
    return datetime.utcnow()


def _utc_now_iso():
    return _utc_now().isoformat()


def _parse_datetime(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "")
        )
    except Exception:
        return None


def _expire_stale_offers(c):
    """
    Expires driver offers after 3 minutes.

    If another suitable available driver exists, the order is
    immediately offered to that driver. Otherwise the order is
    returned to NEW so it can be dispatched again.
    """

    now = _utc_now()
    cutoff = now - timedelta(minutes=OFFER_TIMEOUT_MINUTES)

    stale = c.execute(
        """
        SELECT *
        FROM orders
        WHERE status='OFFERED'
        AND offer_started_at IS NOT NULL
        """
    ).fetchall()

    for order in stale:
        started = _parse_datetime(order["offer_started_at"])

        if not started or started > cutoff:
            continue

        previous_driver_id = order["driver_id"]

        candidates = c.execute(
            """
            SELECT *
            FROM drivers
            WHERE available=1
            ORDER BY name
            """
        ).fetchall()

        candidates = [
            d for d in candidates
            if d["id"] != previous_driver_id
            and capability_ok(
                d["capability"],
                order["transport_type"]
            )
        ]

        if candidates:
            d = min(
                candidates,
                key=lambda x: distance_km(
                    x["lat"],
                    x["lng"],
                    50.1109,
                    8.6821
                )
            )

            round_no = int(order["offer_round"] or 0) + 1
            new_time = _utc_now_iso()

            c.execute(
                """
                UPDATE orders
                SET
                    status='OFFERED',
                    driver_id=?,
                    offer_started_at=?,
                    offer_round=?
                WHERE id=?
                AND status='OFFERED'
                """,
                (
                    d["id"],
                    new_time,
                    round_no,
                    order["id"]
                )
            )

            c.execute(
                """
                UPDATE drivers
                SET last_offer_at=?
                WHERE id=?
                """,
                (
                    new_time,
                    d["id"]
                )
            )

            c.execute(
                """
                INSERT INTO events(
                    order_id,
                    status,
                    note,
                    created_at
                )
                VALUES(?,?,?,?)
                """,
                (
                    order["id"],
                    "OFFERED",
                    (
                        "3-Minuten-Angebot abgelaufen. "
                        f"Neues Angebot an {d['name']}."
                    ),
                    new_time
                )
            )

        else:
            c.execute(
                """
                UPDATE orders
                SET
                    status='NEW',
                    driver_id=NULL,
                    offer_started_at=NULL
                WHERE id=?
                AND status='OFFERED'
                """,
                (
                    order["id"],
                )
            )

            c.execute(
                """
                INSERT INTO events(
                    order_id,
                    status,
                    note,
                    created_at
                )
                VALUES(?,?,?,?)
                """,
                (
                    order["id"],
                    "OFFER_EXPIRED",
                    (
                        "3-Minuten-Angebot abgelaufen. "
                        "Kein weiterer passender Fahrer verfügbar."
                    ),
                    _utc_now_iso()
                )
            )


# =========================================================
# DATABASE WRAPPER
# =========================================================

class DBConn:
    """
    Small SQLite/PostgreSQL compatibility wrapper
    used by ENTLASS-CONNECT.
    """

    def __init__(
        self,
        raw,
        postgres=False
    ):
        self.raw = raw
        self.postgres = postgres
        self._last_insert_id = None


    def _convert_sql(self, sql):

        if not self.postgres:
            return sql

        sql = sql.replace(
            "?",
            "%s"
        )

        # SQLite INTEGER PRIMARY KEY AUTOINCREMENT
        # -> PostgreSQL identity column
        sql = sql.replace(
            "INTEGER PRIMARY KEY AUTOINCREMENT",
            "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
        )

        sql = sql.replace(
            "INTEGER PRIMARY KEY",
            "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
        )

        # PostgreSQL supports IF NOT EXISTS
        # for ADD COLUMN.
        if (
            sql.lstrip().upper().startswith(
                "ALTER TABLE"
            )
            and
            "ADD COLUMN" in sql.upper()
        ):

            sql = sql.replace(
                " ADD COLUMN ",
                " ADD COLUMN IF NOT EXISTS "
            )

        # SQLite boolean aggregate
        # -> PostgreSQL compatible expression
        sql = sql.replace(
            "SUM(status='COMPLETED')",
            "SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END)"
        )

        return sql


    def execute(
        self,
        sql,
        params=()
    ):

        if self.postgres:

            sql = self._convert_sql(
                sql
            )

            stripped = sql.lstrip()


            # -------------------------------------------------
            # SQLite INSERT OR IGNORE
            # -> PostgreSQL ON CONFLICT DO NOTHING
            # -------------------------------------------------

            if stripped.upper().startswith(
                "INSERT OR IGNORE INTO"
            ):

                sql = sql.replace(
                    "INSERT OR IGNORE INTO",
                    "INSERT INTO",
                    1
                )

                sql = (
                    sql.rstrip()
                    .rstrip(";")
                    + " ON CONFLICT DO NOTHING"
                )


            # -------------------------------------------------
            # PostgreSQL order creation
            # -------------------------------------------------

            if (
                "INSERT INTO orders(" in sql.upper()
                and
                "RETURNING" not in sql.upper()
            ):

                cur = self.raw.execute(
                    sql + " RETURNING id",
                    params
                )

                row = cur.fetchone()

                self._last_insert_id = (
                    row["id"]
                    if row
                    else None
                )

                return cur


            return self.raw.execute(
                sql,
                params
            )


        # SQLite
        return self.raw.execute(
            sql,
            params
        )


    def executemany(
        self,
        sql,
        seq
    ):

        if self.postgres:
            sql = self._convert_sql(
                sql
            )

        return self.raw.executemany(
            sql,
            seq
        )


    def executescript(
        self,
        sql
    ):

        if self.postgres:

            statements = [
                s.strip()
                for s in sql.split(";")
                if s.strip()
            ]

            for stmt in statements:

                self.raw.execute(
                    self._convert_sql(
                        stmt
                    )
                )

        else:

            self.raw.executescript(
                sql
            )


    def last_insert_id(self):

        if self.postgres:

            return self._last_insert_id

        return self.raw.execute(
            "SELECT last_insert_rowid() id"
        ).fetchone()["id"]


    def commit(self):
        self.raw.commit()


    def close(self):
        self.raw.close()


# =========================================================
# DATABASE CONNECTION
# =========================================================

def db():

    if USE_POSTGRES:

        if psycopg is None:

            raise RuntimeError(
                "psycopg fehlt – bitte "
                "psycopg[binary] in "
                "requirements.txt eintragen."
            )

        raw = psycopg.connect(
            DATABASE_URL,
            row_factory=dict_row
        )

        return DBConn(
            raw,
            postgres=True
        )


    c = sqlite3.connect(
        DB
    )

    c.row_factory = sqlite3.Row

    return DBConn(
        c,
        postgres=False
    )


# =========================================================
# DATABASE INITIALIZATION
# =========================================================

def init_db():

    c = db()


    # =====================================================
    # BASE TABLES
    # =====================================================

    c.executescript("""

    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY,
      username TEXT UNIQUE,
      password_hash TEXT,
      role TEXT NOT NULL,
      name TEXT,
      active INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS drivers(
      id INTEGER PRIMARY KEY,
      user_id INTEGER,
      name TEXT,
      phone TEXT,
      capability TEXT NOT NULL,
      available INTEGER DEFAULT 1,
      lat REAL,
      lng REAL,
      last_seen TEXT,
      shift_active INTEGER DEFAULT 0,
      notification_enabled INTEGER DEFAULT 0,
      gps_enabled INTEGER DEFAULT 0,
      last_offer_at TEXT,
      shift_started_at TEXT
    );

    CREATE TABLE IF NOT EXISTS orders(
      id INTEGER PRIMARY KEY,
      order_no TEXT UNIQUE,
      patient_name TEXT,
      patient_ref TEXT,
      pickup TEXT,
      destination TEXT,
      transport_type TEXT,
      date TEXT,
      pickup_time TEXT,
      payer TEXT,
      insurance_no TEXT,
      approval TEXT,
      reason TEXT,
      notes TEXT,
      status TEXT,
      driver_id INTEGER,
      created_by INTEGER,
      created_at TEXT,
      accepted_at TEXT,
      completed_at TEXT,
      tracking_token TEXT UNIQUE,
      price_cents INTEGER DEFAULT 0,
      invoice_no TEXT,
      direction TEXT DEFAULT 'hinfahrt',
      treatment_facility TEXT,
      distance_km REAL DEFAULT 12.4,
      copay_cents INTEGER DEFAULT 0,
      family_email TEXT,
      family_tracking_sent_at TEXT
    );

    CREATE TABLE IF NOT EXISTS shift_logs(
      id INTEGER PRIMARY KEY,
      driver_id INTEGER NOT NULL,
      started_at TEXT NOT NULL,
      ended_at TEXT,
      break_minutes INTEGER DEFAULT 0,
      created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY,
      order_id INTEGER,
      status TEXT,
      note TEXT,
      created_at TEXT,
      lat REAL,
      lng REAL
    );

    CREATE TABLE IF NOT EXISTS invoices(
      id INTEGER PRIMARY KEY,
      invoice_no TEXT UNIQUE,
      order_id INTEGER,
      payer TEXT,
      amount_cents INTEGER,
      pdf_path TEXT,
      created_at TEXT,
      sent_at TEXT
    );

    CREATE TABLE IF NOT EXISTS email_log(
      id INTEGER PRIMARY KEY,
      order_id INTEGER,
      recipient TEXT,
      subject TEXT,
      body TEXT,
      created_at TEXT,
      sent INTEGER DEFAULT 0
    );

    """)


    # =====================================================
    # IMPORTANT DATABASE MIGRATION
    # =====================================================

    invoice_columns = [

        (
            "invoice_no",
            "TEXT"
        ),

        (
            "order_id",
            "INTEGER"
        ),

        (
            "payer",
            "TEXT"
        ),

        (
            "amount_cents",
            "INTEGER DEFAULT 0"
        ),

        (
            "pdf_path",
            "TEXT"
        ),

        (
            "created_at",
            "TEXT"
        ),

        (
            "sent_at",
            "TEXT"
        ),

    ]


    for col, definition in invoice_columns:

        try:

            c.execute(
                f"""
                ALTER TABLE invoices
                ADD COLUMN {col} {definition}
                """
            )

        except Exception as exc:

            error_text = str(exc).lower()

            if (
                "already exists" not in error_text
                and
                "duplicate column" not in error_text
            ):

                pass


    # =====================================================
    # ORDER MIGRATIONS
    # =====================================================

    order_columns = [

        (
            "direction",
            "TEXT DEFAULT 'hinfahrt'"
        ),

        (
            "treatment_facility",
            "TEXT"
        ),

        (
            "distance_km",
            "REAL DEFAULT 12.4"
        ),

        (
            "copay_cents",
            "INTEGER DEFAULT 0"
        ),

        (
            "family_email",
            "TEXT"
        ),

        (
            "family_tracking_sent_at",
            "TEXT"
        ),

        (
            "offer_started_at",
            "TEXT"
        ),

        (
            "offer_round",
            "INTEGER DEFAULT 0"
        ),

    ]


    for col, definition in order_columns:

        try:

            c.execute(
                f"""
                ALTER TABLE orders
                ADD COLUMN {col} {definition}
                """
            )

        except Exception:
            pass


    # =====================================================
    # DRIVER MIGRATIONS
    # =====================================================

    driver_columns = [

        (
            "shift_active",
            "INTEGER DEFAULT 0"
        ),

        (
            "notification_enabled",
            "INTEGER DEFAULT 0"
        ),

        (
            "gps_enabled",
            "INTEGER DEFAULT 0"
        ),

        (
            "last_offer_at",
            "TEXT"
        ),

        (
            "shift_started_at",
            "TEXT"
        ),

    ]


    for col, definition in driver_columns:

        try:

            c.execute(
                f"""
                ALTER TABLE drivers
                ADD COLUMN {col} {definition}
                """
            )

        except Exception:
            pass


    # =====================================================
    # DEMO USERS
    # =====================================================

    users = [

        (
            "admin",
            "admin123",
            "admin",
            "Admin"
        ),

        (
            "dispatcher",
            "dispatch123",
            "dispatcher",
            "Dispatcher"
        ),

        (
            "station",
            "station123",
            "hospital",
            "Station / Krankenhaus"
        ),

        (
            "pflege",
            "pflege123",
            "hospital",
            "Pflegeheim"
        ),

    ]


    for u in users:

        c.execute(
            """
            INSERT OR IGNORE INTO users(
                username,
                password_hash,
                role,
                name
            )
            VALUES(?,?,?,?)
            """,
            (
                u[0],
                generate_password_hash(
                    u[1]
                ),
                u[2],
                u[3]
            )
        )


    # =====================================================
    # DEMO DRIVERS
    # =====================================================

    driver_specs = [

        (
            "Max Müller",
            "+49 151 10000001",
            "liege"
        ),

        (
            "Anna Weber",
            "+49 151 10000002",
            "liege"
        ),

        (
            "Peter Klein",
            "+49 151 10000003",
            "rollstuhl"
        ),

        (
            "Sofia Becker",
            "+49 151 10000004",
            "rollstuhl"
        ),

        (
            "Lukas Wagner",
            "+49 151 10000005",
            "rollstuhl"
        ),

        (
            "Nina Fischer",
            "+49 151 10000006",
            "rollstuhl"
        ),

        (
            "Jonas Hoffmann",
            "+49 151 10000007",
            "rollstuhl"
        ),

        (
            "Laura Schmitt",
            "+49 151 10000008",
            "sitzend"
        ),

        (
            "Tim Schneider",
            "+49 151 10000009",
            "sitzend"
        ),

        (
            "Eva Bauer",
            "+49 151 10000010",
            "sitzend"
        ),

    ]


    for name, phone, cap in driver_specs:

        username = (
            "fahrer"
            + str(
                driver_specs.index(
                    (
                        name,
                        phone,
                        cap
                    )
                )
                + 1
            )
        )


        c.execute(
            """
            INSERT OR IGNORE INTO users(
                username,
                password_hash,
                role,
                name
            )
            VALUES(?,?,?,?)
            """,
            (
                username,
                generate_password_hash(
                    "fahrer123"
                ),
                "driver",
                name
            )
        )


        uid = c.execute(
            """
            SELECT id
            FROM users
            WHERE username=?
            """,
            (
                username,
            )
        ).fetchone()["id"]


        existing = c.execute(
            """
            SELECT id
            FROM drivers
            WHERE user_id=?
            """,
            (
                uid,
            )
        ).fetchone()


        if not existing:

            i = driver_specs.index(
                (
                    name,
                    phone,
                    cap
                )
            )

            lat = (
                50.11
                + i * 0.004
            )

            lng = (
                8.68
                + i * 0.005
            )


            c.execute(
                """
                INSERT INTO drivers(
                    user_id,
                    name,
                    phone,
                    capability,
                    available,
                    lat,
                    lng,
                    last_seen
                )
                VALUES(?,?,?,?,0,?,?,?)
                """,
                (
                    uid,
                    name,
                    phone,
                    cap,
                    lat,
                    lng,
                    datetime.utcnow()
                    .isoformat()
                )
            )


    c.commit()
    c.close()


# =========================================================
# LOGIN REQUIRED
# =========================================================

def login_required(
    roles=None
):

    def deco(fn):

        from functools import wraps

        @wraps(fn)
        def wrapper(
            *a,
            **kw
        ):

            if not session.get(
                "user_id"
            ):

                return redirect(
                    url_for("login")
                )


            if (
                roles
                and
                session.get("role")
                not in roles
            ):

                abort(403)


            return fn(
                *a,
                **kw
            )


        return wrapper

    return deco


# =========================================================
# CAPABILITY
# =========================================================

def capability_ok(
    driver_cap,
    requested
):

    return (
        driver_cap == "liege"
        or (
            driver_cap == "rollstuhl"
            and requested in (
                "rollstuhl",
                "sitzend"
            )
        )
        or (
            driver_cap == "sitzend"
            and requested == "sitzend"
        )
    )


# =========================================================
# DISTANCE
# =========================================================

def distance_km(
    lat1,
    lng1,
    lat2,
    lng2
):

    from math import (
        radians,
        sin,
        cos,
        sqrt,
        atan2
    )


    if None in (
        lat1,
        lng1,
        lat2,
        lng2
    ):

        return 9999


    R = 6371

    p1 = radians(lat1)
    p2 = radians(lat2)

    dp = radians(
        lat2 - lat1
    )

    dl = radians(
        lng2 - lng1
    )


    a = (
        sin(dp / 2) ** 2
        +
        cos(p1)
        *
        cos(p2)
        *
        sin(dl / 2) ** 2
    )


    return (
        2
        *
        R
        *
        atan2(
            sqrt(a),
            sqrt(1 - a)
        )
    )


# =========================================================
# TARIFF
# =========================================================

def tariff_for(order):

    km = float(
        order["distance_km"]
        or 12.4
    )


    transport_type = order[
        "transport_type"
    ]


    if transport_type == "sitzend":

        return {
            "code": "510000",
            "label": (
                "Sitzendkrankenfahrt "
                "– Grundpauschale"
            ),
            "base": 2.40,
            "km_price": 2.35,
            "km": km,
            "basis": (
                "Hessen 2026 Demo-Basis"
            )
        }


    if transport_type == "rollstuhl":

        return {
            "code": "R-Demo",
            "label": (
                "Rollstuhltransport "
                "– Demo-Tarif"
            ),
            "base": 19.00,
            "km_price": 2.20,
            "km": km,
            "basis": (
                "Illustrativer Demo-Wert "
                "– Vertrag erforderlich"
            )
        }


    return {
        "code": "L-Demo",
        "label": (
            "Liegendtransport "
            "– Demo-Tarif"
        ),
        "base": 49.00,
        "km_price": 2.50,
        "km": km,
        "basis": (
            "Illustrativer Demo-Wert "
            "– Vertrag erforderlich"
        )
    }


# =========================================================
# MAKE INVOICE
# =========================================================

def make_invoice(order):

    os.makedirs(
        "invoices",
        exist_ok=True
    )


    inv = (
        f"EC-{datetime.now():%Y%m%d}-"
        f"{order['id']:05d}"
    )


    path = os.path.abspath(
        f"invoices/{inv}.pdf"
    )


    tariff = tariff_for(
        order
    )


    gross = round(
        tariff["base"]
        +
        tariff["km_price"]
        *
        tariff["km"],
        2
    )


    # Demo statutory co-payment
    copay = min(
        gross,
        max(
            5.00,
            min(
                10.00,
                gross * 0.10
            )
        )
    )


    payer_amount = max(
        0,
        gross - copay
    )


    # =====================================================
    # PDF
    # =====================================================

    styles = getSampleStyleSheet()


    navy = colors.HexColor(
        "#123b58"
    )

    teal = colors.HexColor(
        "#2e9a8b"
    )

    line = colors.HexColor(
        "#d8e5e2"
    )

    pale = colors.HexColor(
        "#eef7f4"
    )


    doc = SimpleDocTemplate(
        path,
        pagesize=A4,
        rightMargin=38,
        leftMargin=38,
        topMargin=35,
        bottomMargin=35
    )


    story = []


    # =====================================================
    # HEADER / LOGO
    # =====================================================

    logo_path = os.path.join(
        os.path.dirname(__file__),
        "static",
        "img",
        "logo.jpeg"
    )


    try:

        from reportlab.platypus import Image

        logo = Image(
            logo_path,
            width=62,
            height=62
        )


        header = Table(
            [[
                logo,

                Paragraph(
                    "<b>ENTLASS</b><br/>"
                    "<font color='#2e9a8b'>"
                    "<b>CONNECT</b>"
                    "</font><br/>"
                    "<font size='8'>"
                    "DIGITAL. SICHER. GEMEINSAM."
                    "</font>",
                    styles["Normal"]
                ),

                Paragraph(
                    "<b>KOSTENTRÄGERRECHNUNG</b><br/>"
                    "<font size='9'>"
                    "Patiententransport · Demo"
                    "</font>",
                    styles["Normal"]
                )
            ]],
            colWidths=[
                70,
                230,
                210
            ]
        )


        header.setStyle(
            TableStyle([
                (
                    "VALIGN",
                    (0, 0),
                    (-1, -1),
                    "MIDDLE"
                ),
                (
                    "LINEBELOW",
                    (0, 0),
                    (-1, -1),
                    1,
                    teal
                ),
                (
                    "BOTTOMPADDING",
                    (0, 0),
                    (-1, -1),
                    10
                )
            ])
        )


        story.append(
            header
        )


    except Exception:

        story.append(
            Paragraph(
                "ENTLASS-CONNECT",
                styles["Title"]
            )
        )


    story += [

        Spacer(
            1,
            15
        ),

        Paragraph(
            f"<b>Rechnungsnummer:</b> "
            f"{inv}",
            styles["Normal"]
        ),

        Paragraph(
            f"<b>Rechnungsdatum:</b> "
            f"{datetime.now():%d.%m.%Y}",
            styles["Normal"]
        ),

        Paragraph(
            f"<b>Leistungsdatum:</b> "
            f"{order['date']}",
            styles["Normal"]
        ),

        Spacer(
            1,
            12
        )
    ]


    # =====================================================
    # RECIPIENT
    # =====================================================

    recipient = Table(
        [

            [
                Paragraph(
                    "<b>LEISTUNGSERBRINGER</b>",
                    styles["Normal"]
                ),

                Paragraph(
                    "<b>KOSTENTRÄGER</b>",
                    styles["Normal"]
                )
            ],

            [
                Paragraph(
                    "ENTLASS-CONNECT "
                    "Patiententransport<br/>"
                    "Demo-Fahrdienst<br/>"
                    "IK: DEMO-IK-123456789",
                    styles["Normal"]
                ),

                Paragraph(
                    f"{order['payer'] or 'Krankenkasse'}"
                    "<br/>"
                    "Abrechnung Krankenbeförderung",
                    styles["Normal"]
                )
            ]

        ],
        colWidths=[
            255,
            255
        ]
    )


    recipient.setStyle(
        TableStyle([

            (
                "BACKGROUND",
                (0, 0),
                (-1, 0),
                pale
            ),

            (
                "BOX",
                (0, 0),
                (-1, -1),
                .6,
                line
            ),

            (
                "INNERGRID",
                (0, 0),
                (-1, -1),
                .4,
                line
            ),

            (
                "VALIGN",
                (0, 0),
                (-1, -1),
                "TOP"
            ),

            (
                "TOPPADDING",
                (0, 0),
                (-1, -1),
                8
            ),

            (
                "BOTTOMPADDING",
                (0, 0),
                (-1, -1),
                8
            )

        ])
    )


    story.append(
        recipient
    )


    story.append(
        Spacer(
            1,
            15
        )
    )


    # =====================================================
    # PATIENT DATA
    # =====================================================

    patient = Table(
        [

            [
                "Patient / Fallnummer",
                order["patient_ref"] or "—",
                "Auftrag",
                order["order_no"]
            ],

            [
                "Patient",
                order["patient_name"] or "—",
                "Transportart",
                order["transport_type"].title()
            ],

            [
                "Abholung",
                order["pickup"],
                "Ziel",
                order["destination"]
            ],

            [
                "Fahrt",
                (
                    f"{order['date']} · "
                    f"{order['pickup_time']}"
                ),
                "Fahrtrichtung",
                (
                    order["direction"]
                    or "hinfahrt"
                )
                .replace(
                    "_",
                    " "
                )
                .title()
            ],

            [
                "Versicherungs-Nr.",
                order["insurance_no"] or "—",
                "Genehmigung",
                order["approval"] or "—"
            ]

        ],
        colWidths=[
            120,
            145,
            100,
            145
        ]
    )


    patient.setStyle(
        TableStyle([

            (
                "BOX",
                (0, 0),
                (-1, -1),
                .6,
                line
            ),

            (
                "INNERGRID",
                (0, 0),
                (-1, -1),
                .4,
                line
            ),

            (
                "BACKGROUND",
                (0, 0),
                (0, -1),
                pale
            ),

            (
                "BACKGROUND",
                (2, 0),
                (2, -1),
                pale
            ),

            (
                "FONTNAME",
                (0, 0),
                (0, -1),
                "Helvetica-Bold"
            ),

            (
                "FONTNAME",
                (2, 0),
                (2, -1),
                "Helvetica-Bold"
            ),

            (
                "FONTSIZE",
                (0, 0),
                (-1, -1),
                8
            ),

            (
                "VALIGN",
                (0, 0),
                (-1, -1),
                "TOP"
            ),

            (
                "TOPPADDING",
                (0, 0),
                (-1, -1),
                6
            ),

            (
                "BOTTOMPADDING",
                (0, 0),
                (-1, -1),
                6
            )

        ])
    )


    story.append(
        patient
    )


    story.append(
        Spacer(
            1,
            18
        )
    )


    # =====================================================
    # POSITIONS
    # =====================================================

    positions = [

        [
            "Pos.",
            "Leistung",
            "Menge",
            "Einzelpreis",
            "Gesamt"
        ],

        [
            "1",
            tariff["label"],
            "1",
            f"{tariff['base']:.2f} €",
            f"{tariff['base']:.2f} €"
        ],

        [
            "2",
            (
                f"Besetzt-km "
                f"({tariff['km']:.1f} km)"
            ),
            f"{tariff['km']:.1f}",
            f"{tariff['km_price']:.2f} €",
            (
                f"{tariff['km_price'] * tariff['km']:.2f} €"
            )
        ]

    ]


    pos = Table(
        positions,
        colWidths=[
            35,
            250,
            60,
            90,
            75
        ]
    )


    pos.setStyle(
        TableStyle([

            (
                "BACKGROUND",
                (0, 0),
                (-1, 0),
                navy
            ),

            (
                "TEXTCOLOR",
                (0, 0),
                (-1, 0),
                colors.white
            ),

            (
                "FONTNAME",
                (0, 0),
                (-1, 0),
                "Helvetica-Bold"
            ),

            (
                "BOX",
                (0, 0),
                (-1, -1),
                .6,
                line
            ),

            (
                "INNERGRID",
                (0, 0),
                (-1, -1),
                .4,
                line
            ),

            (
                "ALIGN",
                (2, 1),
                (-1, -1),
                "RIGHT"
            ),

            (
                "FONTSIZE",
                (0, 0),
                (-1, -1),
                8
            ),

            (
                "TOPPADDING",
                (0, 0),
                (-1, -1),
                7
            ),

            (
                "BOTTOMPADDING",
                (0, 0),
                (-1, -1),
                7
            )

        ])
    )


    story.append(
        pos
    )


    story.append(
        Spacer(
            1,
            10
        )
    )


    # =====================================================
    # TOTALS
    # =====================================================

    totals = Table(
        [

            [
                "Gesamt-Brutto",
                f"{gross:.2f} €"
            ],

            [
                "Zuzahlung Versicherter",
                f"- {copay:.2f} €"
            ],

            [
                "Rechnungsbetrag Kostenträger",
                f"{payer_amount:.2f} €"
            ]

        ],
        colWidths=[
            390,
            120
        ]
    )


    totals.setStyle(
        TableStyle([

            (
                "ALIGN",
                (1, 0),
                (1, -1),
                "RIGHT"
            ),

            (
                "BOX",
                (0, 0),
                (-1, -1),
                .6,
                line
            ),

            (
                "INNERGRID",
                (0, 0),
                (-1, -1),
                .4,
                line
            ),

            (
                "BACKGROUND",
                (0, 2),
                (-1, 2),
                pale
            ),

            (
                "FONTNAME",
                (0, 2),
                (-1, 2),
                "Helvetica-Bold"
            ),

            (
                "FONTSIZE",
                (0, 0),
                (-1, -1),
                9
            ),

            (
                "TOPPADDING",
                (0, 0),
                (-1, -1),
                7
            ),

            (
                "BOTTOMPADDING",
                (0, 0),
                (-1, -1),
                7
            )

        ])
    )


    story.append(
        totals
    )


    story.append(
        Spacer(
            1,
            15
        )
    )


    story.append(
        Paragraph(
            f"<b>Tarifgrundlage:</b> "
            f"{tariff['basis']}. "
            "Die konkrete Vergütung ist vor "
            "Produkteinsatz anhand des jeweiligen "
            "Krankenkassen-/Leistungserbringervertrags "
            "und Abrechnungswegs zu hinterlegen.",
            styles["Normal"]
        )
    )


    story.append(
        Spacer(
            1,
            8
        )
    )


    story.append(
        Paragraph(
            "Zuzahlung: grundsätzlich 10 % je Fahrt, "
            "mindestens 5,00 € und höchstens 10,00 €, "
            "soweit keine Befreiung bzw. gesetzliche "
            "Ausnahme vorliegt.",
            styles["Normal"]
        )
    )


    story.append(
        Spacer(
            1,
            12
        )
    )


    story.append(
        Paragraph(
            "<b>DEMO-DOKUMENT</b> – Diese Rechnung dient "
            "ausschließlich der Funktionsdemonstration "
            "von ENTLASS-CONNECT und ist keine echte "
            "Abrechnung mit einer Krankenkasse.",
            styles["Normal"]
        )
    )


    doc.build(
        story
    )


    # =====================================================
    # SAVE INVOICE TO DATABASE
    # =====================================================

    c = db()


    # -----------------------------------------------------
    # IMPORTANT:
    #
    # We intentionally DO NOT use:
    #
    # ON CONFLICT(invoice_no)
    #
    # because older Render databases may not have a UNIQUE
    # constraint on invoice_no.
    #
    # Instead we check the order_id.
    # -----------------------------------------------------

    existing_invoice = c.execute(
        """
        SELECT id
        FROM invoices
        WHERE order_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            order["id"],
        )
    ).fetchone()


    if existing_invoice:

        # -------------------------------------------------
        # UPDATE EXISTING INVOICE
        # -------------------------------------------------

        c.execute(
            """
            UPDATE invoices
            SET
                invoice_no=?,
                payer=?,
                amount_cents=?,
                pdf_path=?,
                created_at=?
            WHERE id=?
            """,
            (
                inv,
                order["payer"],
                round(
                    payer_amount * 100
                ),
                path,
                datetime.utcnow()
                .isoformat(),
                existing_invoice["id"]
            )
        )


    else:

        # -------------------------------------------------
        # CREATE NEW INVOICE
        # -------------------------------------------------

        c.execute(
            """
            INSERT INTO invoices(
                invoice_no,
                order_id,
                payer,
                amount_cents,
                pdf_path,
                created_at
            )
            VALUES(
                ?,?,?,?,?,?
            )
            """,
            (
                inv,
                order["id"],
                order["payer"],
                round(
                    payer_amount * 100
                ),
                path,
                datetime.utcnow()
                .isoformat()
            )
        )


    # -----------------------------------------------------
    # UPDATE ORDER
    # -----------------------------------------------------

    c.execute(
        """
        UPDATE orders
        SET
            invoice_no=?,
            price_cents=?,
            copay_cents=?
        WHERE id=?
        """,
        (
            inv,
            round(
                gross * 100
            ),
            round(
                copay * 100
            ),
            order["id"]
        )
    )


    c.commit()
    c.close()


    return (
        inv,
        path
    )


# =========================================================
# TEMPLATE CONTEXT
# =========================================================

@app.context_processor
def inject():
    return {
        "session_user":
            session.get("name"),

        "session_role":
            session.get("role")
    }


# =========================================================
# ERROR 403
# =========================================================

@app.errorhandler(403)
def forbidden(error):

    if session.get(
        "role"
    ) == "driver":

        return redirect(
            url_for("driver")
        )


    return (
        "403 – Zugriff verweigert",
        403
    )


# =========================================================
# MOBILE DETECTION
# =========================================================

def is_mobile_device():

    ua = (
        request.headers.get(
            "User-Agent"
        )
        or ""
    ).lower()


    mobile_tokens = (
        "iphone",
        "ipad",
        "ipod",
        "android",
        "mobile",
        "webos",
        "blackberry",
        "windows phone",
    )


    return any(
        token in ua
        for token in mobile_tokens
    )


# =========================================================
# INDEX
# =========================================================

@app.route("/")
def index():

    if session.get(
        "user_id"
    ):

        return redirect(
            url_for("dashboard")
        )


    if is_mobile_device():

        return render_template(
            "login_mobile_.html"
        )


    return render_template(
        "login.html"
    )


# =========================================================
# LOGIN
# =========================================================

@app.route(
    "/login",
    methods=[
        "GET",
        "POST"
    ]
)
def login():

    if request.method == "POST":

        c = db()


        u = c.execute(
            """
            SELECT *
            FROM users
            WHERE username=?
            AND active=1
            """,
            (
                request.form[
                    "username"
                ],
            )
        ).fetchone()


        c.close()


        if (
            u
            and
            check_password_hash(
                u["password_hash"],
                request.form[
                    "password"
                ]
            )
        ):

            session.clear()

            session["user_id"] = u[
                "id"
            ]

            session["username"] = u[
                "username"
            ]

            session["role"] = u[
                "role"
            ]

            session["name"] = u[
                "name"
            ]


            if u["role"] == "driver":

                return redirect(
                    url_for("driver")
                )


            return redirect(
                url_for("dashboard")
            )


        return render_template(
            (
                "login_mobile_.html"
                if is_mobile_device()
                else "login.html"
            ),
            error=(
                "Benutzername oder "
                "Passwort falsch."
            )
        )


    return render_template(
        (
            "login_mobile_.html"
            if is_mobile_device()
            else "login.html"
        )
    )


# =========================================================
# LOGOUT
# =========================================================

@app.route(
    "/logout"
)
def logout():

    session.clear()

    return redirect(
        url_for("index")
    )


# =========================================================
# DASHBOARD
# =========================================================

@app.route(
    "/dashboard"
)
@login_required([
    "admin",
    "dispatcher",
    "hospital"
])
def dashboard():

    c = db()

    stats = {}
    for status in STATUS:
        row = c.execute(
            """SELECT COUNT(*) AS n FROM orders WHERE status=?""",
            (status,)
        ).fetchone()
        stats[status] = row["n"] if row else 0

    stats["today"] = c.execute(
        """SELECT COUNT(*) AS n FROM orders WHERE date=?""",
        (datetime.now().strftime("%Y-%m-%d"),)
    ).fetchone()["n"]
    stats["open"] = sum(
        stats.get(x, 0) for x in (
            "NEW", "OFFERED", "ACCEPTED", "TO_PICKUP",
            "PICKED_UP", "TO_DESTINATION", "ARRIVED"
        )
    )
    stats["available"] = c.execute(
        """SELECT COUNT(*) AS n FROM drivers WHERE available=1"""
    ).fetchone()["n"]
    stats["completed"] = stats.get("COMPLETED", 0)

    recent = c.execute(
        """SELECT o.*, d.name driver_name
           FROM orders o LEFT JOIN drivers d ON d.id=o.driver_id
           ORDER BY o.id DESC LIMIT 100"""
    ).fetchall()

    drivers = c.execute(
        """SELECT * FROM drivers ORDER BY name"""
    ).fetchall()

    alerts = c.execute(
        """SELECT o.id,o.order_no,o.status,o.pickup,o.destination,o.transport_type
           FROM orders o
           WHERE o.status IN ('PROBLEM','NEW')
           ORDER BY o.id DESC LIMIT 50"""
    ).fetchall()

    invoices = c.execute(
        """SELECT * FROM invoices ORDER BY id DESC LIMIT 50"""
    ).fetchall()

    c.close()

    return render_template(
        "dashboard.html",
        stats=stats,
        orders=recent,
        drivers=drivers,
        alerts=alerts,
        invoices=invoices
    )


# =========================================================
# NEW ORDER
# =========================================================

@app.route(
    "/orders/new",
    methods=[
        "GET",
        "POST"
    ]
)
@login_required([
    "admin",
    "dispatcher",
    "hospital"
])
def new_order():

    if request.method == "GET":

        return render_template(
            "new_order.html",
            today=datetime.now().strftime("%Y-%m-%d")
        )


    data = request.form


    c = db()


    # =====================================================
    # ORDER NUMBER
    # =====================================================

    now = datetime.now()


    order_no = (
        f"EC-{now:%Y%m%d}-"
        f"{now:%H%M%S}-"
        f"{secrets.token_hex(2).upper()}"
    )


    tracking_token = (
        secrets.token_urlsafe(
            20
        )
    )


    # =====================================================
    # CREATE ORDER
    # =====================================================

    cur = c.execute(
        """
        INSERT INTO orders(
            order_no,
            patient_name,
            patient_ref,
            pickup,
            destination,
            transport_type,
            date,
            pickup_time,
            payer,
            insurance_no,
            approval,
            reason,
            notes,
            status,
            created_by,
            created_at,
            tracking_token,
            price_cents,
            direction,
            treatment_facility,
            distance_km,
            copay_cents,
            family_email
        )
        VALUES(
            ?,?,?,?,?,?,?,?,?,?,
            ?,?,?,?,?,?,?,?,?,?,
            ?,?,?
        )
        """,
        (
            order_no,

            data.get(
                "patient_name",
                ""
            ).strip(),

            data.get(
                "patient_ref",
                ""
            ).strip(),

            data.get(
                "pickup",
                ""
            ).strip(),

            data.get(
                "destination",
                ""
            ).strip(),

            data.get(
                "transport_type",
                "sitzend"
            ).strip(),

            data.get(
                "date",
                ""
            ).strip(),

            data.get(
                "pickup_time",
                ""
            ).strip(),

            data.get(
                "payer",
                ""
            ).strip(),

            data.get(
                "insurance_no",
                ""
            ).strip(),

            data.get(
                "approval",
                ""
            ).strip(),

            data.get(
                "reason",
                ""
            ).strip(),

            data.get(
                "notes",
                ""
            ).strip(),

            "NEW",

            session[
                "user_id"
            ],

            datetime.utcnow()
            .isoformat(),

            tracking_token,

            0,

            data.get(
                "direction",
                "hinfahrt"
            ).strip(),

            data.get(
                "treatment_facility",
                ""
            ).strip(),

            float(
                data.get(
                    "distance_km"
                )
                or 12.4
            ),

            0,

            data.get(
                "family_email",
                ""
            ).strip()
        )
    )


    order_id = (
        c.last_insert_id()
    )


    # PostgreSQL safety fallback: if the wrapper could not expose
    # the generated ID, resolve it from the unique tracking token.
    if not order_id:

        inserted_order = c.execute(
            """
            SELECT id
            FROM orders
            WHERE tracking_token=?
            """,
            (
                tracking_token,
            )
        ).fetchone()

        if inserted_order:
            order_id = inserted_order["id"]


    if not order_id:

        c.close()

        raise RuntimeError(
            "Auftrag wurde erstellt, aber die Auftrags-ID konnte nicht ermittelt werden."
        )


    # =====================================================
    # EVENT
    # =====================================================

    c.execute(
        """
        INSERT INTO events(
            order_id,
            status,
            note,
            created_at
        )
        VALUES(?,?,?,?)
        """,
        (
            order_id,
            "NEW",
            (
                "Auftrag erstellt von "
                f"{session.get('name', 'User')}"
            ),
            datetime.utcnow()
            .isoformat()
        )
    )


    c.commit()
    c.close()


    return redirect(
        url_for(
            "order_detail",
            order_id=order_id
        )
    )


# =========================================================
# ORDER DETAIL
# =========================================================

@app.route(
    "/orders/<int:order_id>"
)
@login_required([
    "admin",
    "dispatcher",
    "hospital",
    "driver"
])
def order_detail(
    order_id
):

    c = db()


    order = c.execute(
        """
        SELECT
            o.*,
            d.name driver_name,
            d.phone driver_phone
        FROM orders o
        LEFT JOIN drivers d
            ON d.id=o.driver_id
        WHERE o.id=?
        """,
        (
            order_id,
        )
    ).fetchone()


    if not order:

        c.close()

        abort(404)


    events = c.execute(
        """
        SELECT *
        FROM events
        WHERE order_id=?
        ORDER BY id
        """,
        (
            order_id,
        )
    ).fetchall()


    c.close()


    return render_template(
        "order_detail.html",
        order=order,
        events=events
    )


# =========================================================
# ORDER OFFER
# =========================================================

@app.post(
    "/orders/<int:order_id>/offer"
)
@login_required([
    "admin",
    "dispatcher",
    "hospital"
])
def offer_order(
    order_id
):

    c = db()


    order = c.execute(
        """
        SELECT *
        FROM orders
        WHERE id=?
        """,
        (
            order_id,
        )
    ).fetchone()


    if not order:

        c.close()

        abort(404)


    now = _utc_now_iso()

    current_round = int(
        order["offer_round"] or 0
    ) + 1

    c.execute(
        """
        UPDATE orders
        SET
            status='OFFERED',
            offer_started_at=?,
            offer_round=?
        WHERE id=?
        """,
        (
            now,
            current_round,
            order_id
        )
    )


    c.execute(
        """
        INSERT INTO events(
            order_id,
            status,
            note,
            created_at
        )
        VALUES(?,?,?,?)
        """,
        (
            order_id,
            "OFFERED",
            "Auftrag an Fahrer angeboten – 3-Minuten-Angebot gestartet.",
            now
        )
    )


    c.commit()
    c.close()


    return redirect(
        url_for(
            "order_detail",
            order_id=order_id
        )
    )


# =========================================================
# DISPATCH
# =========================================================

@app.get("/orders/<int:order_id>/dispatch")
@login_required(["admin", "dispatcher", "hospital"])
def dispatch(order_id):
    c = db()
    order = c.execute(
        """SELECT * FROM orders WHERE id=?""", (order_id,)
    ).fetchone()
    if not order:
        c.close()
        abort(404)

    drivers = c.execute(
        """SELECT * FROM drivers WHERE available=1 ORDER BY name"""
    ).fetchall()
    ranked = []
    for d in drivers:
        if not capability_ok(d["capability"], order["transport_type"]):
            continue
        dist = distance_km(
            d["lat"], d["lng"], 50.1109, 8.6821
        )
        score = max(0, min(100, round(65 + (35 * max(0, 1 - min(dist, 20) / 20)))))
        ranked.append((score, round(dist, 1), d))
    ranked.sort(key=lambda x: (-x[0], x[1], x[2]["name"]))
    c.close()
    return render_template("dispatch.html", order=order, ranked=ranked)


def _dispatch_order(order_id, requested_driver_id=None):
    c = db()

    # First release offers that have already expired.
    _expire_stale_offers(c)

    order = c.execute(
        "SELECT * FROM orders WHERE id=?",
        (order_id,)
    ).fetchone()

    if not order:
        c.close()
        return {"error": "Auftrag nicht gefunden."}, 404

    if order["status"] not in ("NEW", "OFFERED"):
        c.close()
        return {
            "error": "Auftrag ist nicht mehr disponierbar."
        }, 409

    if requested_driver_id:
        drivers = c.execute(
            """
            SELECT *
            FROM drivers
            WHERE id=?
            AND available=1
            """,
            (requested_driver_id,)
        ).fetchall()
    else:
        drivers = c.execute(
            """
            SELECT *
            FROM drivers
            WHERE available=1
            ORDER BY name
            """
        ).fetchall()

    candidates = [
        d for d in drivers
        if capability_ok(
            d["capability"],
            order["transport_type"]
        )
    ]

    if not candidates:
        c.close()
        return {
            "error": "Kein passender Fahrer verfügbar."
        }, 409

    if requested_driver_id:
        d = candidates[0]
    else:
        d = min(
            candidates,
            key=lambda x: distance_km(
                x["lat"],
                x["lng"],
                50.1109,
                8.6821
            )
        )

    now = _utc_now_iso()
    round_no = int(order["offer_round"] or 0) + 1

    c.execute(
        """
        UPDATE orders
        SET
            status='OFFERED',
            driver_id=?,
            offer_started_at=?,
            offer_round=?
        WHERE id=?
        """,
        (
            d["id"],
            now,
            round_no,
            order_id
        )
    )

    c.execute(
        """
        UPDATE drivers
        SET last_offer_at=?
        WHERE id=?
        """,
        (
            now,
            d["id"]
        )
    )

    c.execute(
        """
        INSERT INTO events(
            order_id,
            status,
            note,
            created_at
        )
        VALUES(?,?,?,?)
        """,
        (
            order_id,
            "OFFERED",
            (
                f"3-Minuten-Angebot an {d['name']} "
                f"(Runde {round_no})"
            ),
            now
        )
    )

    c.commit()
    c.close()

    return {
        "ok": True,
        "driver": d["name"],
        "offer_round": round_no,
        "offer_timeout_minutes": OFFER_TIMEOUT_MINUTES
    }, 200


@app.post("/api/dispatch/<int:order_id>")
@login_required(["admin", "dispatcher", "hospital"])
def api_dispatch(order_id):
    payload = request.get_json(silent=True) or {}
    driver_id = payload.get("driver_id")
    try:
        driver_id = int(driver_id) if driver_id is not None else None
    except (TypeError, ValueError):
        return jsonify(error="Ungültige Fahrer-ID."), 400
    result, code = _dispatch_order(order_id, driver_id)
    return jsonify(result), code


# =========================================================
# DRIVER
# =========================================================

@app.route(
    "/driver"
)
@login_required([
    "driver"
])
def driver():

    c = db()


    d = c.execute(
        """
        SELECT *
        FROM drivers
        WHERE user_id=?
        """,
        (
            session[
                "user_id"
            ],
        )
    ).fetchone()


    if not d:

        c.close()

        abort(403)


    _expire_stale_offers(c)

    # Only the driver to whom an offer was actually assigned
    # may see and accept that offer.
    orders = c.execute(
        """
        SELECT *
        FROM orders
        WHERE status IN(
            'OFFERED',
            'ACCEPTED',
            'TO_PICKUP',
            'PICKED_UP',
            'TO_DESTINATION',
            'ARRIVED'
        )
        AND driver_id=?
        ORDER BY
            CASE
                WHEN status='OFFERED'
                THEN 0
                ELSE 1
            END,
            id DESC
        """,
        (
            d["id"],
        )
    ).fetchall()

    offers = [
        o for o in orders
        if o["status"] == "OFFERED"
    ]

    active = [
        o for o in orders
        if o["status"] != "OFFERED"
    ]

    today_count = c.execute(
        """
        SELECT COUNT(*) AS n
        FROM orders
        WHERE driver_id=?
        AND date=?
        """,
        (
            d["id"],
            datetime.now().strftime("%Y-%m-%d")
        )
    ).fetchone()["n"]

    c.close()

    return render_template(
        "driver.html",
        driver=d,
        orders=orders,
        offers=offers,
        active=active,
        today_count=today_count,
        next_status=NEXT_STATUS,
        offer_timeout_minutes=OFFER_TIMEOUT_MINUTES
    )


# =========================================================
# DRIVER ACCEPT
# =========================================================

@app.post(
    "/driver/accept/<int:order_id>"
)
@login_required([
    "driver"
])
def driver_accept(
    order_id
):

    c = db()


    d = c.execute(
        """
        SELECT *
        FROM drivers
        WHERE user_id=?
        """,
        (
            session[
                "user_id"
            ],
        )
    ).fetchone()


    o = c.execute(
        """
        SELECT *
        FROM orders
        WHERE id=?
        """,
        (
            order_id,
        )
    ).fetchone()


    if (
        not d
        or not o
        or o["status"] != "OFFERED"
        or o["driver_id"] != d["id"]
        or not capability_ok(
            d["capability"],
            o["transport_type"]
        )
    ):

        c.close()

        abort(403)


    c.execute(
        """
        UPDATE orders
        SET
            driver_id=?,
            status='ACCEPTED',
            accepted_at=?,
            offer_started_at=NULL
        WHERE id=?
        """,
        (
            d["id"],
            datetime.utcnow()
            .isoformat(),
            order_id
        )
    )


    c.execute(
        """
        UPDATE drivers
        SET available=0
        WHERE id=?
        """,
        (
            d["id"],
        )
    )


    c.execute(
        """
        INSERT INTO events(
            order_id,
            status,
            note,
            created_at
        )
        VALUES(?,?,?,?)
        """,
        (
            order_id,
            "ACCEPTED",
            (
                f"Angenommen "
                f"von {d['name']}"
            ),
            datetime.utcnow()
            .isoformat()
        )
    )


    c.commit()


    accepted_order = c.execute(
        """
        SELECT *
        FROM orders
        WHERE id=?
        """,
        (
            order_id,
        )
    ).fetchone()


    c.close()


    if (
        accepted_order
        and accepted_order["family_email"]
        and not accepted_order["family_tracking_sent_at"]
    ):

        try:

            send_family_tracking_email(
                accepted_order,
                d["name"]
            )

        except Exception:

            # Email failure must never block
            # driver acceptance.
            pass


    return redirect(
        url_for("driver")
    )


# =========================================================
# DRIVER STATUS UPDATE
# =========================================================

@app.post(
    "/driver/status/<int:order_id>"
)
@login_required([
    "driver"
])
def driver_status(
    order_id
):

    new_status = (
        request.form.get(
            "status"
        )
        or ""
    ).strip().upper()


    if new_status not in STATUS:

        abort(400)


    c = db()


    d = c.execute(
        """
        SELECT *
        FROM drivers
        WHERE user_id=?
        """,
        (
            session[
                "user_id"
            ],
        )
    ).fetchone()


    order = c.execute(
        """
        SELECT *
        FROM orders
        WHERE id=?
        """,
        (
            order_id,
        )
    ).fetchone()


    if (
        not d
        or not order
        or order["driver_id"] != d["id"]
    ):

        c.close()

        abort(403)


    expected_status = NEXT_STATUS.get(
        order["status"]
    )

    if (
        expected_status is None
        or new_status != expected_status
    ):
        c.close()
        abort(400)


    c.execute(
        """
        UPDATE orders
        SET status=?
        WHERE id=?
        """,
        (
            new_status,
            order_id
        )
    )


    if new_status == "COMPLETED":

        c.execute(
            """
            UPDATE orders
            SET completed_at=?
            WHERE id=?
            """,
            (
                datetime.utcnow()
                .isoformat(),
                order_id
            )
        )

        c.execute(
            """
            UPDATE drivers
            SET available=1
            WHERE id=?
            """,
            (
                d["id"],
            )
        )


    c.execute(
        """
        INSERT INTO events(
            order_id,
            status,
            note,
            created_at,
            lat,
            lng
        )
        VALUES(?,?,?,?,?,?)
        """,
        (
            order_id,
            new_status,
            (
                f"Status geändert von "
                f"{d['name']}"
            ),
            datetime.utcnow()
            .isoformat(),
            d["lat"],
            d["lng"]
        )
    )


    c.commit()
    c.close()


    return redirect(
        url_for(
            "order_detail",
            order_id=order_id
        )
    )


# =========================================================
# DRIVER AVAILABILITY / SHIFT
# =========================================================

@app.post(
    "/driver/availability"
)
@login_required([
    "driver"
])
def driver_availability():

    val = (
        request.form.get(
            "available"
        )
        or "0"
    ).strip() == "1"

    now = datetime.utcnow().isoformat()

    c = db()

    d = c.execute(
        """
        SELECT *
        FROM drivers
        WHERE user_id=?
        """,
        (
            session["user_id"],
        )
    ).fetchone()

    if not d:

        c.close()

        abort(404)


    if val:

        c.execute(
            """
            UPDATE drivers
            SET
                available=1,
                shift_active=1,
                shift_started_at=?,
                last_seen=?
            WHERE id=?
            """,
            (
                now,
                now,
                d["id"]
            )
        )


        c.execute(
            """
            INSERT INTO shift_logs(
                driver_id,
                started_at,
                created_at
            )
            VALUES(?,?,?)
            """,
            (
                d["id"],
                now,
                now
            )
        )


    else:

        c.execute(
            """
            UPDATE shift_logs
            SET ended_at=?
            WHERE driver_id=?
            AND ended_at IS NULL
            """,
            (
                now,
                d["id"]
            )
        )


        c.execute(
            """
            UPDATE drivers
            SET
                available=0,
                shift_active=0,
                shift_started_at=NULL,
                last_seen=?
            WHERE id=?
            """,
            (
                now,
                d["id"]
            )
        )


    c.commit()
    c.close()


    return redirect(
        url_for("driver")
    )


# =========================================================
# DRIVER PERMISSIONS
# =========================================================

@app.route(
    "/driver/permissions",
    methods=[
        "GET",
        "POST"
    ]
)
@login_required([
    "driver"
])
def driver_permissions():

    if request.method == "GET":

        return redirect(
            url_for("driver")
        )


    c = db()


    c.execute(
        """
        UPDATE drivers
        SET
            notification_enabled=?,
            gps_enabled=?,
            last_seen=?
        WHERE user_id=?
        """,
        (
            1
            if request.form.get(
                "notifications"
            )
            else 0,

            1
            if request.form.get(
                "gps"
            )
            else 0,

            datetime.utcnow()
            .isoformat(),

            session["user_id"]
        )
    )


    c.commit()
    c.close()


    return redirect(
        url_for("driver")
    )


# =========================================================
# SERVICE WORKER
# =========================================================

@app.get(
    "/fahrer-sw.js"
)
def fahrer_service_worker():

    service_worker = """

self.addEventListener(
    "install",
    function(event) {
        self.skipWaiting();
    }
);

self.addEventListener(
    "activate",
    function(event) {
        event.waitUntil(
            self.clients.claim()
        );
    }
);

self.addEventListener(
    "fetch",
    function(event) {
        // Network-first behavior.
    }
);

"""


    return app.response_class(
        service_worker,
        mimetype="application/javascript",
        headers={
            "Cache-Control":
                "no-cache, no-store, "
                "must-revalidate"
        }
    )


# =========================================================
# DRIVER PROBLEM
# =========================================================

@app.post(
    "/driver/problem/<int:order_id>"
)
@login_required([
    "driver"
])
def driver_problem(order_id):

    note = request.form.get(
        "problem",
        "Sonstiges"
    )


    c = db()


    d = c.execute(
        """
        SELECT *
        FROM drivers
        WHERE user_id=?
        """,
        (
            session["user_id"],
        )
    ).fetchone()


    o = c.execute(
        """
        SELECT *
        FROM orders
        WHERE id=?
        AND driver_id=?
        """,
        (
            order_id,
            d["id"]
        )
    ).fetchone()


    if not o:
        abort(404)


    now = datetime.utcnow().isoformat()


    c.execute(
        """
        INSERT INTO events(
            order_id,
            status,
            note,
            created_at
        )
        VALUES(?,?,?,?)
        """,
        (
            order_id,
            "PROBLEM",
            note,
            now
        )
    )


    c.execute(
        """
        UPDATE orders
        SET status='PROBLEM'
        WHERE id=?
        """,
        (
            order_id,
        )
    )


    c.commit()
    c.close()


    return redirect(
        url_for("driver")
    )


# =========================================================
# ALERTS
# =========================================================

@app.get(
    "/api/alerts"
)
@login_required([
    "admin",
    "dispatcher"
])
def alerts():

    c = db()

    _expire_stale_offers(c)

    rows = c.execute(
        """
        SELECT
            o.id,
            o.order_no,
            o.status,
            o.pickup,
            o.destination,
            o.transport_type
        FROM orders o
        WHERE o.status IN(
            'PROBLEM',
            'NEW'
        )
        ORDER BY o.id DESC
        """
    ).fetchall()


    c.close()


    return jsonify(
        alerts=[
            dict(r)
            for r in rows
        ]
    )


# =========================================================
# GPS
# =========================================================

@app.post(
    "/api/gps"
)
@login_required([
    "driver"
])
def gps():

    payload = request.get_json(
        force=True
    )


    c = db()


    c.execute(
        """
        UPDATE drivers
        SET
            lat=?,
            lng=?,
            last_seen=?
        WHERE user_id=?
        """,
        (
            payload.get(
                "lat"
            ),

            payload.get(
                "lng"
            ),

            datetime.utcnow()
            .isoformat(),

            session["user_id"]
        )
    )


    c.commit()
    c.close()


    return jsonify(
        ok=True
    )


# =========================================================
# REPORTS
# =========================================================

@app.get(
    "/reports"
)
@login_required([
    "admin",
    "dispatcher",
    "hospital",
    "driver"
])
def reports():

    c = db()


    role = session[
        "role"
    ]


    driver_filter = request.args.get(
        "driver_id",
        type=int
    )


    if role == "driver":

        d = c.execute(
            """
            SELECT *
            FROM drivers
            WHERE user_id=?
            """,
            (
                session["user_id"],
            )
        ).fetchone()


        driver_filter = (
            d["id"]
            if d
            else -1
        )


    drivers = c.execute(
        """
        SELECT *
        FROM drivers
        ORDER BY name
        """
    ).fetchall()


    logs_sql = (
        "SELECT "
        "sl.*, "
        "d.name driver_name, "
        "d.capability "
        "FROM shift_logs sl "
        "JOIN drivers d "
        "ON d.id=sl.driver_id "
        "WHERE 1=1"
    )


    params = []


    if driver_filter:

        logs_sql += (
            " AND sl.driver_id=?"
        )

        params.append(
            driver_filter
        )


    logs_sql += (
        " ORDER BY sl.started_at DESC "
        "LIMIT 200"
    )


    logs = c.execute(
        logs_sql,
        params
    ).fetchall()


    now = datetime.utcnow()

    rows = []

    total_seconds = 0


    for r in logs:

        try:

            start_dt = datetime.fromisoformat(
                r["started_at"]
            )


            end_dt = (

                datetime.fromisoformat(
                    r["ended_at"]
                )

                if r["ended_at"]

                else now
            )


            seconds = (
                max(
                    0,
                    (
                        end_dt
                        -
                        start_dt
                    ).total_seconds()
                )
                -
                int(
                    r["break_minutes"]
                    or 0
                ) * 60
            )


        except Exception:

            seconds = 0


        total_seconds += seconds


        rows.append(
            {
                "driver_name":
                    r["driver_name"],

                "capability":
                    r["capability"],

                "started_at":
                    r["started_at"],

                "ended_at":
                    r["ended_at"],

                "break_minutes":
                    r["break_minutes"]
                    or 0,

                "hours":
                    round(
                        seconds / 3600,
                        2
                    ),

                "status":
                    (
                        "Offen"
                        if not r["ended_at"]
                        else "Abgeschlossen"
                    )
            }
        )


    where = ""
    p2 = []


    if driver_filter:

        where = (
            " WHERE driver_id=?"
        )

        p2 = [
            driver_filter
        ]


    order_stats = c.execute(
        f"""
        SELECT
            COUNT(*) total,
            SUM(status='COMPLETED') completed,
            COALESCE(
                SUM(distance_km),
                0
            ) km
        FROM orders
        {where}
        """,
        p2
    ).fetchone()


    c.close()


    return render_template(
        "reports.html",
        drivers=drivers,
        rows=rows,
        total_hours=round(
            total_seconds / 3600,
            2
        ),
        order_stats=order_stats,
        driver_filter=driver_filter,
        role=role
    )


# =========================================================
# TRACK
# =========================================================

@app.get(
    "/track/<token>"
)
def track(token):

    c = db()


    o = c.execute(
        """
        SELECT
            o.*,
            d.name driver_name,
            d.lat,
            d.lng
        FROM orders o
        LEFT JOIN drivers d
            ON d.id=o.driver_id
        WHERE o.tracking_token=?
        """,
        (
            token,
        )
    ).fetchone()


    events = (

        c.execute(
            """
            SELECT *
            FROM events
            WHERE order_id=?
            ORDER BY id
            """,
            (
                o["id"],
            )
        ).fetchall()

        if o

        else []
    )


    c.close()


    if not o:
        abort(404)


    return render_template(
        "track.html",
        order=o,
        events=events
    )


# =========================================================
# INVOICE DOWNLOAD
# =========================================================

@app.get(
    "/invoice/<int:order_id>"
)
@login_required([
    "admin",
    "dispatcher"
])
def invoice(order_id):

    c = db()


    o = c.execute(
        """
        SELECT *
        FROM orders
        WHERE id=?
        """,
        (
            order_id,
        )
    ).fetchone()


    c.close()


    if not o:
        abort(404)


    if not o["invoice_no"]:

        make_invoice(
            o
        )


        c = db()


        o = c.execute(
            """
            SELECT *
            FROM orders
            WHERE id=?
            """,
            (
                order_id,
            )
        ).fetchone()


        c.close()


    # ---------------------------------------------------------
    # INVOICE FILE RECOVERY
    # ---------------------------------------------------------
    # Render uses an ephemeral filesystem. After a restart/redeploy,
    # the database record can still contain the old PDF path while
    # the physical PDF file is gone. In that case regenerate it
    # automatically instead of returning HTTP 500.
    # ---------------------------------------------------------

    c = db()


    inv = c.execute(
        """
        SELECT *
        FROM invoices
        WHERE order_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            order_id,
        )
    ).fetchone()


    c.close()


    pdf_missing = (
        not inv
        or not inv["pdf_path"]
        or not os.path.isfile(inv["pdf_path"])
    )


    if pdf_missing:

        # Reload the current order before rebuilding the PDF.
        c = db()

        current_order = c.execute(
            """
            SELECT *
            FROM orders
            WHERE id=?
            """,
            (
                order_id,
            )
        ).fetchone()

        c.close()


        if not current_order:

            abort(404)


        try:

            make_invoice(
                current_order
            )

        except Exception as exc:

            app.logger.exception(
                "Invoice regeneration failed for order %s",
                order_id
            )

            return (
                jsonify(
                    error=(
                        "Rechnung konnte nicht erstellt werden: "
                        f"{exc}"
                    )
                ),
                500
            )


        # Read the newly generated invoice record.
        c = db()

        inv = c.execute(
            """
            SELECT *
            FROM invoices
            WHERE order_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                order_id,
            )
        ).fetchone()

        c.close()


    if (
        not inv
        or not inv["pdf_path"]
        or not os.path.isfile(inv["pdf_path"])
    ):

        return (
            jsonify(
                error=(
                    "Die Rechnungs-PDF konnte nicht "
                    "bereitgestellt werden."
                )
            ),
            500
        )


    return send_file(
        inv["pdf_path"],
        as_attachment=True,
        download_name=(
            f"{inv['invoice_no']}.pdf"
        )
    )


# =========================================================
# API ORDERS
# =========================================================

@app.get(
    "/api/orders"
)
@login_required()
def api_orders():

    c = db()


    rows = c.execute(
        """
        SELECT
            order_no,
            status,
            transport_type,
            pickup,
            destination,
            date,
            pickup_time,
            driver_id
        FROM orders
        ORDER BY id DESC
        LIMIT 100
        """
    ).fetchall()


    c.close()


    return jsonify(
        [
            dict(x)
            for x in rows
        ]
    )


# =========================================================
# HEALTH
# =========================================================

@app.get(
    "/health"
)
def health():

    return {
        "status": "ok",
        "app": "ENTLASS-CONNECT"
    }


# =========================================================
# DATABASE STARTUP INITIALIZATION / MIGRATIONS
# =========================================================
# Run migrations when Gunicorn imports app.py. This is required
# for existing PostgreSQL databases created before newer columns
# (e.g. family_email) were added.
try:
    init_db()
except Exception as exc:
    print(f"DATABASE INITIALIZATION FAILED: {exc}")
    raise
