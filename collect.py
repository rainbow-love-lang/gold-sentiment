import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import requests

EMAIL = os.environ["MYFXBOOK_EMAIL"].strip()
PASSWORD = os.environ["MYFXBOOK_PASSWORD"].strip()
BASE = "https://www.myfxbook.com/api"
SYMBOLS = ["XAUUSD"]  # 増やすならここに追記
CSV_PATH = "positionbook.csv"
MAX_PRICE_AGE_SEC = 900  # 価格がこれより古ければ採用しない（15分）

# ---- バックオフ（採取に失敗したとき、自分から間隔を空けるための設定）----
STATE_PATH = "collect_state.json"   # 連続失敗回数と、次に試してよい時刻
LOG_PATH = "login_log.csv"          # 診断用。Myfxbookを叩いた記録
LOG_KEEP = 300                      # ログはこの行数だけ保持する
LOG_FIELDS = ["attempted_at", "result", "message", "fail_count", "forced"]
BACKOFF_MIN = {1: 30, 2: 120, 3: 360}   # 連続失敗回数 -> 次に試すまでの分
BACKOFF_MAX_MIN = 360                   # 4回目以降の上限（6時間）

UA = {"User-Agent": "gold-sentiment/1.0"}
BROWSER = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0.0.0 Safari/537.36")}

# スポット価格の優先順位。上から順に試して最初に成功したものを採用する
SPOT_SOURCES = {
    "XAUUSD": [("xaus", "XAU"), ("yahoo", "XAUUSD=X"), ("stooq", "xauusd")],
    "XAGUSD": [("xaus", "XAG"), ("yahoo", "XAGUSD=X"), ("stooq", "xagusd")],
}

# 先物価格（参考値・乖離の実測用）
FUT_SOURCES = {
    "XAUUSD": [("yahoo", "GC=F")],
    "XAGUSD": [("yahoo", "SI=F")],
}

FIELDS = [
    "snapshot_time", "instrument", "price", "price_fut", "price_src",
    "long_pct", "short_pct", "net_pct", "long_ratio",
    "long_vol", "short_vol", "net_vol_pct",
    "long_positions", "short_positions",
    "avg_long_price", "avg_short_price",
    "fetched_at",
]


class ApiRejected(RuntimeError):
    """APIが error=true を返した（＝Myfxbookに拒否された）"""

    def __init__(self, path, message):
        self.path = path
        self.message = message
        super().__init__(f"{path} failed: {message}")


class ApiUnreachable(RuntimeError):
    """通信そのものが成立しなかった（接続不能・5xx・JSONでない応答）"""

    def __init__(self, path, last):
        self.path = path
        self.last = last
        super().__init__(f"{path} unreachable: {last}")


def api(path, params, attempts=3):
    """通信エラー・5xxのみリトライ。API側のerror=trueは即中断（無駄撃ち防止）。"""
    last = None
    for i in range(1, attempts + 1):
        try:
            r = requests.get(f"{BASE}/{path}", params=params, headers=UA, timeout=20)
            r.raise_for_status()
            d = r.json()
        except Exception as e:  # JSONでない応答もここで捕まる
            last = e
            print(f"[retry {i}/{attempts}] {path}: {e}")
            time.sleep(5 * i)
            continue
        if str(d.get("error")).lower() == "true":
            raise ApiRejected(path, d.get("message"))
        return d
    raise ApiUnreachable(path, last)


def login():
    raw = api("login.json", {"email": EMAIL, "password": PASSWORD})["session"]
    # Myfxbookはsessionをエンコード済みの文字列で返す。
    # そのまま渡すとrequestsが % を %25 に二重エンコードし "Invalid session." になる
    session = unquote(raw)
    print("login ok")
    return session


def logout(session):
    try:
        api("logout.json", {"session": session}, attempts=1)
    except Exception as e:
        print("[warn] logout skipped:", e)


def outlook(session):
    return api("get-community-outlook.json", {"session": session}).get("symbols") or []


# ---------- バックオフの状態管理 ----------

def parse_iso(s):
    """ISO8601文字列をaware datetimeにする。読めなければNone"""
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def write_state(fail_count, next_try_at):
    """内容が同じなら毎回同じバイト列になるように書く（無駄なコミットを避ける）"""
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"fail_count": fail_count, "next_try_at": next_try_at},
                  f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def read_state():
    """読めないときは必ず「試してよい」を返す。状態ファイルの不具合で採取を止めない"""
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        fc = int(d.get("fail_count") or 0)
        return {"fail_count": max(fc, 0), "next_try_at": d.get("next_try_at") or None}
    except Exception as e:
        print(f"[warn] state unreadable -> treat as ok to try: {e}")
        return {"fail_count": 0, "next_try_at": None}


