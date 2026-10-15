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
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "plays.csv"
ARTISTS_PATH = ROOT / "data" / "artists.json"
STATUS_PATH = ROOT / "data" / "status.json"
DATA = ROOT / "data"                            # Spotifyから取り寄せた過去の履歴は data/history*.csv（いくつあってもOK）
TRACKS_PATH = ROOT / "data" / "tracks.json"
EXCLUDE_PATH = ROOT / "data" / "exclude.json"   # 集計から外す再生（画像の取得もしない）    # 過去の履歴の曲の情報（ジャケ写・参加アーティストなど）
# --- Spotifyへの問い合わせ回数の上限 ---
# 2026/9/28〜10/15 の記録では、曲情報・アーティスト写真の問い合わせが「24時間で600回」に達すると毎回
# 429（多すぎ）で止められ、最初の問い合わせから24時間たつまで待たされていた（再生履歴の取得は別枠で、止められない）。
# そこで直近24時間の回数を自分で数え、上限の少し手前で止まるようにし、1時間ごとに均等に使う。
DAILY_QUOTA = 600        # Spotifyの上限（観測値）
DAILY_BUDGET = 590       # 自分で守る上限（観測値の98%）。回数は1回ずつ正確に数え、実行も1つずつ順番なので超えない
PER_RUN = 25             # 1回（1時間）に使う最大回数。前回から時間が空いたときは、その分（最大3時間分）まとめて使う
PACE = 0.5                                      # Spotifyへの問い合わせの間隔（秒）。短すぎると「429 多すぎ」で止められる
MIN_COOLDOWN = 30 * 60                          # 429で止められたとき、最低これだけ（秒）補完を休む
FIELDS = ["played_at", "track_id", "track", "artists", "album", "duration_ms", "image", "artist_ids"]
API = "https://api.spotify.com/v1"
SPOTIFY_ID = re.compile(r"^[0-9A-Za-z]{22}$")


class SyncError(Exception):
    """code: not_configured / token_expired / bad_client / forbidden / temporary / unknown"""
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
        missing = [n for n, v in (("SPOTIFY_CLIENT_ID", cid), ("SPOTIFY_CLIENT_SECRET", secret), ("SPOTIFY_REFRESH_TOKEN", refresh)) if not v]
        raise SyncError("not_configured", "初期設定がまだ終わっていません（GitHubのSecretに " + "、".join(missing) + " がありません）。")
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


RATE = {"until": None}   # 429で止められたときの「この時刻までは補完を休む」


class Budget:
    """今回の実行で使ってよい問い合わせ回数"""
    def __init__(self, n=0):
        self.left, self.used = max(0, n), 0

    def take(self):
        if self.left <= 0:
            return False
        self.left -= 1
        self.used += 1
        return True


BUDGET = Budget(0)
REPORT = {}   # 今回の実行で何を更新したか（status.json に残し、ページの「記録の状態」に表示する）


def recent_usage(st, now):
    """status.json に残した「いつ何回問い合わせたか」のうち、直近24時間の分"""
    keep = []
    for at, n in st.get("usage", []):
        try:
            if now - datetime.fromisoformat(at) < timedelta(hours=24):
                keep.append([at, int(n)])
        except (TypeError, ValueError):
            continue
    return keep


def cooling():
    """前回429で止められていて、まだ休む時間中なら True"""
    until = RATE["until"] or load_status().get("cooldownUntil")
    if not until:
        return False
    try:
        return datetime.now(timezone.utc) < datetime.fromisoformat(until)
    except ValueError:
        return False


