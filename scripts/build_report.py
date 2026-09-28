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
ARTISTS_PATH = ROOT / "data" / "artists.json"
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
            r["artist_ids"] = r.get("artist_ids") or ""
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


ARTIST_IMG = {}


def build_artist_images(rows):
    """アーティスト名 → アーティスト写真URL"""
    cache = {}
    if ARTISTS_PATH.exists():
        try:
            cache = json.loads(ARTISTS_PATH.read_text(encoding="utf-8") or "{}")
        except ValueError:
            cache = {}
    for r in rows:
        names = r["artists"].split(" / ")
        ids = r["artist_ids"].split(" / ") if r["artist_ids"] else []
        if len(names) != len(ids):
            continue
        for name, aid in zip(names, ids):
            img = (cache.get(aid) or {}).get("image")
            if img:
                ARTIST_IMG[name] = img


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
        img = ARTIST_IMG.get(a, "")
        for tid, _ in ([] if img else track_counts[a].most_common()):
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

TEMPLATE = Path(__file__).resolve().parent / "template.html"


def load_artist_images(rows):
    """アーティスト名 → アーティスト写真URL（ページ用）"""
    out = {}
    if not ARTISTS_PATH.exists():
        return out
    try:
        cache = json.loads(ARTISTS_PATH.read_text(encoding="utf-8") or "{}")
    except ValueError:
        return out
    for r in rows:
        names = r["artists"].split(" / ")
        ids = r["artist_ids"].split(" / ") if r["artist_ids"] else []
        if len(names) != len(ids):
            continue
        for name, aid in zip(names, ids):
            img = (cache.get(aid) or {}).get("image")
            if img:
                out[name] = img
    return out


def write_html(rows):
    """再生記録をまるごとページに埋め込む（集計はページ側で行う）"""
    songs, index = [], {}
    plays = []
    for r in sorted(rows, key=lambda x: x["dt"]):
        k = r["track_id"]
        if k not in index:
            index[k] = len(songs)
            songs.append([r["track"], r["artists"], r["image"], round(r["ms"] / 60000, 2)])
        elif r["image"] and not songs[index[k]][2]:
            songs[index[k]][2] = r["image"]
        plays.append([int(r["dt"].timestamp()), index[k]])
    data = {
        "checked": int(datetime.now(timezone.utc).timestamp() * 1000),
        "songs": songs,
        "plays": plays,
        "artistImages": load_artist_images(rows),
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload, 1)
    (DOCS / "index.html").write_text(html, encoding="utf-8")


# ---------------- メイン ----------------

def page_url():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return ""
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/"


def main():
    rows = load()
    build_artist_images(rows)
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
    write_html(rows)
    print("README.md と docs/index.html を更新しました")


if __name__ == "__main__":
    main()
