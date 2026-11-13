#!/usr/bin/env python3
"""data/ の記録から、専用ページ（docs/）を作ります。
ページのデザインは scripts/template.html にあり、ここではデータを入れるだけです。
（集計はページを開いたときにブラウザ側で行います）

再生の記録は「年ごとのファイル」（docs/d/2026-…json など）に分けて置きます。
ファイル名には中身から作った「指紋」（SHA-256）が入っていて、ページは読み込んだファイルの指紋と件数を
確かめてから表示します。作り終えたら、書き出したファイルを読み直して元のデータと1件も違わないことを確かめ、
違いがあればページを公開しません（前のページがそのまま残ります）。
"""
import csv
import hashlib
import html
import json
import os
import re
import shutil
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from records import ARTISTS_PATH, JST, STATUS_PATH, TRACKS_PATH, excluded_plays, history_paths, load_live, read_json

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).resolve().parent / "template.html"
README = ROOT / "README.md"
DOCS = ROOT / "docs"
DOWNLOADS = DOCS / "downloads"
PARTS = DOCS / "d"                                  # 年ごとのデータファイルの置き場所
WORKFLOW = ROOT / ".github" / "workflows" / "spotify.yml"
SPOTIFY_ID = re.compile(r"^[0-9A-Za-z]{22}$")


