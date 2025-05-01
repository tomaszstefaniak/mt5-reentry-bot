import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional
from flask import Flask, request, render_template_string, redirect, url_for, flash

# --------------------------------------------
# Use real MetaTrader5 API on Windows, otherwise fall back to stub
# --------------------------------------------
if sys.platform.startswith("win"):
    import MetaTrader5 as mt5
else:
    from mt5_stub import mt5

# --------------------------------------------
# DATA MODELS
# --------------------------------------------
@dataclass
class LimitOrder:
    ticket: int
    symbol: str
    entry_price: float
    direction: str       # 'LONG' or 'SHORT'
    volume: float
    sl: float            # stop-loss level
    tp: float            # take-profit level
    mode: str            # 'AUTOMATIC' or 'MANUAL'
    adjust_wait: float = 0.0      # seconds (AUTO mode)
    adjust_pct: float = 0.0       # percent (AUTO mode)
    pip_distance: float = 0.0     # pips (MANUAL mode)
    active: bool = True           # watcher flag
    is_market: bool = False       # determines if was originally a market position

@dataclass
class Settings:
    mode: str = 'AUTOMATIC'
    adjust_wait: float = 5.0
    adjust_pct: float = 50.0
    pip_distance: float = 20.0
    enable_market: bool = False

# --------------------------------------------
# GLOBAL STATE
# --------------------------------------------
per_pair_settings: Dict[str, Settings] = {}
global_settings = Settings()
bot: Optional['MT5TradingBot'] = None