def hit_limit(e):
    """429を受けたら、Spotifyが指定した時間（最低30分）だけ補完を休む（回数を数えているので、ふだんは起きない想定）"""
    try:
        wait = int(e.headers.get("Retry-After") or 0)
    except (TypeError, ValueError):
        wait = 0
    wait = max(wait, MIN_COOLDOWN)
    until = datetime.now(timezone.utc) + timedelta(seconds=wait)
    RATE["until"] = until.isoformat(timespec="seconds")
    jst = until + timedelta(hours=9)
    print(f"Spotifyから「問い合わせが多すぎる」と言われたため、{jst:%m/%d %H:%M}（日本時間）ごろまで画像の補完を休みます。"
          "再生の記録は続けます。")


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


def excluded_plays():
    """data/exclude.json に載っている再生（日時と曲IDの組）"""
    out = set()
    if EXCLUDE_PATH.exists():
        try:
            data = json.loads(EXCLUDE_PATH.read_text(encoding="utf-8") or "{}")
        except ValueError:
            return out
        for g in data.get("groups", []):
            for p in g.get("plays", []):
                if len(p) >= 2:
                    out.add((p[0][:19] + "Z", p[1]))
    return out


def history_paths():
    """取り寄せた過去の履歴のファイル（history.csv、history-20261015.csv など）"""
    return sorted(p for p in DATA.glob("history*.csv") if p.is_file())


def history_records():
    """過去の履歴のすべての行（ファイルをまたいで順に）"""
    for path in history_paths():
        with path.open(newline="", encoding="utf-8-sig") as f:
            yield from csv.DictReader(f)


def load_tracks_cache():
    if TRACKS_PATH.exists():
        try:
            return json.loads(TRACKS_PATH.read_text(encoding="utf-8") or "{}")
        except ValueError:
            pass
    return {}


def song_key(track, artists):
    """ページと同じく「曲名＋メインのアーティスト」で同じ曲とみなす"""
    return ((track or "").strip().lower(), (artists or "").split(" / ")[0].strip().lower())


def album_key(album, artists):
    return ((album or "").strip().lower(), (artists or "").split(" / ")[0].strip().lower()) if album else None


def full_info(t, album=None):
    """/tracks の結果（または /albums の中の曲＋そのアルバム）を tracks.json の形にする"""
    alb = album or t.get("album") or {}
    return {
        "image": pick_image(alb),
        "artists": " / ".join(a.get("name", "") for a in t.get("artists", [])),
        "artist_ids": " / ".join(a.get("id") or "" for a in t.get("artists", [])),
        "duration_ms": t.get("duration_ms", 0),
        "album_id": alb.get("id", ""),
    }


def lookup(token, path, what):
    """1回問い合わせる。成功→結果 / 見つからない→{} / 中断すべき→None"""
    if not BUDGET.take():
        return None
    try:
        res = api_get(token, path)
        time.sleep(PACE)
        return res
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            return {}
        if e.code == 429:
            hit_limit(e)
        else:
            print(f"{what}の取得を中断しました（次回また続きから）: {e.code}")
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        print(f"{what}の取得を中断しました（次回また続きから）: {e}")
    return None


def history_songs(cache, rows):
    """過去の履歴と自動記録を曲ごとにまとめる（再生回数・最後に聴いた日時・曲の番号・アルバム・ジャケ写の有無）"""
    songs, album_img = {}, {}
    def add(k, when, tid, akey, image):
        s = songs.setdefault(k, {"plays": 0, "last": "", "ids": [], "album": None, "image": False})
        s["plays"] += 1
        s["last"] = max(s["last"], when)
        if tid and tid not in s["ids"]:
            s["ids"].append(tid)
        s["album"] = s["album"] or akey
        if image:
            s["image"] = True
            if akey:
                album_img.setdefault(akey, image)
    for r in rows:                                   # 自動記録（ジャケ写つき）
        add(song_key(r["track"], r["artists"]), r["played_at"][:19], "", album_key(r["album"], r["artists"]), r["image"])
    ex = excluded_plays()
    for r in history_records():
        tid = r.get("track_id") or ""
        when = (r.get("played_at") or "")[:19]
        if (when + "Z", tid) in ex:
            continue   # 除外した再生の曲は調べない（問い合わせの節約）
        add(song_key(r.get("track"), r.get("artists")), when, tid if SPOTIFY_ID.match(tid) else "",
            album_key(r.get("album"), r.get("artists")), (cache.get(tid) or {}).get("image", ""))
    return songs, album_img


