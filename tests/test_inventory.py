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


def test_ssh_auth_dropins_win_and_key_only_is_unknown(tmp_path):
    (tmp_path / "sshd_config.d").mkdir()
    (tmp_path / "sshd_config").write_text("Include /etc/ssh/sshd_config.d/*.conf\nPort 2222\nPasswordAuthentication yes\n")
    assert inv._ssh_details(tmp_path) == {"ports": [2222], "auth": "password"}
    (tmp_path / "sshd_config.d" / "50-cloud.conf").write_text("PasswordAuthentication no\n")
    assert inv._ssh_details(tmp_path)["auth"] == "unknown"      # keys only: not open, not a password
    empty = tmp_path / "none"
    empty.mkdir()
    assert inv._ssh_details(empty) == {"ports": [22], "auth": "password"}   # OpenSSH's default


def test_service_state_words(monkeypatch):
    answers = {"active": "running", "inactive": "stopped", "failed": "stopped", "unknown": "absent"}
    monkeypatch.setattr(inv.shutil, "which", lambda name: "/usr/bin/" + name)
    for systemctl, expected in answers.items():
        monkeypatch.setattr(inv, "_run", lambda args, timeout=0, out=systemctl: out + "\n")
        assert inv.service_state(["x"], running=False) == expected
    assert inv.service_state([], running=True) == "running"


def test_remote_tool_ids_follow_the_cms_list():
    ids = [t[0] for t in inv.REMOTE_TOOLS]
    assert "realvnc" in ids and "vnc" not in ids and "rdp" in ids