def ensure_files():
    """初回実行で確実に作る。存在しないファイルを git add すると失敗するため"""
    if not os.path.exists(STATE_PATH):
        write_state(0, None)
        print(f"[info] created {STATE_PATH}")
    if not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) == 0:
        with open(LOG_PATH, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=LOG_FIELDS).writeheader()
        print(f"[info] created {LOG_PATH}")


def append_log(now, result, message, fail_count, forced):
    """Myfxbookを叩いた記録を1行足し、直近LOG_KEEP行だけ残す"""
    rows = []
    try:
        with open(LOG_PATH, newline="", encoding="utf-8") as f:
            rows = [{k: r.get(k, "") for k in LOG_FIELDS} for r in csv.DictReader(f)]
    except Exception as e:
        print(f"[warn] log unreadable -> recreating: {e}")
        rows = []

    rows.append({"attempted_at": now.isoformat(timespec="seconds"),
                 "result": result,
                 "message": str(message or "")[:300],
                 "fail_count": fail_count,
                 "forced": "1" if forced else "0"})
    rows = rows[-LOG_KEEP:]

    with open(LOG_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        w.writeheader()
        w.writerows(rows)


def finish_failure(state, now, forced, result, message):
    """バケットを埋められずに終わった。原因を問わず連続失敗回数を進める"""
    fc = state["fail_count"] + 1
    wait = BACKOFF_MIN.get(fc, BACKOFF_MAX_MIN)
    nxt = (now + timedelta(minutes=wait)).isoformat(timespec="seconds")
    write_state(fc, nxt)
    append_log(now, result, message, fc, forced)
    print(f"[fail] {result}: {message}")
    print(f"[backoff] consecutive failures {fc} -> next attempt after {nxt} (+{wait}min)")


def finish_success(state, now, forced):
    write_state(0, None)
    append_log(now, "ok", "", 0, forced)
    if state["fail_count"]:
        print(f"[recover] failure counter {state['fail_count']} -> 0")


def forced_run():
    """collect.yml の workflow_dispatch 入力から渡る。外部スケジューラは送らないのでfalse"""
    return os.environ.get("FORCE_LOGIN", "").strip().lower() in ("true", "1", "yes", "on")


# ---------- 価格取得 ----------

_xaus_cache = {}


def _age_sec(iso):
    """ISO8601文字列の経過秒。判定不能ならNone"""
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds()
    except Exception:
        return None


def _xaus_payload():
    """1回の実行で1度だけ取得してキャッシュする"""
    if "d" not in _xaus_cache:
        r = requests.get("https://xaus.com/api/v1/spot",
                         params={"compact": "1", "fresh": str(int(time.time()))},
                         headers=UA, timeout=15)
        r.raise_for_status()
        _xaus_cache["d"] = r.json()
    return _xaus_cache["d"]


def price_xaus(code):
    d = _xaus_payload()
    state = (d.get("data_state") or {}).get("status")
    if state == "unavailable":
        raise RuntimeError("upstream unavailable")
    field = {"XAU": "spot_usd_oz", "XAG": "silver_usd_oz"}.get(code)
    v = d.get(field)
    if v is None:
        raise RuntimeError(f"{field} missing")
    age = _age_sec(d.get("price_as_of") or d.get("updated_at"))
    if age is not None and age > MAX_PRICE_AGE_SEC:
        raise RuntimeError(f"stale {int(age)}s")
    if d.get("stale"):
        print(f"[warn] xaus stale flag set (age {age})")
    return float(v)


def price_stooq(code):
    # 為替・貴金属は出来高を持たないため f に v を含めない（含めると404）
    r = requests.get("https://stooq.com/q/l/",
                     params={"s": code, "f": "sd2t2ohlc", "h": "", "e": "csv"},
                     headers=BROWSER, timeout=15)
    r.raise_for_status()
    return float(next(csv.DictReader(r.text.splitlines()))["Close"])


def price_yahoo(code):
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{code}",
                     params={"interval": "1d", "range": "1d"},
                     headers=BROWSER, timeout=15)
    r.raise_for_status()
    return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])


FETCHERS = {"xaus": price_xaus, "stooq": price_stooq, "yahoo": price_yahoo}


def get_price(inst, table):
    """価格は補助情報。全ソース失敗しても空欄で返し、採取自体は止めない。
    戻り値は (価格, 使ったソース名)"""
    for kind, code in table.get(inst, []):
        try:
            v = FETCHERS[kind](code)
            print(f"price {inst} <- {kind}:{code} = {v}")
            return round(v, 3), f"{kind}:{code}"
        except Exception as e:
            print(f"[warn] price {kind}:{code}: {e}")
    return "", ""


