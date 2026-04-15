import os, time, json, threading, requests, websocket
from collections import deque
from datetime import datetime
from flask import Flask, jsonify, Response

TOKEN            = os.getenv("TOKEN")
CHAT_ID          = os.getenv("CHAT_ID")
SPREAD_THRESHOLD = float(os.getenv("SPREAD_THRESHOLD", "2.0"))
BIAS_WINDOW      = int(os.getenv("BIAS_WINDOW", "100"))
SIGNAL_COOLDOWN  = int(os.getenv("SIGNAL_COOLDOWN", "60"))
PORT             = int(os.getenv("PORT", "8080"))

# Binance spot
BIN_URLS = [
    "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
    "https://api1.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
    "https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT",
    "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT",
]

# MEXC futures REST (no geo restrictions)
MEXC_URL = "https://contract.mexc.com/api/v1/contract/ticker?symbol=BTC_USDT"

state = {
    "binance_price": None,
    "mexc_price": None,
    "spread_history": deque(maxlen=BIAS_WINDOW),
    "last_signal_ts": 0, "bias": 0.0, "deviation": 0.0,
    "sig_count": 0, "tick_count": 0, "max_dev": 0.0,
    "last_signal": None,
}
lock = threading.Lock()
working_bin_url = None

DASHBOARD_HTML = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")).read()

app = Flask(__name__)

@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    return r

@app.route("/")
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")

@app.route("/api/data")
def api_data():
    with lock:
        return jsonify({
            "binance": state["binance_price"],
            "poly_mid": state["mexc_price"],  # labelled as poly for frontend
            "bias": round(state["bias"], 4),
            "deviation": round(state["deviation"], 4),
            "sig_count": state["sig_count"],
            "tick_count": state["tick_count"],
            "max_dev": round(state["max_dev"], 4),
            "last_signal": state["last_signal"],
            "threshold": SPREAD_THRESHOLD,
        })

def send_tg(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"[TG ERR] {e}")

# ── BINANCE ───────────────────────────────────────────────────────────────────
def fetch_bin():
    global working_bin_url
    urls = ([working_bin_url] + [u for u in BIN_URLS if u != working_bin_url]) if working_bin_url else BIN_URLS
    for url in urls:
        try:
            r = requests.get(url, timeout=4)
            if r.status_code == 200:
                p = float(r.json()["price"])
                if p > 0:
                    working_bin_url = url
                    return p
        except: continue
    return None

def binance_thread():
    while True:
        try:
            p = fetch_bin()
            if p:
                with lock: state["binance_price"] = p
                check_signal()
                print(f"[BIN] ${p:,.0f}")
        except Exception as e: print(f"[BIN ERR] {e}")
        time.sleep(1)

# ── MEXC FUTURES ──────────────────────────────────────────────────────────────
def fetch_mexc():
    try:
        r = requests.get(MEXC_URL, timeout=5)
        data = r.json()
        # MEXC returns lastPrice in data.data
        price = float(data["data"]["lastPrice"])
        if price > 0:
            return price
    except Exception as e:
        print(f"[MEXC ERR] {e}")
    return None

def mexc_thread():
    print("[MEXC] Starting futures polling...")
    while True:
        try:
            p = fetch_mexc()
            if p:
                with lock: state["mexc_price"] = p
                print(f"[MEXC] ${p:,.2f}")
                check_signal()
            else:
                print("[MEXC] no price")
        except Exception as e: print(f"[MEXC ERR] {e}")
        time.sleep(2)

# ── SIGNAL ────────────────────────────────────────────────────────────────────
def check_signal():
    with lock:
        b = state["binance_price"]
        m = state["mexc_price"]
    if not b or not m: return

    # Spread = MEXC futures - Binance spot (in $)
    spread = m - b

    with lock:
        state["spread_history"].append(spread)
        hist = list(state["spread_history"])

    if len(hist) < 10: return

    bias = sum(hist) / len(hist)
    dev  = spread - bias  # deviation from rolling mean
    now  = time.time()

    with lock:
        state["bias"]       = bias
        state["deviation"]  = dev
        state["tick_count"] += 1
        if abs(dev) > state["max_dev"]: state["max_dev"] = abs(dev)
        last = state["last_signal_ts"]

    if abs(dev) >= SPREAD_THRESHOLD and (now - last) > SIGNAL_COOLDOWN:
        d = "UP" if dev > 0 else "DOWN"
        send_tg(
            f"{'📈' if dev>0 else '📉'} <b>РАСКОРРЕЛЯЦИЯ {d}</b>\n\n"
            f"Binance spot: <b>${b:,.0f}</b>\n"
            f"MEXC futures: <b>${m:,.0f}</b>\n"
            f"Спред: <b>${spread:+.2f}</b>\n"
            f"Отклонение от bias: <b>${dev:+.2f}</b>\n"
            f"Bias: <b>${bias:.2f}</b>\n"
            f"⏱ {datetime.utcnow().strftime('%H:%M:%S')} UTC"
        )
        with lock:
            state["last_signal_ts"] = now
            state["sig_count"] += 1
            state["last_signal"] = {
                "direction": d, "deviation": round(dev, 2),
                "binance": b, "mexc": m,
                "time": datetime.utcnow().strftime("%H:%M:%S"),
            }
        print(f"[SIGNAL] {d} spread={spread:+.2f} dev={dev:+.2f}")

if __name__ == "__main__":
    print(f"[START] BTC Lag Bot (Binance vs MEXC futures) on :{PORT}")
    send_tg(f"🚀 <b>BTC Lag Bot запущен</b>\nBinance spot vs MEXC futures\nПорог: ${SPREAD_THRESHOLD}")
    threading.Thread(target=binance_thread, daemon=True).start()
    threading.Thread(target=mexc_thread, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False)
