import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional
from flask import Flask, request, render_template_string, redirect, url_for, flash

# ————————————————————————————————————————————
# Use real MetaTrader5 API on Windows, otherwise fall back to macOS stub
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
    Represents a user-placed order to track.
    """
    ticket: int
    symbol: str
    entry_price: float
    direction: str          # 'LONG' or 'SHORT'
    volume: float
    sl: float               # stop-loss
    tp: float               # take-profit
    mode: str               # 'AUTOMATIC' or 'MANUAL'
    adjust_wait: float = 0.0      # AUTO mode
    adjust_pct: float = 0.0       # AUTO mode
    pip_distance: float = 0.0     # MANUAL mode
    active: bool = True           # keep watcher alive

@dataclass
class Settings:
    """
    Global settings for re-entry behavior.
    """
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
        Poll orders every second; track new ones (limit + optional market).
        """
        while self.running:
            orders = mt5.orders_get() or []
            for o in orders:
                is_limit  = o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT)
                is_market = o.type in (mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL)
                if is_limit or (is_market and self.settings.enable_market):
                    with self.lock:
                        if o.ticket not in self.tracked:
                            self._add(o)
            time.sleep(1)

    def _add(self, o):
        """
        Record order and start a watcher thread.
        """
        s = global_settings
        lo = LimitOrder(
            ticket=o.ticket,
            symbol=o.symbol,
            entry_price=o.price_open,
            direction='LONG' if o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY) else 'SHORT',
            volume=o.volume_initial if hasattr(o, 'volume_initial') else o.volume,
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
        """
        1) Wait for fill (position appears), 
        2) then wait for SL/TP exit, 
        3) then re-enter.
        """
        # wait for the position to exist
        while self.running:
            if mt5.positions_get(ticket=lo.ticket):
                break
            time.sleep(0.5)

        # wait for it to close
        while lo.active and self.running:
            if not mt5.positions_get(ticket=lo.ticket):
                if lo.mode == 'AUTOMATIC':
                    self._auto(lo)
                else:
                    self._manual(lo)
                lo.active = False
            time.sleep(0.5)

    def _auto(self, lo: LimitOrder):
        # duplicate at entry
        self._send(lo.entry_price, lo)
        time.sleep(lo.adjust_wait)
        tick = mt5.symbol_info_tick(lo.symbol)
        movement = tick.last - lo.sl
        adj = movement * lo.adjust_pct / 100
        new_entry = lo.entry_price - adj if lo.direction=='LONG' else lo.entry_price + adj
        delta = new_entry - lo.entry_price
        lo.entry_price += delta; lo.sl += delta; lo.tp += delta
        self._send(lo.entry_price, lo)

    def _manual(self, lo: LimitOrder):
        point = mt5.symbol_info(lo.symbol).point
        mult = -1 if lo.direction=='LONG' else 1
        delta = lo.pip_distance * point * mult
        new_entry = lo.sl + delta
        lo.entry_price = new_entry; lo.sl += delta; lo.tp += delta
        self._send(lo.entry_price, lo)

    def _send(self, price: float, lo: LimitOrder):
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
        mt5.order_send(req)

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
    <input type="checkbox" name="enable_market" {{ 'checked' if settings.enable_market else '' }}>
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
