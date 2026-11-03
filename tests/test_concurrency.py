import threading

import pytest
from django.db import connection

from voucher import services
from voucher.models import Assignment, TicketRequest


@pytest.mark.django_db(transaction=True)
def test_two_vouchers_one_waiting_person_gives_exactly_one_assignment(make_request, make_voucher):
    request = make_request()
    vouchers = [make_voucher(), make_voucher()]
    barrier = threading.Barrier(len(vouchers))
    results, errors = [], []

    def worker(voucher):
        try:
            barrier.wait()
            results.append(services.assign_next(voucher))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(v,)) for v in vouchers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    assert len([r for r in results if r is not None]) == 1
    assert Assignment.objects.filter(status=Assignment.Status.ACTIVE).count() == 1
    request.refresh_from_db()
    assert request.status == TicketRequest.Status.ASSIGNED