def to_row(r, source):
    """記録の1行 → 集計用の形（日時が読めない行は None）"""
    try:
        dt = datetime.strptime(r["played_at"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "dt": dt, "track_id": r.get("track_id") or r.get("track") or "",
        "track": r.get("track") or "", "artists": r.get("artists") or "",
        "album": r.get("album") or "",
        "image": r.get("image") or "", "artist_ids": r.get("artist_ids") or "",
        "ms": int(float(r.get("duration_ms") or 0)), "src": source,
        "has_id": bool(SPOTIFY_ID.match(r.get("track_id") or "")),   # 曲の番号つき（拡張ストリーミング履歴・自動記録）
    }


def read_csv(path, source):
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [x for x in (to_row(r, source) for r in csv.DictReader(f)) if x]


def norm(text):
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def base_title(text):
    """「In The Dark - From the Netflix Series…」「Wait So Long (Why Do I Have To)」→ 本来の曲名部分
    （取り寄せたデータの種類によって、曲名の付け方が少し違うため）"""
    return re.split(r" - | \(| \[| feat\. ", norm(text))[0]


def merge_history(live, hist, spans):
    """取り寄せた過去の履歴のうち、いまの記録（live）と重ならない再生だけを残す。
    ・曲の番号つき（拡張ストリーミング履歴）：再生が終わった時刻（秒）と曲が一致すれば同じ再生
      （Spotifyの自動記録と拡張ストリーミング履歴は、同じ再生なら同じ時刻になる）
    ・番号なし（アカウントデータ・分単位）：拡張ストリーミング履歴がある期間（spans）は使わない
      （拡張の方が正確で、プライベートセッションも見分けられるため）。
      それ以外の期間は、前後1分以内に同じ曲（または同じアーティスト）の再生があれば同じ再生とみなす
    いまの記録が止まっていた期間や、記録しきれなかった再生は、過去の履歴で埋まる。"""
    keys = {(r["dt"], r["track_id"]) for r in live}      # 時刻（秒）と曲の番号
    named = {(r["dt"], norm(r["track"])) for r in live}   # 時刻（秒）と曲名（番号が違っても同じ曲なら同じ再生）
    near = {}                                              # 分 → その前後に終わった再生（曲名・アーティスト）
    def mark(r):
        m = int(r["dt"].timestamp()) // 60
        for k in (m - 1, m, m + 1):
            near.setdefault(k, set()).update({"t:" + base_title(r["track"]), "a:" + norm(r["artists"].split(" / ")[0])})
    for r in live:
        mark(r)
    precise = [r for r in hist if r["has_id"]]
    rough = [r for r in hist if not r["has_id"]]
    out, rough_seen = [], set()
    for r in precise:                      # 番号つきを優先
        if (r["dt"], r["track_id"]) in keys or (r["dt"], norm(r["track"])) in named:
            continue
        keys.add((r["dt"], r["track_id"]))
        mark(r)
        out.append(r)
    for r in rough:
        if any(a <= r["dt"] <= b for a, b in spans):
            continue                       # 拡張ストリーミング履歴の期間内
        m = int(r["dt"].timestamp()) // 60
        here = near.get(m, ())
        if "t:" + base_title(r["track"]) in here or "a:" + norm(r["artists"].split(" / ")[0]) in here:
            continue                       # いまの記録と重なる
        key = (m, norm(r["track"]))
        if key in rough_seen:              # 別のファイルで取り込み済み
            continue
        rough_seen.add(key)
        out.append(r)
    return out


REMIND_DAYS = 180   # 取り込み済みの履歴の最終日から、この日数がたったら「取り寄せの時期」を知らせる（連携のやり直しと同じ半年ごと）


def history_spans():
    """取り込んだ「拡張ストリーミング履歴」がカバーしている期間（曲の番号つきの行の最初〜最後）"""
    spans = []
    for path in history_paths():
        ids = [r["dt"] for r in read_csv(path, "history") if r["has_id"]]
        if ids:
            spans.append((min(ids), max(ids)))
    return spans


def fetch_reminder(st):
    """取り寄せのお知らせ用：取り込み済みの最終日と、まだ埋まっていない「欠けているかもしれない期間」"""
    spans = history_spans()
    def covered(g):
        try:
            a = datetime.fromisoformat(g["from"].replace("Z", "+00:00"))
            b = datetime.fromisoformat(g["to"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            return True
        return any(x <= a and b <= y for x, y in spans)
    gaps = [g for g in st.get("gaps", []) if not covered(g)]
    until = max((y for _, y in spans), default=None)
    return {"historyUntil": until.isoformat() if until else None, "remindDays": REMIND_DAYS,
            "gaps": [{"from": to_ms(g["from"].replace("Z", "+00:00")), "to": to_ms(g["to"].replace("Z", "+00:00"))} for g in gaps]}


def load_rows():
    """いまの記録（plays-（年）.csv）と、取り寄せた過去の履歴（history*.csv）を合わせる。
    同じ再生を二重に数えないよう、merge_history で重なりを取り除く。"""
    live = [x for x in (to_row(r, "live") for r in load_live()) if x]
    hist, spans = [], []
    for path in history_paths():
        rows = read_csv(path, "history")
        ids = [r["dt"] for r in rows if r["has_id"]]
        if ids:
            spans.append((min(ids), max(ids)))   # 拡張ストリーミング履歴がカバーしている期間
        hist += rows
    hist = merge_history(live, hist, spans)
    tracks = read_json(TRACKS_PATH)
    for r in hist + live:
        info = tracks.get(r["track_id"]) or {}
        if info.get("artists") and r["src"] == "history":
            r["artists"] = info["artists"]          # 参加アーティストまで含めた名前に置き換え
        r["image"] = r["image"] or info.get("image", "")
        r["artist_ids"] = r["artist_ids"] or info.get("artist_ids", "")
        if info.get("duration_ms"):
            r["ms"] = int(info["duration_ms"])
    rows = hist + live
    ex = excluded_plays()
    if ex:
        before = len(rows)
        rows = [r for r in rows if (r["dt"].strftime("%Y-%m-%dT%H:%M:%SZ"), r["track_id"]) not in ex]
        print(f"除外リストにより {before - len(rows):,}件の再生を集計から外しました")
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


IMG_PREFIX = "https://i.scdn.co/image/"


def short_img(url):
    return "~" + url[len(IMG_PREFIX):] if url.startswith(IMG_PREFIX) else url



def to_ms(iso):
    try:
        return int(datetime.fromisoformat(iso).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


# ---------------- README → ページ内の説明書 ----------------
# README.md をページ用のHTMLに変換します（このREADMEで使っている書き方だけに対応した簡易版）。

LI = re.compile(r"^(\s*)([-*]|\d+\.)\s+(.*)$")


def anchor(text):
    """GitHubと同じ規則で見出しのリンク名を作る"""
    t = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return "g-" + t.replace(" ", "-")


def inline(text):
    parts = text.split("`")
    out = []
    for n, part in enumerate(parts):
        if n % 2:
            out.append("<code>" + html.escape(part) + "</code>")
            continue
        t = html.escape(part, quote=False)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)

        def link(m):
            label, url = m.group(1), m.group(2)
            if url.startswith("#"):
                return f'<a href="#g-{url[1:]}" data-jump>{label}</a>'
            return f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{label}</a>'
        t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, t)
        out.append(t)
    return "".join(out)


def table_html(rows):
    head, body = rows[0], [r for r in rows[1:] if not all(set(c) <= set("-: ") for c in r)]
    t = "<div class=\"tbl-scroll\"><table class=\"bt\"><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
    t += "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body)
    return t + "</tbody></table></div>"


def block_in_item(lines, i):
    """箇条書きの中に字下げして書いたコード（```）や表（|）を読み取る"""
    s = lines[i].strip()
    if s.startswith("```"):
        j, code = i + 1, []
        while j < len(lines) and not lines[j].strip().startswith("```"):
            code.append(lines[j].strip())
            j += 1
        return "<pre><code>" + html.escape("\n".join(code)) + "</code></pre>", j + 1
    if s.startswith("|"):
        rows = []
        while i < len(lines) and lines[i].strip().startswith("|"):
            rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
            i += 1
        return table_html(rows), i
    return None, i


def parse_list(lines, i, indent):
    first = LI.match(lines[i]).group(2)
    tag = "ol" if first[0].isdigit() else "ul"
    start = int(first[:-1]) if tag == "ol" else 1
    items = []
    while i < len(lines):
        m = LI.match(lines[i])
        if not m:
            if not lines[i].strip():
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines) and items:
                    nxt = lines[j]
                    mm = LI.match(nxt)
                    nind = len(nxt) - len(nxt.lstrip())
                    if (mm and len(mm.group(1)) >= indent) or (not mm and nind > indent):
                        i = j
                        continue
                break
            ind = len(lines[i]) - len(lines[i].lstrip())
            if ind > indent and items:
                blk, i2 = block_in_item(lines, i)
                if blk:
                    items[-1][1].append(blk)
                    i = i2
                else:
                    items[-1][1].append("<br>" + inline(lines[i].strip()))
                    i += 1
                continue
            break
        ind = len(m.group(1))
        if ind < indent:
            break
        if ind > indent and items:
            sub, i = parse_list(lines, i, ind)
            items[-1][1].append(sub)
            continue
        items.append([inline(m.group(3)), []])
        i += 1
    body = "".join(f"<li>{t}{''.join(x)}</li>" for t, x in items)
    attr = f' start="{start}"' if start != 1 else ""
    return f"<{tag}{attr}>{body}</{tag}>", i


def markdown_to_html(md):
    lines = md.replace("\r\n", "\n").split("\n")
    out, i, anchors = [], 0, {}
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            j = i + 1
            code = []
            while j < len(lines) and not lines[j].startswith("```"):
                code.append(lines[j])
                j += 1
            out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
            i = j + 1
        elif re.match(r"^#{1,6} ", line):
            level = len(line) - len(line.lstrip("#"))
            text = line[level:].strip()
            aid = anchor(text)
            m = re.match(r"^([A-Z])\. ", text)
            if m:
                anchors[m.group(1)] = aid
            if text == "止まったときの直し方":
                anchors["fix"] = aid
            if text.startswith("はじめての設定"):
                anchors["setup"] = aid
            out.append(f'<h{level} id="{html.escape(aid)}">{inline(text)}</h{level}>')
            i += 1
        elif line.strip() == "---":
            out.append("<hr>")
            i += 1
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append(table_html(rows))
        elif line.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].startswith(">"):
                quote.append(inline(lines[i].lstrip(">").strip()))
                i += 1
            out.append("<blockquote><p>" + "<br>".join(quote) + "</p></blockquote>")
        elif LI.match(line):
            h, i = parse_list(lines, i, len(LI.match(line).group(1)))
            out.append(h)
        elif not line.strip():
            i += 1
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,6} |```|\||>|---\s*$)", lines[i]) and not LI.match(lines[i]):
                para.append(inline(lines[i].strip()))
                i += 1
            out.append("<p>" + "<br>".join(para) + "</p>")
    return "\n".join(out), anchors


def guide():
    if not README.exists():
        return "", {}
    return markdown_to_html(README.read_text(encoding="utf-8"))



# ---------------- ダウンロード用ファイル ----------------
# ページから「更新用ファイル」「友だちに渡すキット」「再生記録（CSV）」を取得できるようにする。
# zip の中身は、GitHubのリポジトリと同じ構成にしている。

FIXED_TIME = (2026, 1, 1, 0, 0, 0)   # 中身が同じなら zip も同じになるよう日時を固定


def add_file(z, arcname, data, executable=False):
    info = zipfile.ZipInfo(arcname, date_time=FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = ((0o755 if executable else 0o644) | 0o100000) << 16
    z.writestr(info, data)


SCRIPTS = Path(__file__).resolve().parent
VERSION = "v29"   # プログラムを更新したら数字を上げる（更新用ファイルの名前に使う）
KIT_FILES = ["fetch.py", "build_report.py", "records.py", "auth.py", "import_history.py", "template.html",
             "setup.html", "import.html", "page-common.css"]


def quota_info(st):
    """問い合わせ枠：直近24時間で使った回数、上限、次に回数が戻る時刻と回数、1回の確認で使える目安"""
    lr = st.get("lastRun") or {}
    try:
        base = datetime.fromisoformat(lr.get("at") or st.get("at"))
    except (TypeError, ValueError):
        return None
    usage = []
    for t, n in st.get("usage") or []:
        try:
            ts = datetime.fromisoformat(t)
        except (TypeError, ValueError):
            continue
        if ts > base - timedelta(hours=24):
            usage.append((ts, int(n)))
    usage.sort()
    nxt = None
    if usage:
        t0 = usage[0][0] + timedelta(hours=24)
        nxt = {"at": int(t0.timestamp() * 1000), "n": sum(n for t, n in usage if t == usage[0][0])}
    recent = sorted(n for _, n in usage[-6:]) or [0]
    return {"used": sum(n for _, n in usage), "budget": lr.get("budget") or 590,
            "perRun": recent[len(recent) // 2], "next": nxt, "at": int(base.timestamp() * 1000)}


def update_zip_name(repo):
    """更新用ファイルの名前（例：spotify-log-v15.zip）"""
    return f"{repo.split('/')[-1]}-{VERSION}.zip"


def build_downloads(rows, repo):
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    for old in DOWNLOADS.glob("*.zip"):
        old.unlink()                        # 古いバージョンのzipを残さない

    # 更新用ファイル：自分のリポジトリを最新版にするとき、そのままアップロードする分（README.md・scripts/・動かす設定）
    name = update_zip_name(repo)
    with zipfile.ZipFile(DOWNLOADS / name, "w") as z:
        top = name[:-4]
        add_file(z, f"{top}/README.md", README.read_bytes())
        if WORKFLOW.exists():
            add_file(z, f"{top}/workflow/spotify.yml", WORKFLOW.read_bytes())   # 見えるフォルダに入れる（.github は端末によって隠れて見えないため）
        for f in KIT_FILES:
            if (SCRIPTS / f).exists():
                add_file(z, f"{top}/scripts/{f}", (SCRIPTS / f).read_bytes())

    # 友だちに渡すキット：GitHubのリポジトリと同じ構成（個人の記録 data/ は含めない）
    files = [("README.md", README), (".gitignore", ROOT / ".gitignore"), (".github/workflows/spotify.yml", WORKFLOW)]
    files += [(f"scripts/{name}", SCRIPTS / name) for name in KIT_FILES]
    with zipfile.ZipFile(DOWNLOADS / "listening-log-kit.zip", "w") as z:
        for arc, path in files:
            if path.exists():
                add_file(z, f"listening-log-kit/{arc}", path.read_bytes())

    # 再生記録（過去の履歴も含む全件）：Excelで文字化けしないよう BOM付きUTF-8、日時は日本時間
    with (DOWNLOADS / "plays.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["再生日時（日本時間）", "曲名", "アーティスト", "アルバム", "曲の長さ（秒）", "記録元"])
        for r in rows:
            w.writerow([r["dt"].astimezone(JST).strftime("%Y-%m-%d %H:%M:%S"), r["track"], r["artists"], r["album"],
                        round(r["ms"] / 1000), "自動記録" if r["src"] == "live" else "取り寄せた履歴"])


def write_tool_pages(repo):
    """連携ページ（setup.html）と履歴の取り込みページ（import.html）を docs に置く"""
    css = (SCRIPTS / "page-common.css").read_text(encoding="utf-8")
    for name in ("setup.html", "import.html"):
        src = SCRIPTS / name
        if src.exists():
            page = src.read_text(encoding="utf-8").replace("/*__CSS__*/", css, 1).replace("__REPO__", repo)
            (DOCS / name).write_text(page, encoding="utf-8")


def split_data(data, song_year, play_year):
    """songs・p・artistImages を、年ごとのファイルとアーティスト写真のファイルに分ける。
    曲は初めて聴いた順に並んでいるので、年ごとに切り分けて順につなげれば元とまったく同じになる"""
    parts = {}
    for n, y in enumerate(song_year):
        parts.setdefault(y, {"songs": [], "p": []})["songs"].append(data["songs"][n])
    for n, y in enumerate(play_year):
        parts.setdefault(y, {"songs": [], "p": []})["p"] += data["p"][2 * n:2 * n + 2]
    files = [(str(y), parts[y]) for y in sorted(parts)]
    files.append(("artists", data["artistImages"]))
    return files


def fingerprint(raw):
    return hashlib.sha256(raw).hexdigest()


def write_parts(files):
    """データファイルを書き出し、ページが確かめるための一覧（名前・指紋・件数）を返す"""
    shutil.rmtree(PARTS, ignore_errors=True)       # 前回のファイルを残さない
    PARTS.mkdir(parents=True)
    listing = []
    for name, obj in files:
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        h = fingerprint(raw)
        path = f"d/{name}-{h[:16]}.json"             # 中身が変われば名前も変わる（古いページと新しいデータが混ざらない）
        (DOCS / path).write_bytes(raw)
        entry = {"f": path, "sha256": h, "bytes": len(raw)}
        if name == "artists":
            entry["artists"] = len(obj)
        else:
            entry.update(songs=len(obj["songs"]), plays=len(obj["p"]) // 2)
        listing.append(entry)
    return listing


def verify_output(data, rest):
    """書き出したページとデータファイルを読み直し、元のデータと1件も違わないことを確かめる"""
    page = (DOCS / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<script type="application/json" id="data">(.*?)</script>', page, re.S)
    if not m or "__DATA__" in page:
        raise SystemExit("確認エラー：ページにデータが入っていません")
    shell = json.loads(m.group(1))
    if shell != rest:
        raise SystemExit("確認エラー：ページに入れた情報が元と違います")
    songs, plays, artists = [], [], None
    for e in shell["files"]:
        raw = (DOCS / e["f"]).read_bytes()
        if fingerprint(raw) != e["sha256"] or len(raw) != e["bytes"]:
            raise SystemExit(f"確認エラー：{e['f']} の指紋が合いません")
        obj = json.loads(raw)
        if "artists" in e:
            artists = obj
        else:
            songs += obj["songs"]
            plays += obj["p"]
    if songs != data["songs"] or plays != data["p"] or artists != data["artistImages"]:
        raise SystemExit("確認エラー：データファイルをつなげた結果が元のデータと違います")
    print(f"確認OK：データファイル {len(shell['files'])}個を読み直し、元のデータと一致しました")


def main():
    rows = load_rows()
    songs, index, plays, prev = [], {}, [], 0
    song_year, play_year = [], []
    for r in rows:
        y = r["dt"].astimezone(JST).year
        # 同じ曲がシングルとアルバムなどで別の番号になっていることがあるので、
        # 「曲名＋メインのアーティスト」が同じなら同じ曲としてまとめる
        k = (r["track"].strip().lower(), r["artists"].split(" / ")[0].strip().lower()) if r["track"] else r["track_id"]
        # 曲名・アーティストは新しい記録のもので上書き（名前が変わった曲に対応）
        if k not in index:
            index[k] = len(songs)
            songs.append([r["track"], r["artists"], short_img(r["image"]), round(r["ms"] / 60000, 2)])
            song_year.append(y)
        else:
            sg = songs[index[k]]
            if r["src"] == "live":
                sg[0], sg[1] = r["track"] or sg[0], r["artists"] or sg[1]
            # ジャケ写は新しい記録のものを優先（Spotify側で画像が変わったら、次に聴いたときに反映）
            if r["image"] and (r["src"] == "live" or not sg[2]):
                sg[2] = short_img(r["image"])
        t = int(r["dt"].timestamp())
        plays += [t - prev, index[k]]     # 容量を減らすため、前の再生からの秒数で保存
        play_year.append(y)
        prev = t

    st = read_json(STATUS_PATH)
    guide_html, guide_anchors = guide()
    repo = os.environ.get("GITHUB_REPOSITORY", "your-name/listening-log")
    data = {
        "songs": songs,
        "p": plays,
        "imgPrefix": IMG_PREFIX,
        "artistImages": {k: short_img(v) for k, v in artist_images(rows).items()},
        "status": {
            "state": st.get("state", "ok"),
            "code": st.get("code", ""),
            "message": st.get("message", ""),
            "at": to_ms(st.get("at")),
            "lastSuccess": to_ms(st.get("lastSuccess")),
            "tokenSince": st.get("tokenSince", ""),
            "fetch": fetch_reminder(st),
            "lastExternal": to_ms(st.get("lastExternal")),
            "triggerExpires": os.environ.get("TRIGGER_TOKEN_EXPIRES", "").strip(),
            "progress": st.get("progress"),
            "quota": quota_info(st),
            "lastRun": dict(st.get("lastRun") or {}, at=to_ms((st.get("lastRun") or {}).get("at")),
                            coolingUntil=to_ms((st.get("lastRun") or {}).get("coolingUntil"))) if st.get("lastRun") else None,
        },
        "guide": f"https://github.com/{repo}#止まったときの直し方",
        "guideHtml": guide_html,
        "guideAnchors": guide_anchors,
        "actions": f"https://github.com/{repo}/actions",
        "setup": "setup.html", "importPage": "import.html",
        "downloads": {"zip": "downloads/listening-log-kit.zip", "update": "downloads/" + update_zip_name(repo),
                      "version": VERSION, "csv": "downloads/plays.csv", "rows": len(rows)},
    }
    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    rest = {k: v for k, v in data.items() if k not in ("songs", "p", "artistImages")}
    rest["files"] = write_parts(split_data(data, song_year, play_year))
    payload = json.dumps(rest, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload, 1)
    (DOCS / "index.html").write_text(page, encoding="utf-8")
    verify_output(data, rest)
    write_tool_pages(repo)
    build_downloads(rows, repo)
    print(f"docs/ を更新しました（再生 {len(rows):,}件 / 曲 {len(songs):,}曲）")


if __name__ == "__main__":
    main()
