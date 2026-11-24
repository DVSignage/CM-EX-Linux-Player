from cmx import inventory as inv


def test_apps_hash_format():
    apps = [{"name": "mpv", "version": "0.37", "publisher": "Debian", "installed": None},
            {"name": "zsh", "version": "5.9", "publisher": None, "installed": "2026-01-02"}]
    h = inv.apps_hash(apps)
    import hashlib
    expected = hashlib.sha256("mpv\t0.37\tDebian\t\nzsh\t5.9\t\t2026-01-02".encode()).hexdigest()
    assert h == "sha256:" + expected


def test_reporter_cadence(tmp_path):
    now = [100_000.0]
    r = inv.InventoryReporter(tmp_path / "s.json", clock=lambda: now[0])
    body = {"apps_hash": "a"}
    assert r.due(body)
    r.sent(body)
    now[0] += 60
    assert not r.due({"apps_hash": "b"})        # never within 15 min of the last report
    now[0] += inv.MIN_GAP
    assert r.due({"apps_hash": "b"})            # a change, after the gap
    assert not r.due(body)                      # unchanged and not a day old
    now[0] += inv.FULL_EVERY
    assert r.due(body)                          # the daily full report
    assert inv.InventoryReporter(tmp_path / "s.json").state["fingerprint"] == inv.fingerprint(body)


def test_collect_shape():
    body = inv.collect("2.1.0", "10.0.0.5")
    assert body["type"] == "inventory" and body["kind"] == "screen"
    assert body["agent"] == {"name": inv.AGENT_NAME, "version": "2.1.0"}
    assert set(body["system"]) == {"pending_reboot", "runs_as_admin", "webview2", "dotnet", "lan_ip"}
    assert body["apps_count"] >= len(body["apps"]) and body["apps_hash"].startswith("sha256:")
