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
    Represents a user‑placed limit order to track.
    """
    ticket: int
    symbol: str
    entry_price: float
    direction: str          # 'LONG' or 'SHORT'
    volume: float
    sl: float               # stop‑loss level
    tp: float               # take‑profit level
    mode: str               # 'AUTOMATIC' or 'MANUAL'
    adjust_wait: float = 0.0      # wait time (s) before adjustment in AUTO mode
    adjust_pct: float = 0.0       # adjustment percentage in AUTO mode
    pip_distance: float = 0.0     # fixed pip offset in MANUAL mode
    active: bool = True           # flag to keep watcher loop alive

@dataclass
class Settings:
    """
    Global default settings for re‑entry behavior.
    """
    mode: str = 'AUTOMATIC'
    adjust_wait: float = 5.0      # seconds
    adjust_pct: float = 50.0      # percentage
    pip_distance: float = 20.0    # pips

# In‑memory storage for per‑symbol overrides (future use)
per_pair_settings: Dict[str, Settings] = {}
# The shared global settings instance
global_settings = Settings()

# Single bot instance holder
bot: Optional['MT5TradingBot'] = None


# ————————————————————————————————————————————
# BOT CORE
# ————————————————————————————————————————————
class MT5TradingBot:
    """
    Core logic: monitors limit orders, reacts when SL is hit,
    and re‑enters trades per user settings.
    """
    def __init__(self):
        # Initialize the MT5 connection
        if not mt5.initialize():
            raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
        self.settings = global_settings
        self.tracked: Dict[int, LimitOrder] = {}  # ticket → LimitOrder
        self.running = True
        self.lock = threading.Lock()  # protects self.tracked

    def monitor(self):
        """
        Main monitoring loop: polls for new limit orders every second.
        """
        while self.running:
            orders = mt5.orders_get()
            for o in orders or []:
                # Only process BUY_LIMIT or SELL_LIMIT orders
                if o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT):
                    with self.lock:
                        if o.ticket not in self.tracked:
                            self._add(o)  # start watching this new order
            time.sleep(1)

    def _add(self, o):
        """
        Record order details and spawn a watcher thread.
        """
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
        # Daemon thread ends when program exits or lo.active flips False
        threading.Thread(target=self._watch, args=(lo,), daemon=True).start()

    def _watch(self, lo: LimitOrder):
        """
        Watches a single limit order: triggers on SL hit, then re‑entry logic.
        """
        while lo.active and self.running:
            pos = mt5.positions_get(ticket=lo.ticket)
            # If position no longer exists → SL/TP was hit
            if not pos:
                if lo.mode == 'AUTOMATIC':
                    self._auto(lo)
                else:
                    self._manual(lo)
                lo.active = False  # stop this watcher
            time.sleep(0.5)

    def _auto(self, lo: LimitOrder):
        """
        Automatic re‑entry: duplicate order immediately, wait, then adjust.
        """
        # 1) Place duplicate at original entry
        self._send(lo.entry_price, lo)

        # 2) Wait for configured interval
        time.sleep(lo.adjust_wait)

        # 3) Check how far price moved beyond the SL
        tick = mt5.symbol_info_tick(lo.symbol)
        movement = tick.last - lo.sl
        adj = movement * lo.adjust_pct / 100

        # 4) Compute new entry (minus for longs, plus for shorts)
        if lo.direction == 'LONG':
            new_entry = lo.entry_price - adj
        else:
            new_entry = lo.entry_price + adj

        # 5) Shift entry/SL/TP by the same delta to preserve spacing
        delta = new_entry - lo.entry_price
        lo.entry_price += delta
        lo.sl += delta
        lo.tp += delta

        # 6) Place the adjusted order
        self._send(lo.entry_price, lo)

    def _manual(self, lo: LimitOrder):
        """
        Manual re‑entry: offset by fixed pip distance from SL.
        """
        point = mt5.symbol_info(lo.symbol).point
        # Negative for longs, positive for shorts
        multiplier = -1 if lo.direction == 'LONG' else 1
        delta = lo.pip_distance * point * multiplier

        new_entry = lo.sl + delta
        lo.entry_price = new_entry
        lo.sl += delta
        lo.tp += delta

        self._send(lo.entry_price, lo)

    def _send(self, price: float, lo: LimitOrder):
        """
        Sends a pending limit order request to MT5.
        """
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
        """
        Gracefully stop monitoring and shut down MT5 connection.
        """
        self.running = False
        mt5.shutdown()


# ————————————————————————————————————————————
# FLASK WEB UI
# ————————————————————————————————————————————
app = Flask(__name__)
app.secret_key = 'demo_secret'  # required for Flash messages

# Inline HTML template for simplicity
TEMPLATE = '''
<!doctype html>
<title>MT5 Re-Entry Bot</title>
<h2>Bot Control</h2>
<form method="post" action="{{ url_for('start') }}">
  <label>Mode:</label>
  <select name="mode">
    <option value="AUTOMATIC" {{ 'selected' if settings.mode=='AUTOMATIC' else '' }}>
      Automatic
    </option>
    <option value="MANUAL" {{ 'selected' if settings.mode=='MANUAL' else '' }}>
      Manual
    </option>
  </select><br><br>
  <label>Wait (s):</label>
  <input name="wait" type="number" step="0.01" value="{{ settings.adjust_wait }}"><br><br>
  <label>Adjust %:</label>
  <input name="pct" type="number" step="0.01" value="{{ settings.adjust_pct }}"><br><br>
  <label>Pip Distance:</label>
  <input name="pip" type="number" step="0.1" value="{{ settings.pip_distance }}"><br><br>
  <button type="submit">Start Bot</button>
</form>
<form method="post" action="{{ url_for('stop') }}" style="margin-top:10px;">
  <button type="submit">Stop Bot</button>
</form>
'''

@app.route('/')
def index():
    """Render control panel with current global settings."""
    return render_template_string(TEMPLATE, settings=global_settings)

@app.route('/start', methods=['POST'])
def start():
    """
    Start the MT5TradingBot with the user‑provided settings.
    If already running, flash a warning.
    """
    global bot
    # Update global settings from form inputs
    global_settings.mode = request.form['mode']
    global_settings.adjust_wait = float(request.form['wait'])
    global_settings.adjust_pct = float(request.form['pct'])
    global_settings.pip_distance = float(request.form['pip'])

    if bot:
        flash('Bot already running')
    else:
        try:
            bot = MT5TradingBot()
            # Launch monitoring in background
            threading.Thread(target=bot.monitor, daemon=True).start()
            flash('Bot started')
        except Exception as e:
            flash(f'Error: {e}')
    return redirect(url_for('index'))

@app.route('/stop', methods=['POST'])
def stop():
    """
    Stop the bot if it’s running; otherwise flash a notice.
    """
    global bot
    if bot:
        bot.stop()
        bot = None
        flash('Bot stopped')
    else:
        flash('Bot not running')
    return redirect(url_for('index'))

if __name__ == '__main__':
    # Launch Flask in debug mode, listening on all interfaces
    app.run(debug=True, host='0.0.0.0')
