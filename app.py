"""
Talha's Room - Cloud Dashboard Relay Server
============================================
Deploy this on Render.com (free) for a PERMANENT URL.
Local PC pushes MT5 data here every 2 seconds.
Dashboard is served from here to any browser in the world.

PERMANENT URLs:
  Admin:  https://YOUR-APP.onrender.com/?pin=7788
  Viewer: https://YOUR-APP.onrender.com/view
"""

import os
import json
import time
import datetime
import uuid
import hmac
import hashlib
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler, SimpleHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from typing import Optional, Dict, Any, List
import threading

# ============================================================
# CONFIGURATION - Change these as needed
# ============================================================
ADMIN_PIN = "7788"
PUSH_SECRET = "talha_aura_secret_2026"  # Must match local_pusher.py
PORT = int(os.environ.get("PORT", 5000))  # Render sets PORT env var

# ============================================================
# TELEMETRY DATA STORE
# ============================================================
class CloudTelemetryStore:
    """Thread-safe in-memory store for relay data."""
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {
            "title": "Talha's Room",
            "system_status": "WAITING FOR MT5 CONNECTION",
            "bot_status": "Waiting for local MT5 data push...",
            "symbol": "XAUUSD",
            "account": {
                "balance": 0.0,
                "equity": 0.0,
                "margin": 0.0,
                "free_margin": 0.0,
                "floating_pnl": 0.0,
                "peak_pnl": 0.0,
                "session_profit": 0.0,
                "baskets_won": 0,
                "baskets_total": 0,
                "win_rate": 100.0
            },
            "positions": [],
            "equity_history": [],
            "last_update": ""
        }
        self.history_limit = 120
        self.last_push_time = 0
        
        # Access control
        self.pending_requests: Dict[str, Dict[str, Any]] = {}
        self.approved_tokens: set = set()
        
        # Command queue (for emergency close etc.)
        self.command_queue: list = []

    def update_from_push(self, payload: Dict[str, Any]):
        with self.lock:
            if "account" in payload:
                self.data["account"].update(payload["account"])
            if "positions" in payload:
                self.data["positions"] = payload["positions"]
            if "bot_status" in payload:
                self.data["bot_status"] = payload["bot_status"]
            if "symbol" in payload:
                self.data["symbol"] = payload["symbol"]
            if "session_profit" in payload:
                self.data["account"]["session_profit"] = payload["session_profit"]
            if "baskets_won" in payload:
                self.data["account"]["baskets_won"] = payload["baskets_won"]
            if "baskets_total" in payload:
                self.data["account"]["baskets_total"] = payload["baskets_total"]
                
            now_str = datetime.datetime.now().strftime("%H:%M:%S")
            self.data["last_update"] = now_str
            self.data["system_status"] = "ONLINE"
            self.last_push_time = time.time()
            
            eq = self.data["account"].get("equity", 0)
            bal = self.data["account"].get("balance", 0)
            
            history = self.data["equity_history"]
            if not history or history[-1]["time"] != now_str:
                history.append({"time": now_str, "equity": eq, "balance": bal})
                if len(history) > self.history_limit:
                    history.pop(0)

    def get_snapshot(self):
        with self.lock:
            # Check if data is stale (no push for 15 seconds)
            if self.last_push_time > 0 and (time.time() - self.last_push_time) > 15:
                self.data["system_status"] = "MT5 DISCONNECTED"
                self.data["bot_status"] = "No data received - Check local pusher is running"
            return dict(self.data)

    def pop_commands(self):
        with self.lock:
            cmds = list(self.command_queue)
            self.command_queue.clear()
            return cmds

    def queue_command(self, cmd: dict):
        with self.lock:
            self.command_queue.append(cmd)


store = CloudTelemetryStore()


