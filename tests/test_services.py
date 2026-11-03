import re
from datetime import timedelta

import pytest
from django.utils import timezone

from voucher import services
from voucher.models import Assignment, AuditLog, Event, TicketRequest, Voucher
from voucher.services import InvalidTransition

S = TicketRequest.Status
VS = Voucher.Status
AS = Assignment.Status


def refresh(*objs):
    for obj in objs:
        obj.refresh_from_db()


def assign(make_request, make_voucher, **kwargs):
    request = make_request(**kwargs)
    voucher = make_voucher()
    assignment = services.assign_next(voucher)
    assert assignment is not None
    return assignment, voucher, request


def no_codes_in_audit():
    codes = set(Voucher.objects.values_list("code", flat=True))
    for entry in AuditLog.objects.all():
        dumped = str(entry.details) + entry.action
        assert not any(code in dumped for code in codes)


# --- assign_next ------------------------------------------------------------


def test_assign_next_takes_lowest_waiting_position(make_request, make_voucher, mails):
    later = make_request(position=5)
    first = make_request(position=2)
    make_request(position=1, status=S.PAUSED)
    voucher = make_voucher()

    assignment = services.assign_next(voucher)

    assert assignment.ticket_request == first
    refresh(voucher, first, later)
    assert voucher.status == VS.ASSIGNED
    assert first.status == S.ASSIGNED
    assert later.status == S.WAITING
    assert assignment.deadline_at == assignment.assigned_at + timedelta(hours=24)
    assert len(mails) == 1
    assert mails[0].to == [first.user.email]
    assert re.fullmatch(r"\[41C3\] Dein Voucher – bitte bis (Mo|Di|Mi|Do|Fr|Sa|So) \d\d:\d\d einlösen", mails[0].subject)
    assert voucher.code in mails[0].body
    assert "voucher@example.de" in mails[0].body
    html = mails[0].alternatives[0][0]
    assert "<strong>Trage beim Einlösen voucher@example.de als Empfängeradresse" in html
    assert AuditLog.objects.filter(action="assign").count() == 1
    no_codes_in_audit()


def test_assign_next_without_waiting_people(make_voucher):
    voucher = make_voucher()
    assert services.assign_next(voucher) is None
    refresh(voucher)
    assert voucher.status == VS.RECEIVED


def test_assign_next_ignores_non_free_voucher(make_request, make_voucher):
    make_request()
    voucher = make_voucher(status=VS.INVALID)
    assert services.assign_next(voucher) is None


def test_auto_assign_off_keeps_voucher_received_until_switched_on(event, make_request, make_voucher):
    event.auto_assign = False
    event.save()
    request = make_request()
    voucher = services.create_voucher(event, "MANUAL0000000001")
    refresh(voucher, request)
    assert voucher.status == VS.RECEIVED
    assert request.status == S.WAITING

    services.set_auto_assign(event, True)
    refresh(voucher, request)
    assert voucher.status == VS.ASSIGNED
    assert request.status == S.ASSIGNED


def test_admin_assigns_specific_person(event, make_request, make_voucher):
    event.auto_assign = False
    event.save()
    make_request()
    paused = make_request(status=S.PAUSED)
    voucher = make_voucher()
    assignment = services.assign_to(voucher, paused)
    assert assignment.ticket_request == paused
    with pytest.raises(InvalidTransition):
        services.assign_to(voucher, paused)


def test_create_voucher_rejects_known_code(event, make_voucher):
    make_voucher(code="DUPLICATE0000001")
    with pytest.raises(InvalidTransition):
        services.create_voucher(event, "DUPLICATE0000001")


# --- waiting list -------------------------------------------------------------


def test_join_appends_and_receives_free_voucher(event, make_user, make_request, make_voucher):
    make_request(position=7, status=S.COMPLETED)
    voucher = make_voucher()
    request = services.join_waitlist(event, make_user())
    refresh(request, voucher)
    assert request.position == 8
    assert request.status == S.ASSIGNED
    assert voucher.status == VS.ASSIGNED


