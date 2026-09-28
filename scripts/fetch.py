#!/usr/bin/env python3
"""Spotifyの「最近再生した曲」（直近50曲）を取得し、data/plays.csv に新しい分を追記します。
あわせて、ジャケ写やアーティスト画像が未取得のものを少しずつ補完します。
GitHub Actions から1時間ごとに実行されます。
"""
import base64
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "plays.csv"
ARTISTS_PATH = ROOT / "data" / "artists.json"
FIELDS = ["played_at", "track_id", "track", "artists", "album", "duration_ms", "image", "artist_ids"]
API = "https://api.spotify.com/v1"
LOOKUP_LIMIT = 40  # 1回の実行で補完する最大件数（曲・アーティストそれぞれ）
SPOTIFY_ID = re.compile(r"^[0-9A-Za-z]{22}$")


def get_access_token():
    cid = os.environ["SPOTIFY_CLIENT_ID"].strip()
    secret = os.environ["SPOTIFY_CLIENT_SECRET"].strip()
    refresh = os.environ["SPOTIFY_REFRESH_TOKEN"].strip()
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh}).encode(),
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["access_token"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        if "invalid_client" in detail:
            sys.exit("Client ID / Client Secret が正しくありません。GitHubのSecretを確認してください。\n" + detail)
        if "invalid_grant" in detail:
            sys.exit("リフレッシュトークンが無効か期限切れです（約6か月ごとに必要）。\n"
                     "Macで scripts/auth.py を実行し直し、SPOTIFY_REFRESH_TOKEN を更新してください。\n" + detail)
        sys.exit(f"トークン取得エラー ({e.code}): {detail}")


def api_get(token, path):
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def pick_image(obj):
    imgs = (obj or {}).get("images") or []
    if not imgs:
        return ""
    imgs = sorted(imgs, key=lambda i: i.get("width") or 0)
    for i in imgs:
        if (i.get("width") or 0) >= 200:
            return i.get("url", "")
    return imgs[-1].get("url", "")


def track_extras(t):
    image = pick_image(t.get("album"))
    artist_ids = " / ".join(a.get("id") or "" for a in t.get("artists", []))
    return image, artist_ids


def load_rows():
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open(newline="", encoding="utf-8-sig") as f:
        return [{k: (r.get(k) or "") for k in FIELDS} for r in csv.DictReader(f)]


def save_rows(rows):
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda r: r["played_at"])
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def main():
    token = get_access_token()
    try:
        items = api_get(token, "/me/player/recently-played?limit=50").get("items", [])
    except urllib.error.HTTPError as e:
        sys.exit(f"再生履歴の取得エラー ({e.code}): {e.read().decode('utf-8', 'replace')}")

    rows = load_rows()
    seen = {r["played_at"] for r in rows}
    known = {}  # track_id -> (image, artist_ids)
    added = 0

    for it in items:
        t = it.get("track")
        played_at = (it.get("played_at") or "")[:19]
        if not t or len(played_at) < 19:
            continue
        played_at += "Z"
        tid = t.get("id") or t.get("uri") or t.get("name", "")
        image, artist_ids = track_extras(t)
        known[tid] = (image, artist_ids)
        if played_at in seen:
            continue
        seen.add(played_at)
        rows.append({
            "played_at": played_at,
            "track_id": tid,
            "track": t.get("name", ""),
            "artists": " / ".join(a.get("name", "") for a in t.get("artists", [])),
            "album": (t.get("album") or {}).get("name", ""),
            "duration_ms": t.get("duration_ms", 0),
            "image": image,
            "artist_ids": artist_ids,
        })
        added += 1

    # --- 画像などが欠けている行を補完 ---
    def fill(tid, image, artist_ids):
        for r in rows:
            if r["track_id"] == tid:
                r["image"] = r["image"] or image
                r["artist_ids"] = r["artist_ids"] or artist_ids

    for tid, (image, artist_ids) in known.items():
        fill(tid, image, artist_ids)

    missing = []
    for r in rows:
        tid = r["track_id"]
        if (not r["image"] or not r["artist_ids"]) and SPOTIFY_ID.match(tid) and tid not in missing:
            missing.append(tid)
    looked = 0
    for tid in missing[:LOOKUP_LIMIT]:
        try:
            t = api_get(token, f"/tracks/{tid}")
        except urllib.error.HTTPError as e:
            print(f"曲情報の補完を中断しました ({e.code})")
            break
        fill(tid, *track_extras(t))
        looked += 1

    save_rows(rows)

    # --- アーティスト画像 ---
    cache = {}
    if ARTISTS_PATH.exists():
        cache = json.loads(ARTISTS_PATH.read_text(encoding="utf-8") or "{}")
    need = []
    for r in rows:
        for aid in r["artist_ids"].split(" / "):
            if SPOTIFY_ID.match(aid) and aid not in cache and aid not in need:
                need.append(aid)
    got = 0
    for aid in need[:LOOKUP_LIMIT]:
        try:
            a = api_get(token, f"/artists/{aid}")
        except urllib.error.HTTPError as e:
            print(f"アーティスト画像の取得を中断しました ({e.code})")
            break
        cache[aid] = {"name": a.get("name", ""), "image": pick_image(a)}
        got += 1
    if cache:
        ARTISTS_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    print(f"新しい再生 {added}件 / 曲情報の補完 {looked}件 / アーティスト画像 {got}件")


if __name__ == "__main__":
    main()
