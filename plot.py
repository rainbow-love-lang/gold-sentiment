import csv, json, os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

CSV_PATH = "positionbook.csv"
OUT_DIR = "docs"
MAX_POINTS = 336        # 畳んだ後の点数の上限
JST = timezone(timedelta(hours=9))

NET_MIN_SPAN = 6.0      # Net軸の最低表示幅（%）
NET_PAD_RATIO = 0.15    # データ範囲に対する上下の余白比
COLLAPSE_MIN = 3        # 同値がこの行数以上続いたら畳む／市場停止と見なす

LOOKBACK = 3            # 何点前と比較するか
VOL_THR = 0.02          # 建玉量が±2%動いたら「増減あり」
PRICE_THR = 0.0015      # 価格が±0.15%動いたら「上下あり」（3本とも共通）
AVG_THR = 0.0003        # 平均建値が±0.03%動いたら「動きあり」
NEAR_DIST = 30.0        # 建値帯への接近と見なす距離（ドル）
MIN_N_FOR_RATE = 20     # 母数がこれ未満のときは％を出さない

# 検証ホライズン。採取が毎時なので 1行＝1時間
# チャートで一般に使われる時間軸に合わせている
HORIZONS = ((1, "1時間"), (4, "4時間"), (24, "1日"))

# 無変化の判定に使う列。価格は含めない
# （相場が止まっていてもスポットAPIが微差を返しうるため）
STILL_KEYS = ("net_vol_pct", "avg_long_price", "avg_short_price")

HEAD = """<!doctype html><html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>リテール建玉センチメント</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
body{background:#111;color:#eee;font-family:system-ui,sans-serif;margin:0;padding:12px}
h2{font-size:17px;margin:18px 0 2px}
.meta{font-size:12px;color:#999;margin-bottom:10px;line-height:1.6}
.val{font-size:13px;color:#ccc;margin:0 0 8px;line-height:1.7}
.card{background:#1a1a1a;border-left:3px solid #4ea1ff;padding:10px 12px;
margin:8px 0 10px;border-radius:4px}
.label{font-size:16px;font-weight:bold;color:#fff}
.tags{font-size:12px;color:#ffb74d;margin-top:4px;line-height:1.6}
.note{font-size:12px;color:#999;margin-top:6px;line-height:1.6}
.diag{font-size:11px;color:#6d6d6d;margin-top:8px;padding-top:7px;
border-top:1px solid #2a2a2a;line-height:1.6}
.wrap{position:relative;height:54vh;margin-bottom:26px}
b{color:#fff}
</style></head><body>
<div class="meta">Net＝(ロング数量−ショート数量)÷合計×100。プラスは買い持ち優勢。<br>
破線はリテールの平均建値（＝ストップが溜まりやすい帯）。価格はスポット基準。時刻はJST。<br>
建玉が動かない時間帯（週末など）は圧縮表示。ラベルの「〜」は時間が飛んでいる箇所。<br>
判定は「リテールと価格は逆相関する」という仮説に基づく検証用の表示です。</div>
"""


def jst(iso, fmt="%m/%d %H:%M"):
    try:
        return datetime.fromisoformat(iso).astimezone(JST).strftime(fmt)
    except Exception:
        return iso


def ts(r):
    """snapshot_time を datetime に。失敗はNone"""
    try:
        return datetime.fromisoformat(r.get("snapshot_time", ""))
    except Exception:
        return None


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt(v, nd=1):
    n = fnum(v)
    return f"{n:.{nd}f}" if n is not None else "-"


def pct(r, nd=1):
    """相対変化率を符号つき％で表示"""
    return f"{r * 100:+.{nd}f}%" if r is not None else "-"


def still_key(r):
    return tuple((r.get(k) or "").strip() for k in STILL_KEYS)


def frozen_flags(rs):
    """建玉が動かない連続ブロック（週末・市場停止）に含まれる行にTrueを立てる。
    ブロック先頭は「動いていた最後の値」なので生かし、以降の重複行だけを停止扱いにする"""
    flags = [False] * len(rs)
    i = 0
    n = len(rs)
    while i < n:
        j = i + 1
        key = still_key(rs[i])
        while j < n and still_key(rs[j]) == key:
            j += 1
        if j - i >= COLLAPSE_MIN:
            for k in range(i + 1, j):
                flags[k] = True
        i = j
    return flags


