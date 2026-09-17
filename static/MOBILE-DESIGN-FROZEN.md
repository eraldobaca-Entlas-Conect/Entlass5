# ENTLASS-CONNECT Login – Design Freeze

The login visual is split into:
- Desktop master design: existing rules in `static/design.css`
- Mobile iOS/Android layer: the responsive block at the end of `static/design.css`

Mobile requirements:
- 100dvh / safe-area support
- no horizontal scrolling
- no normal page scrolling on the login screen
- 16px input font to prevent iOS Safari zoom
- touch-friendly controls
- translucent blue/green login panel integrated with the background
- desktop artwork is not changed by mobile media queries

Functional form markup remains in `templates/login.html`.
