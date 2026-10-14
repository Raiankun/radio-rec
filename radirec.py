#!/usr/bin/env python3
"""radirec - radiko 録音ツール（個人利用向け）

必要なもの: Python 3.8+ / ffmpeg（PATH に通しておく）
外部ライブラリ不要。

使い方:
  python3 radirec.py stations                       # 自分のエリアの局一覧
  python3 radirec.py programs TBS [20261014]        # 番組表
  python3 radirec.py live TBS 60                    # 今から60分ライブ録音
  python3 radirec.py timefree TBS 202610140100 202610140300
  python3 radirec.py auto                           # config.json のキーワードに一致した番組を自動録音
"""
import argparse
import base64
import datetime as dt
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

AUTH_KEY = "bcd151073c03b352e1ef2fd66c32209da9ca0afa"  # pc_html5 公開キー
UA = "Mozilla/5.0"
HERE = os.path.dirname(os.path.abspath(__file__))
JST = dt.timezone(dt.timedelta(hours=9))
CHUNK_SEC = 300  # タイムフリーは5分単位で取得して結合する


def log(*a):
    print(dt.datetime.now(JST).strftime("[%Y-%m-%d %H:%M:%S]"), *a, flush=True)


def http(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(), r.headers


# ---------------- 認証 ----------------
def authorize():
    """(token, area_id) を返す。日本国内のIPからでないと失敗する。"""
    _, h = http("https://radiko.jp/v2/api/auth1", {
        "X-Radiko-App": "pc_html5", "X-Radiko-App-Version": "0.0.1",
        "X-Radiko-User": "dummy_user", "X-Radiko-Device": "pc",
    })
    token = h["X-Radiko-AuthToken"]
    off, length = int(h["X-Radiko-KeyOffset"]), int(h["X-Radiko-KeyLength"])
    partial = base64.b64encode(AUTH_KEY[off:off + length].encode()).decode()
    body, _ = http("https://radiko.jp/v2/api/auth2", {
        "X-Radiko-AuthToken": token, "X-Radiko-PartialKey": partial,
        "X-Radiko-User": "dummy_user", "X-Radiko-Device": "pc",
    })
    area = body.decode().split(",")[0].strip()
    if not area.startswith("JP"):
        sys.exit("認証失敗: 日本国内からアクセスしてください")
    return token, area


# ---------------- 情報取得 ----------------
def stations(area):
    root = ET.fromstring(http(f"https://radiko.jp/v3/station/list/{area}.xml")[0])
    return [(s.findtext("id"), s.findtext("name")) for s in root.iter("station")]


def weekly_programs(station):
    root = ET.fromstring(http(f"https://radiko.jp/v3/program/station/weekly/{station}.xml")[0])
    for p in root.iter("prog"):
        yield {
            "station": station,
            "ft": p.get("ft"), "to": p.get("to"),
            "title": p.findtext("title") or "",
            "pfm": p.findtext("pfm") or "",
            "info": re.sub(r"<[^>]+>", "", p.findtext("info") or ""),
        }


def playlist_base(station, timefree):
    root = ET.fromstring(http(f"https://radiko.jp/v3/station/stream/pc_html5/{station}.xml")[0])
    want = "1" if timefree else "0"
    for u in root.iter("url"):
        if u.get("timefree") == want and u.get("areafree") == "0":
            return u.findtext("playlist_create_url")
    sys.exit(f"{station}: ストリームURLが見つかりません")


# ---------------- 録音 ----------------
def ffmpeg(token, url, out, duration=None):
    cmd = ["ffmpeg", "-loglevel", "error", "-y",
           "-headers", f"X-Radiko-AuthToken: {token}\r\n", "-i", url]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-vn", "-c:a", "copy", "-bsf:a", "aac_adtstoasc", out]
    return subprocess.run(cmd).returncode == 0


def safe(name):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_")[:80]


def out_path(outdir, station, ft, title=""):
    os.makedirs(outdir, exist_ok=True)
    name = f"{ft[:8]}_{ft[8:12]}_{station}" + (f"_{safe(title)}" if title else "")
    return os.path.join(outdir, name + ".m4a")


def rec_live(station, minutes, outdir):
    token, _ = authorize()
    base = playlist_base(station, timefree=False)
    url = f"{base}?station_id={station}&l=15&lsid={secrets.token_hex(16)}&type=b"
    now = dt.datetime.now(JST).strftime("%Y%m%d%H%M%S")
    out = out_path(outdir, station, now)
    log(f"ライブ録音開始 {station} {minutes}分 -> {out}")
    ok = ffmpeg(token, url, out, duration=int(minutes * 60))
    log("完了" if ok else "失敗", out)
    return ok


def rec_timefree(station, ft, to, outdir, title=""):
    ft, to = ft.ljust(14, "0"), to.ljust(14, "0")
    token, _ = authorize()
    base = playlist_base(station, timefree=True)
    lsid = secrets.token_hex(16)
    t0 = dt.datetime.strptime(ft, "%Y%m%d%H%M%S")
    t1 = dt.datetime.strptime(to, "%Y%m%d%H%M%S")
    out = out_path(outdir, station, ft, title)
    log(f"タイムフリー録音 {station} {ft}-{to} -> {out}")

    with tempfile.TemporaryDirectory() as tmp:
        parts, seek = [], t0
        while seek < t1:
            q = urllib.parse.urlencode({
                "station_id": station, "start_at": ft, "ft": ft, "end_at": to, "to": to,
                "seek": seek.strftime("%Y%m%d%H%M%S"), "l": CHUNK_SEC, "lsid": lsid, "type": "c",
            })
            part = os.path.join(tmp, f"{len(parts):04d}.m4a")
            for attempt in range(3):
                if ffmpeg(token, f"{base}?{q}", part):
                    break
                time.sleep(3)
            else:
                log("チャンク取得失敗", seek)
                return False
            parts.append(part)
            seek += dt.timedelta(seconds=CHUNK_SEC)
        lst = os.path.join(tmp, "list.txt")
        with open(lst, "w") as f:
            f.writelines(f"file '{p}'\n" for p in parts)
        total = int((t1 - t0).total_seconds())
        ok = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                             "-i", lst, "-t", str(total), "-c", "copy", out]).returncode == 0
    log("完了" if ok else "失敗", out)
    return ok