# --------------------------------------------
# BOT CORE
# --------------------------------------------
class MT5TradingBot:
    def __init__(self):
        if not mt5.initialize():
            raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
        self.settings = global_settings
        self.tracked: Dict[int, LimitOrder] = {}
        self.running = True
        self.lock = threading.Lock()

    def monitor(self):
        """
        1) Poll new pending LIMIT orders → _add()
        2) If enabled, poll existing MARKET positions → _add_position()
        """
        while self.running:
            # ----- LIMIT orders -----
            for o in (mt5.orders_get() or []):
                if o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT):
                    with self.lock:
                        if o.ticket not in self.tracked:
                            print(f"[BOT] Detected new LIMIT order #{o.ticket}")
                            self._add(o)

            # ----- MARKET positions -----
            if self.settings.enable_market:
                for p in (mt5.positions_get() or []):
                    if p.ticket not in self.tracked:
                        print(f"[BOT] Detected new MARKET position #{p.ticket}")
                        self._add_position(p)

            time.sleep(1)

    def _add(self, o):
        """Track a pending limit order."""
        s = self.settings
        lo = LimitOrder(
            ticket      = o.ticket,
            symbol      = o.symbol,
            entry_price = getattr(o, 'price_open', o.price),
            direction   = 'LONG' if o.type==mt5.ORDER_TYPE_BUY_LIMIT else 'SHORT',
            volume      = o.volume_initial,
            sl          = o.sl,
            tp          = o.tp,
            mode        = s.mode,
            adjust_wait = s.adjust_wait,
            adjust_pct  = s.adjust_pct,
            pip_distance= s.pip_distance,
            is_market   = False   # This is a limit order
        )
        print(f"[BOT] Tracking LIMIT #{lo.ticket}: entry={lo.entry_price}, sl={lo.sl}, tp={lo.tp}")
        self.tracked[lo.ticket] = lo
        threading.Thread(target=self._watch_limit, args=(lo,), daemon=True).start()

    def _add_position(self, p):
        """Track an already-open market position."""
        s = self.settings
        lo = LimitOrder(
            ticket      = p.ticket,
            symbol      = p.symbol,
            entry_price = p.price_open,
            direction   = 'LONG' if p.type==mt5.ORDER_TYPE_BUY else 'SHORT',
            volume      = p.volume,
            sl          = p.sl,
            tp          = p.tp,
            mode        = s.mode,
            adjust_wait = s.adjust_wait,
            adjust_pct  = s.adjust_pct,
            pip_distance= s.pip_distance,
            is_market   = True    # Flag it as a market position
        )
        print(f"[BOT] Tracking MARKET #{lo.ticket}: entry={lo.entry_price}, sl={lo.sl}, tp={lo.tp}")
        self.tracked[p.ticket] = lo
        threading.Thread(target=self._watch_position, args=(lo,), daemon=True).start()

    def _watch_limit(self, lo: LimitOrder):
        """Phase 1: wait for that LIMIT order to fill (and pick up any user tweaks)."""
        while self.running:
            orders = mt5.orders_get() or []
            current = next((x for x in orders if x.ticket==lo.ticket), None)
            if current:
                new_entry = getattr(current,'price_open', current.price)
                if (new_entry!=lo.entry_price) or (current.sl!=lo.sl) or (current.tp!=lo.tp):
                    lo.entry_price, lo.sl, lo.tp = new_entry, current.sl, current.tp
                    print(f"[BOT] UPDATED LIMIT #{lo.ticket} before fill: entry={lo.entry_price}, sl={lo.sl}, tp={lo.tp}")
                time.sleep(0.5)
                continue
            # no longer pending → filled
            print(f"[BOT] Order {lo.ticket} filled")
            break

        self._watch_common(lo)

    def _watch_position(self, lo: LimitOrder):
        """Phase 1: wait until the market position is truly open, pick up any SL/TP changes."""
        while self.running:
            pos = mt5.positions_get(ticket=lo.ticket)
            if pos:
                p = pos[0]
                if (p.sl!=lo.sl) or (p.tp!=lo.tp):
                    lo.sl, lo.tp = p.sl, p.tp
                    print(f"[BOT] UPDATED SL/TP on POSITION #{lo.ticket}: sl={lo.sl}, tp={lo.tp}")
                break
            time.sleep(0.5)

        self._watch_common(lo)

    def _watch_common(self, lo: LimitOrder):
        """Phase 2: monitor until SL/TP hit, then re-enter."""
        while self.running and lo.active:
            pos = mt5.positions_get(ticket=lo.ticket)
            if pos:
                p = pos[0]
                if (p.sl!=lo.sl) or (p.tp!=lo.tp):
                    lo.sl, lo.tp = p.sl, p.tp
                    print(f"[BOT] UPDATED SL/TP on POSITION #{lo.ticket}: sl={lo.sl}, tp={lo.tp}")
                time.sleep(0.5)
                continue

            # position gone → SL/TP hit
            print(f"[BOT] SL/TP hit for ticket {lo.ticket}")
            if lo.mode == 'AUTOMATIC':
                self._auto(lo)
            else:
                self._manual(lo)
            lo.active = False
            self.tracked.pop(lo.ticket, None)

    def _auto(self, lo: LimitOrder):
        """
        1) Duplicate @ current entry/sl/tp
        2) Wait adjust_wait
        3) If price moved further into loss, remove old + re-send at adjusted
        """
        print(f"[BOT] AUTO re-entry for ticket {lo.ticket} @ {lo.entry_price}")
        
        # For market positions, we need to send a market order directly, not a pending order
        if lo.is_market:
            lo.ticket = self._send_market(lo.entry_price, lo)
        else:
            lo.ticket = self._send(lo.entry_price, lo)

        if not lo.is_market:  # Only do the adjustment for pending limit orders
            time.sleep(lo.adjust_wait)
            tick     = mt5.symbol_info_tick(lo.symbol)
            movement = tick.last - lo.sl
            adj      = movement * lo.adjust_pct / 100
            if lo.direction=='LONG':
                new_entry = lo.entry_price - adj
            else:
                new_entry = lo.entry_price + adj

            # only adjust if moved further against us
            if (lo.direction=='LONG' and new_entry < lo.entry_price) or \
               (lo.direction=='SHORT' and new_entry > lo.entry_price):

                print(f"[BOT] AUTO adjusting existing pending @ {new_entry:.8f}")
                # remove old pending
                remove_req = {
                    'action': mt5.TRADE_ACTION_REMOVE,
                    'order':  lo.ticket,
                }
                res = mt5.order_send(remove_req)
                print(f"[BOT] order_send(REMOVE) → retcode={res.retcode}")

                # update our local levels
                delta = new_entry - lo.entry_price
                lo.entry_price += delta
                lo.sl          += delta
                lo.tp          += delta

                # re-send the adjusted pending
                lo.ticket = self._send(lo.entry_price, lo)
            else:
                print("[BOT] Price did not move further losing → no adjustment")

    def _manual(self, lo: LimitOrder):
        """
        1) Shift SL by pip_distance
        2) Apply same to entry & TP
        3) Place
        """
        print(f"[BOT] MANUAL re-entry for ticket {lo.ticket}")
        point = mt5.symbol_info(lo.symbol).point
        mult  = -1 if lo.direction=='LONG' else 1
        delta = lo.pip_distance * point * mult

        lo.entry_price = lo.sl + delta
        lo.sl         += delta
        lo.tp         += delta
        
        print(f"[BOT] MANUAL placing @ {lo.entry_price:.8f}")
        
        # For market positions, we need to send a market order directly
        if lo.is_market:
            lo.ticket = self._send_market(lo.entry_price, lo)
        else:
            lo.ticket = self._send(lo.entry_price, lo)

    def _send(self, price: float, lo: LimitOrder) -> int:
        """
        Send a pending limit order request. Returns the new ticket.
        """
        req = {
            'action':    mt5.TRADE_ACTION_PENDING,
            'symbol':    lo.symbol,
            'volume':    lo.volume,
            'type':      mt5.ORDER_TYPE_BUY_LIMIT if lo.direction=='LONG'
                        else mt5.ORDER_TYPE_SELL_LIMIT,
            'price':     price,
            'sl':        lo.sl,
            'tp':        lo.tp,
            'deviation': 10,
            'magic':     123456,
            'comment':   'AutoReEntryBot'
        }
        result = mt5.order_send(req)
        print(f"[BOT] order_send() → {req}  retcode={result.retcode}, ticket={getattr(result,'order',None)}")
        return getattr(result, 'order', 0)

    def _send_market(self, price: float, lo: LimitOrder) -> int:
        """
        Send a market order request. Returns the new ticket.
        """
        req = {
            'action':    mt5.TRADE_ACTION_DEAL,  # Use DEAL for market orders
            'symbol':    lo.symbol,
            'volume':    lo.volume,
            'type':      mt5.ORDER_TYPE_BUY if lo.direction=='LONG'
                        else mt5.ORDER_TYPE_SELL,
            'price':     mt5.symbol_info_tick(lo.symbol).ask if lo.direction=='LONG' 
                        else mt5.symbol_info_tick(lo.symbol).bid,  # Use current price
            'sl':        lo.sl,
            'tp':        lo.tp,
            'deviation': 10,
            'magic':     123456,
            'comment':   'AutoReEntryBot'
        }
        result = mt5.order_send(req)
        print(f"[BOT] order_send(MARKET) → {req}  retcode={result.retcode}, ticket={getattr(result,'order',None)}")
        return getattr(result, 'order', 0)

    def stop(self):
        self.running = False
        mt5.shutdown()


