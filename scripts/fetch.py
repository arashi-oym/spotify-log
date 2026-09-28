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
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "plays.csv"
ARTISTS_PATH = ROOT / "data" / "artists.json"
STATUS_PATH = ROOT / "data" / "status.json"
HISTORY_PATH = ROOT / "data" / "history.csv"   # Spotifyから取り寄せた過去の履歴（あれば）
TRACKS_PATH = ROOT / "data" / "tracks.json"    # 過去の履歴の曲の情報（ジャケ写・参加アーティストなど）
HISTORY_LOOKUP_LIMIT = 300                      # 過去の履歴の曲情報を1回に調べる最大件数
ARTIST_LOOKUP_LIMIT = 150                       # アーティスト情報（写真・ジャンル）を1回に調べる最大件数
MB_LIMIT = 60                                   # MusicBrainzでジャンルを調べる最大件数（1秒に1回までの決まりがあるため）
MB_API = "https://musicbrainz.org/ws/2"
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


def enrich_history(token):
    """過去の履歴（history.csv）の曲について、ジャケ写・参加アーティスト・曲の長さを少しずつ調べて tracks.json に保存する。
    よく聴いた曲から順に調べるので、ランキング上位から先に画像がそろっていく。"""
    if not HISTORY_PATH.exists():
        return 0
    cache = {}
    if TRACKS_PATH.exists():
        try:
            cache = json.loads(TRACKS_PATH.read_text(encoding="utf-8") or "{}")
        except ValueError:
            cache = {}
    counts = {}
    with HISTORY_PATH.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            tid = r.get("track_id") or ""
            if SPOTIFY_ID.match(tid) and tid not in cache:
                counts[tid] = counts.get(tid, 0) + 1
    todo = sorted(counts, key=lambda k: -counts[k])[:HISTORY_LOOKUP_LIMIT]
    done = 0
    for tid in todo:
        try:
            t = api_get(token, f"/tracks/{tid}")
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                cache[tid] = {}          # Spotifyから消えた曲など。次回からは調べない
                continue
            print(f"過去の履歴の曲情報の取得を中断しました（次回また続きから）: {e.code}")
            break
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            print(f"過去の履歴の曲情報の取得を中断しました（次回また続きから）: {e}")
            break
        image, artist_ids = track_extras(t)
        cache[tid] = {
            "image": image,
            "artists": " / ".join(a.get("name", "") for a in t.get("artists", [])),
            "artist_ids": artist_ids,
            "duration_ms": t.get("duration_ms", 0),
        }
        done += 1
        time.sleep(0.05)
    if done or todo:
        TRACKS_PATH.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
    left = max(0, len(counts) - len(todo))
    print(f"過去の履歴：曲情報 {done}件を取得（残り 約{left:,}曲）")
    return done


def mb_get(path):
    repo = os.environ.get("GITHUB_REPOSITORY", "arashi-oym/spotify-log")
    req = urllib.request.Request(MB_API + path, headers={
        "User-Agent": f"MyListeningLog/1.0 ( https://github.com/{repo} )", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def mb_genres(spotify_artist_id):
    """MusicBrainz（無料の公開音楽データベース）で、Spotifyのアーティストに対応するジャンルを調べる"""
    time.sleep(1.1)
    url = urllib.parse.quote(f"https://open.spotify.com/artist/{spotify_artist_id}", safe="")
    try:
        d = mb_get(f"/url?resource={url}&inc=artist-rels&fmt=json")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []                 # MusicBrainzに登録がない
        raise
    mbid = next((rel["artist"]["id"] for rel in d.get("relations", []) if rel.get("artist")), None)
    if not mbid:
        return []
    time.sleep(1.1)
    a = mb_get(f"/artist/{mbid}?inc=genres&fmt=json")
    gs = sorted(a.get("genres") or [], key=lambda g: -(g.get("count") or 0))
    return [g.get("name", "") for g in gs if g.get("name")]


def artist_play_counts(rows):
    """アーティストごとのおおよその再生回数（よく聴いた順に調べるため）"""
    counts = Counter()
    for r in rows:
        for aid in r["artist_ids"].split(" / "):
            if SPOTIFY_ID.match(aid):
                counts[aid] += 1
    if HISTORY_PATH.exists() and TRACKS_PATH.exists():
        try:
            tracks = json.loads(TRACKS_PATH.read_text(encoding="utf-8") or "{}")
        except ValueError:
            tracks = {}
        per_track = Counter()
        with HISTORY_PATH.open(newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                per_track[r.get("track_id") or ""] += 1
        for tid, c in per_track.items():
            for aid in ((tracks.get(tid) or {}).get("artist_ids") or "").split(" / "):
                if SPOTIFY_ID.match(aid):
                    counts[aid] += c
    return counts


def update_artists(token, rows):
    """アーティストの写真とジャンルを、よく聴いたアーティストから順に調べて artists.json に保存する。
    ジャンルはまずSpotifyから取り、Spotifyにないときだけ MusicBrainz で補う。"""
    cache = {}
    if ARTISTS_PATH.exists():
        try:
            cache = json.loads(ARTISTS_PATH.read_text(encoding="utf-8") or "{}")
        except ValueError:
            cache = {}
    counts = artist_play_counts(rows)
    need = [aid for aid in sorted(counts, key=lambda k: -counts[k]) if "genres" not in (cache.get(aid) or {})]
    got = mb_used = 0
    mb_ok = True
    for aid in need[:ARTIST_LOOKUP_LIMIT]:
        try:
            a = api_get(token, f"/artists/{aid}")
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                cache[aid] = {"name": "", "image": "", "genres": [], "genreSource": "none"}
                continue
            print(f"アーティスト情報の取得を中断しました（次回また続きから）: {e.code}")
            break
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            print(f"アーティスト情報の取得を中断しました（次回また続きから）: {e}")
            break
        entry = {"name": a.get("name", ""), "image": pick_image(a)}
        genres = [g for g in (a.get("genres") or []) if g]
        source = "spotify" if genres else ""
        if not genres:
            if not mb_ok or mb_used >= MB_LIMIT:
                cache[aid] = {**(cache.get(aid) or {}), **entry}   # 写真だけ保存し、ジャンルは次回
                break
            try:
                genres = mb_genres(aid)
                mb_used += 1
                source = "musicbrainz" if genres else "none"
            except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout, TimeoutError, ValueError) as e:
                print(f"MusicBrainzの問い合わせを中断しました（次回また続きから）: {e}")
                mb_ok = False
                cache[aid] = {**(cache.get(aid) or {}), **entry}
                break
        entry["genres"] = genres
        entry["genreSource"] = source
        cache[aid] = entry
        got += 1
        time.sleep(0.05)
    if cache:
        ARTISTS_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    left = sum(1 for aid in counts if "genres" not in (cache.get(aid) or {}))
    print(f"アーティスト情報 {got}件を取得（うちMusicBrainz {mb_used}件）／ジャンル未取得 残り約{left:,}組")
    return got


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
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                continue
            print(f"曲情報の補完を中断しました（次回また試します）: {e.code}")
            break
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            print(f"曲情報の補完を中断しました（次回また試します）: {e}")
            break
        fill(tid, *track_extras(t))
        looked += 1

    save_rows(rows)
    enrich_history(token)

    got = update_artists(token, rows)

    print(f"新しい再生 {added}件 / 曲情報の補完 {looked}件 / アーティスト情報 {got}件")


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