# ---------- CSV ----------

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

    if header and header != FIELDS:  # 列構成を変えた場合は自動で詰め替え
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
        if not header:  # ヘッダ二重書き込みを防ぐ
            w.writeheader()
        w.writerows(fresh)
    return len(fresh)


def collect(state, now, stamp, filled, forced):
    """Myfxbookを叩いて1バケット分を埋める。埋められなければ終了コード1で抜ける"""
    try:
        session = login()
    except ApiRejected as e:
        finish_failure(state, now, forced, "login_rejected", e.message)
        sys.exit(1)
    except ApiUnreachable as e:
        finish_failure(state, now, forced, "login_unreachable", e.last)
        sys.exit(1)

    try:
        try:
            symbols = outlook(session)
        finally:
            logout(session)
    except ApiRejected as e:
        finish_failure(state, now, forced, "outlook_rejected", e.message)
        sys.exit(1)
    except ApiUnreachable as e:
        finish_failure(state, now, forced, "outlook_unreachable", e.last)
        sys.exit(1)

    found = {s.get("name"): s for s in symbols}
    print(f"symbols fetched: {len(found)}")

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

        tot = lo + sh
        vtot = (lv or 0) + (sv or 0)
        net_vol = (round((lv - sv) / vtot * 100, 2)
                   if (lv is not None and sv is not None and vtot) else "")

        spot, src = get_price(inst, SPOT_SOURCES)
        fut, _ = get_price(inst, FUT_SOURCES)
        if spot != "" and fut != "":
            print(f"basis {inst}: fut-spot = {round(fut - spot, 2)}")

        rows.append({"snapshot_time": stamp,
                     "instrument": inst,
                     "price": spot,
                     "price_fut": fut,
                     "price_src": src,
                     "long_pct": lo,
                     "short_pct": sh,
                     "net_pct": round(lo - sh, 2),
                     "long_ratio": round(lo / tot * 100, 2) if tot else "",
                     "long_vol": lv if lv is not None else "",
                     "short_vol": sv if sv is not None else "",
                     "net_vol_pct": net_vol,
                     "long_positions": x.get("longPositions", ""),
                     "short_positions": x.get("shortPositions", ""),
                     "avg_long_price": x.get("avgLongPrice", ""),
                     "avg_short_price": x.get("avgShortPrice", ""),
                     "fetched_at": now.isoformat(timespec="seconds"),
                     })
        print(f"{inst}: L{lo}/S{sh} netVol {net_vol} "
              f"avgL {x.get('avgLongPrice')} avgS {x.get('avgShortPrice')}")

    print(f"added {save(rows)} rows")

    # 応答は返ったが行を作れなかった銘柄があれば、それも「バケットを埋められなかった」扱い。
    # ここを緑で通すと、スキップ機構が効かないまま叩き続ける輪に戻る
    got = {r["instrument"] for r in rows}
    missing = [i for i in SYMBOLS if i not in got and (i, stamp) not in filled]
    if missing:
        finish_failure(state, now, forced, "no_symbol", "missing: " + ", ".join(missing))
        sys.exit(1)

    finish_success(state, now, forced)


def main():
    ensure_files()

    now = datetime.now(timezone.utc)
    stamp = now.replace(minute=0, second=0,
                        microsecond=0).isoformat(timespec="seconds")

    # このバケットが全銘柄そろっていれば、Myfxbookを呼ばずに終了する。
    # get-community-outlook は無料枠で24時間100リクエスト。
    # これにより消費は起動回数ではなくバケット数（1日最大24回）に固定される。
    _, existing = read_existing()
    filled = {(r.get("instrument"), r.get("snapshot_time")) for r in existing}
    if all((inst, stamp) in filled for inst in SYMBOLS):
        print(f"[skip] bucket {stamp} already filled")
        return

    # 前回までに連続失敗していれば、待ち時間が明けるまでMyfxbookを呼ばない。
    # 手動起動で FORCE_LOGIN=true を渡したときだけ、この待ちを飛ばす
    state = read_state()
    forced = forced_run()
    nxt = parse_iso(state["next_try_at"]) if state["next_try_at"] else None
    if nxt and now < nxt:
        if forced:
            print(f"[force] backoff until {state['next_try_at']} bypassed by manual run")
        else:
            wait = int((nxt - now).total_seconds() // 60)
            print(f"[backoff] waiting until {state['next_try_at']} "
                  f"({wait}min left, consecutive failures {state['fail_count']}) "
                  f"-> skip without calling Myfxbook")
            return

    try:
        collect(state, now, stamp, filled, forced)
    except Exception as e:  # 想定外の例外でも輪に戻らないよう、必ず間隔を空ける
        finish_failure(state, now, forced, "error", f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
