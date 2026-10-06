import pytest

from app.bot.navigation import Navigation
from app.core.permissions import InputError


def test_tokens_are_bound_one_use_and_expire():
    now = [10]
    nav = Navigation(lambda: now[0])
    token = nav.issue(111, "disable", "private-target")
    assert len(token.encode()) <= 64
    assert "private-target" not in token
    with pytest.raises(InputError):
        nav.consume(222, token)
    assert nav.consume(111, token) == ("disable", "private-target")
    with pytest.raises(InputError):
        nav.consume(111, token)
    other = nav.issue(111, "confirm")
    nav.set_form(111, "name")
    now[0] += 601
    with pytest.raises(InputError):
        nav.consume(111, other)
    assert nav.get_form(111) is None


def test_cancel_invalidates_all_pending_confirmation_tokens():
    nav = Navigation()
    token = nav.issue(111, "confirm")
    nav.set_form(111, "name")
    nav.cancel(111)
    assert nav.get_form(111) is None
    with pytest.raises(InputError):
        nav.consume(111, token)