def enrich_history(token):
    """過去の履歴の曲のジャケ写・参加アーティスト・曲の長さを、問い合わせをできるだけ節約して埋める。
    ① 同じアルバムの別の曲でジャケ写が分かっていれば、問い合わせずにそのジャケ写を使う（参加アーティストなどはあとで）
    ② よく聴いた曲から（同じ回数なら最近聴いた曲から）1曲ずつ調べる
    ③ そのアルバムにまだ調べていない曲が2曲以上あれば、アルバムごと1回で調べて全部埋める
    ④ ジャケ写が全部そろったら、①の曲の参加アーティスト・曲の長さを同じ方法で仕上げる"""
    if not history_paths():
        return 0
    cache = load_tracks_cache()
    songs, album_img = history_songs(cache, load_rows())

    shared = 0                                      # ①
    for s in songs.values():
        if not s["image"] and s["album"] in album_img:
            for tid in s["ids"]:
                if tid not in cache:
                    cache[tid] = {"image": album_img[s["album"]], "partial": 1}
            s["image"] = True
            shared += 1

    if cooling():                                   # ①は問い合わせなしなので、休憩中でも行う
        if shared:
            TRACKS_PATH.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
        print(f"過去の履歴：同じアルバムから {shared}曲のジャケ写を設定。Spotifyの制限で休憩中のため、問い合わせはお休みします")
        REPORT.update(jackets=shared, jacketsLeft=sum(1 for s in songs.values() if not s["image"] and any(t not in cache for t in s["ids"])))
        return shared

    by_album = {}
    for k, s in songs.items():
        if s["album"]:
            by_album.setdefault(s["album"], []).append(k)
    def needs(tid, phase):
        return tid not in cache if phase == 1 else bool((cache.get(tid) or {}).get("partial"))
    def todo(phase):
        keys = [k for k, s in songs.items()
                if (not s["image"] if phase == 1 else True) and any(needs(t, phase) for t in s["ids"])]
        return sorted(keys, key=lambda k: (songs[k]["plays"], songs[k]["last"]), reverse=True)

    asked = filled = 0
    stop = False
    for phase in (1, 2):
        for k in todo(phase):
            s = songs[k]
            tid = next((t for t in s["ids"] if needs(t, phase)), None)
            if tid is None or (phase == 1 and s["image"]):
                continue                             # 同じアルバムの問い合わせで埋まった
            t = lookup(token, f"/tracks/{tid}", "過去の履歴の曲情報")
            if t is None:
                stop = True
                break
            asked += 1
            info = full_info(t) if t else {}         # {} はSpotifyから消えた曲。次回からは調べない
            aid = info.pop("album_id", "")           # アルバムの番号はこの場で使うだけ（保存しない）
            cache[tid] = info
            s["image"] = s["image"] or bool(info.get("image"))
            filled += 1
            # ③ 同じアルバムに、まだの曲が2曲以上残っていればアルバムごと調べる
            rest = [g for g in by_album.get(s["album"], []) if g != k
                    and any(needs(x, 1) or needs(x, 2) for x in songs[g]["ids"])]
            if len(rest) >= 2 and aid:
                a = lookup(token, f"/albums/{aid}", "アルバムの曲情報")
                if a is None:
                    stop = True
                    break
                asked += 1
                items = ((a.get("tracks") or {}).get("items")) or []
                by_id = {it.get("id"): it for it in items}
                by_name = {(it.get("name") or "").strip().lower(): it for it in items}
                for g in rest:
                    for x in songs[g]["ids"]:
                        if needs(x, 1) or needs(x, 2):
                            it = by_id.get(x) or by_name.get(g[0])
                            if it:
                                cache[x] = full_info(it, a)
                                cache[x].pop("album_id", None)
                                songs[g]["image"] = songs[g]["image"] or bool(cache[x]["image"])
                                filled += 1
        if stop or (phase == 1 and todo(1)):
            break                                    # ジャケ写がそろうまでは④に進まない

    if shared or asked:
        TRACKS_PATH.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
    left = sum(1 for s in songs.values() if not s["image"] and any(t not in cache for t in s["ids"]))
    finish = sum(1 for v in cache.values() if (v or {}).get("partial"))
    print(f"過去の履歴：ジャケ写 {filled + shared}曲分を取得（問い合わせ {asked}回・同じアルバムから {shared}曲）"
          f" / ジャケ写の残り 約{left:,}曲 / 参加アーティストなどの仕上げ残り 約{finish:,}曲")
    REPORT.update(jackets=filled + shared, jacketsLeft=left, finishLeft=finish)
    return filled + shared


