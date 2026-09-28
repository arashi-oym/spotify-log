#!/usr/bin/env python3
"""【最初に1回だけ Macで実行】Spotifyにログインして、リフレッシュトークンを取得します。
使い方: ターミナルで  python3 scripts/auth.py
"""
import base64
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPE = "user-read-recently-played"

client_id = input("Client ID を貼り付けて Enter: ").strip()
client_secret = input("Client Secret を貼り付けて Enter: ").strip()
state = secrets.token_urlsafe(16)

auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
    "client_id": client_id,
    "response_type": "code",
    "redirect_uri": REDIRECT_URI,
    "scope": SCOPE,
    "state": state,
})

result = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "error" in q:
            result["error"] = q["error"][0]
            msg = "認証がキャンセルされました。ターミナルに戻ってください。"
        elif q.get("state", [""])[0] == state and "code" in q:
            result["code"] = q["code"][0]
            msg = "認証できました。このタブは閉じて、ターミナルに戻ってください。"
        else:
            msg = ""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"<p style='font-size:20px'>{msg}</p>".encode("utf-8"))

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 8888), Handler)
print("\nブラウザでSpotifyのログイン画面を開きます。「同意する」を押してください…")
webbrowser.open(auth_url)
while "code" not in result and "error" not in result:
    server.handle_request()
server.server_close()

if "error" in result:
    raise SystemExit(f"認証に失敗しました: {result['error']}")

basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
req = urllib.request.Request(
    "https://accounts.spotify.com/api/token",
    data=urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": REDIRECT_URI,
    }).encode(),
    headers={
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    },
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        token = json.load(r)
except urllib.error.HTTPError as e:
    raise SystemExit("トークン取得に失敗しました:\n" + e.read().decode("utf-8", "replace"))

print("\n===== ここから下の1行が SPOTIFY_REFRESH_TOKEN です =====")
print(token["refresh_token"])
print("======================================================")
print("この値をGitHubのSecretに登録してください（他人に見せないでください）。")
