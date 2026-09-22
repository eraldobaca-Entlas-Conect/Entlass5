import json
import os
import urllib.request
from datetime import datetime
from html import escape

from flask import url_for

from core import db


def send_family_tracking_email(order, driver_name=""):
    """
    Sends one tracking email to the family via Resend.

    WhatsApp: NO
    SMS: NO
    Share button: NO

    A failed email never blocks the driver acceptance.
    """

    recipient = (order["family_email"] or "").strip()

    if not recipient:
        return False, "Keine Familien-E-Mail hinterlegt."

    api_key = os.getenv("RESEND_API_KEY", "").strip()
    from_email = os.getenv("RESEND_FROM_EMAIL", "").strip()
    from_name = os.getenv(
        "RESEND_FROM_NAME",
        "ENTLASS-CONNECT"
    ).strip()

    if not api_key or not from_email:
        return False, "Resend configuration missing."

    # ---------------------------------------------------------
    # CHECK IF ALREADY SENT
    # ---------------------------------------------------------

    c = db()

    already_sent = c.execute(
        """
        SELECT id
        FROM email_log
        WHERE order_id=?
        AND sent=1
        LIMIT 1
        """,
        (order["id"],)
    ).fetchone()

    if already_sent or order["family_tracking_sent_at"]:
        c.close()
        return True, "Bereits gesendet."

    c.close()

    # ---------------------------------------------------------
    # TRACKING URL
    # ---------------------------------------------------------

    base_url = (
        os.getenv(
            "PUBLIC_BASE_URL",
            ""
        )
        .strip()
        .rstrip("/")
    )

    tracking_path = url_for(
        "track",
        token=order["tracking_token"]
    )

    if base_url:
        tracking_url = (
            base_url +
            tracking_path
        )
    else:
        tracking_url = url_for(
            "track",
            token=order["tracking_token"],
            _external=True
        )

    # ---------------------------------------------------------
    # SAFE VALUES
    # ---------------------------------------------------------

    order_no = escape(
        order["order_no"] or ""
    )

    driver = escape(
        driver_name or "zugewiesener Fahrer"
    )

    tracking_url_safe = escape(
        tracking_url,
        quote=True
    )

    subject = (
        "ENTLASS-CONNECT – "
        f"Transport {order['order_no']} angenommen"
    )

    # ---------------------------------------------------------
    # EMAIL HTML
    # ---------------------------------------------------------

    html = f"""
<!doctype html>
<html lang="de">

<head>
<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width, initial-scale=1">
</head>

<body style="
    margin:0;
    padding:0;
    background:#f4f7f8;
    font-family:Arial,sans-serif;
    color:#17324d;
">

<div style="
    max-width:620px;
    margin:30px auto;
    background:#ffffff;
    border-radius:12px;
    padding:30px;
">

<h2 style="color:#17324d;">
    ENTLASS-CONNECT
</h2>

<p>
    Der Patiententransport wurde angenommen.
</p>

<div style="
    background:#eef7f4;
    padding:16px;
    border-radius:8px;
">

<p>
<strong>Auftrag:</strong>
{order_no}
</p>

<p>
<strong>Fahrer:</strong>
{driver}
</p>

</div>

<p>
Über den folgenden Link können Sie den
aktuellen Transportstatus abrufen:
</p>

<p style="margin:25px 0;">

<a href="{tracking_url_safe}"
   style="
       display:inline-block;
       padding:13px 20px;
       background:#2e9a8b;
       color:#ffffff;
       text-decoration:none;
       border-radius:8px;
       font-weight:bold;
   ">
    Transport verfolgen
</a>

</p>

<p style="
    font-size:12px;
    color:#667781;
">
Dieser Link enthält nur die für die
Transportverfolgung erforderlichen Informationen.
</p>

</div>

</body>
</html>
"""

    # ---------------------------------------------------------
    # RESEND
    # ---------------------------------------------------------

    payload = {
        "from": (
            f"{from_name} <{from_email}>"
            if from_name
            else from_email
        ),
        "to": [recipient],
        "subject": subject,
        "html": html,
    }

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization":
                f"Bearer {api_key}",

            "Content-Type":
                "application/json",

            "Accept":
                "application/json",
        },
        method="POST",
    )

    try:

        with urllib.request.urlopen(
            req,
            timeout=15
        ) as response:

            response_body = (
                response
                .read()
                .decode("utf-8")
            )

        now = datetime.utcnow().isoformat()

        c = db()

        c.execute(
            """
            INSERT INTO email_log(
                order_id,
                recipient,
                subject,
                body,
                created_at,
                sent
            )
            VALUES(?,?,?,?,?,1)
            """,
            (
                order["id"],
                recipient,
                subject,
                html,
                now,
            )
        )

        c.execute(
            """
            UPDATE orders
            SET family_tracking_sent_at=?
            WHERE id=?
            """,
            (
                now,
                order["id"],
            )
        )

        c.commit()
        c.close()

        return True, response_body

    except Exception as exc:

        # Email failure must NOT block the order.

        try:

            c = db()

            c.execute(
                """
                INSERT INTO email_log(
                    order_id,
                    recipient,
                    subject,
                    body,
                    created_at,
                    sent
                )
                VALUES(?,?,?,?,?,0)
                """,
                (
                    order["id"],
                    recipient,
                    subject,
                    html,
                    datetime.utcnow().isoformat(),
                )
            )

            c.commit()
            c.close()

        except Exception:
            pass

        return False, str(exc)