#!/usr/bin/env sh
set -eu

OPTIONS=/data/options.json
CONFIG=/config/zendure-proxy.yaml

python - <<'PY'
import json
from pathlib import Path
import yaml

options_path = Path("/data/options.json")
config_path = Path("/config/zendure-proxy.yaml")
options = json.loads(options_path.read_text()) if options_path.exists() else {}

config = {
    "server": {
        "host": "0.0.0.0",
        "port": 1880,
        "log_level": options.get("log_level", "INFO"),
    },
    "zendure": {
        "timeout_seconds": options.get("timeout_seconds", 5),
        "cache_ttl_seconds": options.get("cache_ttl_seconds", 60),
        "devices": options.get("devices", []),
    },
    "proxy": {
        "language": "EN",
    },
}
config_path.parent.mkdir(parents=True, exist_ok=True)
config_path.write_text(yaml.safe_dump(config, sort_keys=False))
PY

exec python -m uvicorn zendure_proxy.server:app --host 0.0.0.0 --port 1880