def artist_play_counts(rows, last=None):
    """アーティストごとのおおよその再生回数（よく聴いた順に調べるため）。last を渡すと最後に聴いた日時も入れる"""
    counts = Counter()
    last = {} if last is None else last
    def seen(aid, when):
        if when > last.get(aid, ""):
            last[aid] = when
    for r in rows:
        for aid in r["artist_ids"].split(" / "):
            if SPOTIFY_ID.match(aid):
                counts[aid] += 1
                seen(aid, r["played_at"][:19])
    if history_paths() and TRACKS_PATH.exists():
        try:
            tracks = json.loads(TRACKS_PATH.read_text(encoding="utf-8") or "{}")
        except ValueError:
            tracks = {}
        per_track, track_last = Counter(), {}
        ex = excluded_plays()
        for r in history_records():
            when = (r.get("played_at") or "")[:19]
            tid = r.get("track_id") or ""
            if (when + "Z", tid) in ex:
                continue
            per_track[tid] += 1
            track_last[tid] = max(track_last.get(tid, ""), when)
        for tid, c in per_track.items():
            for aid in ((tracks.get(tid) or {}).get("artist_ids") or "").split(" / "):
                if SPOTIFY_ID.match(aid):
                    counts[aid] += c
                    seen(aid, track_last[tid])
    return counts


def artists_needed(rows):
    """写真をまだ調べていないアーティスト。いま聴いている曲（自動記録）のアーティストを先に、
    あとはよく聴いた順、同じ回数なら最近聴いた順"""
    cache = {}
    if ARTISTS_PATH.exists():
        try:
            cache = json.loads(ARTISTS_PATH.read_text(encoding="utf-8") or "{}")
        except ValueError:
            cache = {}
    last = {}
    counts = artist_play_counts(rows, last)
    live = {aid for r in rows for aid in r["artist_ids"].split(" / ") if SPOTIFY_ID.match(aid)}
    return sorted((aid for aid in counts if aid not in cache),
                  key=lambda k: (k in live, counts[k], last.get(k, "")), reverse=True)


