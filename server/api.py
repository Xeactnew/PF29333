"""JSON API used by the VPS node agents and the per-account MT5 workers."""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta

from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import select

from . import notify, runtime, security, services, system
from .db import Account, Deal, NewsEvent, Node, Snapshot, utcnow
from .web import db_session

router = APIRouter(prefix="/api")


def _node(s, authorization: str | None) -> Node:
    token = (authorization or "").removeprefix("Bearer ").strip()
    node = s.scalar(select(Node).where(Node.token_hash == security.hash_token(token))) if token else None
    if not node or not node.enabled:
        raise HTTPException(401, "bad node token")
    return node


def _account(s, node: Node, account_id: int) -> Account:
    acc = s.get(Account, account_id)
    if not acc or acc.node_id != node.id:
        raise HTTPException(409, "account not assigned to this node")
    return acc


@router.post("/node/heartbeat")
async def heartbeat(request: Request, s=Depends(db_session), authorization: str | None = Header(None)):
    node = _node(s, authorization)
    body = await request.json()
    was_offline = not services.node_online(node)
    node.last_seen = utcnow()
    node.info = {k: body.get(k) for k in ("version", "hostname", "running", "cpu", "ram", "disk_free_gb",
                                          "node_hash", "autologon", "reboot_pending", "uptime_h", "elevated",
                                          "last_result", "log_tail", "ram_total_gb", "folders_locked",
                                          "template_has_login", "base_ready", "servers_size")}
    if was_offline:
        services.assign_accounts(s)
    s.commit()
    if was_offline:
        notify.users_changed()
    accounts = list(s.scalars(select(Account).where(Account.node_id == node.id)))
    _, pkg_hash = system.node_package()
    return {"node_id": node.id, "name": node.name, "max_slots": node.max_slots,
            "node_hash": pkg_hash, "commands": services.take_node_commands(node.id),
            "servers": _servers_info(),
            "assignments": [{"account_id": a.id, "login": a.login, "server": a.server,
                             "password": security.decrypt(a.password_enc)} for a in accounts]}


def _news_windows(s, settings: dict) -> list[dict]:
    if not settings.get("news_protection"):
        return []
    cur = [c.strip() for c in settings.get("news_currencies", "USD").split(",") if c.strip()]
    now = utcnow()
    rows = s.scalars(select(NewsEvent).where(NewsEvent.impact == "High", NewsEvent.currency.in_(cur),
                                             NewsEvent.time >= now - timedelta(hours=3),
                                             NewsEvent.time <= now + timedelta(hours=24)).order_by(NewsEvent.time))
    return [{"time": (n.time - datetime(1970, 1, 1)).total_seconds(),
             "title": n.title, "currency": n.currency} for n in rows]


@router.post("/worker/{account_id}/poll")
async def worker_poll(account_id: int, request: Request, s=Depends(db_session),
                      authorization: str | None = Header(None)):
    """Worker sends its latest state and the last event it processed;
    gets back settings, new events and news windows."""
    node = _node(s, authorization)
    acc = _account(s, node, account_id)
    body = await request.json()
    ack = body.get("ack")
    if isinstance(ack, int) and ack > acc.event_cursor:
        acc.event_cursor = ack
    rep = body.get("report")
    if rep:
        acc.conn_status = rep.get("status", acc.conn_status)
        acc.conn_error = str(rep.get("error", ""))[:1000]
        if rep.get("account"):
            a = rep["account"]
            acc.balance = float(a.get("balance", acc.balance))
            acc.equity = float(a.get("equity", acc.equity))
            acc.currency = str(a.get("currency", acc.currency))[:8]
            acc.broker = str(a.get("company", acc.broker))[:128]
            real = str(a.get("server") or "").strip()[:128]
            if real and real != acc.server:
                services.log(s, "server_corrected", acc.user_id, f"{acc.login}: {acc.server} -> {real}", "mt5")
                acc.server = real
        acc.live_state = {k: rep.get(k) or [] for k in ("positions", "pendings", "waiting", "notes", "other_positions")}
        acc.live_state.update({k: rep.get(k) for k in ("blocked", "symbol", "day_pnl", "server_time")})
        acc.live_state["journal"] = [str(x)[:300] for x in (rep.get("journal") or [])[-30:]]
        acc.live_state["unknown_server"] = bool(rep.get("unknown_server"))
        acc.last_report = utcnow()
        if rep.get("snapshot") and rep.get("account"):
            s.add(Snapshot(account_id=acc.id, balance=acc.balance, equity=acc.equity))
    settings = services.worker_settings(acc)
    events = services.events_for_account(s, acc)
    s.commit()
    return {"copy_enabled": bool(acc.copy_enabled and services.user_can_trade(acc.user)),
            "settings": settings, "news": _news_windows(s, settings),
            "events": [{"id": e.id, "kind": e.kind, **e.payload} for e in events],
            "cursor": acc.event_cursor}