def test_join_twice_is_invalid_but_rejoin_after_withdraw_goes_to_end(event, make_user, make_request):
    user = make_user()
    request = services.join_waitlist(event, user)
    with pytest.raises(InvalidTransition):
        services.join_waitlist(event, user)
    make_request()
    services.withdraw(request)
    rejoined = services.join_waitlist(event, user)
    assert rejoined.pk == request.pk
    assert rejoined.status == S.WAITING
    assert rejoined.position == 3


def test_pause_resume(make_request, make_voucher):
    request = make_request()
    services.pause(request)
    voucher = make_voucher()
    assert services.assign_next(voucher) is None
    with pytest.raises(InvalidTransition):
        services.pause(request)
    services.resume(request)
    refresh(request, voucher)
    assert request.status == S.ASSIGNED  # free voucher handed out on resume


def test_reinstate_removed_person_at_end_of_list(make_request, make_voucher):
    removed = make_request(status=S.REMOVED, position=1)
    make_request(position=5)
    voucher = make_voucher()
    services.assign_next(voucher)  # goes to the waiting person, not to the removed one

    services.reinstate(removed)
    refresh(removed)
    assert removed.status == S.WAITING
    assert removed.position == 6
    with pytest.raises(InvalidTransition):
        services.reinstate(removed)


def test_reorder_and_move_and_skip(event, make_request):
    a, b, c = make_request(), make_request(), make_request()
    services.reorder(event, [c.pk, a.pk, b.pk])
    refresh(a, b, c)
    assert [r.pk for r in event.requests.order_by("position")] == [c.pk, a.pk, b.pk]

    services.move_to(b, 1)
    assert [r.pk for r in event.requests.order_by("position")] == [b.pk, c.pk, a.pk]

    services.skip(b)
    assert [r.pk for r in event.requests.order_by("position")] == [c.pk, b.pk, a.pk]
    with pytest.raises(InvalidTransition):
        services.skip(TicketRequest.objects.get(pk=a.pk))


# --- holder transitions --------------------------------------------------------


def test_redeem_then_pay(make_request, make_voucher, mails):
    assignment, voucher, request = assign(make_request, make_voucher)
    mails.clear()

    services.mark_redeemed(assignment, order_number="ABC12")
    refresh(assignment, voucher, request)
    assert (voucher.status, request.status) == (VS.REDEEMED, S.REDEEMED)
    assert assignment.order_number == "ABC12"
    assert len(mails) == 1 and "bitte bezahlen" in mails[0].subject
    assert "/a/" in mails[0].body  # "Bezahlt" link

    services.mark_paid(assignment)
    refresh(assignment, voucher, request)
    assert (voucher.status, request.status) == (VS.PAID, S.PAID)
    assert assignment.status == AS.ACTIVE
    assert assignment.paid_at is not None


def test_transitions_are_idempotent(make_request, make_voucher, mails):
    assignment, voucher, request = assign(make_request, make_voucher)
    services.mark_redeemed(assignment)
    audit_count, mail_count = AuditLog.objects.count(), len(mails)
    redeemed_at = Assignment.objects.get(pk=assignment.pk).redeemed_at

    with pytest.raises(InvalidTransition):
        services.mark_redeemed(assignment)
    with pytest.raises(InvalidTransition):
        services.report_invalid(assignment)
    with pytest.raises(InvalidTransition):
        services.withdraw(request)

    assert AuditLog.objects.count() == audit_count
    assert len(mails) == mail_count
    assert Assignment.objects.get(pk=assignment.pk).redeemed_at == redeemed_at


def test_pay_before_redeem_is_invalid(make_request, make_voucher):
    assignment, voucher, request = assign(make_request, make_voucher)
    with pytest.raises(InvalidTransition):
        services.mark_paid(assignment)
    refresh(request)
    assert request.status == S.ASSIGNED


def test_report_invalid_keeps_position_and_alerts_admins(make_request, make_voucher, mails):
    assignment, voucher, request = assign(make_request, make_voucher, position=3)
    second = make_voucher()
    mails.clear()

    services.report_invalid(assignment)
    refresh(assignment, voucher, request, second)
    assert voucher.status == VS.INVALID
    assert assignment.status == AS.REVOKED and assignment.revoke_reason == "invalid"
    assert request.position == 3
    # The free second voucher goes to the same person right away.
    assert request.status == S.ASSIGNED
    assert second.status == VS.ASSIGNED
    admin_mails = [m for m in mails if m.to == ["orga@example.de"]]
    assert len(admin_mails) == 1
    assert voucher.code not in admin_mails[0].body


