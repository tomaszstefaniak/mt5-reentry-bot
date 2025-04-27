import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional
from flask import Flask, request, render_template_string, redirect, url_for, flash

# ————————————————————————————————————————————
# Use real MetaTrader5 API on Windows, otherwise fall back to stub
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
    direction: str       # 'LONG' or 'SHORT'
    volume: float
    sl: float            # stop-loss level
    tp: float            # take-profit level
    mode: str            # 'AUTOMATIC' or 'MANUAL'
    adjust_wait: float = 0.0      # seconds (AUTO mode)
    adjust_pct: float = 0.0       # percent (AUTO mode)
    pip_distance: float = 0.0     # pips (MANUAL mode)
    duplicate_ticket: Optional[int] = None
    active: bool = True           # watcher flag

@dataclass
class Settings:
    mode: str = 'AUTOMATIC'
    adjust_wait: float = 5.0
    adjust_pct: float = 50.0
    pip_distance: float = 20.0
    enable_market: bool = False

# In-memory settings
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
        1) Poll pending limit orders → _add()
        2) If enabled, poll live market positions → _add_position()
        """
        while self.running:
            # pending limit orders
            for o in (mt5.orders_get() or []):
                if o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT):
                    with self.lock:
                        if o.ticket not in self.tracked:
                            print(f"[BOT] Detected new LIMIT order #{o.ticket}")
                            self._add(o)
            # market orders
            if self.settings.enable_market:
                for p in (mt5.positions_get() or []):
                    if p.ticket not in self.tracked:
                        print(f"[BOT] Detected new MARKET position #{p.ticket}")
                        self._add_position(p)
            time.sleep(1)

    def _add(self, o):
        """
        Build a LimitOrder from a pending order and spawn its watcher.
        """
        lo = LimitOrder(
            ticket      = o.ticket,
            symbol      = o.symbol,
            entry_price = o.price_open,
            direction   = 'LONG' if o.type == mt5.ORDER_TYPE_BUY_LIMIT else 'SHORT',
            volume      = o.volume_initial,
            sl          = o.sl,
            tp          = o.tp,
            mode        = self.settings.mode,
            adjust_wait = self.settings.adjust_wait,
            adjust_pct  = self.settings.adjust_pct,
            pip_distance= self.settings.pip_distance
        )
        self.tracked[o.ticket] = lo
        threading.Thread(target=self._watch, args=(lo,), daemon=True).start()

    def _add_position(self, p):
        """
        Build a LimitOrder from an already-filled market position.
        """
        lo = LimitOrder(
            ticket      = p.ticket,
            symbol      = p.symbol,
            entry_price = p.price_open,
            direction   = 'LONG' if p.type == mt5.ORDER_TYPE_BUY else 'SHORT',
            volume      = p.volume,
            sl          = p.sl,
            tp          = p.tp,
            mode        = self.settings.mode,
            adjust_wait = self.settings.adjust_wait,
            adjust_pct  = self.settings.adjust_pct,
            pip_distance= self.settings.pip_distance
        )
        self.tracked[p.ticket] = lo
        threading.Thread(target=self._watch, args=(lo,), daemon=True).start()

    def _watch(self, lo: LimitOrder):
        """
        1) Wait for fill (limit) or existence (market)
        2) Then watch until position disappears (SL/TP hit)
        3) Trigger AUTO or MANUAL re-entry
        """
        # wait for the position to appear
        while self.running:
            if mt5.positions_get(ticket=lo.ticket):
                print(f"[BOT] Order {lo.ticket} filled at {lo.entry_price}")
                break
            time.sleep(0.5)

        # watch for SL/TP exit
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
        a) Duplicate immediately with exact entry/SL/TP
        b) Wait adjust_wait seconds
        c) If price moves further losing-side beyond SL, modify in-place
        """
        print(f"[BOT] AUTO re-entry for ticket {lo.ticket} @ {lo.entry_price}")
        # a) duplicate
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
        lo.duplicate_ticket = res.order
        print(f"[BOT] order_send() → {req}")

        # b) wait
        time.sleep(lo.adjust_wait)

        # c) conditional adjustment
        tick = mt5.symbol_info_tick(lo.symbol)
        movement = tick.last - lo.sl
        if (lo.direction=='LONG' and movement<0) or (lo.direction=='SHORT' and movement>0):
            adj = abs(movement) * lo.adjust_pct/100
            new_entry = lo.entry_price - adj if lo.direction=='LONG' else lo.entry_price + adj
            delta = new_entry - lo.entry_price
            lo.entry_price += delta
            lo.sl          += delta
            lo.tp          += delta
            print(f"[BOT] AUTO placing adjusted @ {new_entry}")
            mod_req = {
                'action':    mt5.TRADE_ACTION_MODIFY,
                'order':     lo.duplicate_ticket,
                'price':     lo.entry_price,
                'sl':        lo.sl,
                'tp':        lo.tp,
                'deviation': 10
            }
            mt5.order_send(mod_req)
            print(f"[BOT] order_modify() → {mod_req}")
        else:
            print("[BOT] Price did not move further losing → no adjustment")

    def _manual(self, lo: LimitOrder):
        """
        MANUAL:
        1) Offset SL by pip_distance
        2) Apply same offset to entry & TP
        3) Duplicate
        """
        print(f"[BOT] MANUAL re-entry for ticket {lo.ticket}")
        point = mt5.symbol_info(lo.symbol).point
        multiplier = -1 if lo.direction=='LONG' else 1
        delta = lo.pip_distance * point * multiplier
        lo.entry_price = lo.sl + delta
        lo.sl         += delta
        lo.tp         += delta
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
        print(f"[BOT] order_send() → {req}")

    def stop(self):
        """Gracefully stop and disconnect."""
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

  <label>Pip Distance:</label>
  <input name="pip"  type="number" step="0.1"  value="{{ settings.pip_distance }}"><br><br>

  <label>
    <input type="checkbox" name="enable_market" {% if settings.enable_market %}checked{% endif %}>
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
    app.run(debug=True, host='0.0.0.0', port=5001)
