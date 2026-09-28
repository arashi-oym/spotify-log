#!/usr/bin/env python3
"""data/plays.csv を集計して、
- README.md（GitHubアプリ用のランキング）
- docs/index.html（スマホ向けの専用ページ。GitHub Pagesで公開）
を書き出します。
"""
import csv
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "plays.csv"
README = ROOT / "README.md"
DOCS = ROOT / "docs"


# ---------------- データ読み込み・集計 ----------------

def load():
    rows = []
    if not CSV_PATH.exists():
        return rows
    with CSV_PATH.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            dt = datetime.strptime(r["played_at"][:19], "%Y-%m-%dT%H:%M:%S")
            r["dt"] = dt.replace(tzinfo=timezone.utc).astimezone(JST)
            r["ms"] = int(r.get("duration_ms") or 0)
            r["image"] = r.get("image") or ""
            rows.append(r)
    return rows


def rank_songs(rows, n):
    counts, ms, info = Counter(), defaultdict(int), {}
    for r in rows:
        k = r["track_id"]
        counts[k] += 1
        ms[k] += r["ms"]
        if r["image"] or k not in info:
            info[k] = r
    ranked = sorted(counts, key=lambda k: (-counts[k], -ms[k]))[:n]
    return [{
        "t": info[k]["track"], "a": info[k]["artists"], "c": counts[k],
        "m": ms[k] // 60000, "img": info[k]["image"],
    } for k in ranked]


