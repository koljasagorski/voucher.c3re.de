# Entscheidungen

Hier stehen Defaults und Auslegungen, die die Spezifikation offen lässt.

## Verteilungsmodell

- Die Warteliste ist strikt FIFO nach `position`. Neue Einträge landen am Listenende.
- **Nicht umgesetzt, aber als Option notiert:** Eine einmalige Auslosung aller Einträge,
  die bis zu einem Stichtag (z. B. `phase_start`) eingegangen sind. Das würde den Vorteil
  für „wer zuerst klickt“ bzw. „wer zuerst freigeschaltet wird“ entfernen. Admins können
  die Reihenfolge ohnehin jederzeit per Drag-and-drop ändern.

## Datenmodell

- `User` nutzt `email` als `USERNAME_FIELD`; `username`, `first_name`, `last_name` sind
  entfernt (keine Klarnamen).
- Es darf nur ein Event gleichzeitig `is_active` sein (Partial-Unique-Constraint).
- Pro Voucher **und** pro TicketRequest höchstens eine aktive Assignment (Partial-Unique-
  Constraints). Das ist zusätzlich die Absicherung gegen Doppelzuweisungen.
- `Assignment` hat zwei zusätzliche Felder `redeem_reminder_sent_at` und
  `pay_reminder_sent_at`. Ohne sie wären die Erinnerungen in `process_deadlines` nicht
  idempotent.
- `InboundMail.parse_status` hat zusätzlich `discarded` für „Admin hat verworfen“.
- `Voucher.__str__` enthält nie den Code.
- SQLite läuft mit `transaction_mode = IMMEDIATE`, damit Schreibtransaktionen serialisiert
  werden (PostgreSQL nutzt `select_for_update`).
