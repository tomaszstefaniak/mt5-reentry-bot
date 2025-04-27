import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional
from flask import Flask, request, render_template_string, redirect, url_for, flash

# ————————————————————————————————————————————
# Use real MetaTrader5 API on Windows, otherwise fall back to our macOS stub
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
    """
    Represents a user-placed limit order to track.
    """
    ticket: int
    symbol: str
    entry_price: float
    direction: str          # 'LONG' or 'SHORT'
    volume: float
    sl: float               # stop-loss level
    tp: float               # take-profit level
    mode: str               # 'AUTOMATIC' or 'MANUAL'
    adjust_wait: float = 0.0      # wait time (s) before adjustment in AUTO mode
    adjust_pct: float = 0.0       # adjustment percentage in AUTO mode
    pip_distance: float = 0.0     # fixed pip offset in MANUAL mode
    active: bool = True           # flag to keep watcher loop alive

@dataclass
class Settings:
    """
    Global default settings for re-entry behavior.
    """
    mode: str = 'AUTOMATIC'
    adjust_wait: float = 5.0      # seconds
    adjust_pct: float = 50.0      # percentage
    pip_distance: float = 20.0    # pips

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
        while self.running:
            orders = mt5.orders_get()
            for o in orders or []:
                if o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT):
                    with self.lock:
                        if o.ticket not in self.tracked:
                            self._add(o)
            time.sleep(1)

    def _add(self, o):
        s = global_settings
        lo = LimitOrder(
            ticket=o.ticket,
            symbol=o.symbol,
            entry_price=o.price_open,
            direction='LONG' if o.type == mt5.ORDER_TYPE_BUY_LIMIT else 'SHORT',
            volume=o.volume_initial,
            sl=o.sl,
            tp=o.tp,
            mode=s.mode,
            adjust_wait=s.adjust_wait,
            adjust_pct=s.adjust_pct,
            pip_distance=s.pip_distance
        )
        self.tracked[o.ticket] = lo
        threading.Thread(target=self._watch, args=(lo,), daemon=True).start()

    def _watch(self, lo: LimitOrder):
        # 1) Wait until the limit order actually fills
        while self.running:
            order = mt5.order_get(ticket=lo.ticket)
            # If stub or real, order.state==FILLED signals execution
            if order and getattr(order, "state", None) == mt5.ORDER_STATE_FILLED:
                break
            time.sleep(0.5)

        # 2) Now watch the live position for SL/TP exit
        while lo.active and self.running:
            pos = mt5.positions_get(ticket=lo.ticket)
            if not pos:
                if lo.mode == 'AUTOMATIC':
                    self._auto(lo)
                else:
                    self._manual(lo)
                lo.active = False
            time.sleep(0.5)

    def _auto(self, lo: LimitOrder):
        self._send(lo.entry_price, lo)
        time.sleep(lo.adjust_wait)
        tick = mt5.symbol_info_tick(lo.symbol)
        movement = tick.last - lo.sl
        adj = movement * lo.adjust_pct / 100
        if lo.direction == 'LONG':
            new_entry = lo.entry_price - adj
        else:
            new_entry = lo.entry_price + adj
        delta = new_entry - lo.entry_price
        lo.entry_price += delta
        lo.sl += delta
        lo.tp += delta
        self._send(lo.entry_price, lo)

    def _manual(self, lo: LimitOrder):
        point = mt5.symbol_info(lo.symbol).point
        multiplier = -1 if lo.direction == 'LONG' else 1
        delta = lo.pip_distance * point * multiplier
        new_entry = lo.sl + delta
        lo.entry_price = new_entry
        lo.sl += delta
        lo.tp += delta
        self._send(lo.entry_price, lo)

    def _send(self, price: float, lo: LimitOrder):
        request = {
            'action': mt5.TRADE_ACTION_PENDING,
            'symbol': lo.symbol,
            'volume': lo.volume,
            'type': mt5.ORDER_TYPE_BUY_LIMIT if lo.direction == 'LONG'
                    else mt5.ORDER_TYPE_SELL_LIMIT,
            'price': price,
            'sl': lo.sl,
            'tp': lo.tp,
            'deviation': 10,
            'magic': 123456,
            'comment': 'AutoReEntryBot'
        }
        mt5.order_send(request)

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
    <option value="MANUAL" {{ 'selected' if settings.mode=='MANUAL' else '' }}>Manual</option>
  </select><br><br>
  <label>Wait (s):</label><input name="wait" type="number" step="0.01" value="{{ settings.adjust_wait }}"><br><br>
  <label>Adjust %:</label><input name="pct" type="number" step="0.01" value="{{ settings.adjust_pct }}"><br><br>
  <label>Pip Distance:</label><input name="pip" type="number" step="0.1" value="{{ settings.pip_distance }}"><br><br>
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
    global_settings.mode = request.form['mode']
    global_settings.adjust_wait = float(request.form['wait'])
    global_settings.adjust_pct = float(request.form['pct'])
    global_settings.pip_distance = float(request.form['pip'])
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
    app.run(debug=True, host='0.0.0.0')
