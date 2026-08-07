import csv, json, os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

CSV_PATH = "positionbook.csv"
OUT_DIR = "docs"
MAX_POINTS = 336        # 毎時1点で約14日分
JST = timezone(timedelta(hours=9))

NET_MIN_SPAN = 6.0      # Net軸の最低表示幅（%）
NET_PAD_RATIO = 0.15    # データ範囲に対する上下の余白比

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
.wrap{position:relative;height:54vh;margin-bottom:26px}
b{color:#fff}
</style></head><body>
<div class="meta">Net＝(ロング数量−ショート数量)÷合計×100。プラスは買い持ち優勢。<br>
破線はリテールの平均建値（＝ストップが溜まりやすい帯）。時刻はJST。</div>
"""


def jst(iso, fmt="%m/%d %H:%M"):
    try:
        return datetime.fromisoformat(iso).astimezone(JST).strftime(fmt)
    except Exception:
        return iso


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt(v, nd=1):
    n = fnum(v)
    return f"{n:.{nd}f}" if n is not None else "-"


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
for inst, rs in by_inst.items():
    rs = rs[-MAX_POINTS:]
    last = rs[-1]
    labels = [jst(r.get("snapshot_time", "")) for r in rs]
    net = [
        fnum(r.get("net_vol_pct"))
        if fnum(r.get("net_vol_pct")) is not None
        else fnum(r.get("net_pct"))
        for r in rs
    ]
    price = [fnum(r.get("price")) for r in rs]
    avgl = [fnum(r.get("avg_long_price")) for r in rs]
    avgs = [fnum(r.get("avg_short_price")) for r in rs]

    ymin, ymax = net_range(net)
    pr = 3 if len(rs) < 8 else 0   # 点が少ないうちはマーカーを出す

    html += f"""<h2>{inst}</h2>
<div class="val">{jst(last.get("snapshot_time",""))} JST ／ Net <b>{fmt(net[-1], 2)}</b>％ ／ 価格 <b>{fmt(last.get("price"), 1)}</b><br>
ロング {fmt(last.get("long_pct"), 1)}％・ショート {fmt(last.get("short_pct"), 1)}％ ／ 平均建値 L {fmt(last.get("avg_long_price"), 1)}・S {fmt(last.get("avg_short_price"), 1)}</div>
<div class="wrap"><canvas id="c_{inst}"></canvas></div>
<script>
new Chart(document.getElementById("c_{inst}"), {{
type:"line",
data:{{ labels:{json.dumps(labels, ensure_ascii=False)}, datasets:[
{{label:"Net (L-S) %", data:{json.dumps(net)}, yAxisID:"y",
borderColor:"#4ea1ff", borderWidth:2, pointRadius:{pr}, tension:.2, spanGaps:true}},
{{label:"Price", data:{json.dumps(price)}, yAxisID:"y1",
borderColor:"#ffb74d", borderWidth:1.6, pointRadius:{pr}, tension:.2, spanGaps:true}},
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
