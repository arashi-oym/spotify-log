#!/usr/bin/env python3
"""Spotifyから取り寄せた再生履歴を、この仕組みで使える形（data/history-日付.csv）に変換します。

ふだんは専用ページの「過去の再生履歴を取り込む」ページ（import.html）を使ってください（スマホだけでできます）。
これはパソコンで変換したい場合の予備の方法です。

使い方:
    Mac      : python3 scripts/import_history.py ~/Downloads/my_spotify_data.zip
    Windows  : py scripts\\import_history.py %USERPROFILE%\\Downloads\\my_spotify_data.zip

できた data/history-日付.csv を、GitHubの data フォルダにアップロードすれば合流します。
前に取り込んだ履歴は上書きせず、別のファイルとして加わります（重なる期間は自動で二重に数えません）。

・「拡張ストリーミング履歴」（全期間）と「アカウントデータ」（直近1年分）のどちらにも対応
・曲（音楽）だけを取り出し、ポッドキャストや動画は除きます
・30秒以上再生したものだけを「1回」と数えます（自動記録と同じ基準）
・プライベートセッション中の再生は除きます
・IPアドレスや端末名などの個人情報は書き出しません
"""
import csv
import json
import re
import sys
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / ("history-" + datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d") + ".csv")
FIELDS = ["played_at", "track_id", "track", "artists", "album", "duration_ms", "ms_played"]
MIN_MS = 30000
EXT = re.compile(r"Streaming_History_Audio_.*\.json$", re.I)      # 拡張ストリーミング履歴
ACC = re.compile(r"StreamingHistory_?music_.*\.json$", re.I)       # アカウントデータ


def read_files(src):
    src = Path(src).expanduser()
    ext, acc = [], []
    if src.is_file() and src.suffix.lower() == ".zip":
        with zipfile.ZipFile(src) as z:
            for n in z.namelist():
                name = n.split("/")[-1]
                if EXT.search(name):
                    ext += json.loads(z.read(n).decode("utf-8"))
                elif ACC.search(name):
                    acc += json.loads(z.read(n).decode("utf-8"))
    else:
        for p in src.rglob("*.json"):
            if EXT.search(p.name):
                ext += json.loads(p.read_text(encoding="utf-8"))
            elif ACC.search(p.name):
                acc += json.loads(p.read_text(encoding="utf-8"))
    return ext, acc


def from_extended(recs):
    out = []
    for r in recs:
        uri = r.get("spotify_track_uri") or ""
        if not uri.startswith("spotify:track:") or r.get("incognito_mode"):
            continue
        ms = int(r.get("ms_played") or 0)
        if ms < MIN_MS or not r.get("ts"):
            continue
        out.append({"played_at": r["ts"][:19] + "Z", "track_id": uri.split(":")[-1],
                    "track": r.get("master_metadata_track_name") or "",
                    "artists": r.get("master_metadata_album_artist_name") or "",
                    "album": r.get("master_metadata_album_album_name") or "", "ms_played": ms})
    return out


def from_account(recs):
    out = []
    for r in recs:
        ms = int(r.get("msPlayed") or 0)
        if ms < MIN_MS or not r.get("endTime") or not r.get("trackName"):
            continue
        out.append({"played_at": r["endTime"].replace(" ", "T")[:16] + ":00Z", "track_id": "",
                    "track": r["trackName"], "artists": r.get("artistName") or "", "album": "", "ms_played": ms})
    return out


def main():
    if len(sys.argv) < 2:
        sys.exit("使い方: python3 scripts/import_history.py <my_spotify_data.zip または 展開したフォルダ>")
    ext, acc = read_files(sys.argv[1])
    if ext:
        rows, kind = from_extended(ext), "拡張ストリーミング履歴（全期間）"
    elif acc:
        rows, kind = from_account(acc), "アカウントデータ（直近1年分）"
    else:
        sys.exit("再生履歴のファイルが見つかりませんでした。Spotifyから届いた my_spotify_data.zip を指定してください。")

    def key(r):
        return r["track_id"] or (r["track"], r["artists"])
    longest = defaultdict(int)   # 曲の長さの目安：その曲でいちばん長く再生された時間
    for r in rows:
        longest[key(r)] = max(longest[key(r)], r["ms_played"])
    rows.sort(key=lambda r: r["played_at"])
    seen, uniq = set(), []
    for r in rows:
        k = (r["played_at"], key(r))
        if k not in seen:
            seen.add(k)
            r["duration_ms"] = longest[key(r)]
            uniq.append(r)
    if not uniq:
        sys.exit("30秒以上再生した曲の記録が見つかりませんでした。")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(uniq)
    print(f"{OUT} を作りました（{kind}）")
    print(f"  再生 {len(uniq):,}回（{uniq[0]['played_at'][:10]} 〜 {uniq[-1]['played_at'][:10]}）")
    print(f"  曲 {len({key(r) for r in uniq}):,}曲")


if __name__ == "__main__":
    main()
