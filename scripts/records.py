"""記録ファイルの読み書き（fetch.py と build_report.py が共通で使います）

自動記録は data/plays-2026.csv のように「日本時間の年」ごとのファイルに分けて保存します。
（毎回書き換わるのはその年のファイルだけになり、過去の年のファイルは二度と変わりません）
書き込んだあとは必ず読み直し、1行も違わないことを確かめてから置き換えます。
v27まで使っていた data/plays.csv が残っていれば、年ごとに分けて同じだと確かめたあとで消します。
"""
import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LEGACY_CSV = DATA / "plays.csv"                 # v27までの記録（1つのファイル）
ARTISTS_PATH = DATA / "artists.json"            # アーティスト写真
STATUS_PATH = DATA / "status.json"              # 前回の実行結果
TRACKS_PATH = DATA / "tracks.json"              # 過去の履歴の曲の情報（ジャケ写・参加アーティストなど）
EXCLUDE_PATH = DATA / "exclude.json"            # 集計から外す再生（身に覚えのない再生など）
FIELDS = ["played_at", "track_id", "track", "artists", "album", "duration_ms", "image", "artist_ids"]
JST = timezone(timedelta(hours=9))


class RecordError(Exception):
    """記録ファイルの中身が確かめられないとき（このときは何も書き換えずに止まる）"""


def read_json(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError:
            pass
    return {}


def history_paths():
    """取り寄せた過去の履歴のファイル（history.csv、history-20261015.csv など。いくつあってもOK）"""
    return sorted(p for p in DATA.glob("history*.csv") if p.is_file())


def excluded_plays():
    """data/exclude.json に載っている再生（日時と曲IDの組）"""
    out = set()
    for g in read_json(EXCLUDE_PATH).get("groups", []):
        for p in g.get("plays", []):
            if len(p) >= 2:
                out.add((p[0][:19] + "Z", p[1]))
    return out


def year_of(played_at):
    """再生日時（世界標準時の "2026-09-27T05:19:39Z"）→ 日本時間の年"""
    try:
        dt = datetime.strptime(played_at[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        raise RecordError(f"再生日時の形が正しくない行があります（{played_at!r}）")
    return dt.astimezone(JST).year


def year_paths():
    return sorted(DATA.glob("plays-[0-9][0-9][0-9][0-9].csv"))


def _read(path):
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [{k: (r.get(k) or "") for k in FIELDS} for r in csv.DictReader(f)]


def _text(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def _merge(paths):
    out = {}
    for path in paths:
        for r in _read(path):
            k = r["played_at"]
            if k in out and out[k] != r:
                raise RecordError(f"同じ再生（{k}）なのに中身が違う行が見つかりました（{path.name}）。どちらが正しいか決められないため止めました")
            out[k] = r
    return sorted(out.values(), key=lambda r: r["played_at"])


def load_live():
    """自動記録をすべて読む（年ごとのファイル＋残っていれば v27までの plays.csv）"""
    return _merge(([LEGACY_CSV] if LEGACY_CSV.exists() else []) + year_paths())


def save_live(rows):
    """年ごとのファイルに保存する。変わった年だけを書き換え、読み直して同じだと確かめてから置き換える"""
    # CSVに書くと数字も文字になるので、先に文字にそろえておく（読み直したときと同じ形で比べるため）
    rows = sorted(({k: "" if r.get(k) is None else str(r.get(k)) for k in FIELDS} for r in rows), key=lambda r: r["played_at"])
    if len({r["played_at"] for r in rows}) != len(rows):
        raise RecordError("同じ日時の再生が2件以上あります")
    years = {}
    for r in rows:
        years.setdefault(year_of(r["played_at"]), []).append(r)
    for path in year_paths():
        if int(path.stem[-4:]) not in years:
            raise RecordError(f"{path.name} の分の再生が、保存しようとした記録に入っていません")
    DATA.mkdir(parents=True, exist_ok=True)
    for y, part in sorted(years.items()):
        path = DATA / f"plays-{y}.csv"
        text = _text(part)
        if path.exists() and path.read_bytes() == text.encode("utf-8"):
            continue                                   # 中身が同じ年は書き直さない
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_bytes(text.encode("utf-8"))
            if _read(tmp) != part:
                raise RecordError(f"{path.name} を書き込んだあとの読み直しで違いが見つかりました")
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()                           # 途中で失敗しても、書きかけのファイルを残さない
    if _merge(year_paths()) != rows:                  # 全体をもう一度読み直して確かめる
        raise RecordError("保存したあとの読み直しで、記録の件数か中身が合いませんでした")
    if LEGACY_CSV.exists():
        LEGACY_CSV.unlink()                            # 年ごとのファイルにすべて入ったと確かめたので、古いファイルは消す
        print(f"記録を年ごとのファイルに分けました（{', '.join(p.name for p in year_paths())}）")
