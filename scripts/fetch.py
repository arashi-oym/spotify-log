#!/usr/bin/env python3
"""Spotifyの「最近再生した曲」（直近50曲）を取得し、data/plays.csv に新しい分だけ追記します。
GitHub Actions から1時間ごとに実行されます。
"""
import base64
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "plays.csv"
FIELDS = ["played_at", "track_id", "track", "artists", "album", "duration_ms"]


def get_access_token():
    cid = os.environ["SPOTIFY_CLIENT_ID"]
    secret = os.environ["SPOTIFY_CLIENT_SECRET"]
    refresh = os.environ["SPOTIFY_REFRESH_TOKEN"]
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        }).encode(),
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["access_token"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        if e.code == 400:
            sys.exit(
                "Spotifyの認証期限が切れた可能性があります（約6か月ごとに必要）。\n"
                "Macで scripts/auth.py を実行し直し、SPOTIFY_REFRESH_TOKEN を更新してください。\n"
                + detail
            )
        sys.exit(f"トークン取得エラー ({e.code}): {detail}")


def fetch_recent(token):
    req = urllib.request.Request(
        "https://api.spotify.com/v1/me/player/recently-played?limit=50",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("items", [])
    except urllib.error.HTTPError as e:
        sys.exit(f"再生履歴の取得エラー ({e.code}): {e.read().decode('utf-8', 'replace')}")


def load_seen():
    if not CSV_PATH.exists():
        return set()
    with CSV_PATH.open(newline="", encoding="utf-8-sig") as f:
        return {row["played_at"] for row in csv.DictReader(f)}


def main():
    token = get_access_token()
    items = fetch_recent(token)
    seen = load_seen()

    new_rows = []
    for it in items:
        t = it.get("track")
        played_at = (it.get("played_at") or "")[:19]
        if not t or len(played_at) < 19:
            continue
        played_at += "Z"  # 例: 2026-09-28T12:34:56Z（UTC）
        if played_at in seen:
            continue
        seen.add(played_at)
        new_rows.append({
            "played_at": played_at,
            "track_id": t.get("id") or t.get("uri") or t.get("name", ""),
            "track": t.get("name", ""),
            "artists": " / ".join(a.get("name", "") for a in t.get("artists", [])),
            "album": (t.get("album") or {}).get("name", ""),
            "duration_ms": t.get("duration_ms", 0),
        })

    if not new_rows:
        print("新しい再生はありません")
        return

    new_rows.sort(key=lambda r: r["played_at"])
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_header = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        w.writerows(new_rows)
    print(f"{len(new_rows)}件を追加しました")


if __name__ == "__main__":
    main()
