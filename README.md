# MT5 Demo Trading Agent

A Windows desktop application for testing AI/research trading agents on MetaTrader 5 demo accounts. Built with Electron + React + TypeScript (frontend) and Python + FastAPI (backend).

**Demo trading only by default.** Past performance does not guarantee live profitability.

## Architecture

```
frontend/          Electron + React + TypeScript desktop UI
backend/
  mt5/             MT5 connector, market data, execution engine
  agent/           Trading agent interface + implementations
  risk/            Risk engine, position sizing
  storage/         SQLite persistence
  api/             FastAPI routes
```

The agent never places orders directly. It returns structured trade signals. The risk engine evaluates each signal and decides whether execution is allowed.

## Prerequisites

- **Windows 10/11** (MT5 only runs on Windows)
- **MetaTrader 5** terminal installed locally
- **Python 3.10+** with pip
- **Node.js 18+** with npm
- A **demo account** with any MT5 broker

## Setup

### 1. Backend

```bash
cd backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# Edit .env with your MT5 terminal path
# Add Supabase values for auth/admin:
# SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY
```

### 2. Frontend

```bash
cd frontend
npm install
copy .env.example .env
# Add VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY for auth
```

### 3. Run

Start the backend first:

```bash
cd backend
venv\Scripts\activate
python main.py
```

Then start the frontend dev server:

```bash
cd frontend
npm run dev
```

Open `http://localhost:5173` in your browser, or run `npm run electron:dev` for the desktop app.

## Auth + Admin (Supabase)

- The app now opens on a login/register screen first.
- Bootstrap admin user is available from the auth page button using backend env:
  - `ADMIN_BOOTSTRAP_USERNAME=admin`
  - `ADMIN_BOOTSTRAP_PASSWORD=admin`
- Admin users can open `/admin` from sidebar to:
  - list users
  - create users
  - change user role
  - view user activity logs

For production, set:
- `AUTH_REQUIRED=true`
- `ENABLE_ADMIN_BOOTSTRAP=false`
- change admin bootstrap password to a strong one before disabling bootstrap.

## Demo Workflow

1. Open MetaTrader 5 terminal on your machine
2. Start the backend (`python main.py`)
3. Start the frontend (`npm run dev`)
4. Go to **Connection** page, enter your demo account credentials, click **Connect**
5. Go to **Market**, select **EURUSD**, view live quotes and chart
6. Go to **Strategy**, select the **SMA_Crossover** agent, click **Generate Signal**
7. Review the signal and risk decision
8. Go to **Execution**, click **Run Full Cycle** to generate + evaluate + execute
9. View results on **Dashboard** and **Logs**

## Agents

- **MockAgent**: Random signal generator for testing the pipeline
- **SMA_Crossover**: Simple Moving Average crossover (fast=10, slow=30)

To add a new agent, implement the `TradingAgent` interface in `backend/agent/interface.py` and register it in `backend/api/routes.py`.

## Risk Engine Rules

- Configurable risk % per trade
- Max daily loss % limit
- Max concurrent positions
- Minimum confidence threshold
- Maximum spread threshold
- Allowed symbols whitelist
- Stop loss required on every trade
- No martingale / no averaging down
- Panic stop button to halt all trading

## Safety

- Demo-only by default. Live trading requires `LIVE_TRADING_ENABLED=true` in config (clearly marked unsafe).
- The agent cannot place orders directly.
- Every trade must pass the risk engine.
- Every trade must have a stop loss.
- Position sizing is based on risk rules, not blind fixed lots (unless explicitly selected).

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | /api/connect | Connect to MT5 demo account |
| POST | /api/disconnect | Disconnect from MT5 |
| GET | /api/status | Get connection and account status |
| GET | /api/account | Get account info |
| POST | /api/verify-terminal | Verify MT5 terminal installation |
| POST | /api/symbols/select | Enable symbols in MarketWatch |
| GET | /api/market/tick/{symbol} | Get latest tick |
| GET | /api/market/bars/{symbol} | Get OHLCV bars |
| POST | /api/agent/evaluate | Generate and evaluate a signal |
| GET | /api/agents | List available agents |
| POST | /api/agent/set | Set active agent |
| POST | /api/trade/execute | Execute a trade |
| GET | /api/positions | Get open positions |
| POST | /api/positions/close | Close a position |
| GET | /api/risk/settings | Get risk settings |
| POST | /api/risk/settings | Update risk settings |
| POST | /api/risk/panic-stop | Toggle panic stop |
| GET | /api/logs | Get system logs |
| GET | /api/trade-history | Get trade history |
