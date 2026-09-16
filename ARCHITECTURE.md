# ENTLASS-CONNECT – Zielarchitektur

## Rollen
1. Krankenhaus / Station
2. Pflegeheim
3. Patient / Angehörige (optional order requester / tracking only)
4. Dispatcher
5. Admin
6. Fahrer
7. Krankenkasse / Kostenträger (billing recipient)

## Core flow
Krankenhaus/Pflegeheim → Auftrag → Eligibility → GPS ranking → Fahrerangebot → Annahme
→ Abholung → Ziel → Abschluss → Rechnung → Kostenträger

## Dispatch rules
- Liege: only drivers with `liege`
- Rollstuhl: `liege` or `rollstuhl`
- Sitzend: `liege`, `rollstuhl`, or `sitzend`
- Among eligible available drivers: rank by current GPS distance to pickup.
- Offer window: 5 minutes per driver.
- No acceptance: next eligible driver.
- No acceptance after the configured chain: Dispatcher/Admin manual assignment.

## Data minimisation
The demo uses patient name/reference only where necessary. A production implementation
should minimise health-related information and separate operational data from billing data.

## Production evolution
- PostgreSQL
- Redis/Celery/RQ or equivalent for offer timers and jobs
- Map/geocoding provider
- Web Push / native mobile push
- secure transactional email
- ORBIS/HIS interface
- Krankenkassen billing interfaces / validated tariff tables
- immutable audit log
- encryption, backups, retention/deletion policy
- SSO for hospital users where appropriate
- DSGVO / AVV / TOMs / security review
