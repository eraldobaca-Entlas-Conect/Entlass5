# ENTLASS-CONNECT – Fresh Demo MVP (from zero)

Digital platform for patient transport: hospital / Pflegeheim → Patiententransportdienst → Krankenkasse.

## Included in this demo
- Responsive Web-App for PC, iOS and Android browsers
- Roles: Krankenhaus/Pflegeheim, Dispatcher, Admin, Fahrer
- 10 demo drivers: 2 Liege, 5 Rollstuhl, 3 Sitzend
- Capability-based dispatch logic
- GPS browser endpoint for driver position
- 5-minute offer concept (server-side OFFERED state; production needs a real expiry worker)
- Driver workflow: angenommen → unterwegs → abgeholt → unterwegs zum Ziel → angekommen → abgeschlossen
- Public tracking link `/track/<token>`
- Automatic PDF invoice at completion
- SQLite demo database
- Render deployment files

## Demo accounts
- admin / admin123
- dispatcher / dispatch123
- station / station123
- pflege / pflege123
- Fahrer: fahrer1 ... fahrer10 / fahrer123

## Run locally
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python app.py
```
Open http://localhost:5000

## Render
Create a new Web Service from this repository. Render detects `render.yaml`, or use:
- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app`

### Important for the real pilot
This is a DEMO/MVP, not a production medical-data system. Before processing real patient data, add:
- PostgreSQL instead of demo SQLite
- TLS/HTTPS, strong secrets and password policy
- role-based access control and audit logs
- GDPR/DSGVO data minimisation, retention/deletion rules and processor agreements
- encrypted storage/backups
- secure email provider / Krankenkassen billing integration
- real geocoding/maps and GPS consent/device handling
- reliable 5-minute offer worker/queue
- invoice validation against each Krankenkasse contract/tariff
- real ORBIS/HIS integration only after the hospital approves an interface
- penetration test and security review

No real patient data is included in the demo.


## Fresh rebuild policy
This project is intentionally independent from the old Entlass2 codebase.
Do not copy old Python, templates, CSS, JavaScript, database files, or configuration
into this repository. The old repository can remain inactive/archive-only.
