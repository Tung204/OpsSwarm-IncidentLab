#!/usr/bin/env bash
set -euo pipefail

PROM="${PROMETHEUS_URL:-http://localhost:9090}"
AM="${ALERTMANAGER_URL:-http://localhost:9093}"
LAB="${INCIDENTLAB_URL:-http://localhost:8080}"

printf '1/5 IncidentLab health...\n'
curl -fsS "$LAB/health" | python3 -m json.tool

printf '\n2/5 Prometheus ready...\n'
curl -fsS "$PROM/-/ready"

printf '\n3/5 Prometheus targets...\n'
curl -fsS "$PROM/api/v1/targets" > /tmp/opsswarm-targets.json
python3 - <<'PY'
import json
p=json.load(open('/tmp/opsswarm-targets.json'))
active=p['data']['activeTargets']
for t in active:
    print(t['labels'].get('job'), t.get('scrapeUrl'), t.get('health'), t.get('lastError',''))
required=[t for t in active if t['labels'].get('job')=='demomart']
assert len(required)==5, f'expected 5 demomart targets, got {len(required)}'
assert all(t.get('health')=='up' for t in required), 'one or more DemoMart targets are not UP'
PY

printf '\n4/5 Query metric...\n'
curl -fsS --get "$PROM/api/v1/query" --data-urlencode 'query=demomart_service_healthy' > /tmp/opsswarm-query.json
python3 - <<'PY'
import json
p=json.load(open('/tmp/opsswarm-query.json'))
assert p['status']=='success'
print('series=',len(p['data']['result']))
assert len(p['data']['result'])==5
PY

printf '\n5/5 Alertmanager status...\n'
curl -fsS "$AM/-/ready"

printf '\nOBSERVABILITY_SMOKE_TEST = PASS\n'
