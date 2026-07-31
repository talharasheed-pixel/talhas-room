"""
Talha's Room - Local MT5 Cloud Pusher
=====================================
Run this script on your local PC alongside MT5.
It polls live MetaTrader 5 balance, equity, and open position data
and pushes it to your permanent Cloud Relay Server (Render / VPS) every 2 seconds.

Usage:
  python local_pusher.py --cloud-url https://YOUR-APP.onrender.com
"""

import os
import sys
import time
import json
import argparse
import urllib.request
import urllib.error
import datetime

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False


DEFAULT_CLOUD_URL = os.environ.get("CLOUD_DASHBOARD_URL", "http://localhost:5000")
PUSH_SECRET = "talha_aura_secret_2026"


def fetch_mt5_snapshot():
    """Extract current account & position snapshot from MetaTrader 5."""
    if not MT5_AVAILABLE:
        return {
            "symbol": "XAUUSD",
            "bot_status": "MT5 Python library not installed on local PC",
            "account": {
                "balance": 10000.0,
                "equity": 10000.0,
                "margin": 0.0,
                "free_margin": 10000.0,
                "floating_pnl": 0.0,
                "session_profit": 0.0,
                "baskets_won": 0,
                "baskets_total": 0,
                "win_rate": 100.0
            },
            "positions": []
        }

    if not mt5.terminal_info():
        if not mt5.initialize():
            return {
                "symbol": "XAUUSD",
                "bot_status": "MT5 Terminal Not Connected - Open MT5",
                "account": {
                    "balance": 0.0, "equity": 0.0, "margin": 0.0, "free_margin": 0.0,
                    "floating_pnl": 0.0, "session_profit": 0.0, "baskets_won": 0, "baskets_total": 0, "win_rate": 100.0
                },
                "positions": []
            }

    acc = mt5.account_info()
    if acc is None:
        return None

    positions_raw = mt5.positions_get(symbol="XAUUSD")
    if positions_raw is None:
        positions_raw = mt5.positions_get()

    pos_list = []
    total_floating = 0.0
    if positions_raw:
        for p in positions_raw:
            is_prof = (p.profit >= 0)
            pos_list.append({
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": "BUY" if p.type == 0 else "SELL",
                "volume": round(p.volume, 2),
                "open_price": round(p.price_open, 2),
                "current_price": round(p.price_current, 2),
                "profit": round(p.profit, 2),
                "status": "PROFIT" if is_prof else "HOLDING (NO LOSS EXIT)"
            })
            total_floating += p.profit

    bot_status = f"LIVE - {len(pos_list)} Open Position(s)" if pos_list else "MONITORING PULLBACK - Ready for Entry"

    return {
        "symbol": "XAUUSD",
        "bot_status": bot_status,
        "account": {
            "balance": round(acc.balance, 2),
            "equity": round(acc.equity, 2),
            "margin": round(acc.margin, 2),
            "free_margin": round(acc.margin_free, 2),
            "floating_pnl": round(total_floating, 2),
            "session_profit": 0.0,
            "baskets_won": 0,
            "baskets_total": 0,
            "win_rate": 100.0
        },
        "positions": pos_list
    }


def execute_commands(commands):
    """Execute incoming commands from cloud dashboard (e.g., Emergency Close)."""
    if not MT5_AVAILABLE or not commands:
        return

    for cmd in commands:
        if cmd.get("action") == "CLOSE_ALL":
            print("🚨 Emergency CLOSE_ALL triggered from Cloud Dashboard!")
            positions = mt5.positions_get(symbol="XAUUSD")
            if positions:
                for p in positions:
                    tick = mt5.symbol_info_tick(p.symbol)
                    if tick:
                        price = tick.bid if p.type == 0 else tick.ask
                        order_type = mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY
                        req = {
                            "action": mt5.TRADE_ACTION_DEAL,
                            "symbol": p.symbol,
                            "volume": p.volume,
                            "type": order_type,
                            "position": p.ticket,
                            "price": price,
                            "deviation": 20,
                            "magic": p.magic,
                            "comment": "Cloud Close All",
                            "type_filling": mt5.ORDER_FILLING_IOC
                        }
                        mt5.order_send(req)


def push_loop(cloud_url: str):
    """Continuously send MT5 updates to Cloud Relay every 2 seconds."""
    endpoint = cloud_url.rstrip("/") + "/api/push"
    print(f"\n=======================================================")
    print(f"   TALHA'S ROOM - LOCAL CLOUD DATA PUSHER ACTIVE       ")
    print(f"=======================================================")
    print(f"   Target Cloud URL: {endpoint}")
    print(f"   Push Interval:    Every 2 seconds")
    print(f"   Press Ctrl+C to stop.")
    print(f"=======================================================\n")

    while True:
        try:
            snapshot = fetch_mt5_snapshot()
            if snapshot:
                payload_bytes = json.dumps(snapshot).encode("utf-8")
                req = urllib.request.Request(
                    endpoint,
                    data=payload_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Push-Secret": PUSH_SECRET
                    },
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=5) as response:
                    res_body = json.loads(response.read().decode("utf-8"))
                    cmds = res_body.get("commands", [])
                    if cmds:
                        execute_commands(cmds)

                    now_str = datetime.datetime.now().strftime("%H:%M:%S")
                    eq = snapshot["account"]["equity"]
                    pos_count = len(snapshot["positions"])
                    print(f"[{now_str}] Pushed to Cloud | Equity: ${eq:,.2f} | Open Positions: {pos_count} | Status: OK")
        except urllib.error.URLError as e:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Cloud Push Failed: {e.reason}")
        except Exception as e:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Push Error: {e}")

        time.sleep(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Talha's Room Local MT5 Cloud Pusher")
    parser.add_argument("--cloud-url", default=DEFAULT_CLOUD_URL, help="URL of deployed cloud server")
    args = parser.parse_args()

    push_loop(args.cloud_url)