def collapse(rs):
    """建玉が動かない連続ブロックを最初と最後の2点に畳む"""
    out = []
    i = 0
    n = len(rs)
    while i < n:
        j = i + 1
        key = still_key(rs[i])
        while j < n and still_key(rs[j]) == key:
            j += 1
        block = rs[i:j]
        if len(block) >= COLLAPSE_MIN:
            head = dict(block[0])
            head["_gap"] = len(block) - 2
            out.append(head)
            out.append(dict(block[-1]))
        else:
            out.extend(dict(b) for b in block)
        i = j
    return out


def split_price(r):
    """(スポット, 先物) を返す。
    price_src が空の行は旧仕様＝price に先物が入っているため先物側へ回す"""
    src = (r.get("price_src") or "").strip()
    p = fnum(r.get("price"))
    f = fnum(r.get("price_fut"))
    if src:
        return p, f
    return None, (f if f is not None else p)


def ref_price(r):
    """判定に使う価格。スポット優先、無ければ先物で代用"""
    s, f = split_price(r)
    return s if s is not None else f


def last_valid(seq):
    for v in reversed(seq):
        if v is not None:
            return v
    return None


# ---------- 判定 ----------

def rel(cur, prev):
    """相対変化率を返す。判定不能はNone"""
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev)


def dir3(cur, prev, thr):
    """相対変化から +1 / 0 / -1 を返す。判定不能はNone"""
    r = rel(cur, prev)
    if r is None:
        return None
    if r > thr:
        return 1
    if r < -thr:
        return -1
    return 0


# (Δロング量, Δショート量) -> (ラベル, 想定方向, 説明)
VERDICT = {
    (1, 0):   ("売り目線", "sell", "買いが溜まっている＝下落の燃料"),
    (1, -1):  ("売り目線（両側）", "sell", "買い増加かつ売り減少。偏りが加速"),
    (0, 1):   ("買い目線", "buy", "売りが溜まっている＝上昇の燃料"),
    (-1, 1):  ("買い目線（両側）", "buy", "売り増加かつ買い減少。偏りが加速"),
    (-1, 0):  ("燃料消費・下落側", None, "ロングが投げている最中"),
    (0, -1):  ("燃料消費・上昇側", None, "ショートが焼かれている最中"),
    (1, 1):   ("対立激化", None, "総建玉が膨張。どちらかが必ず焼かれる"),
    (-1, -1): ("手仕舞い", None, "両者が撤退。材料に乏しい"),
    (0, 0):   ("動意なし", None, "建玉に目立った動きなし"),
}


def flow_tag(side, d_avg, d_vol, price, avg):
    """平均建値の動きだけでは新規か決済か決まらないため、
    建玉量の増減と「現値が平均建値のどちら側にあるか」を併用して切り分ける。
    新規注文は現値で入るので、平均は必ず現値の側へ引かれる。"""
    if None in (d_avg, d_vol, price, avg) or d_avg == 0 or price == avg:
        return None
    toward = (d_avg > 0) == (price > avg)   # 平均が現値へ近づいたか
    if toward and d_vol > 0:
        return f"新規流入{side}"
    if toward and d_vol < 0:
        return f"{side}深い建玉が撤退"
    if (not toward) and d_vol < 0:
        return f"{side}浅い建玉が撤退"
    if (not toward) and d_vol > 0:
        return f"{side}建値が現値と逆行"
    return None   # 建玉量が横ばいのときは判断を保留


