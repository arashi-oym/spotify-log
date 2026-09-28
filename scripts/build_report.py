#!/usr/bin/env python3
"""data/plays.csv を集計して、ランキングを README.md に書き出します。
（README.md はGitHubのリポジトリ画面にそのまま表示されるので、スマホのGitHubアプリで見られます）
"""
import csv
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "plays.csv"
README = ROOT / "README.md"


def esc(s):
    return (s or "").replace("|", "\\|")


def load():
    rows = []
    if not CSV_PATH.exists():
        return rows
    with CSV_PATH.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            dt = datetime.strptime(r["played_at"][:19], "%Y-%m-%dT%H:%M:%S")
            r["dt"] = dt.replace(tzinfo=timezone.utc).astimezone(JST)
            r["ms"] = int(r.get("duration_ms") or 0)
            rows.append(r)
    return rows


def song_table(rows, n):
    counts = Counter(r["track_id"] for r in rows)
    info, ms = {}, defaultdict(int)
    for r in rows:
        info[r["track_id"]] = r
        ms[r["track_id"]] += r["ms"]
    ranked = sorted(counts.items(), key=lambda x: (-x[1], -ms[x[0]]))[:n]
    lines = ["| # | 曲 | アーティスト | 回数 | 分 |", "|---:|---|---|---:|---:|"]
    for i, (tid, c) in enumerate(ranked, 1):
        r = info[tid]
        lines.append(f"| {i} | {esc(r['track'])} | {esc(r['artists'])} | {c} | {ms[tid] // 60000} |")
    return "\n".join(lines)


def artist_table(rows, n):
    counts, ms = Counter(), defaultdict(int)
    for r in rows:
        for a in r["artists"].split(" / "):
            if a:
                counts[a] += 1
                ms[a] += r["ms"]
    ranked = sorted(counts.items(), key=lambda x: (-x[1], -ms[x[0]]))[:n]
    lines = ["| # | アーティスト | 回数 | 分 |", "|---:|---|---:|---:|"]
    for i, (a, c) in enumerate(ranked, 1):
        lines.append(f"| {i} | {esc(a)} | {c} | {ms[a] // 60000} |")
    return "\n".join(lines)


def monthly_top(rows):
    by_month = defaultdict(list)
    for r in rows:
        by_month[r["dt"].month].append(r)
    lines = ["| 月 | 1位の曲 | アーティスト | 回数 | その月の再生数 |", "|---:|---|---|---:|---:|"]
    for m in sorted(by_month):
        mr = by_month[m]
        tid, c = Counter(r["track_id"] for r in mr).most_common(1)[0]
        r = next(x for x in reversed(mr) if x["track_id"] == tid)
        lines.append(f"| {m}月 | {esc(r['track'])} | {esc(r['artists'])} | {c} | {len(mr)} |")
    return "\n".join(lines)


def main():
    rows = load()
    out = ["# 🎧 Spotify 再生記録", ""]
    if not rows:
        out.append("まだ記録がありません。1時間ほど待つと反映されます。")
        README.write_text("\n".join(out) + "\n", encoding="utf-8")
        return

    latest = max(r["dt"] for r in rows)
    year, month = latest.year, latest.month
    this_year = [r for r in rows if r["dt"].year == year]
    this_month = [r for r in this_year if r["dt"].month == month]
    total_min = sum(r["ms"] for r in this_year) // 60000

    out += [
        f"最終記録：{latest:%Y-%m-%d %H:%M}（日本時間）",
        "",
        f"**{year}年**：{len(this_year):,}回再生・約{total_min:,}分",
        "",
        f"## {month}月のトップソング",
        "",
        song_table(this_month, 20),
        "",
        f"## {year}年のトップソング",
        "",
        song_table(this_year, 50),
        "",
        f"## {year}年のトップアーティスト",
        "",
        artist_table(this_year, 20),
        "",
        f"## {year}年 月ごとの1位",
        "",
        monthly_top(this_year),
        "",
    ]

    past_years = sorted({r["dt"].year for r in rows if r["dt"].year != year}, reverse=True)
    for y in past_years:
        yr = [r for r in rows if r["dt"].year == y]
        out += [
            "<details>",
            f"<summary>{y}年のランキング（{len(yr):,}回再生）</summary>",
            "",
            song_table(yr, 50),
            "",
            artist_table(yr, 20),
            "",
            "</details>",
            "",
        ]

    out.append("※「分」は曲の長さ×回数の目安です。")
    README.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("README.md を更新しました")


if __name__ == "__main__":
    main()
