import csv
import os
import time
from datetime import datetime, timezone

import requests

EMAIL    = os.environ["MYFXBOOK_EMAIL"].strip()
PASSWORD = os.environ["MYFXBOOK_PASSWORD"].strip()
BASE     = "https://www.myfxbook.com/api"
SYMBOLS  = ["XAUUSD"]                                   # 増やすならここに追記
STOOQ    = {"XAUUSD": "xauusd", "XAGUSD": "xagusd"}     # 価格取得用のコード対応表
CSV_PATH = "positionbook.csv"
UA       = {"User-Agent": "gold-sentiment/1.0"}

FIELDS = [
    "snapshot_time", "instrument", "price",
    "long_pct", "short_pct", "net_pct", "long_ratio",
    "long_vol", "short_vol", "net_vol_pct",
    "long_positions", "short_positions",
    "avg_long_price", "avg_short_price",
    "fetched_at",
]


def api(path, params, attempts=3):
    """通信エラー・5xxのみリトライ。API側のerror=trueは即中断（無駄撃ち防止）。"""
    last = None
    for i in range(1, attempts + 1):
        try:
            r = requests.get(f"{BASE}/{path}", params=params, headers=UA, timeout=20)
            r.raise_for_status()
            d = r.json()
        except Exception as e:                       # JSONでない応答もここで捕まる
            last = e
            print(f"[retry {i}/{attempts}] {path}: {e}")
            time.sleep(5 * i)
            continue
        if str(d.get("error")).lower() == "true":
            raise RuntimeError(f"{path} failed: {d.get('message')}")
        return d
    raise RuntimeError(f"{path} unreachable: {last}")


def login():
    return api("login.json", {"email": EMAIL, "password": PASSWORD})["session"]


def logout(session):
    try:
        api("logout.json", {"session": session}, attempts=1)
    except Exception as e:
        print("[warn] logout skipped:", e)


def outlook(session):
    return api("get-community-outlook.json", {"session": session}).get("symbols") or []


def stooq_price(inst):
    """価格は取れなくても致命的でないので、失敗は空欄で返す"""
    code = STOOQ.get(inst, inst.lower())
    try:
        r = requests.get("https://stooq.com/q/l/",
                         params={"s": code, "f": "sd2t2ohlcv", "h": "", "e": "csv"},
                         headers=UA, timeout=15)
        r.raise_for_status()
        return round(float(next(csv.DictReader(r.text.splitlines()))["Close"]), 3)
    except Exception as e:
        print(f"[warn] stooq {code}: {e}")
        return ""


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def read_existing():
    if not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0:
        return [], []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        return (rd.fieldnames or []), list(rd)


def save(new_rows):
    header, old = read_existing()
    keys = {(r.get("instrument"), r.get("snapshot_time")) for r in old}
    fresh = [r for r in new_rows if (r["instrument"], r["snapshot_time"]) not in keys]

    if header and header != FIELDS:                  # 列構成を変えた場合は自動で詰め替え
        print("[info] schema changed -> rewriting csv")
        merged = [{k: r.get(k, "") for k in FIELDS} for r in old] + fresh
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(merged)
        return len(fresh)

    if not fresh:
        return 0
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if not header:                               # ヘッダ二重書き込みを防ぐ
            w.writeheader()
        w.writerows(fresh)
    return len(fresh)


def main():
    now = datetime.now(timezone.utc)
    stamp = now.replace(minute=0, second=0, microsecond=0).isoformat(timespec="seconds")

    session = login()
    try:
        symbols = outlook(session)
    finally:
        logout(session)

    found = {s.get("name"): s for s in symbols}
    print("available:", ", ".join(sorted(k for k in found if k)))

    rows = []
    for inst in SYMBOLS:
        x = found.get(inst)
        if not x:
            print(f"[skip] {inst} not in outlook")
            continue

        lo, sh = num(x.get("longPercentage")), num(x.get("shortPercentage"))
        lv, sv = num(x.get("longVolume")), num(x.get("shortVolume"))
        if lo is None or sh is None:
            print(f"[skip] {inst} percentage missing")
            continue

        tot  = lo + sh
        vtot = (lv or 0) + (sv or 0)
        net_vol = round((lv - sv) / vtot * 100, 2) if (lv is not None and sv is not None and vtot) else ""

        rows.append({
            "snapshot_time":   stamp,
            "instrument":      inst,
            "price":           stooq_price(inst),
            "long_pct":        lo,
            "short_pct":       sh,
            "net_pct":         round(lo - sh, 2),
            "long_ratio":      round(lo / tot * 100, 2) if tot else "",
            "long_vol":        lv if lv is not None else "",
            "short_vol":       sv if sv is not None else "",
            "net_vol_pct":     net_vol,
            "long_positions":  x.get("longPositions", ""),
            "short_positions": x.get("shortPositions", ""),
            "avg_long_price":  x.get("avgLongPrice", ""),
            "avg_short_price": x.get("avgShortPrice", ""),
            "fetched_at":      now.isoformat(timespec="seconds"),
        })
        print(f"{inst}: L{lo}/S{sh} netVol {net_vol} avgL {x.get('avgLongPrice')} avgS {x.get('avgShortPrice')}")

    print(f"added {save(rows)} rows")


if __name__ == "__main__":
    main()