def judge(rows, i):
    """rows[i] 時点の判定。判定不能ならNone"""
    if i < LOOKBACK:
        return None
    cur, prev = rows[i], rows[i - LOOKBACK]

    rl = rel(fnum(cur.get("long_vol")), fnum(prev.get("long_vol")))
    rs_ = rel(fnum(cur.get("short_vol")), fnum(prev.get("short_vol")))
    if rl is None or rs_ is None:
        return None

    def to_dir(r):
        return 1 if r > VOL_THR else (-1 if r < -VOL_THR else 0)

    dl, ds = to_dir(rl), to_dir(rs_)
    label, bias, desc = VERDICT[(dl, ds)]
    tags = []

    p = ref_price(cur)
    al, as_ = fnum(cur.get("avg_long_price")), fnum(cur.get("avg_short_price"))

    # 新規参入か手仕舞いかの切り分け
    for key, side, avg, dv in (
        ("avg_long_price", "L", al, dl),
        ("avg_short_price", "S", as_, ds),
    ):
        d_avg = dir3(fnum(cur.get(key)), fnum(prev.get(key)), AVG_THR)
        t = flow_tag(side, d_avg, dv, p, avg)
        if t:
            tags.append(t)

    # 建値帯への接近と突破
    if p is not None:
        dists = [abs(p - a) for a in (al, as_) if a is not None]
        if dists and min(dists) <= NEAR_DIST:
            tags.append("建値帯に接近")
        pp = ref_price(rows[i - 1]) if i >= 1 else None
        pal = fnum(rows[i - 1].get("avg_long_price")) if i >= 1 else None
        pas = fnum(rows[i - 1].get("avg_short_price")) if i >= 1 else None
        for a, b, nm in ((al, pal, "L"), (as_, pas, "S")):
            if None not in (a, b, p, pp) and (p - a) * (pp - b) < 0:
                tags.append(f"建値帯を突破{nm}")

    # 偏り極大：これまでの記録の最大／最小圏
    nets = [fnum(r.get("net_vol_pct")) for r in rows[: i + 1]]
    nets = [v for v in nets if v is not None]
    n = fnum(cur.get("net_vol_pct"))
    if n is not None and len(nets) >= 10:
        if n >= max(nets):
            tags.append("偏り極大（買い側）")
        elif n <= min(nets):
            tags.append("偏り極大（売り側）")

    return {"label": label, "bias": bias, "desc": desc, "tags": tags,
            "d_long": rl, "d_short": rs_}


def window_ok(i, k, h, times, frozen):
    """判定時点iからk（=i+h）までの窓が検証に使えるかを見る。
    ・欠測で行が飛んでいれば経過時間がh時間からずれる
    ・週末など市場停止をまたぐ窓は、止まった価格と比較することになる"""
    ti, tk = times[i], times[k]
    if ti is None or tk is None:
        return False
    if abs((tk - ti).total_seconds() - h * 3600) > 60:
        return False
    return not any(frozen[x] for x in range(i, k + 1))


def verify(rows, judges, h, times, frozen):
    """判定のh時間後の値動きと突き合わせる。
    (的中リスト, 除外の内訳) を返す"""
    out = []
    st = {"bias": 0, "pending": 0, "gap": 0, "flat": 0}
    for i, j in enumerate(judges):
        if not j:
            continue
        if not j["bias"]:
            st["bias"] += 1
            continue
        k = i + h
        if k >= len(rows):
            st["pending"] += 1      # まだ将来が確定していない
            continue
        if not window_ok(i, k, h, times, frozen):
            st["gap"] += 1
            continue
        d = dir3(ref_price(rows[k]), ref_price(rows[i]), PRICE_THR)
        if not d:                   # 価格が動いていない／判定不能
            st["flat"] += 1
            continue
        out.append((d > 0) == (j["bias"] == "buy"))
    return out, st


def rate(hits):
    """母数が小さいうちは％を出さない。偶然と区別がつかないため"""
    n = len(hits)
    if n == 0:
        return "集計中（n=0）"
    ok = sum(1 for h in hits if h)
    if n < MIN_N_FOR_RATE:
        return f"集計中（{ok}/{n}）"
    return f"{ok}/{n}（{ok / n * 100:.0f}%）"


def vol_stats(rows, key):
    """|Δ建玉| の分布。閾値VOL_THRが妥当かを見るための材料。
    週末など完全に動かない点は除外する"""
    vals = []
    for i in range(LOOKBACK, len(rows)):
        r = rel(fnum(rows[i].get(key)), fnum(rows[i - LOOKBACK].get(key)))
        if r is not None and r != 0:
            vals.append(abs(r))
    if not vals:
        return None
    vals.sort()

    def q(p):
        return vals[min(len(vals) - 1, int(len(vals) * p))]

    return {"n": len(vals), "med": q(0.5), "p75": q(0.75),
            "over": sum(1 for v in vals if v > VOL_THR)}


