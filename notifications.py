"""ENTLASS-CONNECT family email notifications via Resend."""

import json
import os
import urllib.request
from datetime import datetime
from html import escape

from flask import url_for


def _db():
    """
    Import db lazily from app.py.

    This avoids a circular import when app.py imports
    send_family_tracking_email from this module.
    """
    from app import db
    return db()


def send_family_tracking_email(
    order,
    driver_name=""
):
    """
    Send the family tracking email once via Resend.

    Important:
    - No WhatsApp
    - No SMS
    - No Share
    - Does not expose diagnosis
    - Does not expose insurance number
    - Does not block driver acceptance if sending fails
    """

    recipient = (
        order["family_email"]
        or ""
    ).strip()


    if not recipient:

        return (
            False,
            "Keine Familien-E-Mail hinterlegt."
        )


    api_key = (
        os.getenv(
            "RESEND_API_KEY",
            ""
        )
        .strip()
    )


    from_email = (
        os.getenv(
            "RESEND_FROM_EMAIL",
            ""
        )
        .strip()
    )


    from_name = (
        os.getenv(
            "RESEND_FROM_NAME",
            "ENTLASS-CONNECT"
        )
        .strip()
    )


    if not api_key:

        return (
            False,
            "RESEND_API_KEY fehlt."
        )


    if not from_email:

        return (
            False,
            "RESEND_FROM_EMAIL fehlt."
        )


    # =====================================================
    # CHECK WHETHER THIS EMAIL WAS ALREADY SENT
    # =====================================================

    c = _db()

    try:

        already_sent = c.execute(
            """
            SELECT id
            FROM email_log
            WHERE order_id=?
            AND sent=1
            LIMIT 1
            """,
            (
                order["id"],
            )
        ).fetchone()


        # Extra protection using the order itself.
        already_marked = (
            order["family_tracking_sent_at"]
            if "family_tracking_sent_at"
            in order.keys()
            else None
        )

    finally:

        c.close()


    if already_sent or already_marked:

        return (
            True,
            "Bereits gesendet."
        )


    # =====================================================
    # BUILD TRACKING URL
    # =====================================================

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
        token=order[
            "tracking_token"
        ]
    )


    if base_url:

        tracking_url = (
            base_url
            +
            tracking_path
        )

    else:

        tracking_url = url_for(
            "track",
            token=order[
                "tracking_token"
            ],
            _external=True
        )


    # =====================================================
    # SAFE DISPLAY VALUES
    # =====================================================

    safe_order_no = escape(
        order["order_no"]
        or ""
    )


    safe_driver = escape(
        driver_name
        or "zugewiesener Fahrer"
    )


    safe_tracking_url = escape(
        tracking_url,
        quote=True
    )


    # =====================================================
    # EMAIL SUBJECT
    # =====================================================

    subject = (
        "ENTLASS-CONNECT – "
        f"Transport {order['order_no']} angenommen"
    )


    # =====================================================
    # EMAIL HTML
    # =====================================================
    #
    # Intentionally minimal.
    #
    # NO:
    # - diagnosis
    # - insurance number
    # - patient reference
    # - medical reason
    #
    # Only transport order number, driver and tracking.
    #
    # =====================================================

    html = f"""
<!doctype html>

<html lang="de">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1"
>

<title>
    ENTLASS-CONNECT
</title>

</head>


<body
    style="
        margin:0;
        padding:0;
        background:#f4f7f8;
        font-family:Arial,sans-serif;
        color:#17324d;
    "
>


<div
    style="
        max-width:620px;
        margin:30px auto;
        background:#ffffff;
        border-radius:12px;
        padding:30px;
        box-sizing:border-box;
    "
>


<h2
    style="
        margin-top:0;
        color:#17324d;
    "
>
    ENTLASS-CONNECT
</h2>


<p>
    Der Patiententransport wurde angenommen.
</p>


<div
    style="
        background:#eef7f4;
        border-radius:8px;
        padding:16px;
        margin:20px 0;
    "
>


<p
    style="
        margin:5px 0;
    "
>

<strong>
    Auftrag:
</strong>

{safe_order_no}

</p>


<p
    style="
        margin:5px 0;
    "
>

<strong>
    Fahrer:
</strong>

{safe_driver}

</p>


</div>


<p>

Über den folgenden Link können Sie den
aktuellen Transportstatus abrufen:

</p>


<p
    style="
        margin:25px 0;
    "
>


<a
    href="{safe_tracking_url}"
    style="
        display:inline-block;
        padding:13px 20px;
        background:#2e9a8b;
        color:#ffffff;
        text-decoration:none;
        border-radius:8px;
        font-weight:bold;
    "
>

Transport verfolgen

</a>


</p>


<p
    style="
        font-size:12px;
        color:#667781;
    "
>

Dieser Link enthält nur die für die
Transportverfolgung erforderlichen Informationen.

</p>


</div>


</body>

</html>
"""


    # =====================================================
    # RESEND PAYLOAD
    # =====================================================

    payload = {

        "from": (
            f"{from_name} <{from_email}>"
            if from_name
            else from_email
        ),

        "to": [
            recipient
        ],

        "subject": subject,

        "html": html

    }


    request_data = json.dumps(
        payload
    ).encode(
        "utf-8"
    )


    req = urllib.request.Request(

        "https://api.resend.com/emails",

        data=request_data,

        headers={

            "Authorization":
                f"Bearer {api_key}",

            "Content-Type":
                "application/json",

            "Accept":
                "application/json"

        },

        method="POST"

    )


    # =====================================================
    # SEND EMAIL
    # =====================================================

    try:

        with urllib.request.urlopen(
            req,
            timeout=15
        ) as response:

            response_body = (
                response
                .read()
                .decode(
                    "utf-8"
                )
            )


        # =================================================
        # SUCCESS
        # =================================================

        now = (
            datetime.utcnow()
            .isoformat()
        )


        c = _db()

        try:

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
                VALUES(
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    1
                )
                """,
                (
                    order["id"],
                    recipient,
                    subject,
                    html,
                    now
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
                    order["id"]
                )
            )


            c.commit()

        finally:

            c.close()


        return (
            True,
            response_body
        )


    # =====================================================
    # EMAIL FAILED
    # =====================================================

    except Exception as exc:

        # Log the failed attempt.
        # Do NOT modify the order as "sent".
        #
        # This means a later retry remains possible.

        try:

            c = _db()

            try:

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
                    VALUES(
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        0
                    )
                    """,
                    (
                        order["id"],
                        recipient,
                        subject,
                        html,
                        datetime.utcnow()
                        .isoformat()
                    )
                )


                c.commit()

            finally:

                c.close()

        except Exception:

            # Email logging must never break
            # the driver workflow.
            pass


        return (
            False,
            str(exc)
        )