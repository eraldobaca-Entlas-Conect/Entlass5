# =========================================================
# ENTLASS-CONNECT – MOBILE LOGIN ROUTING
# =========================================================
# Add this helper BEFORE @app.route("/")
# It uses Flask/Werkzeug only – no extra package is required.

def is_mobile_device():
    ua = (request.headers.get("User-Agent") or "").lower()

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

    return any(token in ua for token in mobile_tokens)


@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    # PC/Laptop -> existing login.html
    # iPhone/Android -> separate login_mobile_.html
    if is_mobile_device():
        return render_template("login_mobile_.html")

    return render_template("login.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        c = db()
        u = c.execute(
            "SELECT * FROM users WHERE username=? AND active=1",
            (request.form["username"],)
        ).fetchone()
        c.close()

        if u and check_password_hash(
            u["password_hash"],
            request.form["password"]
        ):
            session.clear()
            session.update(
                user_id=u["id"],
                username=u["username"],
                role=u["role"],
                name=u["name"],
            )

            if u["role"] == "driver":
                return redirect(url_for("driver"))

            return redirect(url_for("dashboard"))

        # Wrong password: return to the correct login page
        error = "Benutzername oder Passwort ist falsch."

        if is_mobile_device():
            return render_template(
                "login_mobile_.html",
                error=error
            )

        return render_template(
            "login.html",
            error=error
        )

    # Direct GET /login
    if is_mobile_device():
        return render_template("login_mobile_.html")

    return render_template("login.html")
