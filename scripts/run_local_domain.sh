#!/usr/bin/env bash
# Run a throwaway local Overte domain (domain-server + assignment clients) for
# development and tests, isolated from any real install via its own HOME.
#
# Usage:
#   scripts/run_local_domain.sh start [BRIDGE_SCRIPT_URL]   # e.g. http://127.0.0.1:9420/overte_bridge.js
#   scripts/run_local_domain.sh stop
#   scripts/run_local_domain.sh status
#
# Env:
#   OVERTE_DIR   Overte server install dir (default /opt/overte, from the overte-server .deb)
#   DOMAIN_HOME  state directory (default ./.overte-local)
#   FRESH=1      wipe the domain's content and settings on start
set -euo pipefail

OVERTE_DIR=${OVERTE_DIR:-/opt/overte}
DOMAIN_HOME=$(realpath -m "${DOMAIN_HOME:-.overte-local}")
RUN_DIR="$DOMAIN_HOME/run"
CONFIG="$DOMAIN_HOME/home/.local/share/Overte/domain-server/config.json"

start() {
  local script_url=${1:-}
  if [[ ! -x "$OVERTE_DIR/domain-server" ]]; then
    echo "domain-server not found in $OVERTE_DIR (install the overte-server package or set OVERTE_DIR)" >&2
    exit 1
  fi
  stop >/dev/null 2>&1 || true
  [[ "${FRESH:-0}" == "1" ]] && rm -rf "$DOMAIN_HOME/home"
  mkdir -p "$RUN_DIR" "$(dirname "$CONFIG")"
  python3 - "$CONFIG" "$script_url" <<'PY'
import json, os, sys
path, url = sys.argv[1], sys.argv[2]
cfg = json.load(open(path)) if os.path.exists(path) else {"version": 2.7}
scripts = [s for s in cfg.get("scripts", {}).get("persistent_scripts", [])
           if "overte_bridge" not in s.get("url", "")]
if url:
    scripts.append({"url": url, "num_instances": 1, "pool": ""})
cfg.setdefault("scripts", {})["persistent_scripts"] = scripts
json.dump(cfg, open(path, "w"), indent=2)
PY
  export HOME="$DOMAIN_HOME/home" LD_LIBRARY_PATH="$OVERTE_DIR/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  setsid "$OVERTE_DIR/domain-server" --logOptions nocolor,nojournald \
    >"$RUN_DIR/domain-server.log" 2>&1 </dev/null &
  echo $! >"$RUN_DIR/domain-server.pid"
  # 7 children: 6 mixers/servers + 1 agent for the persistent bridge script
  setsid "$OVERTE_DIR/assignment-client" -n 7 -a 127.0.0.1 --logOptions nocolor,nojournald \
    >"$RUN_DIR/assignment-client.log" 2>&1 </dev/null &
  echo $! >"$RUN_DIR/assignment-client.pid"
  for _ in $(seq 1 60); do
    if curl -sf http://127.0.0.1:40100/nodes.json 2>/dev/null | grep -q '"entity-server"'; then
      echo "Overte domain running (admin UI http://127.0.0.1:40100, logs in $RUN_DIR)"
      [[ -n "$script_url" ]] && echo "Persistent bridge script: $script_url"
      return 0
    fi
    sleep 1
  done
  echo "domain-server did not come up; see $RUN_DIR/domain-server.log" >&2
  exit 1
}

stop() {
  local any=0
  for name in assignment-client domain-server; do
    local pidfile="$RUN_DIR/$name.pid"
    if [[ -f "$pidfile" ]]; then
      local pid; pid=$(cat "$pidfile")
      # setsid made each a process-group leader: stop the group (children too)
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
      rm -f "$pidfile"; any=1
    fi
  done
  if [[ $any == 1 ]]; then
    for _ in $(seq 1 20); do pgrep -f "^$OVERTE_DIR/(domain-server|assignment-client)" >/dev/null || break; sleep 0.5; done
    echo "stopped"
  else
    echo "not running"
  fi
}

status() {
  local nodes
  nodes=$(curl -sf http://127.0.0.1:40100/nodes.json 2>/dev/null) || { echo "not running"; exit 1; }
  python3 -c 'import json,sys; [print(n["type"], "up", n["uptime"], "s") for n in json.load(sys.stdin)["nodes"]]' <<<"$nodes"
}

case "${1:-}" in
  start) shift; start "$@" ;;
  stop) stop ;;
  status) status ;;
  *) echo "usage: $0 start [BRIDGE_SCRIPT_URL] | stop | status" >&2; exit 2 ;;
esac
