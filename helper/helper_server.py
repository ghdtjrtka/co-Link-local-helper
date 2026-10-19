"""Loopback-only local co-Link helper. Operations are injectable for safe testing."""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.parse
import base64
import zipfile
from pathlib import Path

MAX_BODY = 65536
DEFAULT_ORIGIN = "https://colink.namanavi.com"


def make_handler(allowed_origins, state_dir, *, inspector=None, verifier=None,
                 native_runner=None, backup_provider=None, health_provider=None,
                 enable_conversion=False, execution_inspector=None, recovery_runner=None,
                 verification_runner=None, clock=time.time):
    origins = frozenset(allowed_origins)
    state = Path(state_dir)
    tokens = {}
    lock = threading.Lock()
    operation_lock = threading.Lock()

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("helper_runtime_unavailable")

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "coLinkHelper/1"
        def log_message(self, *_args):
            pass
        def send_json(self, status, value, extra=None):
            raw = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            if extra:
                for k, v in extra.items(): self.send_header(k, v)
            self.end_headers(); self.wfile.write(raw)
        def origin(self):
            value = self.headers.get("Origin")
            return value if value in origins else None
        def cors(self, origin):
            return {"Access-Control-Allow-Origin": origin, "Vary": "Origin",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, X-CoLink-Helper-Token",
                    "Access-Control-Allow-Private-Network": "true"}
        def do_OPTIONS(self):
            origin = self.origin()
            requested = self.headers.get("Access-Control-Request-Headers", "")
            allowed = {"content-type", "x-colink-helper-token"}
            if not origin or any(x.strip().lower() not in allowed for x in requested.split(",") if x.strip()):
                return self.send_json(403, {"error":"origin_rejected"})
            self.send_response(204)
            for k, v in self.cors(origin).items(): self.send_header(k, v)
            self.send_header("Access-Control-Max-Age", "300"); self.end_headers()
        def authorize(self):
            origin = self.origin()
            if not origin:
                self.send_json(403, {"error":"origin_rejected"}); return None
            token = self.headers.get("X-CoLink-Helper-Token", "")
            with lock:
                exp = tokens.get(token, 0)
                if exp < clock(): tokens.pop(token, None); exp = 0
            if not token or not exp:
                self.send_json(401, {"error":"helper_token_required"}, self.cors(origin)); return None
            return origin
        def body(self):
            try: n = int(self.headers.get("Content-Length", "-1"))
            except ValueError: n = -1
            if n < 0 or n > MAX_BODY: raise ValueError("body_size_invalid")
            data = self.rfile.read(n)
            if len(data) != n: raise ValueError("body_size_invalid")
            obj = json.loads(data or b"{}")
            if not isinstance(obj, dict): raise ValueError("json_object_required")
            return obj
        def do_GET(self):
            origin = self.origin()
            if not origin: return self.send_json(403, {"error":"origin_rejected"})
            path = urllib.parse.urlsplit(self.path).path
            if path == "/v1/connect":
                token = secrets.token_urlsafe(32)
                with lock: tokens[token] = clock() + 300
                return self.send_json(200, {"helper_token":token,"expires_in":300}, self.cors(origin))
            if not self.authorize(): return
            if path == "/v1/health":
                health=(health_provider or (lambda:{"status":"unavailable","errors":["helper_runtime_unavailable"]}))()
                return self.send_json(200,health,self.cors(origin))
            if path.startswith("/v1/backups/"):
                bid = path.rsplit("/",1)[-1]
                if not bid.isalnum(): return self.send_json(400,{"error":"invalid_backup_id"},self.cors(origin))
                if backup_provider:
                    try: archive=backup_provider(bid)
                    except FileNotFoundError: return self.send_json(404,{"error":"no_backup"},self.cors(origin))
                else: archive = state / "backups" / (bid + ".zip")
                if not archive.is_file(): return self.send_json(404,{"error":"no_backup"},self.cors(origin))
                data = archive.read_bytes()
                self.send_response(200); self.send_header("Content-Type","application/zip")
                self.send_header("Content-Length",str(len(data))); self.send_header("Content-Disposition",'attachment; filename="original-driver-backup.zip"')
                for k,v in self.cors(origin).items(): self.send_header(k,v)
                self.end_headers(); self.wfile.write(data); return
            self.send_json(404,{"error":"not_found"},self.cors(origin))
        def do_POST(self):
            origin = self.authorize()
            if not origin: return
            try: obj = self.body()
            except Exception as e: return self.send_json(400,{"error":str(e)},self.cors(origin))
            path = urllib.parse.urlsplit(self.path).path
            if not operation_lock.acquire(blocking=False):
                return self.send_json(409,{"error":"helper_busy"},self.cors(origin))
            try:
                if path == "/v1/inspect":
                    result = (inspector or unavailable)(obj.get("instance_id"))
                    return self.send_json(200,result,self.cors(origin))
                if path in ('/v1/restore','/v1/verify'):
                    if not enable_conversion: raise PermissionError('conversion_disabled')
                    envelope,job_id=obj.get('plan'),obj.get('job_id')
                    if not isinstance(envelope,dict) or not isinstance(job_id,str): raise ValueError('invalid_request')
                    runner=recovery_runner if path=='/v1/restore' else verification_runner
                    result=(runner or unavailable)(envelope,job_id)
                    return self.send_json(200,result,self.cors(origin))
                if path == "/v1/execute":
                    envelope, job_id = obj.get("plan"), obj.get("job_id")
                    if not isinstance(envelope,dict) or not isinstance(job_id,str): raise ValueError("invalid_request")
                    if not enable_conversion: raise PermissionError("conversion_disabled")
                    try:
                        payload=json.loads(base64.b64decode(envelope.get("payload_b64",""),validate=True))
                        hint=payload.get("instance_id")
                    except Exception as exc: raise ValueError("invalid_plan_envelope") from exc
                    if not isinstance(hint,str) or len(hint)>256: raise ValueError("invalid_plan_target")
                    inspected=(execution_inspector or inspector or unavailable)(hint)
                    if not isinstance(inspected,dict) or "report" not in inspected or "identity" not in inspected:
                        raise RuntimeError("inspection_unavailable")
                    report=inspected["report"]
                    report["dongle_identity"]=inspected["identity"]
                    plan = (verifier or unavailable)(envelope, report)
                    if plan.get("job_id") != job_id: raise ValueError("job_id_mismatch")
                    result = (native_runner or unavailable)(envelope, report, state)
                    if not isinstance(result,dict): raise ValueError("invalid_native_result")
                    if result.get("reboot_required") or not result.get("success"):
                        result["usable"] = False
                    return self.send_json(200,result,self.cors(origin))
                self.send_json(404,{"error":"not_found"},self.cors(origin))
            except Exception as e:
                self.send_json(400,{"error":str(e) or "operation_failed"},self.cors(origin))
            finally:
                operation_lock.release()
    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin", action="append", default=[])
    ap.add_argument("--development-origins", action="store_true")
    ap.add_argument("--enable-conversion", action="store_true")
    ap.add_argument("--state-dir", type=Path, default=Path.home()/"AppData/Local/co-Link/helper")
    args = ap.parse_args()
    origins = set(args.origin or [DEFAULT_ORIGIN])
    if args.development_origins: origins.update({"http://localhost:3000","http://127.0.0.1:3000"})
    try:
        from .helper_runtime import WindowsHelperRuntime
    except ImportError:
        from helper_runtime import WindowsHelperRuntime
    runtime=WindowsHelperRuntime(args.state_dir,enable_conversion=args.enable_conversion)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), make_handler(origins,args.state_dir,
        inspector=runtime.inspect,verifier=runtime.verify,native_runner=runtime.execute,
        backup_provider=runtime.backup_path,health_provider=runtime.health,
        execution_inspector=runtime.inspect_for_execution,
        recovery_runner=runtime.restore,verification_runner=runtime.verify_conversion,
        enable_conversion=args.enable_conversion))
    server.daemon_threads = True
    try: server.serve_forever()
    finally: server.server_close()

if __name__ == "__main__": main()
