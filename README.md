# MT5 Re‑Entry Bot

A Python‑based MetaTrader5 bot that monitors user‑placed limit orders and, upon Stop‑Loss triggers, automatically re‑enters with configurable adjustment logic. Includes a stubbed demo mode (for macOS/Linux) and a lightweight Flask web UI.

---

## Features

- **Global Order Monitoring**: Watches all pending limit orders across all symbols.  
- **Automatic Mode**: On SL hit, places a duplicate order, waits an adjustable delay, measures price movement beyond SL, then shifts entry/SL/TP by a configurable percentage.  
- **Manual Mode**: On SL hit, places a new order offset by a fixed pip distance and adjusts SL/TP by the same amount.  
- **Per‑Order Parallel Tracking**: Each order is tracked independently in its own thread.  
- **Flask Front‑End**: Simple web form to configure mode & parameters, and to start/stop the bot.  
- **Stubbed Demo**: `mt5_stub.py` lets you demo the full cycle on non‑Windows machines without a live MT5 installation.

---

## Prerequisites

- **Windows** with [MetaTrader5](https://www.metatrader5.com/) for real trading.  
- **Python-3.8+** (macOS/Linux/Windows).  
- pip packages:  
  - `MetaTrader5>=5.0.27`  
  - `Flask>=2.0`

---

## MT5 Connection

Before running the bot in **Real MT5 Mode** on Windows, ensure your MetaTrader5 terminal is configured:

1. **Enable Expert Advisors**  
   - In MT5 go to **Tools → Options → Expert Advisors**.  
   - Check **“Allow automated trading”**.  
   - Check **“Allow DLL imports”**.

2. **Run MT5 as Administrator**  
   This ensures the Python API can establish the local IPC channel.

3. **Install the Python API**  
   In your activated virtual environment:
   ```bash
   pip install MetaTrader5
   ```

4. **Initialization in Code**  
   The script begins with:
   ```python
   import MetaTrader5 as mt5
   if not mt5.initialize():
       raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
   ```
   This locates your running MT5 terminal, loads its API, and opens the connection.

5. **Shutdown**  
   When you stop the bot (`bot.stop()`), it calls:
   ```python
   mt5.shutdown()
   ```
   to cleanly close the API session.

---

## Installation

1. Clone the repo  
   ```bash
   git clone https://github.com/tomaszstefaniak/mt5-reentry-bot.git
   cd mt5-reentry-bot
   ```

2. Create & activate a Python virtual environment  
   ```bash
   python3 -m venv venv
   source venv/bin/activate        # on macOS/Linux
   venv\Scripts\activate.bat     # on Windows
   ```

3. Install dependencies  
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

---

## Usage

### Demo Mode (macOS/Linux)

```bash
python mt5.py
```

1. Open your browser to [http://localhost:5000](http://localhost:5000).  
2. Configure your settings and click **Start Bot**.  
3. Observe the terminal for `[STUB] order_send(): ...` logs demonstrating the re‑entry cycle.

### Real MT5 Mode (Windows)

1. Ensure you’re on Windows with MetaTrader 5 running and configured (see **MT5 Connection**).  
2. Activate your virtual environment and install dependencies.  
3. Run:
   ```bash
   python mt5.py
   ```
4. In your browser go to [http://localhost:5000](http://localhost:5000) and click **Start Bot**.  
5. Place a **Buy Limit** or **Sell Limit** order in MT5; upon SL hit, the bot will automatically re‑enter according to your settings.

---

## Configuration

- **Mode**: `AUTOMATIC` / `MANUAL`  
- **Adjust Wait Time**: Delay (seconds) before adjustment in Automatic mode  
- **Adjust %**: Percentage of price movement beyond SL to shift entry/SL/TP  
- **Pip Distance**: Fixed pip offset in Manual mode  

---

© 2025 myweb3apps
