# Voucher-Warteliste

Web-App, mit der unsere Gruppe Voucher für den Chaos Communication Congress verteilt. Statt
getrennter Voucher-Ketten gibt es eine zentrale Warteliste: Die App kennt jeden Voucher-Code
und entscheidet, wer ihn als Nächstes bekommt.

Ablauf in Kurzform:

1. Admins tragen die Start-Voucher vom CCC ein. Die App weist jeden Voucher der ersten
   wartenden Person zu und schickt ihr Code, Frist und Anleitung.
2. Die Person löst den Code im Ticketshop ein und gibt dabei die Adresse unseres Postfachs
   für den Folgevoucher an. Dann klickt sie auf „Eingelöst“ und nach dem Bezahlen auf „Bezahlt“.
3. Der Ticketshop schickt den Folgevoucher an unser Postfach. Der Worker holt die Mail ab,
   prüft sie, legt den neuen Voucher an, schließt das Kettenglied ab und gibt den Voucher an
   die nächste Person weiter.

Die App spricht nie mit dem Ticketshop. Zustände ändern sich nur durch Klicks der User,
Aktionen der Admins und eingehende Mails.

- Zustände und Übergänge: [`docs/STATES.md`](docs/STATES.md)
- Entscheidungen und Auslegungen der Spezifikation: [`docs/DECISIONS.md`](docs/DECISIONS.md)

## Abhängigkeiten

| Paket | Wozu |
|---|---|
| Django 5.2 (LTS) | Web-Framework, ORM, Templates, Mail-Versand, Signaturen (`django.core.signing`) |
| psycopg 3 | PostgreSQL-Treiber für den Produktivbetrieb |
| dj-database-url | `DATABASE_URL` in Django-Settings übersetzen, eine Zeile statt eigenem Parser |
| imap-tools | Mails per IMAP abholen und als gelesen markieren (`fetch_mail`) |
| django-otp | TOTP für den Admin-Login, OTP-Pflicht für das Django-Admin |
| gunicorn | WSGI-Server im Container |
| whitenoise | Statische Dateien direkt aus dem Container ausliefern, ohne extra Webserver oder CDN |
| pytest, pytest-django | Tests (nur Entwicklung, `requirements-dev.txt`) |

HTMX und SortableJS liegen als Dateien in `voucher/static/voucher/` (keine CDNs, kein npm).
Rate-Limits, Magic-Links und Aktionslinks sind ohne zusätzliche Pakete umgesetzt.

## Lokales Setup

Voraussetzung: Python 3.12.

```bash
python3.12 -m venv .venv
```

```bash
. .venv/bin/activate
```

```bash
pip install -r requirements-dev.txt
```

```bash
DEBUG=1 python manage.py migrate
```

```bash
DEBUG=1 python manage.py createcachetable
```

```bash
DEBUG=1 EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend python manage.py runserver
```

Mit `DEBUG=1` reicht SQLite (`db.sqlite3`), ein Entwicklungs-`SECRET_KEY` wird gesetzt und
Mails landen mit dem Console-Backend im Terminal. Die App läuft dann unter
http://localhost:8000/.

Tests:

```bash
python -m pytest
```

GitHub Actions (`.github/workflows/tests.yml`) führt die Tests bei jedem Push und Pull
Request gegen SQLite und PostgreSQL aus und baut das Docker-Image.

Die Tests laufen standardmäßig gegen SQLite. Gegen PostgreSQL (empfohlen für den
Nebenläufigkeitstest):

```bash
DATABASE_URL=postgres://user:pass@localhost:5432/voucher python -m pytest
```

## Betrieb mit Docker

```bash
cp .env.example .env
```

`.env` ausfüllen (siehe Kommentare darin), dann:

```bash
docker compose up -d --build
```

Dienste:

- **db**: PostgreSQL 16, Daten im Volume `pgdata`.
- **web**: führt beim Start `migrate` und `createcachetable` aus und startet dann gunicorn auf
  `127.0.0.1:8000`.
- **backup**: täglicher `pg_dump` nach `./backups/` (siehe unten).
- **worker**: einfache Schleife (`docker/worker.sh`). `fetch_mail` und `send_queued_mail`
  alle 5 Minuten, `process_deadlines` alle 10 Minuten, `purge_old_data` täglich. Schlägt ein Job
  fehl, wird er beim nächsten Durchlauf erneut versucht.

