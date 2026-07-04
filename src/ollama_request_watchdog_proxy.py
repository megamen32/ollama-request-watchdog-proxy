#!/usr/bin/env python3
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

LISTEN_HOST = os.environ.get('OLLAMA_PROXY_HOST', '127.0.0.1')
LISTEN_PORT = int(os.environ.get('OLLAMA_PROXY_PORT', '11437'))
TARGET = os.environ.get('OLLAMA_TARGET_URL', 'http://127.0.0.1:11434').rstrip('/')
FIRST_BYTE_TIMEOUT = int(os.environ.get('OLLAMA_PROXY_FIRST_BYTE_TIMEOUT_S', '180'))
RESTART_COOLDOWN = int(os.environ.get('OLLAMA_PROXY_RESTART_COOLDOWN_S', '120'))
RETRY_AFTER_RESTART_S = int(os.environ.get('OLLAMA_PROXY_RETRY_AFTER_RESTART_S', '20'))
DEFAULT_MAX_NUM_CTX = int(os.environ.get('OLLAMA_PROXY_MAX_NUM_CTX', '8192'))
ENFORCE_DEFAULT_NUM_CTX = os.environ.get('OLLAMA_PROXY_ENFORCE_DEFAULT_NUM_CTX', '1').lower() in ('1', 'true', 'yes', 'on')
MODEL_CTX_LIMITS_RAW = os.environ.get(
    'OLLAMA_PROXY_MODEL_CTX_LIMITS',
    'qwen-fast=4096,qwen3:4b-instruct=4096,qwen3:4b-instruct-ollama=4096,qwen3-coder:30b=4096,qwen3-coder:30b-instruct-q4_K_M=4096'
)
OLLAMA_BIN = os.environ.get('OLLAMA_BIN', '/usr/local/bin/ollama')
OLLAMA_SERVICE = os.environ.get('OLLAMA_SERVICE', 'ollama.service')
LOG_PREFIX = 'ollama-request-watchdog:'
LAST_RESTART = 0.0

HOP_HEADERS = {
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailers', 'transfer-encoding', 'upgrade', 'content-length',
}

def log(msg):
    print(f'{LOG_PREFIX} {msg}', flush=True)

def parse_model_limits(raw):
    limits = []
    for item in (raw or '').split(','):
        item = item.strip()
        if not item or '=' not in item:
            continue
        prefix, value = item.split('=', 1)
        try:
            limits.append((prefix.strip(), int(value.strip())))
        except ValueError:
            log(f'ignore bad model limit {item!r}')
    limits.sort(key=lambda x: len(x[0]), reverse=True)
    return limits

MODEL_CTX_LIMITS = parse_model_limits(MODEL_CTX_LIMITS_RAW)

def ctx_limit_for_model(model):
    model = str(model or '')
    for prefix, limit in MODEL_CTX_LIMITS:
        if model == prefix or model.startswith(prefix):
            return limit
    return DEFAULT_MAX_NUM_CTX

def run(cmd, timeout=90):
    log(f'run: {cmd}')
    return subprocess.run(cmd, shell=True, timeout=timeout, text=True, capture_output=True)

def service_exists():
    r = run(f'systemctl cat {OLLAMA_SERVICE}', timeout=20)
    return r.returncode == 0

def install_missing_ollama_unit():
    service_path = Path('/etc/systemd/system/ollama.service')
    if service_path.exists() or service_exists():
        return False
    if not Path(OLLAMA_BIN).exists():
        log(f'cannot install {OLLAMA_SERVICE}: binary missing: {OLLAMA_BIN}')
        return False
    unit = f'''[Unit]\nDescription=Ollama Service\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nExecStart={OLLAMA_BIN} serve\nUser=ollama\nGroup=ollama\nRestart=always\nRestartSec=3\nEnvironment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"\nEnvironment="OLLAMA_HOST=0.0.0.0:11434"\n\n[Install]\nWantedBy=multi-user.target\n'''
    tmp = service_path.with_suffix('.service.tmp')
    tmp.write_text(unit)
    tmp.replace(service_path)
    log(f'installed missing {service_path}')
    r = run('systemctl daemon-reload && systemctl enable ollama.service', timeout=60)
    if r.returncode != 0:
        log(f'enable failed rc={r.returncode} stdout={r.stdout[-1000:]} stderr={r.stderr[-1000:]}')
    return True

