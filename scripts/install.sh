#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
install -m 0755 -o root -g root "$ROOT/src/ollama_request_watchdog_proxy.py" /usr/local/sbin/ollama-request-watchdog-proxy
install -m 0644 -o root -g root "$ROOT/systemd/ollama-request-watchdog-proxy.service" /etc/systemd/system/ollama-request-watchdog-proxy.service
mkdir -p /etc/systemd/system/ollama-request-watchdog-proxy.service.d
if [[ ! -f /etc/systemd/system/ollama-request-watchdog-proxy.service.d/20-hardening.conf ]]; then
  install -m 0644 -o root -g root "$ROOT/systemd/20-hardening.conf.example" /etc/systemd/system/ollama-request-watchdog-proxy.service.d/20-hardening.conf
fi
systemctl daemon-reload
systemctl enable --now ollama-request-watchdog-proxy.service
systemctl status ollama-request-watchdog-proxy.service --no-pager -l
