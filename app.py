import os
import re
import json
import time
import socket
import datetime
import uuid
import threading
import subprocess
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from typing import Optional, Dict, Any, List

# Try importing MetaTrader5 package if available
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

class TelemetryStore:
    """In-memory store for live telemetry data and access control system."""
    def __init__(self):
        self.admin_pin = "7788"
        self.global_url = ""
        self.data: Dict[str, Any] = {
            "title": "Talha's Room",
            "system_status": "ONLINE",
            "bot_status": "v5.0 PULLBACK SCALPER READY",
            "symbol": "XAUUSD",
            "account": {
                "balance": 10000.0,
                "equity": 10000.0,
                "margin": 0.0,
                "free_margin": 10000.0,
                "floating_pnl": 0.0,
                "peak_pnl": 0.0,
                "session_profit": 0.0,
                "baskets_won": 0,
                "baskets_total": 0,
                "win_rate": 100.0
            },
            "positions": [],
            "equity_history": [],
            "last_update": datetime.datetime.now().strftime("%H:%M:%S")
        }
        self.history_limit = 60
        # Access control
        self.pending_requests: Dict[str, Dict[str, Any]] = {}
        self.approved_tokens: set = set()

    def update_from_dict(self, payload: Dict[str, Any]):
        if "account" in payload:
            self.data["account"].update(payload["account"])
        if "positions" in payload:
            self.data["positions"] = payload["positions"]
        if "bot_status" in payload:
            self.data["bot_status"] = payload["bot_status"]
        if "symbol" in payload:
            self.data["symbol"] = payload["symbol"]
            
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        self.data["last_update"] = now_str
        
        eq = self.data["account"]["equity"]
        bal = self.data["account"]["balance"]
        
        history = self.data["equity_history"]
        if not history or history[-1]["time"] != now_str:
            history.append({"time": now_str, "equity": eq, "balance": bal})
            if len(history) > self.history_limit:
                history.pop(0)

    def fetch_from_mt5(self):
        """Attempts to poll live data directly from MT5 Terminal if running."""
        if not MT5_AVAILABLE:
            return False
        
        try:
            if not mt5.terminal_info():
                if not mt5.initialize():
                    return False
                    
            acc_info = mt5.account_info()
            if acc_info is None:
                return False
                
            positions_raw = mt5.positions_get(symbol="XAUUSD")
            if positions_raw is None:
                positions_raw = mt5.positions_get()
                
            pos_list = []
            floating_pnl = 0.0
            if positions_raw:
                for p in positions_raw:
                    is_prof = p.profit >= 0
                    p_dict = {
                        "ticket": p.ticket,
                        "symbol": p.symbol,
                        "type": "BUY" if p.type == 0 else "SELL",
                        "lots": round(p.volume, 2),
                        "open_price": round(p.price_open, 2),
                        "current_price": round(p.price_current, 2),
                        "pnl": round(p.profit + p.swap, 2),
                        "pnl_pips": round((p.price_current - p.price_open) * 10.0 if p.type == 0 else (p.price_open - p.price_current) * 10.0, 1),
                        "status": "PROFITABLE" if is_prof else "HOLDING IN LOSS"
                    }
                    pos_list.append(p_dict)
                    floating_pnl += p.profit + p.swap

            balance = acc_info.balance
            equity = acc_info.equity
            
            self.update_from_dict({
                "account": {
                    "balance": round(balance, 2),
                    "equity": round(equity, 2),
                    "margin": round(acc_info.margin, 2),
                    "free_margin": round(acc_info.margin_free, 2),
                    "floating_pnl": round(floating_pnl, 2)
                },
                "positions": pos_list,
                "bot_status": f"SYNCED WITH MT5 TERMINAL ({len(pos_list)} active trades)"
            })
            return True
        except Exception:
            return False

telemetry = TelemetryStore()