@router.post("/worker/{account_id}/deals")
async def worker_deals(account_id: int, request: Request, s=Depends(db_session),
                       authorization: str | None = Header(None)):
    node = _node(s, authorization)
    acc = _account(s, node, account_id)
    body = await request.json()
    have = set(s.scalars(select(Deal.ticket).where(Deal.account_id == acc.id,
                                                   Deal.ticket.in_([d["ticket"] for d in body.get("deals", [])] or [-1]))))
    added = 0
    for d in body.get("deals", []):
        if d["ticket"] in have:
            continue
        s.add(Deal(account_id=acc.id, ticket=int(d["ticket"]), position_id=int(d.get("position_id", 0)),
                   time=datetime(1970, 1, 1) + timedelta(seconds=float(d["time"])),
                   symbol=str(d.get("symbol", ""))[:32], side=d.get("side", ""), entry=d.get("entry", ""),
                   volume=float(d.get("volume", 0)), price=float(d.get("price", 0)),
                   profit=float(d.get("profit", 0)), ours=bool(d.get("ours")),
                   signal_id=d.get("signal_id"), comment=str(d.get("comment", ""))[:64]))
        added += 1
    s.commit()
    return {"added": added}


@router.get("/node/package")
def node_package(s=Depends(db_session), authorization: str | None = Header(None)):
    _node(s, authorization)
    data, h = system.node_package()
    return Response(data, media_type="application/zip", headers={"X-Node-Hash": h})


def _servers_info() -> dict:
    f = system.SERVERS_FILE
    try:
        data = f.read_bytes()
    except OSError:
        return {"size": 0, "sha": ""}
    return {"size": len(data), "sha": hashlib.sha256(data).hexdigest()[:16]}


@router.get("/node/servers")
def get_servers(s=Depends(db_session), authorization: str | None = Header(None)):
    """MT5's broker list (servers.dat), shared so every VPS knows every broker any VPS has found."""
    _node(s, authorization)
    if not system.SERVERS_FILE.exists():
        raise HTTPException(404)
    return Response(system.SERVERS_FILE.read_bytes(), media_type="application/octet-stream")


@router.post("/node/servers")
async def put_servers(request: Request, s=Depends(db_session), authorization: str | None = Header(None)):
    _node(s, authorization)
    data = await request.body()
    if not data or len(data) > 20_000_000:
        raise HTTPException(400, "bad broker list")
    have = system.SERVERS_FILE.stat().st_size if system.SERVERS_FILE.exists() else 0
    if len(data) <= have:
        return {"kept": "ours", "size": have}
    system.SERVERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = system.SERVERS_FILE.with_suffix(".new")
    tmp.write_bytes(data)
    os.replace(tmp, system.SERVERS_FILE)
    return {"kept": "yours", "size": len(data)}


INSTALL_SCRIPT = Path(__file__).parent / "install_node.ps1"
NODE_TOOLS = ("keep_screen_on.ps1", "register_keep_screen_on.ps1", "watchdog.ps1", "register_watchdog.ps1")


@router.get("/node/tool/{name}")
def node_tool(name: str, s=Depends(db_session), authorization: str | None = Header(None)):
    _node(s, authorization)
    if name not in NODE_TOOLS:
        raise HTTPException(404)
    return PlainTextResponse((system.ROOT / "tools" / name).read_text(encoding="utf-8"))


@router.get("/install/{code}.ps1")
def install_script(code: str, request: Request, s=Depends(db_session)):
    """One-line installer for a new VPS. The admin page shows the command."""
    found = services.use_install_code(s, code)
    if not found:
        raise HTTPException(404, "This install link has expired. Make a new one in Admin → Servers.")
    node, token = found
    base = runtime.public_url() or str(request.base_url).rstrip("/")
    script = (INSTALL_SCRIPT.read_text(encoding="utf-8")
              .replace("__SERVER_URL__", base).replace("__NODE_TOKEN__", token)
              .replace("__NODE_NAME__", node.name.replace('"', "'")))
    return PlainTextResponse(script)
