import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional
from flask import Flask, request, render_template_string, redirect, url_for, flash

# ————————————————————————————————————————————
# Use real MT5 on Windows, otherwise use stub
# ————————————————————————————————————————————
if sys.platform.startswith("win"):
    import MetaTrader5 as mt5
else:
    from mt5_stub import mt5

# ————————————————————————————————————————————
# DATA MODELS
# ————————————————————————————————————————————
@dataclass
class LimitOrder:
    ticket: int
    symbol: str
    entry_price: float
    direction: str            # 'LONG' or 'SHORT'
    volume: float
    sl: float                 # stop-loss level
    tp: float                 # take-profit level
    mode: str                 # 'AUTOMATIC' or 'MANUAL'
    adjust_wait: float = 0.0  # seconds before adjustment
    adjust_pct: float = 0.0   # adjustment % in AUTO mode
    pip_distance: float = 0.0 # pips in MANUAL mode
    pending_ticket: Optional[int] = None  # ID of the duplicate pending order
    active: bool = True       # watcher loop flag

@dataclass
class Settings:
    mode: str = 'AUTOMATIC'
    adjust_wait: float = 5.0
    adjust_pct: float = 50.0
    pip_distance: float = 20.0
    enable_market: bool = False

# Global & per-symbol settings
per_pair_settings: Dict[str, Settings] = {}
global_settings = Settings()
bot: Optional['MT5TradingBot'] = None

