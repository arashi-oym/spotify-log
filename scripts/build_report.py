#!/usr/bin/env python3
"""data/ の記録から、専用ページ docs/index.html を作ります。
ページのデザインは scripts/template.html にあり、ここではデータを埋め込むだけです。
（集計はページを開いたときにブラウザ側で行います）
"""
import csv
import html
import json
import os
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))
CSV_PATH = ROOT / "data" / "plays.csv"
ARTISTS_PATH = ROOT / "data" / "artists.json"
STATUS_PATH = ROOT / "data" / "status.json"
DATA = ROOT / "data"                               # 取り寄せた過去の履歴は data/history*.csv（いくつあってもOK）
TRACKS_PATH = ROOT / "data" / "tracks.json"
EXCLUDE_PATH = ROOT / "data" / "exclude.json"   # 集計から外す再生（身に覚えのない再生など）
TEMPLATE = Path(__file__).resolve().parent / "template.html"
README = ROOT / "README.md"
DOCS = ROOT / "docs"
DOWNLOADS = DOCS / "downloads"
WORKFLOW = ROOT / ".github" / "workflows" / "spotify.yml"



def read_json(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError:
            pass
    return {}


SPOTIFY_ID = re.compile(r"^[0-9A-Za-z]{22}$")


def read_csv(path, source):
    rows = []
    if not path.exists():
        return rows
    with path.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                dt = datetime.strptime(r["played_at"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            rows.append({
                "dt": dt, "track_id": r.get("track_id") or r.get("track") or "",
                "track": r.get("track") or "", "artists": r.get("artists") or "",
                "album": r.get("album") or "",
                "image": r.get("image") or "", "artist_ids": r.get("artist_ids") or "",
                "ms": int(float(r.get("duration_ms") or 0)), "src": source,
                "has_id": bool(SPOTIFY_ID.match(r.get("track_id") or "")),   # 曲の番号つき（拡張ストリーミング履歴・自動記録）
            })
    return rows


def history_paths():
    return sorted(p for p in DATA.glob("history*.csv") if p.is_file())


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


def load_rows():
    """いまの記録（plays.csv）と、取り寄せた過去の履歴（history*.csv）を合わせる。
    同じ再生を二重に数えないよう、merge_history で重なりを取り除く。"""
    live = read_csv(CSV_PATH, "live")
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


def excluded_plays():
    """data/exclude.json に載っている再生（日時と曲IDの組）を返す"""
    data, out = read_json(EXCLUDE_PATH), set()
    for g in data.get("groups", []):
        for p in g.get("plays", []):
            if len(p) >= 2:
                out.add((p[0][:19] + "Z", p[1]))
    return out


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


def parse_list(lines, i, indent):
    tag = "ol" if LI.match(lines[i]).group(2)[0].isdigit() else "ul"
    items = []
    while i < len(lines):
        m = LI.match(lines[i])
        if not m:
            if not lines[i].strip():
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                mm = LI.match(lines[j]) if j < len(lines) else None
                if mm and len(mm.group(1)) >= indent and items:
                    i = j
                    continue
                break
            ind = len(lines[i]) - len(lines[i].lstrip())
            if ind > indent and items:
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
    return f"<{tag}>{body}</{tag}>", i


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
            head, body = rows[0], [r for r in rows[1:] if not all(set(c) <= set("-: ") for c in r)]
            t = "<div class=\"tbl-scroll\"><table class=\"bt\"><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
            t += "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body)
            out.append(t + "</tbody></table></div>")
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
# ページから「ファイル一式（zip）」と「再生記録（CSV）」を取得できるようにする。
# zip の中身は、GitHubのリポジトリと同じ構成にしている。

FIXED_TIME = (2026, 1, 1, 0, 0, 0)   # 中身が同じなら zip も同じになるよう日時を固定


def add_file(z, arcname, data, executable=False):
    info = zipfile.ZipInfo(arcname, date_time=FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = ((0o755 if executable else 0o644) | 0o100000) << 16
    z.writestr(info, data)


SCRIPTS = Path(__file__).resolve().parent
KIT_FILES = ["fetch.py", "build_report.py", "auth.py", "import_history.py", "template.html",
             "setup.html", "import.html", "page-common.css"]


def build_downloads(rows):
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    # ファイル一式：GitHubのリポジトリと同じ構成（個人の記録 data/ は含めない）
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


def main():
    rows = load_rows()
    songs, index, plays, prev = [], {}, [], 0
    for r in rows:
        # 同じ曲がシングルとアルバムなどで別の番号になっていることがあるので、
        # 「曲名＋メインのアーティスト」が同じなら同じ曲としてまとめる
        k = (r["track"].strip().lower(), r["artists"].split(" / ")[0].strip().lower()) if r["track"] else r["track_id"]
        # 曲名・アーティストは新しい記録のもので上書き（名前が変わった曲に対応）
        if k not in index:
            index[k] = len(songs)
            songs.append([r["track"], r["artists"], short_img(r["image"]), round(r["ms"] / 60000, 2)])
        else:
            sg = songs[index[k]]
            if r["src"] == "live":
                sg[0], sg[1] = r["track"] or sg[0], r["artists"] or sg[1]
            if r["image"] and not sg[2]:
                sg[2] = short_img(r["image"])
        t = int(r["dt"].timestamp())
        plays += [t - prev, index[k]]     # 容量を減らすため、前の再生からの秒数で保存
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
        },
        "guide": f"https://github.com/{repo}#止まったときの直し方",
        "guideHtml": guide_html,
        "guideAnchors": guide_anchors,
        "actions": f"https://github.com/{repo}/actions",
        "setup": "setup.html", "importPage": "import.html",
        "downloads": {"zip": "downloads/listening-log-kit.zip", "csv": "downloads/plays.csv", "rows": len(rows)},
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload, 1)
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    write_tool_pages(repo)
    build_downloads(rows)
    print(f"docs/index.html を更新しました（再生 {len(rows):,}件 / 曲 {len(songs):,}曲）")


if __name__ == "__main__":
    main()