Mails gehen über eine Warteschlange raus. Ist der SMTP-Server nicht erreichbar, geht keine
Mail verloren: Der Worker versucht es alle 5 Minuten erneut (bis zu 24 Stunden), und im
Orga-Bereich erscheint ein Hinweis.

### Backups

Der Dienst **backup** schreibt einmal täglich einen Dump der Datenbank nach `./backups/`
(`voucher-JJJJMMTT-HHMM.dump`, pg_dump-Custom-Format) und löscht Dumps nach 14 Tagen
(`BACKUP_KEEP_DAYS` in `.env`). Die Dumps enthalten Voucher-Codes und E-Mail-Adressen:
das Verzeichnis nur für Admins lesbar halten und, wenn möglich, zusätzlich verschlüsselt auf
einen anderen Rechner kopieren.

Wiederherstellen:

```bash
docker compose stop web worker
```

```bash
docker compose exec -T db pg_restore --clean --if-exists -U voucher -d voucher < backups/voucher-20261224-0300.dump
```

```bash
docker compose start web worker
```

### Reverse Proxy

Die App erwartet einen Reverse Proxy, der TLS terminiert und auf `127.0.0.1:8000`
weiterleitet. Er muss `X-Forwarded-Proto` und `X-Forwarded-For` setzen. In `.env`:
`SITE_URL=https://…`, `ALLOWED_HOSTS=…` und `USE_X_FORWARDED_FOR=1` (für die Rate-Limits).
Django leitet HTTP auf HTTPS um und setzt HSTS.

Beispiel (Caddy):

```
voucher.example.de {
    reverse_proxy 127.0.0.1:8000
}
```

## Ersten Admin anlegen (mit TOTP)

```bash
docker compose exec web python manage.py create_admin --email orga@example.de --nickname orga
```

Der Befehl fragt zweimal nach dem Passwort (mindestens 12 Zeichen) und gibt dann eine
`otpauth://`-URI und das Secret aus. Beides in eine Authenticator-App eintragen (z. B. Aegis,
andOTP, 1Password). Die Angaben erscheinen nur einmal.

Anmeldung unter `/konto/admin-anmelden/` mit E-Mail, Passwort und dem aktuellen
TOTP-Code. Danach ist unter `/orga/` das Admin-Dashboard erreichbar. Als Fallback gibt es
das Django-Admin unter `/django-admin/`, ebenfalls nur mit OTP.

- Neues TOTP-Gerät (z. B. Handy verloren):
  `python manage.py create_admin --email … --keep-password --reset-totp`
- Weitere Admins legst du mit demselben Befehl an. Admins sind gleichzeitig normale User
  und können sich selbst auf die Warteliste setzen.

Normale User melden sich ohne Passwort per Magic-Link unter `/konto/anmelden/` an. Nach der
Registrierung und der Bestätigung ihrer Adresse erscheinen sie unter *Orga → Freischaltung*.
Dort können Admins sie freischalten oder ablehnen (die Registrierung wird dann gelöscht).
Wer wegen einer verpassten Frist entfernt wurde, lässt sich in der Warteliste (Filter
„Entfernt“) wieder aufnehmen – am Listenende.

## Postfach einrichten

1. Ein eigenes Postfach anlegen, z. B. `voucher@example.de`. Diese Adresse tragen alle User im
   Ticketshop als Empfänger für den Folgevoucher ein (`VOUCHER_INBOX_ADDRESS`, Standard ist
   `IMAP_USER`).
2. IMAP-Zugang in `.env` eintragen: `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD`,
   `IMAP_FOLDER` (Standard `INBOX`). Verbunden wird per IMAPS (Port 993). Verarbeitete Mails
   werden als gelesen markiert, nicht gelöscht.
3. `VOUCHER_ALLOWED_SENDERS`: erlaubte Absender, kommagetrennt. Einträge mit `@` sind
   Adressen, ohne `@` Domains (z. B. `tickets.events.ccc.de`).
4. `VOUCHER_AUTHSERV_ID`: die authserv-id, mit der **unser** Mailserver den
   `Authentication-Results`-Header schreibt (erster Teil vor dem `;`, z. B. `mail.example.de`).
   Automatisch verarbeitet werden nur Mails mit `dkim=pass` oder `spf=pass`, die zur
   From-Domain passen. Alles andere landet in Quarantäne, und die Admins bekommen eine Mail.
   Ist die Variable leer, landet jede Mail in Quarantäne.
