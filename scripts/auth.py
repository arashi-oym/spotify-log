#!/usr/bin/env python3
"""【Spotifyとの連携をやり直すとき】
「再認証.command」をダブルクリックすると、このファイルが動きます。

1. ブラウザでSpotifyの同意画面が開く →「同意する」を押す
2. 新しいリフレッシュトークンが自動でコピーされ、GitHubの設定画面が開く
3. 貼り付けて「Update secret」を押せば完了

Client ID と Client Secret は初回だけ入力すれば、この Mac に保存されて次回から聞かれません。
"""
import base64
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPE = "user-read-recently-played"
DEFAULT_REPO = "arashi-oym/spotify-log"
CONFIG = Path.home() / ".spotify-log.json"   # Client ID などの保存先（このMacの中だけ）


def say(msg=""):
    print(msg, flush=True)


def load_config():
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {}


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        CONFIG.chmod(0o600)
    except OSError:
        pass


def ask_config(cfg):
    say("初回の設定です。Spotify for Developers の画面（My Listening Log → Settings）を見ながら入力してください。")
    cfg["client_id"] = input("  Client ID を貼り付けて Enter: ").strip()
    cfg["client_secret"] = input("  Client Secret を貼り付けて Enter: ").strip()
    repo = input(f"  GitHubのリポジトリ名（そのまま Enter で {DEFAULT_REPO}）: ").strip()
    cfg["repo"] = repo or cfg.get("repo") or DEFAULT_REPO
    save_config(cfg)
    say("  → この Mac に保存しました。次回からは入力不要です。\n")
    return cfg


def get_code(client_id):
    state = secrets.token_urlsafe(16)
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "error" in q:
                result["error"] = q["error"][0]
                msg = "キャンセルされました。ターミナルに戻ってください。"
            elif q.get("state", [""])[0] == state and "code" in q:
                result["code"] = q["code"][0]
                msg = "✅ Spotifyとの連携ができました。このタブは閉じて、ターミナルに戻ってください。"
            else:
                msg = ""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<p style='font:20px sans-serif;padding:24px'>{msg}</p>".encode("utf-8"))

        def log_message(self, *args):
            pass

    try:
        server = HTTPServer(("127.0.0.1", 8888), Handler)
    except OSError:
        sys.exit("⚠️ ほかの「再認証」がまだ開いています。開いているターミナルの画面を閉じてから、もう一度実行してください。")

    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "scope": SCOPE, "state": state, "show_dialog": "true",
    })
    say("① ブラウザでSpotifyの画面を開きます。「同意する」を押してください…")
    webbrowser.open(url)
    while "code" not in result and "error" not in result:
        server.handle_request()
    server.server_close()
    if "error" in result:
        sys.exit("⚠️ 同意がキャンセルされました。もう一度「再認証.command」を開いてやり直してください。")
    return result["code"]


def get_refresh_token(cfg, code):
    basic = base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode({"grant_type": "authorization_code", "code": code,
                                     "redirect_uri": REDIRECT_URI}).encode(),
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["refresh_token"]


def copy_to_clipboard(text):
    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def main():
    say("=" * 56)
    say("  Spotify 再生記録 ─ 連携のやり直し（再認証）")
    say("=" * 56 + "\n")

    cfg = load_config()
    if not cfg.get("client_id") or not cfg.get("client_secret"):
        cfg = ask_config(cfg)

    code = get_code(cfg["client_id"])
    try:
        token = get_refresh_token(cfg, code)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        if "invalid_client" in detail:
            CONFIG.unlink(missing_ok=True)
            sys.exit("⚠️ 保存されていた Client ID / Client Secret が正しくありませんでした。\n"
                     "   保存内容を消したので、もう一度「再認証.command」を開いて入力し直してください。")
        sys.exit("⚠️ トークンの取得に失敗しました。\n" + detail)

    say("\n② 新しいリフレッシュトークンを取得しました。")
    repo = cfg.get("repo") or DEFAULT_REPO
    secret_url = f"https://github.com/{repo}/settings/secrets/actions/SPOTIFY_REFRESH_TOKEN"

    if copy_to_clipboard(token):
        say("   → 自動でコピーしました（まだ何も貼り付けなくてOKです）。\n")
        say("③ GitHubの設定画面を開きます。次の2つだけ行ってください。")
        say("   ・大きな入力欄をクリックして ⌘+V（貼り付け）")
        say("   ・緑の「Update secret」を押す")
        time.sleep(1.5)
        webbrowser.open(secret_url)
    else:
        say("   下の1行をコピーして、GitHubの SPOTIFY_REFRESH_TOKEN に貼り付けてください。")
        say("   " + token)
        say("   設定画面: " + secret_url)

    say("\nこれで完了です。1時間以内に自動で記録が再開します。")
    say("すぐに確かめたいときは、GitHubの「Actions」→「Spotify再生記録」→「Run workflow」を押してください。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\n中止しました。")