# ————————————————————————————————————————————
# BOT CORE
# ————————————————————————————————————————————
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
        1) Poll for new pending LIMIT orders
        2) If market-reentry enabled, poll live POSITIONS
        """
        while self.running:
            # --- LIMIT orders ---
            for o in (mt5.orders_get() or []):
                if o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT):
                    with self.lock:
                        if o.ticket not in self.tracked:
                            print(f"[BOT] Detected new LIMIT order #{o.ticket}")
                            self._add(o, is_market=False)

            # --- MARKET orders (already filled positions) ---
            if self.settings.enable_market:
                for p in (mt5.positions_get() or []):
                    if p.ticket not in self.tracked:
                        print(f"[BOT] Detected new MARKET position #{p.ticket}")
                        self._add(p, is_market=True)

            time.sleep(1)

    def _add(self, o, is_market: bool):
        """
        Build a LimitOrder (from a pending order or live position)
        and spawn its watcher thread.
        """
        s = self.settings

        # safe retrieval of entry price
        entry = getattr(o, 'price_open', None)
        if entry is None and hasattr(o, 'price'):
            entry = o.price

        lo = LimitOrder(
            ticket      = o.ticket,
            symbol      = o.symbol,
            entry_price = entry,
            direction   = ('LONG' if (o.type == mt5.ORDER_TYPE_BUY_LIMIT or
                                      (is_market and o.type == mt5.ORDER_TYPE_BUY))
                           else 'SHORT'),
            volume      = getattr(o, 'volume_initial', getattr(o, 'volume', 0.0)),
            sl          = o.sl,
            tp          = o.tp,
            mode        = s.mode,
            adjust_wait = s.adjust_wait,
            adjust_pct  = s.adjust_pct,
            pip_distance= s.pip_distance
        )
        self.tracked[o.ticket] = lo
        threading.Thread(target=self._watch, args=(lo,), daemon=True).start()

    def _watch(self, lo: LimitOrder):
        """
        1) For LIMIT orders: wait until the pending fills (position appears)
           For MARKET orders: they already are positions.
        2) Then watch until SL/TP hit (position disappears) → re-entry.
        """
        # 1) wait for position
        while self.running:
            if mt5.positions_get(ticket=lo.ticket):
                print(f"[BOT] Order {lo.ticket} filled at {lo.entry_price}")
                break
            time.sleep(0.5)

        # 2) watch for exit
        while lo.active and self.running:
            if not mt5.positions_get(ticket=lo.ticket):
                print(f"[BOT] SL/TP hit for ticket {lo.ticket}")
                if lo.mode == 'AUTOMATIC':
                    self._auto(lo)
                else:
                    self._manual(lo)
                lo.active = False
            time.sleep(0.5)

    def _auto(self, lo: LimitOrder):
        """
        AUTOMATIC:
        a) duplicate at original entry → store pending_ticket
        b) wait adjust_wait
        c) if price moved further losing, order_modify that pending
        """
        # a) initial duplicate
        req = {
            'action':    mt5.TRADE_ACTION_PENDING,
            'symbol':    lo.symbol,
            'volume':    lo.volume,
            'type':      mt5.ORDER_TYPE_BUY_LIMIT if lo.direction=='LONG'
                        else mt5.ORDER_TYPE_SELL_LIMIT,
            'price':     lo.entry_price,
            'sl':        lo.sl,
            'tp':        lo.tp,
            'deviation': 10,
            'magic':     123456,
            'comment':   'AutoReEntryBot'
        }
        res = mt5.order_send(req)
        pending_id = getattr(res, 'order', None)
        lo.pending_ticket = pending_id
        print(f"[BOT] AUTO re-entry for ticket {lo.ticket} @ {lo.entry_price}")

        # b) wait
        time.sleep(lo.adjust_wait)

        # c) compute movement past SL
        tick = mt5.symbol_info_tick(lo.symbol)
        movement = (tick.last - lo.sl) if lo.direction=='LONG' else (lo.sl - tick.last)
        if movement > 0:
            adj = movement * lo.adjust_pct/100
            if lo.direction=='LONG':
                new_entry = lo.entry_price - adj
            else:
                new_entry = lo.entry_price + adj
            delta = new_entry - lo.entry_price
            lo.entry_price += delta
            lo.sl          += delta
            lo.tp          += delta

            # modify existing pending, don't place new
            mod_req = {
                'order': lo.pending_ticket,
                'price': lo.entry_price,
                'sl':    lo.sl,
                'tp':    lo.tp,
                'deviation': 10
            }
            mt5.order_modify(mod_req)
            print(f"[BOT] AUTO placing adjusted @ {lo.entry_price}")
        else:
            print("[BOT] Price did not move further losing → no adjustment")

    def _manual(self, lo: LimitOrder):
        """
        MANUAL: offset by fixed pip_distance from SL, then duplicate.
        """
        point = mt5.symbol_info(lo.symbol).point
        mult  = -1 if lo.direction=='LONG' else 1
        delta = lo.pip_distance * point * mult
        new_entry = lo.sl + delta
        lo.entry_price = new_entry
        lo.sl += delta
        lo.tp += delta

        req = {
            'action':    mt5.TRADE_ACTION_PENDING,
            'symbol':    lo.symbol,
            'volume':    lo.volume,
            'type':      mt5.ORDER_TYPE_BUY_LIMIT if lo.direction=='LONG'
                        else mt5.ORDER_TYPE_SELL_LIMIT,
            'price':     lo.entry_price,
            'sl':        lo.sl,
            'tp':        lo.tp,
            'deviation': 10,
            'magic':     123456,
            'comment':   'AutoReEntryBot'
        }
        mt5.order_send(req)
        print(f"[BOT] MANUAL re-entry for ticket {lo.ticket} @ {lo.entry_price}")

    def stop(self):
        self.running = False
        mt5.shutdown()


# ————————————————————————————————————————————
# FLASK WEB UI
# ————————————————————————————————————————————
app = Flask(__name__)
app.secret_key = 'demo_secret'

TEMPLATE = '''
<!doctype html>
<title>MT5 Re-Entry Bot</title>
<h2>Bot Control</h2>
<form method="post" action="{{ url_for('start') }}">
  <label>Mode:</label>
  <select name="mode">
    <option value="AUTOMATIC" {{ 'selected' if settings.mode=='AUTOMATIC' else '' }}>Automatic</option>
    <option value="MANUAL"    {{ 'selected' if settings.mode=='MANUAL'    else '' }}>Manual</option>
  </select><br><br>

  <label>Wait (s):</label>
  <input name="wait" type="number" step="0.01" value="{{ settings.adjust_wait }}"><br><br>

  <label>Adjust %:</label>
  <input name="pct"  type="number" step="0.01" value="{{ settings.adjust_pct }}"><br><br>

  <label>Pip Dist:</label>
  <input name="pip"  type="number" step="0.1"  value="{{ settings.pip_distance }}"><br><br>

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
    # Avoid macOS AirPlay port conflict
    app.run(debug=True, host='0.0.0.0', port=5001)