# ---------------- 自動録音 ----------------
def auto(config_path):
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    outdir = cfg.get("output_dir", os.path.join(HERE, "recordings"))
    done_path = os.path.join(outdir, ".done.json")
    os.makedirs(outdir, exist_ok=True)
    done = set(json.load(open(done_path)) if os.path.exists(done_path) else [])
    now = dt.datetime.now(JST).strftime("%Y%m%d%H%M%S")

    for rule in cfg["rules"]:
        kws = [k.lower() for k in rule["keywords"]]
        for st in rule["stations"]:
            for p in weekly_programs(st):
                key = f"{st}_{p['ft']}"
                text = (p["title"] + " " + p["pfm"] + " " + p["info"]).lower()
                if key in done or p["to"] > now or not any(k in text for k in kws):
                    continue
                if rec_timefree(st, p["ft"], p["to"], outdir, p["title"]):
                    done.add(key)
                    with open(done_path, "w") as f:
                        json.dump(sorted(done), f)


# ---------------- CLI ----------------
def main():
    ap = argparse.ArgumentParser(description="radiko 録音ツール")
    ap.add_argument("-o", "--outdir", default=os.path.join(HERE, "recordings"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stations")
    p = sub.add_parser("programs"); p.add_argument("station"); p.add_argument("date", nargs="?")
    p = sub.add_parser("live"); p.add_argument("station"); p.add_argument("minutes", type=float)
    p = sub.add_parser("timefree"); p.add_argument("station")
    p.add_argument("ft", help="開始 YYYYMMDDHHMM"); p.add_argument("to", help="終了 YYYYMMDDHHMM")
    p = sub.add_parser("auto"); p.add_argument("-c", "--config", default=os.path.join(HERE, "config.json"))
    a = ap.parse_args()

    if a.cmd == "stations":
        _, area = authorize()
        print(f"エリア: {area}")
        for sid, name in stations(area):
            print(f"  {sid:12} {name}")
    elif a.cmd == "programs":
        for p in weekly_programs(a.station):
            if not a.date or p["ft"].startswith(a.date):
                print(f"{p['ft'][:12]}-{p['to'][8:12]}  {p['title']}  / {p['pfm']}")
    elif a.cmd == "live":
        sys.exit(0 if rec_live(a.station, a.minutes, a.outdir) else 1)
    elif a.cmd == "timefree":
        sys.exit(0 if rec_timefree(a.station, a.ft, a.to, a.outdir) else 1)
    elif a.cmd == "auto":
        auto(a.config)


if __name__ == "__main__":
    main()