5. SMTP für ausgehende Mails: `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`,
   `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL`. Admin-Benachrichtigungen gehen an
   `ADMIN_EMAILS`.

Quarantäne-Mails und Mails ohne Code bearbeitest du unter *Orga → Mail-Log*: Code eintragen
oder verwerfen.

Test ohne echtes Postfach:

```bash
docker compose exec worker python manage.py fetch_mail
```

## Jahreswechsel

1. *Orga → Events → Neues Event*: Name (erscheint im Betreff, z. B. `42C3`), Slug,
   Voucherphase, Fristen und Policy eintragen und **Aktiv** setzen. Das bisherige Event
   wird dabei automatisch deaktiviert.
2. Die Start-Voucher unter *Orga → Slots → Start-Voucher anlegen* eintragen.
3. Accounts bleiben bestehen. Alle müssen sich für das neue Event neu eintragen
   („Ich möchte ein Ticket“).
4. `purge_old_data` löscht 90 Tage nach `phase_end` eines Events dessen Voucher,
   Zuweisungen, Wartelisten-Einträge und Mails.

## Beispielmails als Fixtures ergänzen

Das genaue Format der Folgevoucher-Mails ist noch nicht bekannt. Sobald eine echte Mail
vorliegt:

1. Die Mail als `.eml` exportieren (Quelltext inklusive aller Header) und als
   `tests/fixtures/mails/<name>.eml` ablegen. **Echten Code, Bestellnummer und Namen durch
   Platzhalter ersetzen.**
2. Den obersten `Authentication-Results`-Header so anpassen, dass er mit
   `mx.example.de;` beginnt. Das ist die authserv-id in den Test-Settings.
3. Daneben `tests/fixtures/mails/<name>.json` mit dem erwarteten Ergebnis anlegen:

   ```json
   {"parse_status": "parsed", "code": "ABCDEFGH23456789", "order_number": "XY7Q9"}
   ```

4. `python -m pytest tests/test_mail_in.py` ausführen. Der Test liest automatisch alle
   Fixtures. Schlägt er fehl, die Code-Erkennung in `voucher/mail_in.py`
   (`extract_codes`) oder `VOUCHER_CODE_REGEX` anpassen.

Die beiden vorhandenen Fixtures `replicated_with_link.eml` und `no_code.eml` sind
Platzhalter.

## Konfiguration

| Variable | Bedeutung |
|---|---|
| `SECRET_KEY` | Pflicht in Produktion. Signiert Sessions, Magic-Links und Aktionslinks |
| `DEBUG` | `1` nur lokal |
| `DATABASE_URL` | z. B. `postgres://voucher:…@db:5432/voucher`, ohne Angabe SQLite |
| `ALLOWED_HOSTS` | kommagetrennt |
| `SITE_URL` | öffentliche Basis-URL, für Links in Mails |
| `USE_X_FORWARDED_FOR` | `1` hinter dem Reverse Proxy |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` | SMTP |
| `ADMIN_EMAILS` | Empfänger der Admin-Benachrichtigungen, kommagetrennt |
| `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_FOLDER` | Postfach für Folgevoucher |
| `VOUCHER_INBOX_ADDRESS` | Adresse, die User im Shop eintragen (Standard: `IMAP_USER`) |
| `VOUCHER_ALLOWED_SENDERS` | erlaubte Absender (Adressen oder Domains) |
| `VOUCHER_CODE_REGEX` | Fallback-Muster für Codes, Standard `\b[A-Z0-9]{16}\b` |
| `VOUCHER_AUTHSERV_ID` | authserv-id des eigenen Mailservers |

## Datenschutz und Sicherheit

- Voucher-Codes sehen nur die zugewiesene Person (Dashboard und Mail) und Admins. Sie stehen
  nie in URLs oder im AuditLog. Ein Logging-Filter maskiert alles, was wie ein Code aussieht.
- Die signierten Tokens aus Magic-Links und Aktionslinks werden im Access-Log und in
  Fehlermeldungen durch `***` ersetzt.
- Links in Mails ändern bei GET nichts. Erst der Button auf der Bestätigungsseite (POST) löst
  die Aktion aus, weil Mailclients und Scanner Links vorab aufrufen.
- Keine Tracking-Scripte und keine externen Ressourcen.
- Gespeichert werden nur Nickname und E-Mail-Adresse. User können ihren Account selbst
  löschen.