def stat_line(vs, side):
    if not vs:
        return f"{side} －"
    return (f"{side} 中央値{vs['med'] * 100:.2f}%・上位25%{vs['p75'] * 100:.2f}%・"
            f"閾値超え{vs['over']}/{vs['n']}点")


def net_range(values):
    """Netの表示範囲を決める。必ず0を含め、最低幅を確保する。"""
    vals = [v for v in values if v is not None]
    if not vals:
        return -50.0, 50.0
    lo = min(vals + [0.0])
    hi = max(vals + [0.0])
    pad = max((hi - lo) * NET_PAD_RATIO, NET_MIN_SPAN / 2)
    return max(-100.0, round(lo - pad, 1)), min(100.0, round(hi + pad, 1))


def save(html):
    os.makedirs(OUT_DIR, exist_ok=True)
    open(os.path.join(OUT_DIR, ".nojekyll"), "w").close()
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print("wrote", os.path.join(OUT_DIR, "index.html"))


def load():
    if not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0:
        return []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


rows = load()
if not rows:  # 採取前でもPagesが404にならないように
    save(HEAD + "<p>まだデータがありません。</p></body></html>")
    raise SystemExit(0)

rows.sort(key=lambda r: r.get("snapshot_time", ""))
by_inst = defaultdict(list)
for r in rows:
    by_inst[r.get("instrument", "?")].append(r)