# --------------------------------------------
# FLASK WEB UI
# --------------------------------------------
app = Flask(__name__)
app.secret_key = 'demo_secret'
TEMPLATE = '''
<!doctype html>
<title>MT5 Re-Entry Bot</title>
<h2>Bot Control</h2>
<form method="post" action="{{ url_for('start') }}">
  <label>Mode:</label>
  <select name="mode">
    <option value="AUTOMATIC" {{'selected' if settings.mode=='AUTOMATIC' else ''}}>Automatic</option>
    <option value="MANUAL"    {{'selected' if settings.mode=='MANUAL'    else ''}}>Manual</option>
  </select><br><br>
  <label>Wait (s):</label><input name="wait" type="number" step="0.01" value="{{settings.adjust_wait}}"><br><br>
  <label>Adjust %:</label><input name="pct"  type="number" step="0.01" value="{{settings.adjust_pct}}"><br><br>
  <label>Pip Dist:</label><input name="pip"  type="number" step="0.1"  value="{{settings.pip_distance}}"><br><br>
  <label>
    <input type="checkbox" name="enable_market"
      {% if settings.enable_market %}checked{% endif %}>
    Enable Market-Order Re-Entry
  </label><br><br>
  <button type="submit">Start Bot</button>
</form>
<form method="post" action="{{ url_for('stop') }}" style="margin-top:10px;">
  <button type="submit">Stop Bot</button>
</form>
'''

@app.route('/')
def index():
    return render_template_string(TEMPLATE, settings=global_settings)

@app.route('/start', methods=['POST'])
def start():
    global bot
    global_settings.mode          = request.form['mode']
    global_settings.adjust_wait   = float(request.form['wait'])
    global_settings.adjust_pct    = float(request.form['pct'])
    global_settings.pip_distance  = float(request.form['pip'])
    global_settings.enable_market = ('enable_market' in request.form)
    if bot:
        flash('Bot already running')
    else:
        try:
            bot = MT5TradingBot()
            threading.Thread(target=bot.monitor, daemon=True).start()
            flash('Bot started')
        except Exception as e:
            flash(f'Error: {e}')
    return redirect(url_for('index'))

@app.route('/stop', methods=['POST'])
def stop():
    global bot
    if bot:
        bot.stop()
        bot = None
        flash('Bot stopped')
    else:
        flash('Bot not running')
    return redirect(url_for('index'))

if __name__ == '__main__':
    # Avoid port-5000 conflicts on macOS
    app.run(debug=True, host='0.0.0.0', port=5001)