def test_withdraw_waiting(make_request):
    request = make_request()
    services.withdraw(request)
    refresh(request)
    assert request.status == S.WITHDRAWN


def test_withdraw_assigned_passes_voucher_on(make_request, make_voucher):
    assignment, voucher, request = assign(make_request, make_voucher)
    nxt = make_request()
    services.withdraw(request)
    refresh(assignment, voucher, request, nxt)
    assert request.status == S.WITHDRAWN
    assert assignment.status == AS.REVOKED
    assert nxt.status == S.ASSIGNED
    assert voucher.assignments.get(status=AS.ACTIVE).ticket_request == nxt


def test_withdraw_not_allowed_after_redeem(make_request, make_voucher):
    assignment, voucher, request = assign(make_request, make_voucher)
    services.mark_redeemed(assignment)
    with pytest.raises(InvalidTransition):
        services.withdraw(request)


# --- replicated vouchers ----------------------------------------------------------


def paid_assignment(make_request, make_voucher, order_number=None):
    assignment, voucher, request = assign(make_request, make_voucher)
    services.mark_redeemed(assignment, order_number=order_number)
    services.mark_paid(assignment)
    refresh(assignment)
    return assignment


def test_replicated_voucher_fifo_parent(make_request, make_voucher, mails):
    first = paid_assignment(make_request, make_voucher)
    second = paid_assignment(make_request, make_voucher)
    nxt = make_request()
    mails.clear()

    child = services.register_replicated_voucher("CHILD00000000001")

    refresh(first, second, nxt)
    assert child.parent == first.voucher
    assert child.source == Voucher.Source.REPLICATED
    assert first.status == AS.COMPLETED
    assert first.voucher.status == VS.REPLICATED
    assert first.ticket_request.status == S.COMPLETED
    assert second.status == AS.ACTIVE
    assert nxt.status == S.ASSIGNED
    completion = [m for m in mails if m.to == [first.ticket_request.user.email]]
    assert "Dein Folgevoucher ist eingegangen und wurde weitergegeben." in completion[0].body
    assert "/a/" not in completion[0].body  # no buttons


def test_replicated_voucher_parent_by_order_number(make_request, make_voucher):
    first = paid_assignment(make_request, make_voucher, order_number="AAAAA")
    second = paid_assignment(make_request, make_voucher, order_number="XY7Q9")
    child = services.register_replicated_voucher(
        "CHILD00000000002", search_text="Ihre Bestellung XY7Q9 wurde ..."
    )
    refresh(first, second)
    assert child.parent == second.voucher
    assert second.status == AS.COMPLETED
    assert first.status == AS.ACTIVE


def test_replicated_voucher_without_paid_parent(make_request, make_voucher):
    assign(make_request, make_voucher)
    child = services.register_replicated_voucher("ORPHAN0000000001")
    assert child.parent is None


def test_reparent(make_request, make_voucher):
    first = paid_assignment(make_request, make_voucher)
    second = paid_assignment(make_request, make_voucher)
    child = services.register_replicated_voucher("CHILD00000000003")
    assert child.parent == first.voucher

    services.reparent(child, second.voucher)
    refresh(first, second, child)
    assert child.parent == second.voucher
    assert second.status == AS.COMPLETED
    assert first.status == AS.ACTIVE
    assert first.ticket_request.status == S.PAID
    assert first.voucher.status == VS.PAID
    with pytest.raises(InvalidTransition):
        services.reparent(child, second.voucher)


def test_admin_mark_replicated_and_invalid(make_request, make_voucher):
    done = paid_assignment(make_request, make_voucher)
    services.mark_voucher_replicated(done.voucher)
    refresh(done)
    assert done.status == AS.COMPLETED
    assert done.voucher.status == VS.REPLICATED

    free = make_voucher()
    services.mark_voucher_invalid(free)
    refresh(free)
    assert free.status == VS.INVALID
    with pytest.raises(InvalidTransition):
        services.mark_voucher_invalid(free)