html = HEAD
for inst, all_rows in by_inst.items():
    # 判定は全行で行う（検証の母数を減らさないため）
    judges = [judge(all_rows, i) for i in range(len(all_rows))]
    cur = next((j for j in reversed(judges) if j), None)

    times = [ts(r) for r in all_rows]
    frozen = frozen_flags(all_rows)

    res = {}
    for h, name in HORIZONS:
        hits, st = verify(all_rows, judges, h, times, frozen)
        res[h] = {"hits": hits, "st": st}

    n_judged = sum(1 for j in judges if j)
    n_bias = sum(1 for j in judges if j and j["bias"])
    n_frozen = sum(1 for f in frozen if f)
    vsl = vol_stats(all_rows, "long_vol")
    vss = vol_stats(all_rows, "short_vol")

    # グラフは圧縮後の点を使う
    rs = collapse(all_rows)[-MAX_POINTS:]
    print(f"{inst}: {len(all_rows)} rows -> {len(rs)} points, "
          f"judged {n_judged}, bias {n_bias}, frozen {n_frozen}")
    for h, name in HORIZONS:
        s = res[h]["st"]
        print(f"  {name}: verified {len(res[h]['hits'])}, "
              f"pending {s['pending']}, gap {s['gap']}, flat {s['flat']}")

    last = rs[-1]
    labels = [
        jst(r.get("snapshot_time", "")) + (" 〜" if r.get("_gap") else "")
        for r in rs
    ]
    net = [
        fnum(r.get("net_vol_pct"))
        if fnum(r.get("net_vol_pct")) is not None
        else fnum(r.get("net_pct"))
        for r in rs
    ]
    pairs = [split_price(r) for r in rs]
    spot = [p[0] for p in pairs]
    fut = [p[1] for p in pairs]
    avgl = [fnum(r.get("avg_long_price")) for r in rs]
    avgs = [fnum(r.get("avg_short_price")) for r in rs]

    ymin, ymax = net_range(net)
    pr = 3 if len(rs) < 8 else 0   # 点が少ないうちはマーカーを出す

    ls, lf = last_valid(spot), last_valid(fut)
    basis = f"／ 乖離 <b>{lf - ls:+.1f}</b>" if (ls is not None and lf is not None) else ""

    # 含み損益（ドル）
    la, sa = last_valid(avgl), last_valid(avgs)
    pl = f"ロング勢 <b>{ls - la:+.1f}</b>" if (ls is not None and la is not None) else ""
    ps = f"ショート勢 <b>{sa - ls:+.1f}</b>" if (ls is not None and sa is not None) else ""

    hz_txt = " ／ ".join(f"{name} {rate(res[h]['hits'])}" for h, name in HORIZONS)
    diag_hz = "<br>".join(
        f"　{name}後：検証<b>{len(res[h]['hits'])}</b>点"
        f"（未確定{res[h]['st']['pending']}"
        f"／窓の欠落{res[h]['st']['gap']}"
        f"／価格±{PRICE_THR * 100:.2f}%未満{res[h]['st']['flat']}）"
        for h, name in HORIZONS
    )

    if cur:
        card = f"""<div class="card">
<div class="label">【{cur['label']}】</div>
<div class="tags">{'　'.join('［' + t + '］' for t in cur['tags']) or '－'}</div>
<div class="note">{cur['desc']}<br>
Δ建玉（{LOOKBACK}点前比・閾値±{VOL_THR * 100:.0f}%）：ロング <b>{pct(cur['d_long'], 2)}</b>　ショート <b>{pct(cur['d_short'], 2)}</b><br>
{pl}　{ps}<br>
仮説の一致率：{hz_txt}</div>
<div class="diag">検証の内訳：判定{n_judged}点（うち市場停止{n_frozen}点） → 方向あり{n_bias}点<br>
{diag_hz}<br>
|Δ建玉|の分布（動きのあった点のみ）：{stat_line(vsl, 'L')} ／ {stat_line(vss, 'S')}</div>
</div>"""
    else:
        card = '<div class="card"><div class="note">判定に必要なデータが不足しています。</div></div>'

    html += f"""<h2>{inst}</h2>
<div class="val">{jst(last.get("snapshot_time",""))} JST ／ Net <b>{fmt(net[-1], 2)}</b>％ ／ スポット <b>{fmt(ls, 1)}</b> ／ 先物 {fmt(lf, 1)} {basis}<br>
ロング {fmt(last.get("long_pct"), 1)}％・ショート {fmt(last.get("short_pct"), 1)}％ ／ 平均建値 L {fmt(last.get("avg_long_price"), 1)}・S {fmt(last.get("avg_short_price"), 1)}</div>
{card}
<div class="wrap"><canvas id="c_{inst}"></canvas></div>
<script>
new Chart(document.getElementById("c_{inst}"), {{
type:"line",
data:{{ labels:{json.dumps(labels, ensure_ascii=False)}, datasets:[
{{label:"Net (L-S) %", data:{json.dumps(net)}, yAxisID:"y",
borderColor:"#4ea1ff", borderWidth:2, pointRadius:{pr}, tension:.2, spanGaps:true}},
{{label:"スポット", data:{json.dumps(spot)}, yAxisID:"y1",
borderColor:"#ffb74d", borderWidth:1.8, pointRadius:{pr}, tension:.2, spanGaps:true}},
{{label:"先物 GC=F", data:{json.dumps(fut)}, yAxisID:"y1",
borderColor:"#a1793c", borderWidth:1.2, borderDash:[2,3], pointRadius:0,
tension:.2, spanGaps:true}},
{{label:"平均L建値", data:{json.dumps(avgl)}, yAxisID:"y1",
borderColor:"#66bb6a", borderWidth:1, borderDash:[4,3], pointRadius:0, spanGaps:true}},
{{label:"平均S建値", data:{json.dumps(avgs)}, yAxisID:"y1",
borderColor:"#ef5350", borderWidth:1, borderDash:[4,3], pointRadius:0, spanGaps:true}}]}},
options:{{ responsive:true, maintainAspectRatio:false,
interaction:{{mode:"index",intersect:false}},
scales:{{
x:{{ticks:{{color:"#888",maxTicksLimit:6,maxRotation:0}},grid:{{color:"#222"}}}},
y:{{position:"left",min:{ymin},max:{ymax},ticks:{{color:"#4ea1ff"}},
grid:{{color:c=>c.tick.value===0?"#666":"#222"}}}},
y1:{{position:"right",ticks:{{color:"#ffb74d"}},grid:{{drawOnChartArea:false}}}}
}},
plugins:{{legend:{{labels:{{color:"#ccc",boxWidth:12,font:{{size:11}}}}}}}}
}}
}});
</script>
"""

html += "</body></html>"
save(html)
