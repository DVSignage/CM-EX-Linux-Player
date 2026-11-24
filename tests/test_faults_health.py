from cmx import faults as F
from cmx.health import is_usable, pick_lan_address


def test_fault_reported_only_after_hold_and_ranked():
    now = [0.0]
    f = F.PlayerFaults(clock=lambda: now[0], hold=10)
    f.set(F.PORT, "port busy")
    f.set(F.DOWNLOADS, "x could not be downloaded")
    assert f.top() is None
    now[0] = 10
    assert f.top() == "x could not be downloaded (+1 more)"
    f.clear(F.DOWNLOADS)
    assert f.top() == "port busy"


def test_nothing_plays_hides_items_fail():
    now = [0.0]
    f = F.PlayerFaults(clock=lambda: now[0], hold=0)
    f.set(F.NOTHING_PLAYS, F.nothing_plays_message(2, "why"))
    f.set(F.ITEMS_FAIL, F.items_fail_message(2, 2, "a.mp4"))
    assert f.top() == "Nothing in the playlist plays: all 2 item(s) failed (why); showing the idle screen"


def test_rewording_keeps_the_fault_clock():
    now = [0.0]
    f = F.PlayerFaults(clock=lambda: now[0], hold=10)
    f.set(F.PORT, "a")
    now[0] = 9
    f.set(F.PORT, "b")
    now[0] = 10
    assert f.top() == "b"


def test_usable_addresses():
    assert is_usable("192.168.1.20")
    for bad in (None, "", "127.0.0.1", "0.0.0.0", "169.254.3.4", "224.0.0.1", "255.255.255.255", "nope"):
        assert not is_usable(bad)


def test_lan_address_order():
    calls = []

    def towards(host):
        calls.append(host)
        return {"cms.local": "127.0.0.1", "8.8.8.8": "10.0.0.7"}[host]

    assert pick_lan_address("cms.local", towards, lambda: []) == "10.0.0.7"
    assert calls == ["cms.local", "8.8.8.8"]


def test_lan_address_falls_back_to_real_adapters():
    def towards(host):
        raise OSError("no route")

    nics = lambda: [("172.17.0.1", True), ("192.168.5.9", False)]
    assert pick_lan_address("cms", towards, nics) == "192.168.5.9"
    assert pick_lan_address("cms", towards, lambda: []) is None
