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

## Zustandslogik

- **Idempotenz:** Ein Übergang, der nicht zum aktuellen Zustand passt (auch eine Wiederholung),
  wirft `InvalidTransition` und ändert nichts. Es entstehen weder AuditLog-Einträge noch Mails.
- **Freie Voucher verteilen:** Zusätzlich zu den in der Spezifikation genannten Auslösern
  werden freie Voucher auch dann verteilt, wenn jemand (wieder) `waiting` wird: beim
  Eintragen, bei „Fortsetzen“ und nach „funktioniert nicht“. Sonst bliebe ein Voucher liegen,
  der eingegangen ist, als niemand wartete.
- **Fristablauf:** Die Person, deren Frist abgelaufen ist, bekommt denselben Voucher nicht
  sofort zurück, auch wenn sie nach `end_of_list` die Einzige ist, die wartet.
- **Frist gilt nur fürs Einlösen.** Nach „Eingelöst“ gibt es nur noch Zahlungserinnerungen
  (erst nach `pay_reminder_hours`, danach alle 24 h), aber keinen Ablauf.
- **„Brauche kein Ticket mehr“** ist außer in `waiting`/`assigned` auch in `paused` erlaubt.
  Pausierte Personen sollen sich ebenfalls austragen können.
- **Zuweisung aufheben (Admin):** nur vor dem Einlösen. Die Person wird `paused` und behält
  ihre Position, der Voucher geht an die nächste Person. Wäre sie `waiting`, bekäme sie den
  Voucher sofort zurück.
- **Person überspringen:** Die Person tauscht die Position mit dem nächsten offenen Eintrag
  (`waiting`/`paused`) dahinter, rückt also um einen Platz nach hinten.
- **Positionsfeld:** Die Zahl ist der Rang unter den offenen Einträgen. Umsortieren verwendet
  die vorhandenen Positionswerte neu, dadurch bleiben andere Einträge unberührt.
- **„Vor dir warten N Personen“** zählt Einträge mit Status `waiting` und kleinerer Position.
- **Elternvoucher:** Eine Bestellnummer passt, wenn sie als ganzes Wort im Mailtext vorkommt
  und zu einer aktiven Assignment im Zustand `redeemed` oder `paid` gehört. Sonst gilt FIFO:
  die älteste aktive Assignment im Zustand `paid` (nach `paid_at`). Gibt es keine, bleibt der
  Folgevoucher ohne Elternvoucher, und ein Admin kann ihn zuordnen.
- **Elternvoucher umhängen:** Der alte Elternvoucher wird wieder geöffnet (`paid` bzw.
  `redeemed`), sofern er keine anderen Kinder hat. Der neue wird abgeschlossen, falls er noch
  auf seinen Folgevoucher wartet.
- **Als repliziert markieren (Admin):** schließt das Kettenglied ohne Folgevoucher ab und
  verschickt keine Abschlussmail, weil deren Text („…wurde weitergegeben“) nicht stimmen würde.
- `mark_paid` nimmt optional ebenfalls eine Bestellnummer an, weil die „Bitte bezahlen“-Mail
  das Feld anbietet.
- **Mails** werden beim Übergang gerendert und erst nach dem Commit verschickt
  (`transaction.on_commit`, `robust=True`). Ein SMTP-Fehler rollt so keinen Übergang zurück.
- **Admin-Mails** gehen an `ADMIN_EMAILS`. Ist die Liste leer, gehen sie an alle aktiven
  Staff-User. Sie enthalten nie Voucher-Codes, nur Voucher-IDs und einen Link ins Dashboard.
- Kurze Mails ohne Code und ohne Buttons teilen sich eine Vorlage (`mail/message.*`).

## Mail-Eingang

- **Reihenfolge der Prüfungen:** Message-ID schon bekannt → überspringen (nichts
  gespeichert). Danach Absender (`rejected_sender`), Echtheit (`quarantine`), Code-Suche
  (`no_code`), bekannter Code (`duplicate`). Erst dann `register_replicated_voucher`.
- **Echtheit (Härtung über die Spezifikation hinaus):** Nur der *erste*
  `Authentication-Results`-Header mit unserer `VOUCHER_AUTHSERV_ID` zählt. Unser MTA setzt
  ihn oben drauf, tiefer liegende (fremde oder gefälschte) Header werden ignoriert.
  `dkim=pass` zählt nur, wenn `header.d` zur From-Domain passt (gleich oder Sub-/Oberdomain),
  `spf=pass` nur, wenn `smtp.mailfrom` passt. Sonst könnte jemand mit einer gültigen
  DKIM-Signatur seiner eigenen Domain und gefälschtem From einen Code einschleusen.
  Ist `VOUCHER_AUTHSERV_ID` leer, geht alles in Quarantäne.
