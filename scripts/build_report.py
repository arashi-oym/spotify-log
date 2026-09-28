#!/usr/bin/env python3
"""data/ の記録から、専用ページ docs/index.html を作ります。
ページのデザインは scripts/template.html にあり、ここではデータを埋め込むだけです。
（集計はページを開いたときにブラウザ側で行います）
"""
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "plays.csv"
ARTISTS_PATH = ROOT / "data" / "artists.json"
STATUS_PATH = ROOT / "data" / "status.json"
TEMPLATE = Path(__file__).resolve().parent / "template.html"
DOCS = ROOT / "docs"


def read_json(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError:
            pass
    return {}


def load_rows():
    rows = []
    if not CSV_PATH.exists():
        return rows
    with CSV_PATH.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                dt = datetime.strptime(r["played_at"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            rows.append({
                "dt": dt, "track_id": r.get("track_id") or r.get("track") or "",
                "track": r.get("track") or "", "artists": r.get("artists") or "",
                "image": r.get("image") or "", "artist_ids": r.get("artist_ids") or "",
                "ms": int(r.get("duration_ms") or 0),
            })
    rows.sort(key=lambda x: x["dt"])
    return rows


def artist_images(rows):
    """アーティスト名 → アーティスト写真URL"""
    cache, out = read_json(ARTISTS_PATH), {}
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


def to_ms(iso):
    try:
        return int(datetime.fromisoformat(iso).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def main():
    rows = load_rows()
    songs, index, plays = [], {}, []
    for r in rows:
        k = r["track_id"]
        if k not in index:
            index[k] = len(songs)
            songs.append([r["track"], r["artists"], r["image"], round(r["ms"] / 60000, 2)])
        elif r["image"] and not songs[index[k]][2]:
            songs[index[k]][2] = r["image"]
        plays.append([int(r["dt"].timestamp()), index[k]])

    st = read_json(STATUS_PATH)
    repo = os.environ.get("GITHUB_REPOSITORY", "arashi-oym/spotify-log")
    data = {
        "songs": songs,
        "plays": plays,
        "artistImages": artist_images(rows),
        "status": {
            "state": st.get("state", "ok"),
            "code": st.get("code", ""),
            "message": st.get("message", ""),
            "at": to_ms(st.get("at")),
            "lastSuccess": to_ms(st.get("lastSuccess")),
            "tokenSince": st.get("tokenSince", ""),
        },
        "guide": f"https://github.com/{repo}#止まったときの直し方",
        "actions": f"https://github.com/{repo}/actions",
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload, 1)
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    print(f"docs/index.html を更新しました（再生 {len(plays):,}件 / 曲 {len(songs):,}曲）")


if __name__ == "__main__":
    main()