# --- admin corrections ---------------------------------------------------------------


def test_revoke_pauses_holder_and_reassigns(make_request, make_voucher):
    assignment, voucher, request = assign(make_request, make_voucher)
    nxt = make_request()
    services.revoke(assignment, "Test")
    refresh(assignment, request, nxt)
    assert assignment.status == AS.REVOKED
    assert request.status == S.PAUSED
    assert nxt.status == S.ASSIGNED


def test_extend_deadline(make_request, make_voucher):
    assignment, voucher, request = assign(make_request, make_voucher)
    before = assignment.deadline_at
    services.extend_deadline(assignment, 12)
    refresh(assignment)
    assert assignment.deadline_at == before + timedelta(hours=12)


# --- deadlines -----------------------------------------------------------------------


def test_redeem_reminder_sent_once(make_request, make_voucher, mails):
    assignment, voucher, request = assign(make_request, make_voucher)
    mails.clear()
    soon = assignment.deadline_at - timedelta(hours=5)
    early = assignment.deadline_at - timedelta(hours=7)

    assert services.process_deadlines(now=early)["redeem_reminders"] == 0
    assert services.process_deadlines(now=soon)["redeem_reminders"] == 1
    assert services.process_deadlines(now=soon)["redeem_reminders"] == 0
    assert len(mails) == 1
    assert mails[0].subject.startswith("[41C3] Erinnerung")
    assert "/a/" in mails[0].body


@pytest.mark.parametrize("policy", [Event.MissedDeadlinePolicy.END_OF_LIST, Event.MissedDeadlinePolicy.REMOVE])
def test_deadline_expiry(event, make_request, make_voucher, mails, policy):
    event.missed_deadline_policy = policy
    event.save()
    assignment, voucher, request = assign(make_request, make_voucher)
    nxt = make_request()
    make_request(status=S.COMPLETED, position=50)
    mails.clear()

    Assignment.objects.filter(pk=assignment.pk).update(deadline_at=timezone.now() - timedelta(minutes=1))
    now = timezone.now()
    result = services.process_deadlines(now=now)

    assert result["expired"] == 1
    refresh(assignment, voucher, request, nxt)
    assert assignment.status == AS.EXPIRED
    assert nxt.status == S.ASSIGNED  # passed on to the next one
    assert voucher.assignments.get(status=AS.ACTIVE).ticket_request == nxt
    if policy == Event.MissedDeadlinePolicy.REMOVE:
        assert request.status == S.REMOVED
    else:
        assert request.status == S.WAITING
        assert request.position == 51
    recipients = [m.to for m in mails]
    assert [request.user.email] in recipients
    assert ["orga@example.de"] in recipients
    # idempotent
    assert services.process_deadlines(now=now)["expired"] == 0


def test_expired_holder_does_not_get_same_voucher_back(make_request, make_voucher):
    assignment, voucher, request = assign(make_request, make_voucher)
    services.process_deadlines(now=assignment.deadline_at + timedelta(minutes=1))
    refresh(voucher, request)
    assert voucher.status == VS.RECEIVED
    assert request.status == S.WAITING


def test_pay_reminder_then_daily(make_request, make_voucher, mails):
    assignment, voucher, request = assign(make_request, make_voucher)
    services.mark_redeemed(assignment)
    refresh(assignment)
    mails.clear()
    t0 = assignment.redeemed_at

    assert services.process_deadlines(now=t0 + timedelta(hours=23))["pay_reminders"] == 0
    assert services.process_deadlines(now=t0 + timedelta(hours=25))["pay_reminders"] == 1
    assert services.process_deadlines(now=t0 + timedelta(hours=30))["pay_reminders"] == 0
    assert services.process_deadlines(now=t0 + timedelta(hours=49))["pay_reminders"] == 1
    assert len(mails) == 2
    assert all("Erinnerung" in m.subject for m in mails)
    # a redeemed assignment never expires
    refresh(assignment)
    assert assignment.status == AS.ACTIVE


def test_audit_entries_never_contain_codes(make_request, make_voucher):
    paid_assignment(make_request, make_voucher)
    services.register_replicated_voucher("CHILD00000000009")
    assert AuditLog.objects.count() >= 5
    no_codes_in_audit()
