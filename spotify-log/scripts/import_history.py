#!/usr/bin/env python3
"""Spotifyから取り寄せた「拡張ストリーミング履歴」を、この仕組みで使える形（data/history.csv）に変換します。

使い方（Macのターミナル）:
    cd ~/Documents/spotify-log
    python3 scripts/import_history.py ~/Downloads/my_spotify_data.zip

できた data/history.csv を、GitHubの data フォルダにアップロードすれば合流します。

・曲（音楽）だけを取り出し、ポッドキャストや動画は除きます
・30秒以上再生したものだけを「1回」と数えます（今の記録と同じ基準）
・プライベートセッション中の再生は除きます
・IPアドレスや端末名などの個人情報は書き出しません
"""
import csv
import json
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "history.csv"
FIELDS = ["played_at", "track_id", "track", "artists", "album", "duration_ms", "ms_played"]
MIN_MS = 30000
NAME = re.compile(r"Streaming_History_Audio_.*\.json$")


def read_records(src):
    src = Path(src).expanduser()
    recs = []
    if src.is_file() and src.suffix == ".zip":
        with zipfile.ZipFile(src) as z:
            for n in z.namelist():
                if NAME.search(n):
                    recs += json.loads(z.read(n).decode("utf-8"))
    else:
        for p in src.rglob("*.json"):
            if NAME.search(p.name):
                recs += json.loads(p.read_text(encoding="utf-8"))
    return recs


def main():
    if len(sys.argv) < 2:
        sys.exit("使い方: python3 scripts/import_history.py <my_spotify_data.zip または 展開したフォルダ>")
    recs = read_records(sys.argv[1])
    if not recs:
        sys.exit("Streaming_History_Audio_*.json が見つかりませんでした。「拡張ストリーミング履歴」のファイルか確認してください。")

    music = [r for r in recs if (r.get("spotify_track_uri") or "").startswith("spotify:track:") and not r.get("incognito_mode")]
    # 曲の長さの目安：その曲で一番長く再生された時間（最後まで聴いた回があれば、ほぼ曲の長さになる）
    longest = defaultdict(int)
    for r in music:
        tid = r["spotify_track_uri"].split(":")[-1]
        longest[tid] = max(longest[tid], int(r.get("ms_played") or 0))

    rows = []
    for r in music:
        ms = int(r.get("ms_played") or 0)
        if ms < MIN_MS or not r.get("ts"):
            continue
        tid = r["spotify_track_uri"].split(":")[-1]
        rows.append({
            "played_at": r["ts"][:19] + "Z",
            "track_id": tid,
            "track": r.get("master_metadata_track_name") or "",
            "artists": r.get("master_metadata_album_artist_name") or "",
            "album": r.get("master_metadata_album_album_name") or "",
            "duration_ms": longest[tid],
            "ms_played": ms,
        })
    rows.sort(key=lambda x: x["played_at"])
    # 同じ時刻の重複を除く
    uniq, seen = [], set()
    for r in rows:
        k = (r["played_at"], r["track_id"])
        if k not in seen:
            seen.add(k)
            uniq.append(r)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(uniq)
    print(f"{OUT} を作りました")
    print(f"  再生 {len(uniq):,}回（{uniq[0]['played_at'][:10]} 〜 {uniq[-1]['played_at'][:10]}）")
    print(f"  曲 {len({r['track_id'] for r in uniq}):,}曲")


if __name__ == "__main__":
    main()
