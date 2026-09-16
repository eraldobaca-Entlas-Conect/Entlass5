# Digitale Ablösung des bisherigen Krankenbeförderungsformulars

The supplied paper form contains three main blocks:

1. Verordnung einer Krankenbeförderung
2. Bestätigung des Versicherten / Transporteurs
3. Abrechnungsdaten des Transporteurs

## ENTLASS-CONNECT mapping

### Before the transport – Station / Arzt
Instead of manually checking boxes:
- Grund der Beförderung → guided reason cards
- Hin-/Rückfahrt → one selection
- Transportart → Sitzend / Rollstuhl / Liege
- Genehmigung → status field
- Behandlungstag/Frequenz → structured data when relevant
- Behandlungsstätte → structured destination
- Begründung/Sonstiges → optional note
- Krankenkasse + Versicherungsnummer → structured billing fields

### During transport – Fahrer
- Auftrag angenommen
- Unterwegs zum Abholort
- Patient abgeholt
- Unterwegs zum Ziel
- Angekommen
- Transport abgeschlossen
- optional GPS position
- proof/confirmation can be implemented as digital signature or confirmation step

### After transport – billing
The system can prepare:
- IK des Transporteurs
- Rechnungsnummer
- Belegnummer
- Positionsnummer
- Anzahl
- km
- Gesamt-Brutto
- Zuzahlung
- Kostenträger

The exact billing fields, tariffs, contractual requirements and legal proof requirements must be validated with the relevant Krankenkassen and transport contracts before production use.

## UX principle

The hospital user should NOT see the entire paper form at once.

The intended experience is:
**Patient → Fahrt → Verordnung → Prüfen & Senden**

Four short screens, with defaults and conditional fields. If a field is not relevant to the selected transport/reason, it should not be shown.
