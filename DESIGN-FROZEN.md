# ENTLASS-CONNECT – Frozen Visual Design

`static/design.css` is the visual master layer.

## Rule
Do not change `static/design.css` when adding or fixing business logic. Backend/API/database changes must be implemented without changing the visual layer.

## Visual reference
The approved reference is the ENTLASS-CONNECT multi-screen design board: teal/green/blue gradient login, dark-blue sidebar, white rounded cards, compact KPI cards, live tracking, alarm center, order wizard, driver mobile UI, reports and invoices.

## Integration
`base.html` loads `style.css` first and `design.css` second. `design.css` is the frozen override layer.

## Future work
- Business logic, API, database, Resend, GPS, dispatch algorithm, billing and permissions should be connected underneath this layer.
- If a functional change requires new HTML, preserve the existing classes/layout and do not restyle the visual master.