class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Pure Python Standard Library HTTP Handler with Worldwide Access Control."""

    def log_message(self, format, *args):
        return

    def do_GET(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query = parse_qs(parsed_url.query)
        
        pin = query.get("pin", [""])[0]
        token = query.get("token", [""])[0]
        req_id = query.get("req_id", [""])[0]

        # 1. Main Page Render
        if path == "/" or path == "/view" or path == "/index.html":
            dashboard_path = Path(__file__).parent / "dashboard.html"
            if dashboard_path.exists():
                content = dashboard_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_error(404, "dashboard.html not found")

        # 2. Check Approval Status for Guests
        elif path == "/api/check_approval":
            status_str = "PENDING"
            if req_id in telemetry.approved_tokens:
                status_str = "APPROVED"
            elif req_id in telemetry.pending_requests:
                status_str = telemetry.pending_requests[req_id]["status"]
                
            res = json.dumps({"status": status_str, "token": req_id}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)

        # 3. Main Data Stream / Live Status
        elif path == "/api/status":
            is_admin = (pin == telemetry.admin_pin)
            is_approved = is_admin or (token in telemetry.approved_tokens)

            # Poll MT5 if running
            telemetry.fetch_from_mt5()

            response_data = dict(telemetry.data)
            response_data["is_admin"] = is_admin
            response_data["is_approved"] = is_approved
            response_data["global_url"] = telemetry.global_url
            
            # List pending access requests for Admin
            if is_admin:
                pending_list = []
                for r_id, r_info in telemetry.pending_requests.items():
                    if r_info["status"] == "PENDING":
                        pending_list.append({
                            "req_id": r_id,
                            "ip": r_info["ip"],
                            "time": r_info["time"]
                        })
                response_data["pending_requests"] = pending_list
            else:
                response_data["pending_requests"] = []

            # If not approved and not admin, restrict position details
            if not is_approved:
                body = json.dumps({
                    "is_admin": False,
                    "is_approved": False,
                    "title": "Talha's Room",
                    "message": "ACCESS_RESTRICTED - Waiting for Admin Approval"
                }).encode("utf-8")
            else:
                body = json.dumps(response_data).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_error(404, "Endpoint not found")

    def do_POST(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        
        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length) if content_length > 0 else b"{}"

        # 1. Guest Request Access
        if path == "/api/request_access":
            client_ip = self.client_address[0]
            new_req_id = f"GUEST-{uuid.uuid4().hex[:6].upper()}"
            now_str = datetime.datetime.now().strftime("%H:%M:%S")

            telemetry.pending_requests[new_req_id] = {
                "ip": client_ip,
                "time": now_str,
                "status": "PENDING"
            }

            res = json.dumps({
                "status": "PENDING",
                "req_id": new_req_id,
                "message": "Request submitted to Admin (Talha)"
            }).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)

        # 2. Admin Approve / Deny Guest Request
        elif path == "/api/approve_request":
            try:
                payload = json.loads(post_data.decode("utf-8"))
                pin = payload.get("pin", "")
                req_id = payload.get("req_id", "")
                action = payload.get("action", "ALLOW")

                if pin != telemetry.admin_pin:
                    res = json.dumps({"status": "DENIED", "message": "Invalid Admin PIN"}).encode("utf-8")
                    self.send_response(403)
                else:
                    if action == "ALLOW":
                        telemetry.approved_tokens.add(req_id)
                        if req_id in telemetry.pending_requests:
                            telemetry.pending_requests[req_id]["status"] = "APPROVED"
                        res = json.dumps({"status": "SUCCESS", "message": f"Approved {req_id}"}).encode("utf-8")
                    else:
                        if req_id in telemetry.pending_requests:
                            telemetry.pending_requests[req_id]["status"] = "DENIED"
                        res = json.dumps({"status": "SUCCESS", "message": f"Denied {req_id}"}).encode("utf-8")
                    self.send_response(200)
            except Exception as e:
                res = json.dumps({"status": "ERROR", "message": str(e)}).encode("utf-8")
                self.send_response(400)

            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)

        # 3. Emergency Close All (Admin Only!)
        elif path == "/api/close_all":
            try:
                payload = json.loads(post_data.decode("utf-8")) if post_data else {}
                pin = payload.get("pin", "")

                if pin != telemetry.admin_pin:
                    res = json.dumps({"status": "DENIED", "message": "Only Admin (Talha) can close trades!"}).encode("utf-8")
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(res)))
                    self.end_headers()
                    self.wfile.write(res)
                    return

                closed_count = 0
                if MT5_AVAILABLE and mt5.terminal_info():
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
                                    "comment": "Web Close All",
                                    "type_filling": mt5.ORDER_FILLING_IOC
                                }
                                mt5.order_send(req)
                                closed_count += 1

                telemetry.data["positions"] = []
                telemetry.data["account"]["floating_pnl"] = 0.0
                telemetry.data["bot_status"] = f"EMERGENCY CLOSE ALL EXECUTED BY ADMIN ({closed_count} closed)"
                res = json.dumps({"status": "SUCCESS", "closed_count": closed_count}).encode("utf-8")
                self.send_response(200)
            except Exception as e:
                res = json.dumps({"status": "ERROR", "message": str(e)}).encode("utf-8")
                self.send_response(400)

            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)

        # 4. Telemetry post from MT5 EA
        elif path == "/api/telemetry":
            try:
                payload = json.loads(post_data.decode("utf-8"))
                telemetry.update_from_dict(payload)
                res = json.dumps({"status": "SUCCESS"}).encode("utf-8")
                self.send_response(200)
            except Exception as e:
                res = json.dumps({"status": "ERROR", "message": str(e)}).encode("utf-8")
                self.send_response(400)

            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(res)))
            self.end_headers()
            self.wfile.write(res)

        else:
            self.send_error(404, "Endpoint not found")

def start_global_tunnel():
    """Background daemon to generate and keep alive worldwide HTTPS links via SSH reverse tunnels."""
    tunnel_cmds = [
        ("Pinggy", ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ServerAliveInterval=30", "-p", "443", "-R0:127.0.0.1:5000", "a.pinggy.io"]),
        ("LocalhostRun", ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ServerAliveInterval=30", "-R", "80:127.0.0.1:5000", "nokey@localhost.run"])
    ]
    
    idx = 0
    while True:
        provider_name, cmd = tunnel_cmds[idx % len(tunnel_cmds)]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            for line in iter(proc.stdout.readline, ''):
                if "https://" in line:
                    match = re.search(r'https://[a-zA-Z0-9.-]+', line)
                    if match:
                        global_url = match.group(0).rstrip(".")
                        telemetry.global_url = global_url
                        print("\n" + "=" * 65)
                        print(f"   👑 TALHA'S ROOM - WORLDWIDE GLOBAL ACCESS ({provider_name})")
                        print("=" * 65)
                        print(f"   ADMIN GLOBAL LINK (Full Control on Any Mobile / Laptop / Data):")
                        print(f"   {global_url}/?pin=7788")
                        print(f"\n   SHAREABLE VIEWER LINK (Send to Anyone in the World):")
                        print(f"   {global_url}/view")
                        print("=" * 65 + "\n")
            proc.wait()
        except Exception:
            pass
        idx += 1
        time.sleep(3)

def get_local_ip():
    """Finds local network IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def run_server(host: str = "0.0.0.0", port: int = 5000):
    server_address = (host, port)
    httpd = HTTPServer(server_address, DashboardRequestHandler)
    local_ip = get_local_ip()
    
    print("\n=======================================================")
    print("   TALHA'S ROOM WEB CONTROL CENTER SERVER ACTIVE       ")
    print("=======================================================")
    print(f"   ADMIN URL (Local Wi-Fi):                             ")
    print(f"    http://{local_ip}:{port}/?pin=7788                 ")
    print(f"    http://localhost:{port}/?pin=7788                   ")
    print(f"   SHAREABLE VIEWER LINK (Local Wi-Fi):                ")
    print(f"    http://{local_ip}:{port}/view                      ")
    print("=======================================================")
    print("   Generating Worldwide Global HTTPS Links...          ")
    
    # Start global tunnel thread
    tunnel_thread = threading.Thread(target=start_global_tunnel, daemon=True)
    tunnel_thread.start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        httpd.server_close()

def create_app(engine_ref: Optional[Any] = None):
    """Fallback compatibility layer if referenced as create_app."""
    class DummyApp:
        def run(self, host="0.0.0.0", port=5000, debug=False):
            run_server(host, port)
    return DummyApp()

if __name__ == "__main__":
    run_server()
