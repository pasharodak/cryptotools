"""Quick diagnostic: trigger play and poll bot status."""
import json
import time
import urllib.parse
import urllib.request

API = "http://127.0.0.1:18999"


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


pairs = get(f"{API}/sim/pairs")["pairs"]
pair = pairs[0]
q = urllib.parse.quote(pair, safe="")
rng = get(f"{API}/sim/range?pair={q}&timeframe=1s")
start = rng["start_ms"]
end = min(rng["end_ms"], start + 3 * 86400000)
print("pair", pair, "range", start, end, "pairs", len(pairs))

try:
    play = post(
        "/sim/player/play",
        {
            "pair": pair,
            "pairs": pairs[:2],
            "range_start_ms": start,
            "range_end_ms": end,
            "speed": 3600,
        },
    )
    print("play ok", play.get("status"), "sequential", play.get("sequential"))
except Exception as e:
    print("play failed", e)

for i in range(12):
    time.sleep(10)
    st = get(f"{API}/sim/bots/status")
    inst = get(f"{API}/sim/bots/instances")
    print(
        f"t+{(i+1)*10}s phase={st.get('phase')} running={st.get('running')} "
        f"count={st.get('count')} inst={len(inst.get('instances', []))} "
        f"error={st.get('error')!r} scenarios={list((st.get('scenarios') or {}).keys())[:3]}"
    )
    if st.get("phase") in ("ready", "error", "idle") and not st.get("running"):
        break