def update_artists(token, rows, limit):
    """アーティストの写真を、artists_needed の順に調べて artists.json に保存する"""
    cache = {}
    if ARTISTS_PATH.exists():
        try:
            cache = json.loads(ARTISTS_PATH.read_text(encoding="utf-8") or "{}")
        except ValueError:
            cache = {}
    if cooling():
        print("アーティスト写真：Spotifyの制限で休憩中のため、今回はお休みします")
        REPORT["artistsLeft"] = len(artists_needed(rows))
        return 0
    need = artists_needed(rows)
    got = 0
    for aid in need[:max(0, limit)]:
        if not BUDGET.take():
            break
        try:
            a = api_get(token, f"/artists/{aid}")
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                cache[aid] = {"name": "", "image": ""}
                continue
            if e.code == 429:
                hit_limit(e)
            else:
                print(f"アーティスト写真の取得を中断しました（次回また続きから）: {e.code}")
            break
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            print(f"アーティスト写真の取得を中断しました（次回また続きから）: {e}")
            break
        cache[aid] = {"name": a.get("name", ""), "image": pick_image(a)}
        got += 1
        time.sleep(PACE)
    if cache:
        ARTISTS_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    left = sum(1 for aid in need if aid not in cache)
    REPORT["artistsLeft"] = left
    print(f"アーティスト写真 {got}件を取得（残り 約{left:,}組）")
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
    for tid in ([] if cooling() else missing):
        if not BUDGET.take():
            break
        try:
            t = api_get(token, f"/tracks/{tid}")
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                continue
            if e.code == 429:
                hit_limit(e)
            else:
                print(f"曲情報の補完を中断しました（次回また試します）: {e.code}")
            break
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            print(f"曲情報の補完を中断しました（次回また試します）: {e}")
            break
        fill(tid, *track_extras(t))
        looked += 1
        time.sleep(PACE)

    save_rows(rows)

    # 残りの回数の使い方：アーティスト写真を先に、残りを過去の曲のジャケ写に
    got = update_artists(token, rows, BUDGET.left)
    enrich_history(token)

    print(f"新しい再生 {added}件 / 曲情報の補完 {looked}件 / アーティスト写真 {got}件")
    REPORT.update(added=added, looked=looked, artists=got)


def main():
    """成功・失敗の結果を data/status.json に残し、ページに表示できるようにする。
    終了コード: 0 = 成功 または 一時的なエラー（自動で再試行）、2 = 手当てが必要なエラー"""
    global BUDGET
    st = load_status()
    now = datetime.now(timezone.utc)
    usage = recent_usage(st, now)
    used24 = sum(n for _, n in usage)
    try:
        hours = (now - datetime.fromisoformat(st.get("at", ""))).total_seconds() / 3600
    except (TypeError, ValueError):
        hours = 1
    slots = min(3, max(1, round(hours)))           # GitHubの実行が抜けた時間の分を取り戻す
    BUDGET = Budget(min(PER_RUN * slots, DAILY_BUDGET - used24))
    print(f"今回の問い合わせ枠 {BUDGET.left}回（直近24時間で {used24}/{DAILY_BUDGET}回 使用済み）")
    try:
        sync()
    except SyncError as e:
        st.update({"state": "temporary" if e.code == "temporary" else "error",
                   "code": e.code, "message": e.message, "at": now.isoformat(timespec="seconds")})
        save_status(st)
        print(f"[{e.code}] {e.message}\n{e.detail}")
        sys.exit(0 if e.code in ("temporary", "not_configured") else 2)   # 初期設定前は失敗扱いにしない

    if BUDGET.used:
        usage.append([now.isoformat(timespec="seconds"), BUDGET.used])
    st["usage"] = usage
    st["lastRun"] = dict(REPORT, at=now.isoformat(timespec="seconds"), requests=BUDGET.used,
                         used24=sum(n for _, n in usage), budget=DAILY_BUDGET,
                         coolingUntil=RATE["until"] or (st.get("cooldownUntil") if cooling() else "") or "")
    if RATE["until"]:
        st["cooldownUntil"] = RATE["until"]
    elif st.get("cooldownUntil") and not cooling():
        st.pop("cooldownUntil", None)
    h = token_hash()
    if h and st.get("tokenHash") != h:
        st["tokenHash"] = h
        st["tokenSince"] = now.date().isoformat()   # 連携を始めた日（期限の目安に使う）
    st.update({"state": "ok", "code": "", "message": "", "at": now.isoformat(timespec="seconds"),
               "lastSuccess": now.isoformat(timespec="seconds")})
    save_status(st)


if __name__ == "__main__":
    main()