- **Mehrere verschiedene Codes** in einer Mail → Quarantäne (der Admin wählt).
- **Kein aktives Event** → Quarantäne mit Notiz.
- `rejected_sender` löst keine Admin-Mail aus (sonst Spam an die Admins). Die Mail steht
  trotzdem im Mail-Log.
- Die Rohmail wird gespeichert (nur Admins sehen sie). Mails werden erst als gelesen
  markiert, wenn sie gespeichert sind.
- **Default `VOUCHER_CODE_REGEX`:** `\b[A-Z0-9]{16}\b` (pretix-Standard: 16 Zeichen,
  Großbuchstaben und Ziffern). Hat die Regex eine Gruppe, zählt Gruppe 1.
- **Fixtures:** Jede `tests/fixtures/mails/<name>.eml` braucht eine `<name>.json` mit dem
  erwarteten `parse_status`, `code` und optional `order_number`. Der Test läuft automatisch
  über alle Dateien.

## Login und Registrierung

- **Alle Links aus Mails** (Verifizierung, Magic-Link, E-Mail-Änderung, Aktionslinks)
  zeigen bei GET nur eine Bestätigungsseite. Erst der POST ändert etwas, damit Mail-Scanner
  nichts auslösen.
- **Magic-Link einmalig:** Das signierte Token enthält `last_login`. Der Login ändert
  `last_login`, damit sind dieser Link und alle älteren Links verbraucht. Keine DB-Tabelle
  nötig. Gültigkeit 15 Minuten. Der Link funktioniert nur, wenn der User aktiv, bestätigt
  und freigeschaltet ist, und das wird beim Einlösen erneut geprüft.
- **Verifizierungs- und Änderungslinks** sind 3 Tage gültig.
- **Keine Account-Enumeration:** Registrierung und Magic-Link-Anforderung antworten immer
  gleich. Eine bekannte, unbestätigte Adresse bekommt die Verifizierungsmail erneut.
- **Freischaltung** schickt eine kurze Mail „Du bist freigeschaltet“. Sonst würde der User
  nie erfahren, wann er sich anmelden kann.
- **Admins** melden sich unter `/konto/admin-anmelden/` mit E-Mail, Passwort und TOTP an.
  Ein Admin, der sich per Magic-Link anmeldet, ist ein normaler User ohne OTP-Verifizierung
  und sieht keine Admin-Views. Das Django-Admin (`/django-admin/`) ist eine `OTPAdminSite`.
  Der erste Admin wird mit `manage.py create_admin` angelegt (Passwort und TOTP-Gerät).
- **Rate-Limits** (Fixed Window im Django-Cache, in Produktion als DB-Cache für alle
  gunicorn-Worker):
  Registrierung 5/h pro IP; Magic-Link 10/15 min pro IP und 3/15 min pro Adresse;
  Admin-Login 10/15 min pro IP und 5/15 min pro Adresse; E-Mail-Änderung 5/h pro IP.
  Hinter dem Reverse Proxy `USE_X_FORWARDED_FOR=1` setzen. Dann gilt der rechteste Eintrag
  aus `X-Forwarded-For`.
- **Account löschen** ist nicht möglich, solange man einen Voucher hält (`assigned`,
  `redeemed`, `paid`). Wartelisten-Einträge werden mitgelöscht. AuditLog-Einträge bleiben,
  verlieren aber den Actor.

## Betrieb

- **Worker** ist eine einfache Shell-Schleife (`docker/worker.sh`) statt supercronic, also
  keine zusätzliche Binary im Image. Ein fehlschlagender Job beendet die Schleife nicht.
- **`purge_old_data`** löscht zusätzlich alle Mails, die älter als 90 Tage sind, auch ohne
  Voucher-Bezug. Events und AuditLog bleiben erhalten (der AuditLog enthält nur IDs).
- **Statische Dateien** liefert WhiteNoise mit gehashten Dateinamen aus. Migrationen und
  `createcachetable` laufen beim Start des `web`-Containers.
- **`VOUCHER_INBOX_ADDRESS`** ist eine zusätzliche Variable für die Adresse, die User im Shop
  eintragen. Standard ist `IMAP_USER`.
