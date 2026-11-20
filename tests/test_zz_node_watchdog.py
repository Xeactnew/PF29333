"""This VPS's MT5 manager is restarted by the website when it stops checking in; only one copy runs."""
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

from server import scheduler, system


def test_watchdog_restarts_local_node_only(monkeypatch):
    calls = []
    monkeypatch.setattr(system, "restart_local_node", lambda wait=15: calls.append(1) or "started the MT5 manager again")
    scheduler._heal.clear()
    monkeypatch.setattr(scheduler, "_website_started", 0)
    here = NS(info={"hostname": socket.gethostname()})
    elsewhere = NS(info={"hostname": "some-other-vps"})
    assert scheduler.is_this_vps(here) and not scheduler.is_this_vps(elsewhere)
    assert scheduler.heal_local_node(1, "VPS 1")
    assert not scheduler.heal_local_node(1, "VPS 1")          # at most every 5 minutes
    import time
    time.sleep(0.2)
    assert calls == [1]


def test_restart_local_node_starts_the_task(monkeypatch):
    monkeypatch.setattr(system, "IS_WINDOWS", True)
    seq = [[NS(kill=lambda: None)], []]                       # one stuck copy, then nothing came back
    monkeypatch.setattr(system, "_node_processes", lambda: seq.pop(0) if seq else [])
    ran = []
    monkeypatch.setattr(system.subprocess, "run", lambda args, **kw: ran.append(args) or NS(returncode=0, stdout="", stderr=""))
    assert system.restart_local_node(wait=0) == "started the MT5 manager again"
    assert ran and ran[0][:3] == ["schtasks", "/run", "/tn"] and ran[0][3] == system.NODE_TASK


def test_second_node_agent_stops_with_code_3(tmp_path):
    node_dir = Path(__file__).resolve().parent.parent / "node"
    code = ("import sys; sys.path.insert(0, %r); import node_agent as n; n.HERE = __import__('pathlib').Path(%r); "
            "print(n.single_instance()); print(n.single_instance.__module__)" % (str(node_dir), str(tmp_path)))
    first = subprocess.Popen([sys.executable, "-c", code + "; import time; time.sleep(3)"], stdout=subprocess.PIPE, text=True)
    import time
    time.sleep(1)
    second = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert second.stdout.splitlines()[0] == "False"
    first.kill()
    assert Path(node_dir / "start_node_hidden.vbs").read_text().count('Run "cmd /c start_node.bat", 0') == 1
    assert "errorlevel%==3" in (node_dir / "start_node.bat").read_text()


def test_start_script_relaunches_hidden():
    bat = (Path(__file__).resolve().parent.parent / "node" / "start_node.bat").read_text()
    assert "PF_NODE_HIDDEN" in bat and "start_node_hidden.vbs" in bat and bat.index("exit /b 0") < bat.index(":loop")
