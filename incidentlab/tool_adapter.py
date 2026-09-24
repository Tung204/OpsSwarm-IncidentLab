import os

import requests

URLS = {
    "auth": os.getenv("AUTH_URL", "http://localhost:8001"),
    "order": os.getenv("ORDER_URL", "http://localhost:8002"),
    "inventory": os.getenv("INVENTORY_URL", "http://localhost:8003"),
    "payment": os.getenv("PAYMENT_URL", "http://localhost:8004"),
    "booking-api": os.getenv("BOOKING_API_URL", "http://localhost:8005"),
}
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")

def getm(service):
    r = requests.get(URLS[service] + "/metrics-json", timeout=5); r.raise_for_status(); return r.json()

def all_metrics():
    out={}
    for service in URLS:
        try: out[service]=getm(service)
        except (requests.RequestException, ValueError, KeyError) as exc:
            out[service]={"service":service,"healthy":False,"tool_error":str(exc)}
    return out

def prometheus_query(query):
    r=requests.get(PROMETHEUS_URL+"/api/v1/query",params={"query":query},timeout=5); r.raise_for_status(); body=r.json()
    if body.get("status")!="success": raise RuntimeError(f"Prometheus query failed: {body}")
    return body.get("data",{}).get("result",[])

def prometheus_health():
    try:
        r=requests.get(PROMETHEUS_URL+"/-/ready",timeout=3); return {"reachable":r.ok,"status_code":r.status_code}
    except requests.RequestException as exc:
        return {"reachable":False,"error":str(exc)}

def prometheus_snapshot(service):
    expressions={k:f'demomart_{k}{{service="{service}"}}' for k in ["service_healthy","error_rate","success_rate","latency_ms","cpu_percent","memory_percent","telemetry_present"]}
    out={}
    for key,expr in expressions.items():
        try:
            result=prometheus_query(expr); out[key]=float(result[0]["value"][1]) if result else None
        except (requests.RequestException, RuntimeError, ValueError, IndexError, KeyError) as exc:
            out[key]=None; out.setdefault("errors",{})[key]=str(exc)
    return out

def post(service,path,payload=None):
    r=requests.post(URLS[service]+path,json=payload or {},timeout=5); r.raise_for_status(); return r.json()

def reset_all(): return {service:post(service,"/admin/reset") for service in URLS}
def set_fault(service,fault,enabled=True,value=None): return post(service,"/admin/fault",{"fault":fault,"enabled":enabled,"value":value})
def set_version(service,version): return post(service,"/admin/version",{"version":version})
def recover(action):
    service=action["service"]; typ=action["type"]
    if typ=="rollback":
        set_version(service,action.get("target_version","v2.0")); set_fault(service,"error_rate",False); set_fault(service,"latency_ms",False); set_fault(service,"crash",False); set_fault(service,"db_pool_exhausted",False)
    elif typ=="partial_recovery":
        post(service,"/admin/reset"); set_fault(service,"error_rate",True,0.08)
    else: post(service,"/admin/reset")
    return {"executed":True,"action":action,"state":getm(service)}
