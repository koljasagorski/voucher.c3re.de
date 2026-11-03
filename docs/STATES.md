# Zustände

Alle Übergänge laufen über `voucher/services.py`. Ein Übergang, der nicht zum aktuellen
Zustand passt, wirft `InvalidTransition` und ändert nichts.

## TicketRequest

```mermaid
stateDiagram-v2
    [*] --> waiting: join_waitlist (Listenende)
    waiting --> assigned: assign_next / assign_to
    paused --> assigned: assign_to (Admin)
    waiting --> paused: pause (Admin)
    paused --> waiting: resume (Admin)
    assigned --> redeemed: mark_redeemed („Eingelöst“)
    redeemed --> paid: mark_paid („Bezahlt“)
    paid --> completed: register_replicated_voucher / mark_voucher_replicated
    redeemed --> completed: Folgevoucher per Bestellnummer / Admin
    assigned --> waiting: report_invalid (Position bleibt)
    assigned --> waiting: Frist abgelaufen, Policy end_of_list (Listenende)
    assigned --> removed: Frist abgelaufen, Policy remove
    assigned --> paused: revoke (Admin)
    waiting --> withdrawn: withdraw
    paused --> withdrawn: withdraw
    assigned --> withdrawn: withdraw (Voucher geht weiter)
    withdrawn --> waiting: join_waitlist (Listenende)
    completed --> paid: reparent (Kettenglied wieder offen)
    completed --> redeemed: reparent (ohne paid_at)
```

## Voucher

```mermaid
stateDiagram-v2
    [*] --> received: create_voucher (initial) / register_replicated_voucher (replicated)
    received --> assigned: assign_next / assign_to
    assigned --> redeemed: mark_redeemed
    redeemed --> paid: mark_paid
    paid --> replicated: Folgevoucher eingegangen (FIFO / Bestellnummer) oder Admin
    redeemed --> replicated: Folgevoucher per Bestellnummer oder Admin
    assigned --> received: withdraw / revoke / Frist abgelaufen
    assigned --> invalid: report_invalid („funktioniert nicht“)
    received --> invalid: Admin markiert ungültig
    replicated --> paid: reparent (Kind umgehängt)
    replicated --> redeemed: reparent (ohne paid_at)
```

## Assignment

```mermaid
stateDiagram-v2
    [*] --> active: assign_next / assign_to (Frist = jetzt + redeem_deadline_hours)
    active --> completed: Folgevoucher eingegangen / Admin „repliziert“
    active --> revoked: withdraw / revoke / report_invalid
    active --> expired: process_deadlines (Frist vorbei, noch nicht eingelöst)
    completed --> active: reparent (Kettenglied wieder offen)
```

Pro Voucher und pro TicketRequest gibt es höchstens eine aktive Assignment
(Partial-Unique-Constraints in der Datenbank).
