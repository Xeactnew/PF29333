import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "node"))
import node_agent  # noqa: E402


def make_node(tmp_path):
    tpl = tmp_path / "MT5"
    (tpl / "Config").mkdir(parents=True)
    (tpl / "logs").mkdir()
    (tpl / "terminal64.exe").write_bytes(b"x")
    (tpl / "Config" / "servers.dat").write_bytes(b"servers")
    (tpl / "Config" / "common.ini").write_bytes("[Common]\r\nLogin=0\r\n[Experts]\r\nEnabled=0\r\nAccount=0\r\n".encode("utf-16"))
    cfg = {"server_url": "http://127.0.0.1:9", "node_token": "t", "mt5_template": str(tpl),
           "accounts_dir": str(tmp_path / "accounts"), "python": sys.executable, "poll_seconds": 1}
    return node_agent.Node(cfg)


def test_terminal_copy_and_algo_trading(tmp_path):
    n = make_node(tmp_path)
    exe = n.prepare_terminal(7)
    assert exe.exists() and not (exe.parent / "logs").exists()
    ini = (exe.parent / "Config" / "common.ini").read_bytes().decode("utf-16")
    assert "Enabled=1" in ini and "AllowLiveTrading=1" in ini and "Login=0" in ini and ini.count("[Experts]") == 1


def test_reconcile_starts_restarts_and_stops(tmp_path, monkeypatch):
    n = make_node(tmp_path)
    a = {"account_id": 5, "login": "1", "server": "S", "password": "p"}
    n.reconcile([a])
    assert 5 in n.procs
    first = n.procs[5]["proc"]
    first.wait(30)                      # worker.py exits here (no MetaTrader5 on Linux)
    n.reconcile([a])                    # crashed recently: waits before restarting
    assert n.procs[5]["proc"] is first and n.procs[5]["wait"] > 0
    n.procs[5]["exited_at"] -= 1000
    n.reconcile([a])
    assert n.procs[5]["proc"] is not first
    n.reconcile([{**a, "password": "new"}])   # changed password -> restarted with new details
    assert 5 in n.procs
    n.reconcile([])
    assert 5 not in n.procs and 5 in n.stopped_at


def test_saved_logins_never_copied(tmp_path):
    n = make_node(tmp_path)
    (n.template / "Config" / "accounts.dat").write_bytes(b"owner's saved login")
    exe = n.prepare_terminal(8)
    assert not (exe.parent / "Config" / "accounts.dat").exists()
    assert (exe.parent / "Config" / "servers.dat").exists()


def test_removed_and_leftover_folders_deleted(tmp_path):
    import os
    n = make_node(tmp_path)
    n.prepare_terminal(11)                          # removed while running
    n.stopped_at[11] = time.time() - node_agent.REMOVED_FOLDER_GRACE - 5
    leftover = n.prepare_terminal(12).parent        # left from before the node restarted
    old = time.time() - node_agent.REMOVED_FOLDER_GRACE - 5
    os.utime(leftover, (old, old))
    keep = n.prepare_terminal(13).parent            # still assigned
    os.utime(keep, (old, old))
    n.reconcile([])                                 # nothing assigned yet: folders 11 goes
    assert not n.account_folder(11).exists()
    n.last_wanted = {13}
    n.remove_orphan_folders()
    assert not leftover.exists() and keep.exists()
