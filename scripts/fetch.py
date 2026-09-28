#!/usr/bin/env python3
"""Spotifyの「最近再生した曲」（直近50曲）を取得し、data/plays.csv に新しい分を追記します。
あわせて、ジャケ写やアーティスト画像が未取得のものを少しずつ補完します。
GitHub Actions から1時間ごとに実行されます。
"""
import base64
import csv
import hashlib
import socket
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "plays.csv"
ARTISTS_PATH = ROOT / "data" / "artists.json"
STATUS_PATH = ROOT / "data" / "status.json"
FIELDS = ["played_at", "track_id", "track", "artists", "album", "duration_ms", "image", "artist_ids"]
API = "https://api.spotify.com/v1"
LOOKUP_LIMIT = 40  # 1回の実行で補完する最大件数（曲・アーティストそれぞれ）
SPOTIFY_ID = re.compile(r"^[0-9A-Za-z]{22}$")


class SyncError(Exception):
    """code: token_expired / bad_client / forbidden / temporary / unknown"""
    def __init__(self, code, message, detail=""):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail


def classify_http(e, where):
    detail = e.read().decode("utf-8", "replace")[:500]
    if e.code in (429, 500, 502, 503, 504):
        return SyncError("temporary", f"Spotifyが混み合っているか、一時的に応答していません（{where}・{e.code}）。", detail)
    if e.code in (401, 403):
        return SyncError("forbidden", f"Spotifyにアクセスを拒否されました（{where}・{e.code}）。", detail)
    return SyncError("unknown", f"想定していないエラーが起きました（{where}・{e.code}）。", detail)


def get_access_token():
    cid = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    refresh = os.environ.get("SPOTIFY_REFRESH_TOKEN", "").strip()
    if not (cid and secret and refresh):
        raise SyncError("bad_client", "GitHubのSecret（SPOTIFY_CLIENT_ID など）が登録されていないか、名前が違います。")
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
        detail = e.read().decode("utf-8", "replace")[:500]
        if "invalid_grant" in detail:
            raise SyncError("token_expired", "Spotifyとの連携の期限が切れたか、無効になりました。", detail)
        if "invalid_client" in detail:
            raise SyncError("bad_client", "Client ID または Client Secret が正しくありません。", detail)
        if e.code in (429, 500, 502, 503, 504):
            raise SyncError("temporary", f"Spotifyが一時的に応答していません（{e.code}）。", detail)
        raise SyncError("unknown", f"ログイン処理で想定していないエラーが起きました（{e.code}）。", detail)
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        raise SyncError("temporary", "Spotifyに接続できませんでした（通信エラー）。", str(e))


def api_get(token, path):
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def token_hash():
    raw = os.environ.get("SPOTIFY_REFRESH_TOKEN", "").strip()
    return hashlib.sha256(raw.encode()).hexdigest()[:10] if raw else ""


def load_status():
    if STATUS_PATH.exists():
        try:
            return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {}


def save_status(st):
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


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


def sync():
    token = get_access_token()
    try:
        items = api_get(token, "/me/player/recently-played?limit=50").get("items", [])
    except urllib.error.HTTPError as e:
        raise classify_http(e, "再生履歴の取得")
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        raise SyncError("temporary", "Spotifyに接続できませんでした（通信エラー）。", str(e))

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
        except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout, TimeoutError) as e:
            print(f"曲情報の補完を中断しました（次回また試します）: {e}")
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
        except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout, TimeoutError) as e:
            print(f"アーティスト画像の取得を中断しました（次回また試します）: {e}")
            break
        cache[aid] = {"name": a.get("name", ""), "image": pick_image(a)}
        got += 1
    if cache:
        ARTISTS_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    print(f"新しい再生 {added}件 / 曲情報の補完 {looked}件 / アーティスト画像 {got}件")


def main():
    """成功・失敗の結果を data/status.json に残し、ページに表示できるようにする。
    終了コード: 0 = 成功 または 一時的なエラー（自動で再試行）、2 = 手当てが必要なエラー"""
    st = load_status()
    now = datetime.now(timezone.utc)
    try:
        sync()
    except SyncError as e:
        st.update({"state": "temporary" if e.code == "temporary" else "error",
                   "code": e.code, "message": e.message, "at": now.isoformat(timespec="seconds")})
        save_status(st)
        print(f"[{e.code}] {e.message}\n{e.detail}")
        sys.exit(0 if e.code == "temporary" else 2)

    h = token_hash()
    if h and st.get("tokenHash") != h:
        st["tokenHash"] = h
        st["tokenSince"] = now.date().isoformat()   # 連携を始めた日（期限の目安に使う）
    st.update({"state": "ok", "code": "", "message": "", "at": now.isoformat(timespec="seconds"),
               "lastSuccess": now.isoformat(timespec="seconds")})
    save_status(st)


if __name__ == "__main__":
    main()
