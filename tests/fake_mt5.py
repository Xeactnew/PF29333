"""A small MetaTrader5 stand-in: market + pending orders, SL/TP hits, deals history."""
from types import SimpleNamespace as NS


class FakeMT5:
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT, ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP = 2, 3, 4, 5
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_SLTP, TRADE_ACTION_MODIFY, TRADE_ACTION_REMOVE = 1, 5, 6, 7, 8
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    ORDER_TIME_GTC = 0
    TIMEFRAME_M1 = 1
    CONTRACT = 100

    def __init__(self, bid=4040.0, spread=0.2, balance=10000.0, symbol="XAUUSD.s"):
        self.bid, self.spread, self.balance, self.sym = bid, spread, balance, symbol
        self.positions, self.orders, self.deals = {}, {}, []
        self.ticket = 1000
        self.time = 1_763_200_000
        self.fail_login = False
        self.sent = []

    # ---- connection
    def initialize(self, **kw):
        self.init_kw = kw
        self.login = kw.get("login", 123)
        return not self.fail_login

    def shutdown(self):
        pass

    def last_error(self):
        return (-6, "Terminal: Authorization failed") if self.fail_login else (1, "Success")

    def terminal_info(self):
        return NS(connected=True, trade_allowed=True)

    def account_info(self):
        return NS(login=getattr(self, "login", 123), balance=self.balance, equity=self.balance + self._floating(), currency="USD",
                  company="Fake Broker")

    # ---- symbols
    def symbols_get(self):
        return [NS(name="EURUSD"), NS(name=self.sym), NS(name="XAGUSD")]

    def symbol_info(self, name):
        return NS(name=name, digits=2, volume_step=0.01, volume_min=0.01, volume_max=100.0,
                  trade_contract_size=self.CONTRACT, filling_mode=1, trade_mode=4)

    def symbol_select(self, name, on):
        return True

    @property
    def ask(self):
        return round(self.bid + self.spread, 2)

    def symbol_info_tick(self, name):
        return NS(bid=self.bid, ask=self.ask, time=self.time)

    def order_calc_profit(self, typ, sym, vol, open_, close):
        d = (close - open_) if typ == 0 else (open_ - close)
        return d * self.CONTRACT * vol

    # ---- trading
    def _t(self):
        self.ticket += 1
        return self.ticket

    def _floating(self):
        return sum(self._pnl(p, self.bid if p.type == 0 else self.ask) for p in self.positions.values())

    def _pnl(self, p, close):
        return ((close - p.price_open) if p.type == 0 else (p.price_open - close)) * self.CONTRACT * p.volume

    def _open(self, typ, vol, price, sl, tp, magic, comment, ticket=None):
        t = ticket or self._t()
        p = NS(ticket=t, identifier=t, type=typ, volume=vol, price_open=price, sl=sl, tp=tp, profit=0.0, swap=0.0,
               magic=magic, symbol=self.sym, comment=comment)
        self.positions[t] = p
        self.deals.append(NS(ticket=self._t(), position_id=t, time=self.time, symbol=self.sym, type=typ, entry=0,
                             volume=vol, price=price, profit=0.0, commission=0.0, swap=0.0, fee=0.0,
                             magic=magic, comment=comment))
        return t

    def _close(self, p, price, vol=None, comment="close"):
        vol = vol or p.volume
        pnl = ((price - p.price_open) if p.type == 0 else (p.price_open - price)) * self.CONTRACT * vol
        self.balance += pnl
        self.deals.append(NS(ticket=self._t(), position_id=p.identifier, time=self.time, symbol=self.sym,
                             type=1 - p.type, entry=1, volume=vol, price=price, profit=round(pnl, 2),
                             commission=0.0, swap=0.0, fee=0.0, magic=p.magic, comment=comment))
        if vol >= p.volume - 1e-9:
            del self.positions[p.ticket]
        else:
            p.volume = round(p.volume - vol, 2)

    def order_send(self, r):
        self.sent.append(dict(r))
        ok = lambda order=0, deal=0: NS(retcode=10009, order=order, deal=deal, comment="done")  # noqa: E731
        a = r["action"]
        if a == self.TRADE_ACTION_DEAL:
            if r.get("position"):
                p = self.positions.get(r["position"])
                if not p:
                    return NS(retcode=10013, order=0, deal=0, comment="no position")
                self._close(p, self.bid if p.type == 0 else self.ask, r["volume"])
                return ok()
            typ = r["type"]
            price = self.ask if typ == 0 else self.bid
            t = self._open(typ, r["volume"], price, r.get("sl", 0), r.get("tp", 0), r["magic"], r.get("comment", ""))
            return ok(order=t, deal=t + 1)
        if a == self.TRADE_ACTION_PENDING:
            t = self._t()
            self.orders[t] = NS(ticket=t, type=r["type"], volume_current=r["volume"], price_open=r["price"],
                                sl=r.get("sl", 0), tp=r.get("tp", 0), magic=r["magic"], comment=r.get("comment", ""))
            return NS(retcode=10008, order=t, deal=0, comment="placed")
        if a == self.TRADE_ACTION_SLTP:
            p = self.positions[r["position"]]
            if (p.type == 0 and r["sl"] >= self.bid) or (p.type == 1 and r["sl"] and r["sl"] <= self.ask):
                return NS(retcode=10016, order=0, deal=0, comment="invalid stops")
            p.sl, p.tp = r["sl"], r["tp"]
            return ok()
        if a == self.TRADE_ACTION_MODIFY:
            o = self.orders[r["order"]]
            o.sl, o.tp = r["sl"], r["tp"]
            return ok()
        if a == self.TRADE_ACTION_REMOVE:
            self.orders.pop(r["order"], None)
            return ok()
        return NS(retcode=10013, order=0, deal=0, comment="bad request")

    def positions_get(self, **kw):
        return list(self.positions.values())

    def orders_get(self, **kw):
        return list(self.orders.values())

    def copy_rates_range(self, symbol, timeframe, start, end):
        """One candle per minute between start and end (only reading history)."""
        a, b = int(start.timestamp()) // 60 * 60, int(end.timestamp())
        return [{"time": x, "open": 4040.0, "high": 4041.0, "low": 4039.0, "close": 4040.5,
                 "tick_volume": 10, "spread": 20} for x in range(a, b, 60)]

    def history_deals_get(self, a, b):
        return list(self.deals)

    # ---- market simulation
    def move(self, bid, seconds=1):
        self.bid = round(bid, 2)
        self.time += seconds
        for t, o in list(self.orders.items()):
            fill = ((o.type == 2 and self.ask <= o.price_open) or (o.type == 3 and self.bid >= o.price_open) or
                    (o.type == 4 and self.ask >= o.price_open) or (o.type == 5 and self.bid <= o.price_open))
            if fill:
                del self.orders[t]
                self._open(0 if o.type in (2, 4) else 1, o.volume_current, o.price_open, o.sl, o.tp, o.magic,
                           o.comment, ticket=t)
        for p in list(self.positions.values()):
            px = self.bid if p.type == 0 else self.ask
            if p.type == 0:
                if p.sl and px <= p.sl:
                    self._close(p, p.sl, comment="[sl]")
                elif p.tp and px >= p.tp:
                    self._close(p, p.tp, comment="[tp]")
            else:
                if p.sl and px >= p.sl:
                    self._close(p, p.sl, comment="[sl]")
                elif p.tp and px <= p.tp:
                    self._close(p, p.tp, comment="[tp]")