# ============================================================
# HTTP REQUEST HANDLER
# ============================================================
class CloudDashboardHandler(BaseHTTPRequestHandler):
    
    def log_message(self, format, *args):
        return  # Suppress default logging for cleanliness
    
    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Push-Secret")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    
    def _send_html(self, content: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Push-Secret")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        
        pin = query.get("pin", [""])[0]
        token = query.get("token", [""])[0]
        req_id = query.get("req_id", [""])[0]

        # ---- Serve Dashboard HTML ----
        if path in ("/", "/view", "/index.html"):
            html_path = Path(__file__).parent / "templates" / "dashboard.html"
            if html_path.exists():
                self._send_html(html_path.read_bytes())
            else:
                self.send_error(404, "dashboard.html not found in templates/")
            return

        # ---- Check Guest Approval Status ----
        if path == "/api/check_approval":
            status_str = "PENDING"
            if req_id in store.approved_tokens:
                status_str = "APPROVED"
            elif req_id in store.pending_requests:
                status_str = store.pending_requests[req_id].get("status", "PENDING")
            self._send_json({"status": status_str, "token": req_id})
            return

        # ---- Live Status Data ----
        if path == "/api/status":
            is_admin = (pin == ADMIN_PIN)
            is_approved = is_admin or (token in store.approved_tokens)
            
            if not is_approved:
                self._send_json({
                    "is_admin": False,
                    "is_approved": False,
                    "title": "Talha's Room",
                    "message": "ACCESS_RESTRICTED - Waiting for Admin Approval"
                })
                return
            
            snapshot = store.get_snapshot()
            snapshot["is_admin"] = is_admin
            snapshot["is_approved"] = True
            
            # Include pending requests for admin
            if is_admin:
                pending = []
                for rid, rinfo in store.pending_requests.items():
                    if rinfo.get("status") == "PENDING":
                        pending.append({
                            "req_id": rid,
                            "ip": rinfo.get("ip", "unknown"),
                            "time": rinfo.get("time", "")
                        })
                snapshot["pending_requests"] = pending
            else:
                snapshot["pending_requests"] = []
            
            self._send_json(snapshot)
            return

        # ---- Health Check ----
        if path == "/api/health":
            self._send_json({"status": "OK", "uptime": time.time()})
            return

        self.send_error(404, "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        
        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length) if content_length > 0 else b"{}"

        # ---- Data Push from Local PC ----
        if path == "/api/push":
            # Verify push secret
            push_secret = self.headers.get("X-Push-Secret", "")
            if push_secret != PUSH_SECRET:
                self._send_json({"status": "DENIED", "message": "Invalid push secret"}, 403)
                return
            
            try:
                payload = json.loads(post_data.decode("utf-8"))
                store.update_from_push(payload)
                
                # Return any pending commands back to the local pusher
                commands = store.pop_commands()
                self._send_json({"status": "OK", "commands": commands})
            except Exception as e:
                self._send_json({"status": "ERROR", "message": str(e)}, 400)
            return

        # ---- Guest Request Access ----
        if path == "/api/request_access":
            client_ip = self.client_address[0]
            # Check X-Forwarded-For for real IP behind proxy
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded:
                client_ip = forwarded.split(",")[0].strip()
            
            new_req_id = f"GUEST-{uuid.uuid4().hex[:6].upper()}"
            now_str = datetime.datetime.now().strftime("%H:%M:%S")
            
            store.pending_requests[new_req_id] = {
                "ip": client_ip,
                "time": now_str,
                "status": "PENDING"
            }
            
            self._send_json({
                "status": "PENDING",
                "req_id": new_req_id,
                "message": "Request submitted to Admin (Talha)"
            })
            return

        # ---- Admin Approve/Deny Guest ----
        if path == "/api/approve_request":
            try:
                payload = json.loads(post_data.decode("utf-8"))
                pin = payload.get("pin", "")
                req_id = payload.get("req_id", "")
                action = payload.get("action", "ALLOW")
                
                if pin != ADMIN_PIN:
                    self._send_json({"status": "DENIED", "message": "Invalid Admin PIN"}, 403)
                    return
                
                if action == "ALLOW":
                    store.approved_tokens.add(req_id)
                    if req_id in store.pending_requests:
                        store.pending_requests[req_id]["status"] = "APPROVED"
                    self._send_json({"status": "SUCCESS", "message": f"Approved {req_id}"})
                else:
                    if req_id in store.pending_requests:
                        store.pending_requests[req_id]["status"] = "DENIED"
                    self._send_json({"status": "SUCCESS", "message": f"Denied {req_id}"})
            except Exception as e:
                self._send_json({"status": "ERROR", "message": str(e)}, 400)
            return

        # ---- Emergency Close All (queues command for local pusher) ----
        if path == "/api/close_all":
            try:
                payload = json.loads(post_data.decode("utf-8")) if post_data else {}
                pin = payload.get("pin", "")
                
                if pin != ADMIN_PIN:
                    self._send_json({"status": "DENIED", "message": "Only Admin can close trades!"}, 403)
                    return
                
                store.queue_command({"action": "CLOSE_ALL", "time": time.time()})
                self._send_json({"status": "QUEUED", "message": "Emergency close command sent to MT5", "closed_count": 0})
            except Exception as e:
                self._send_json({"status": "ERROR", "message": str(e)}, 400)
            return

        # ---- Legacy telemetry endpoint (backward compat) ----
        if path == "/api/telemetry":
            try:
                payload = json.loads(post_data.decode("utf-8"))
                store.update_from_push(payload)
                self._send_json({"status": "SUCCESS"})
            except Exception as e:
                self._send_json({"status": "ERROR", "message": str(e)}, 400)
            return

        self.send_error(404, "Not found")


# ============================================================
# SERVER STARTUP
# ============================================================
def run_server():
    server = HTTPServer(("0.0.0.0", PORT), CloudDashboardHandler)
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║         👑 TALHA'S ROOM - CLOUD DASHBOARD SERVER            ║
╠══════════════════════════════════════════════════════════════╣
║  Server running on port {PORT}                               
║  Waiting for MT5 data push from local PC...                 
║                                                              
║  ADMIN URL:  https://YOUR-APP.onrender.com/?pin=7788        
║  VIEWER URL: https://YOUR-APP.onrender.com/view              
╚══════════════════════════════════════════════════════════════╝
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down cloud server...")
        server.server_close()


if __name__ == "__main__":
    run_server()