def rank_artists(rows, n):
    counts, ms = Counter(), defaultdict(int)
    track_counts = defaultdict(Counter)
    images = {}
    for r in rows:
        for a in r["artists"].split(" / "):
            if not a:
                continue
            counts[a] += 1
            ms[a] += r["ms"]
            track_counts[a][r["track_id"]] += 1
            if r["image"]:
                images[(a, r["track_id"])] = r["image"]
    ranked = sorted(counts, key=lambda a: (-counts[a], -ms[a]))[:n]
    out = []
    for a in ranked:
        img = ""
        for tid, _ in track_counts[a].most_common():
            if (a, tid) in images:
                img = images[(a, tid)]
                break
        out.append({"n": a, "c": counts[a], "m": ms[a] // 60000, "img": img})
    return out


def period(key, label, rows, n_songs=50, n_artists=30):
    return {
        "key": key, "label": label, "total": len(rows),
        "minutes": sum(r["ms"] for r in rows) // 60000,
        "songs": rank_songs(rows, n_songs),
        "artists": rank_artists(rows, n_artists),
    }


def monthly_top(rows):
    by_month = defaultdict(list)
    for r in rows:
        by_month[r["dt"].month].append(r)
    out = []
    for m in sorted(by_month):
        top = rank_songs(by_month[m], 1)[0]
        top.update({"month": f"{m}月", "total": len(by_month[m])})
        out.append(top)
    return out


# ---------------- README（GitHubアプリ用） ----------------

def esc_md(s):
    return (s or "").replace("|", "\\|")


def md_songs(items):
    if not items:
        return "記録なし"
    lines = ["| # | 曲 | アーティスト | 回数 | 分 |", "|---:|---|---|---:|---:|"]
    for i, s in enumerate(items, 1):
        lines.append(f"| {i} | {esc_md(s['t'])} | {esc_md(s['a'])} | {s['c']} | {s['m']} |")
    return "\n".join(lines)


def md_artists(items):
    if not items:
        return "記録なし"
    lines = ["| # | アーティスト | 回数 | 分 |", "|---:|---|---:|---:|"]
    for i, a in enumerate(items, 1):
        lines.append(f"| {i} | {esc_md(a['n'])} | {a['c']} | {a['m']} |")
    return "\n".join(lines)


def write_readme(data, page_url):
    out = ["# 🎧 Spotify 再生記録", ""]
    if page_url:
        out += [f"📱 **専用ページ：{page_url}**", ""]
    if not data:
        out.append("まだ記録がありません。")
        README.write_text("\n".join(out) + "\n", encoding="utf-8")
        return
    out += [f"最終記録：{data['updated']}（日本時間）", ""]
    limits = {"7d": (20, 10), "30d": (30, 20), "month": (20, 10), "year": (50, 20)}
    for p in data["periods"]:
        if p["key"] not in limits:
            continue
        ns, na = limits[p["key"]]
        out += [
            f"## {p['label']}（{p['total']:,}回再生・約{p['minutes']:,}分）", "",
            "### トップソング", "", md_songs(p["songs"][:ns]), "",
            "### トップアーティスト", "", md_artists(p["artists"][:na]), "",
        ]
    if data["monthly"]:
        out += [f"## {data['year']}年 月ごとの1位", "",
                "| 月 | 曲 | アーティスト | 回数 |", "|---:|---|---|---:|"]
        for m in data["monthly"]:
            out.append(f"| {m['month']} | {esc_md(m['t'])} | {esc_md(m['a'])} | {m['c']} |")
        out.append("")
    out.append("※「分」は曲の長さ×回数の目安です。")
    README.write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------------- 専用ページ（HTML） ----------------

HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Listening Log">
<meta name="robots" content="noindex">
<title>My Listening Log</title>
<style>
:root{
  --bg:#f5f3ef;--card:#ffffff;--text:#17171a;--sub:#6e6e76;--line:#e7e4de;
  --accent:#16a34a;--accent-soft:rgba(22,163,74,.14);--chip:#ebe8e2;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0e0e10;--card:#18181b;--text:#f4f4f5;--sub:#9d9da6;--line:#27272a;
  --accent:#1ed760;--accent-soft:rgba(30,215,96,.16);--chip:#232327;
  color-scheme:dark;
}}
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--bg)}
body{
  color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Hiragino Kaku Gothic ProN","Noto Sans JP",sans-serif;
  -webkit-font-smoothing:antialiased;line-height:1.4;
  padding:calc(env(safe-area-inset-top,0px) + 20px) 16px calc(env(safe-area-inset-bottom,0px) + 40px);
}
.wrap{max-width:640px;margin:0 auto}
header h1{font-size:28px;font-weight:800;letter-spacing:-.02em}
header p{color:var(--sub);font-size:13px;margin-top:4px}
.tabs{display:flex;gap:8px;overflow-x:auto;margin:20px -16px 0;padding:0 16px 4px;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tab{flex:0 0 auto;border:0;background:var(--chip);color:var(--text);font:inherit;font-size:14px;font-weight:600;
  padding:8px 16px;border-radius:999px;cursor:pointer}
.tab.on{background:var(--text);color:var(--bg)}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:16px}
.stat{background:var(--card);border-radius:16px;padding:14px 16px}
.stat b{display:block;font-size:26px;font-weight:800;letter-spacing:-.02em}
.stat span{color:var(--sub);font-size:12px}
.hero{display:flex;gap:16px;align-items:center;background:var(--card);border-radius:20px;padding:16px;margin-top:10px}
.hero .art{width:104px;height:104px;border-radius:12px;font-size:36px}
.hero .no1{color:var(--accent);font-size:12px;font-weight:800;letter-spacing:.08em}
.hero .name{font-size:20px;font-weight:800;margin-top:2px;word-break:break-word}
.hero .by{color:var(--sub);font-size:14px;margin-top:2px}
.hero .cnt{font-size:13px;margin-top:8px;font-weight:600}
.seg{display:grid;grid-template-columns:1fr 1fr;background:var(--chip);border-radius:12px;padding:3px;margin-top:20px}
.seg button{border:0;background:transparent;color:var(--sub);font:inherit;font-size:14px;font-weight:700;padding:9px;border-radius:9px;cursor:pointer}
.seg button.on{background:var(--card);color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.12)}
.list{margin-top:12px;background:var(--card);border-radius:20px;padding:6px 0;overflow:hidden}
.row{display:flex;align-items:center;gap:12px;padding:9px 14px;position:relative}
.row+.row{border-top:1px solid var(--line)}
.rank{width:24px;text-align:right;font-weight:800;font-size:15px;color:var(--sub);flex:0 0 auto}
.row:nth-child(-n+3) .rank{color:var(--accent)}
.art{width:46px;height:46px;border-radius:8px;flex:0 0 auto;object-fit:cover;background:var(--chip);
  display:flex;align-items:center;justify-content:center;font-weight:800;color:#fff;font-size:18px;overflow:hidden}
.art.round{border-radius:50%}
.meta{flex:1;min-width:0}
.meta .t{font-size:15px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta .a{font-size:13px;color:var(--sub);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar{height:3px;border-radius:2px;background:var(--accent-soft);margin-top:6px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--accent);border-radius:2px}
.num{text-align:right;flex:0 0 auto}
.num b{display:block;font-size:15px;font-weight:800}
.num span{font-size:11px;color:var(--sub)}
h2{font-size:18px;font-weight:800;margin-top:28px}
.empty{color:var(--sub);text-align:center;padding:40px 16px;font-size:14px}
footer{color:var(--sub);font-size:11px;text-align:center;margin-top:28px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>My Listening Log</h1>
    <p id="updated"></p>
  </header>
  <nav class="tabs" id="tabs"></nav>
  <main id="main"></main>
  <footer>「分」は曲の長さ×回数の目安です</footer>
</div>
<script type="application/json" id="data">__DATA__</script>
<script>
(function(){
  var D = JSON.parse(document.getElementById('data').textContent);
  var state = {p: 0, mode: 'songs'};
  function esc(s){return String(s == null ? '' : s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function hue(s){var h=0;for(var i=0;i<s.length;i++){h=(h*31+s.charCodeAt(i))%360;}return h;}
  function art(img, name, extra){
    var cls = 'art' + (extra ? ' ' + extra : '');
    var ph = '<div class="'+cls+'" style="background:hsl('+hue(name)+',55%,48%)">'+esc((name||'?').charAt(0))+'</div>';
    if(!img) return ph;
    return '<img class="'+cls+'" src="'+esc(img)+'" alt="" loading="lazy" onerror="this.outerHTML=this.dataset.ph" data-ph="'+esc(ph)+'">';
  }
  function fmt(n){return Number(n).toLocaleString('ja-JP');}

  if(!D.periods){
    document.getElementById('main').innerHTML='<p class="empty">まだ記録がありません</p>';
    return;
  }
  document.getElementById('updated').textContent = '最終記録 ' + D.updated;

  function renderTabs(){
    document.getElementById('tabs').innerHTML = D.periods.map(function(p,i){
      return '<button class="tab'+(i===state.p?' on':'')+'" data-i="'+i+'">'+esc(p.label)+'</button>';
    }).join('');
  }
  document.getElementById('tabs').addEventListener('click', function(e){
    var b = e.target.closest('.tab'); if(!b) return;
    state.p = +b.dataset.i; render();
  });
  document.getElementById('main').addEventListener('click', function(e){
    var b = e.target.closest('[data-mode]'); if(!b) return;
    state.mode = b.dataset.mode; render();
  });

  function render(){
    renderTabs();
    var p = D.periods[state.p];
    var h = '<div class="stats">'
      + '<div class="stat"><b>'+fmt(p.total)+'</b><span>回再生</span></div>'
      + '<div class="stat"><b>'+fmt(p.minutes)+'</b><span>分（目安）</span></div></div>';
    if(!p.total){
      document.getElementById('main').innerHTML = h + '<p class="empty">この期間の記録はまだありません</p>';
      return;
    }
    var isSong = state.mode === 'songs';
    var items = isSong ? p.songs : p.artists;
    var top = items[0];
    h += '<div class="hero">' + art(top.img, isSong ? top.t : top.n, isSong ? '' : 'round')
      + '<div class="meta"><div class="no1">NO.1 ' + (isSong ? 'SONG' : 'ARTIST') + '</div>'
      + '<div class="name">'+esc(isSong ? top.t : top.n)+'</div>'
      + (isSong ? '<div class="by">'+esc(top.a)+'</div>' : '')
      + '<div class="cnt">'+fmt(top.c)+'回 · '+fmt(top.m)+'分</div></div></div>';
    h += '<div class="seg"><button data-mode="songs" class="'+(isSong?'on':'')+'">曲</button>'
      + '<button data-mode="artists" class="'+(!isSong?'on':'')+'">アーティスト</button></div>';
    var max = top.c;
    h += '<div class="list">' + items.map(function(it,i){
      var name = isSong ? it.t : it.n;
      return '<div class="row"><div class="rank">'+(i+1)+'</div>'
        + art(it.img, name, isSong ? '' : 'round')
        + '<div class="meta"><div class="t">'+esc(name)+'</div>'
        + (isSong ? '<div class="a">'+esc(it.a)+'</div>' : '')
        + '<div class="bar"><i style="width:'+(it.c/max*100).toFixed(1)+'%"></i></div></div>'
        + '<div class="num"><b>'+fmt(it.c)+'</b><span>回</span></div></div>';
    }).join('') + '</div>';
    if(p.key === 'year' && D.monthly && D.monthly.length){
      h += '<h2>月ごとの1位</h2><div class="list">' + D.monthly.map(function(m){
        return '<div class="row"><div class="rank" style="width:36px">'+esc(m.month)+'</div>'
          + art(m.img, m.t, '')
          + '<div class="meta"><div class="t">'+esc(m.t)+'</div><div class="a">'+esc(m.a)+'</div></div>'
          + '<div class="num"><b>'+fmt(m.c)+'</b><span>回</span></div></div>';
      }).join('') + '</div>';
    }
    document.getElementById('main').innerHTML = h;
  }
  render();
})();
</script>
</body>
</html>
"""


def write_html(data):
    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    payload = json.dumps(data or {}, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    (DOCS / "index.html").write_text(HTML.replace("__DATA__", payload), encoding="utf-8")


# ---------------- メイン ----------------

def page_url():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return ""
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/"


def main():
    rows = load()
    data = None
    if rows:
        now = datetime.now(JST)
        latest = max(r["dt"] for r in rows)
        year, month = now.year, now.month
        this_year = [r for r in rows if r["dt"].year == year]
        data = {
            "updated": f"{latest:%Y-%m-%d %H:%M}",
            "year": year,
            "periods": [
                period("7d", "直近7日間", [r for r in rows if r["dt"] >= now - timedelta(days=7)]),
                period("30d", "直近30日間", [r for r in rows if r["dt"] >= now - timedelta(days=30)]),
                period("month", f"{month}月", [r for r in this_year if r["dt"].month == month]),
                period("year", f"{year}年", this_year),
                period("all", "全期間", rows),
            ],
            "monthly": monthly_top(this_year),
        }
    write_readme(data, page_url())
    write_html(data)
    print("README.md と docs/index.html を更新しました")


if __name__ == "__main__":
    main()
