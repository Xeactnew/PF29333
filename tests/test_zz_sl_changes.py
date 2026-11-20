"""The provider moves the stop: by editing the signal, replying to it, or posting a new message.
Groups are kept apart: one group's updates never touch another group's trade."""
from server import telegram_service as ts
from server.db import SessionLocal, Signal, SignalUpdate, main_source
from tests.test_worker import SIG, rig, move  # noqa: F401

A, B = -1001111, -1002222     # two VIP groups (message numbers overlap between groups)
FULL = "XAUUSD SELL 3786-3790\nSL 3795\nTP1 3780\nTP2 3770\nTP3 3760"


def _src():
    with SessionLocal() as s:
        return main_source(s).id


def _new(msg, chat, text=FULL):
    res = ts.handle_message(_src(), msg, text, None, chat_id=chat, chat_title=f"Group {chat}")
    assert res.startswith("signal:"), res
    return int(res.split(":")[1])


def _sig(sid):
    with SessionLocal() as s:
        sig = s.get(Signal, sid)
        ups = [(u.kind, u.value) for u in s.query(SignalUpdate).filter_by(signal_id=sid).order_by(SignalUpdate.id)]
        return sig.sl, list(sig.tps), sig.channel, sig.tg_chat_id, ups


def test_same_message_number_in_two_groups(browser):
    a = _new(88001, A)
    b = _new(88001, B)        # same number, other group: a different signal, not a duplicate
    assert a != b and _sig(a)[2] == f"Group {A}" and _sig(b)[3] == B


def test_edit_moves_stop(browser):
    a = _new(88101, A)
    assert ts.handle_message(_src(), 88101, FULL.replace("3795", "3792"), None, edited=True, chat_id=A) == "levels_edit"
    assert _sig(a)[0] == 3792 and _sig(a)[4] == [("levels", 3792)]


def test_reply_moves_stop_in_many_ways(browser):
    a = _new(88201, A)
    for i, (text, sl) in enumerate([("Move SL to 3791", 3791), ("SL moved to 3789.5", 3789.5), ("New SL 3788", 3788),
                                    ("SL now 3787", 3787), ("Update: SL 3786", 3786), ("SL 3785", 3785),
                                    ("adjust stop loss to 3784", 3784)]):
        res = ts.handle_message(_src(), 88210 + i, text, 88201, chat_id=A)
        assert res.startswith(("update:sl_move", "levels:")), (text, res)
        assert _sig(a)[0] == sl, text
    # a reply re-posting the signal with a new SL
    assert ts.handle_message(_src(), 88230, FULL.replace("3795", "3796.5"), 88201, chat_id=A) == "levels_reply"
    assert _sig(a)[0] == 3796.5
    assert ts.handle_message(_src(), 88232, FULL.replace("3795", "3783.5"), 88201, chat_id=A).startswith("levels")
    assert _sig(a)[0] == 3783.5
    # a reply that repeats the same numbers is commentary, not a second trade
    assert ts.handle_message(_src(), 88231, FULL.replace("3795", "3783.5"), 88201, chat_id=A) == "ignored"
    with SessionLocal() as s:
        assert s.query(Signal).filter(Signal.tg_msg_id == 88231).count() == 0


def test_updates_stay_in_their_own_group(browser):
    a = _new(88301, A)
    b = _new(88302, B)
    assert ts.handle_message(_src(), 88303, "Move SL to 3790", None, chat_id=B).startswith("update:sl_move")
    assert _sig(b)[0] == 3790 and _sig(a)[0] == 3795            # group A's trade untouched
    assert ts.handle_message(_src(), 88304, "TP1 HIT ✅", None, chat_id=A) == "update:tp_hit"
    assert ("tp_hit", 1.0) in _sig(a)[4] and ("tp_hit", 1.0) not in _sig(b)[4]
    # nonsense levels (a pip count, a far-away number) are ignored
    ts.handle_message(_src(), 88305, "Move SL to 150", 88301, chat_id=A)
    ts.handle_message(_src(), 88306, "New SL 4500", 88301, chat_id=A)
    assert _sig(a)[0] == 3795


def test_edited_update_message(browser):
    a = _new(88401, A)
    assert ts.handle_message(_src(), 88402, "Move SL to 3791", 88401, chat_id=A).startswith("update")
    # the update itself is edited to a new number
    res = ts.handle_message(_src(), 88402, "Move SL to 3789", 88401, edited=True, chat_id=A)
    assert res.startswith("update:sl_move") and _sig(a)[0] == 3789


def test_worker_moves_stop_or_closes_if_passed(rig):  # noqa: F811
    mt5, api, w, clock = rig
    api.settings.update(layers=2)
    mt5.move(4036)
    api.push("signal", signal=SIG)        # buy 4030-4037 SL 4027
    w.step()
    api.push("update", update={"signal_id": 1, "kind": "sl_move", "value": 4031})
    w.step()
    assert all(p.sl == 4031 for p in mt5.positions.values()) and all(o.sl == 4031 for o in mt5.orders.values())
    assert w.state["signals"]["1"]["sl"] == 4031
    # new stop already above the price: the provider's stop would have hit, so close
    api.push("update", update={"signal_id": 1, "kind": "sl_move", "value": 4037})
    w.step()
    assert not mt5.positions