def restart_ollama(reason):
    global LAST_RESTART
    now = time.monotonic()
    if now - LAST_RESTART < RESTART_COOLDOWN:
        log(f'skip restart cooldown reason={reason!r}')
        return False
    LAST_RESTART = now
    log(f'RESTART ollama reason={reason}')
    try:
        if not service_exists():
            install_missing_ollama_unit()
        r = run('systemctl restart ollama.service', timeout=90)
        if r.returncode != 0:
            log(f'restart failed rc={r.returncode} stdout={r.stdout[-1000:]} stderr={r.stderr[-1000:]}')
            if 'Unit ollama.service not found' in (r.stdout + r.stderr):
                install_missing_ollama_unit()
            r2 = run('systemctl start ollama.service', timeout=90)
            if r2.returncode != 0:
                log(f'start failed rc={r2.returncode} stdout={r2.stdout[-1000:]} stderr={r2.stderr[-1000:]}')
                return False
        else:
            log('restart ok')
        return True
    except Exception as e:
        log(f'restart failed: {type(e).__name__}: {e}')
        return False

def is_ollama_generate_path(path):
    clean = path.split('?', 1)[0]
    return clean in ('/api/generate', '/api/chat')

def clamp_body_if_needed(method, path, body, headers):
    if method not in ('POST', 'PUT') or not body or not is_ollama_generate_path(path):
        return body, headers, None
    try:
        payload = json.loads(body.decode('utf-8'))
    except Exception:
        return body, headers, None
    if not isinstance(payload, dict):
        return body, headers, None

    model = payload.get('model', '')
    limit = ctx_limit_for_model(model)
    options = payload.get('options')
    if options is None:
        options = {}
    if not isinstance(options, dict):
        return body, headers, {'reject': 400, 'body': '{"error":"options must be a JSON object"}'}

    changed = False
    reasons = []
    old = options.get('num_ctx')
    try:
        old_int = int(old) if old is not None else None
    except Exception:
        old_int = None
        reasons.append(f'invalid_num_ctx={old!r}')

    if old_int is None:
        if ENFORCE_DEFAULT_NUM_CTX:
            options['num_ctx'] = limit
            changed = True
            reasons.append(f'default_num_ctx={limit}')
    elif old_int > limit:
        options['num_ctx'] = limit
        changed = True
        reasons.append(f'clamped_num_ctx={old_int}->{limit}')
    elif old_int < 256:
        options['num_ctx'] = 256
        changed = True
        reasons.append(f'raised_tiny_num_ctx={old_int}->256')

    payload['options'] = options
    if changed:
        new_body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        new_headers = {k: v for k, v in headers.items() if k.lower() != 'content-length'}
        new_headers['Content-Type'] = headers.get('Content-Type', 'application/json')
        log(f'ctx_guard path={path} model={model!r} limit={limit} ' + ' '.join(reasons))
        return new_body, new_headers, None
    return body, headers, None

def upstream_error_is_restartable(e):
    if isinstance(e, socket.timeout):
        return True
    if isinstance(e, ConnectionRefusedError):
        return True
    if isinstance(e, urllib.error.URLError):
        reason = getattr(e, 'reason', None)
        if isinstance(reason, (ConnectionRefusedError, socket.timeout)):
            return True
        text = str(reason or e).lower()
        return 'connection refused' in text or 'timed out' in text
    return False

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def _send_json(self, code, body):
        raw = body.encode('utf-8') if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode('utf-8')
        try:
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            log(f'client_closed while sending json code={code} path={self.path}')

    def do_GET(self):
        if self.path == '/health':
            return self.health()
        return self.proxy()

    def do_POST(self):
        return self.proxy()

    def do_DELETE(self):
        return self.proxy()

    def do_PUT(self):
        return self.proxy()

    def health(self):
        try:
            req = urllib.request.Request(TARGET + '/api/tags', method='GET')
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=3) as resp:
                ok = 200 <= resp.status < 300
            return self._send_json(200 if ok else 503, {
                'ok': ok, 'mode': 'on-demand', 'target': TARGET, 'target_api_tags': ok,
                'first_byte_timeout_s': FIRST_BYTE_TIMEOUT, 'max_num_ctx': DEFAULT_MAX_NUM_CTX,
                'model_ctx_limits': dict(MODEL_CTX_LIMITS),
            })
        except Exception as e:
            return self._send_json(503, {'ok': False, 'mode': 'on-demand', 'target': TARGET, 'error': f'{type(e).__name__}: {e}'})

    def proxy(self):
        started = time.monotonic()
        length = int(self.headers.get('Content-Length', '0') or '0')
        body = self.rfile.read(length) if length else None
        target_url = TARGET + self.path
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
        headers['Host'] = urlsplit(TARGET).netloc
        body, headers, guard_error = clamp_body_if_needed(self.command, self.path, body, headers)
        if guard_error:
            return self._send_json(guard_error['reject'], guard_error['body'])
        req = urllib.request.Request(target_url, data=body, headers=headers, method=self.command)
        first_chunk = False
        headers_sent = False
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=FIRST_BYTE_TIMEOUT) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in HOP_HEADERS:
                        self.send_header(k, v)
                self.send_header('X-Ollama-Watchdog-Proxy', 'on-demand')
                self.send_header('Connection', 'close')
                self.end_headers()
                headers_sent = True
                while True:
                    try:
                        chunk = resp.read(65536)
                    except socket.timeout:
                        if not first_chunk:
                            restart_ollama(f'timeout before first_chunk path={self.path}')
                        else:
                            log(f'stream read timeout after first_chunk path={self.path}; no restart')
                        break
                    if not chunk:
                        break
                    if not first_chunk:
                        first_chunk = True
                        log(f'first_chunk path={self.path} after_s={time.monotonic()-started:.3f}')
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        log(f'client_closed path={self.path} first_chunk={first_chunk}; no restart')
                        return
        except socket.timeout:
            restart_ollama(f'timeout waiting response path={self.path}')
            if not headers_sent:
                self._send_json(504, '{"error":"ollama timeout before first byte; restart attempted"}')
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                self.send_response(e.code)
                for k, v in e.headers.items():
                    if k.lower() not in HOP_HEADERS:
                        self.send_header(k, v)
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('X-Ollama-Watchdog-Proxy', 'on-demand')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                log(f'client_closed while forwarding HTTPError code={e.code} path={self.path}; no restart')
        except (BrokenPipeError, ConnectionResetError):
            log(f'client_closed path={self.path}; no restart')
        except Exception as e:
            restartable = upstream_error_is_restartable(e)
            log(f'upstream_error restartable={restartable} type={type(e).__name__} error={e} path={self.path}')
            if restartable:
                restart_ollama(f'{type(e).__name__}: {e} path={self.path}')
                if self.command in ('GET', 'HEAD') and not headers_sent:
                    deadline = time.monotonic() + RETRY_AFTER_RESTART_S
                    last_error = e
                    while time.monotonic() < deadline:
                        time.sleep(0.5)
                        try:
                            retry_req = urllib.request.Request(target_url, data=body, headers=headers, method=self.command)
                            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                            with opener.open(retry_req, timeout=FIRST_BYTE_TIMEOUT) as resp:
                                raw = resp.read()
                                self.send_response(resp.status)
                                for k, v in resp.headers.items():
                                    if k.lower() not in HOP_HEADERS:
                                        self.send_header(k, v)
                                self.send_header('Content-Length', str(len(raw)))
                                self.send_header('X-Ollama-Watchdog-Proxy', 'on-demand; retry-after-restart')
                                self.send_header('Connection', 'close')
                                self.end_headers()
                                self.wfile.write(raw)
                                log(f'retry_after_restart ok path={self.path} after_s={time.monotonic()-started:.3f}')
                                return
                        except Exception as retry_error:
                            last_error = retry_error
                    log(f'retry_after_restart failed path={self.path}: {type(last_error).__name__}: {last_error}')
            if not headers_sent:
                self._send_json(502, '{"error":"ollama proxy upstream error"}')

    def log_message(self, fmt, *args):
        log(f'{self.client_address[0]} "{fmt % args}"')

if __name__ == '__main__':
    log(f'listening on {LISTEN_HOST}:{LISTEN_PORT}, target={TARGET}, timeout={FIRST_BYTE_TIMEOUT}s, max_num_ctx={DEFAULT_MAX_NUM_CTX}, model_limits={dict(MODEL_CTX_LIMITS)}')
    if not service_exists():
        install_missing_ollama_unit()
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()
