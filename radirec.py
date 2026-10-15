#!/usr/bin/env python3
"""radirec - radiko 録音ツール（個人利用向け）

必要なもの: Python 3.8+ / ffmpeg（PATH に通しておく）
外部ライブラリ不要。

使い方:
  python3 radirec.py ui                             # ブラウザで操作する画面を開く
  python3 radirec.py stations                       # 自分のエリアの局一覧
  python3 radirec.py programs TBS [20261014]        # 番組表
  python3 radirec.py live TBS 60                    # 今から60分ライブ録音
  python3 radirec.py timefree TBS 202610140100 202610140300
  python3 radirec.py auto                           # config.json のキーワード・予約に一致した番組を自動録音
"""
import argparse
import base64
import datetime as dt
import json
import os
import re
import secrets
import shutil
import struct
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
DEFAULT_CONFIG = os.path.join(HERE, "config.json")
JST = dt.timezone(dt.timedelta(hours=9))
CHUNK_SEC = 300  # タイムフリーは5分単位で取得して結合する


class RadikoError(RuntimeError):
    pass


def log(*a):
    print(dt.datetime.now(JST).strftime("[%Y-%m-%d %H:%M:%S]"), *a, flush=True)


def now_str():
    return dt.datetime.now(JST).strftime("%Y%m%d%H%M%S")


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
        raise RadikoError("認証失敗: 日本国内からアクセスしてください")
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
    raise RadikoError(f"{station}: ストリームURLが見つかりません")


# ---------------- 録音 ----------------
# 音質について: radiko の音声は HE-AAC（約48kbps）。録音は再エンコードせず配信データを
# そのまま保存する（無劣化）。ただし配信の ADTS ヘッダーは SBR（高音域の再現）を
# 表せないため、そのまま m4a にすると Apple の再生環境（ミュージック/QuickTime/iPhone）は
# 24kHz の AAC として再生し 12kHz より上が欠ける。録音後に fix_sbr() で明示しておく。
FF_COPY = ["-vn", "-c:a", "copy", "-bsf:a", "aac_adtstoasc"]


def ffmpeg(token, url, out, duration=None):
    cmd = ["ffmpeg", "-loglevel", "error", "-y",
           "-headers", f"X-Radiko-AuthToken: {token}\r\n", "-i", url]
    if duration:
        cmd += ["-t", str(duration)]
    return subprocess.run(cmd + FF_COPY + [out]).returncode == 0


def media_segments(token, url):
    """プレイリストから (EXTINF行, セグメントURL) の一覧を返す。
    ffmpeg に URL を直接渡すと medialist を Range ヘッダー付きで取得し、radiko が
    中身の無い "200 OK" を返して失敗するので、ここは Python 側で取得する。"""
    master = http(url, {"X-Radiko-AuthToken": token})[0].decode()
    media_url = next(l for l in master.splitlines() if l.startswith("http"))
    lines = http(media_url, {"X-Radiko-AuthToken": token})[0].decode().splitlines()
    return [(lines[i - 1], l) for i, l in enumerate(lines)
            if l.startswith("http") and i and lines[i - 1].startswith("#EXTINF")]


def ffmpeg_progress(cmd, total, progress):
    """ffmpeg を走らせ、出力済みの秒数から進み具合を progress(0〜1) に渡す。"""
    proc = subprocess.Popen(cmd + ["-progress", "pipe:1", "-nostats"],
                            stdout=subprocess.PIPE, text=True)
    for line in proc.stdout:
        if progress and line.startswith("out_time_us=") and line[12:].strip().isdigit():
            progress(min(1.0, int(line[12:]) / 1e6 / total))
    return proc.wait() == 0


def safe(name):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_")[:80]


def out_path(outdir, station, ft, title=""):
    os.makedirs(outdir, exist_ok=True)
    name = f"{ft[:8]}_{ft[8:12]}_{station}" + (f"_{safe(title)}" if title else "")
    return os.path.join(outdir, name + ".m4a")


def strip_id3(data):
    """セグメント先頭の ID3 タグ（radiko のタイムスタンプ）を外して ADTS だけにする。"""
    if data[:3] == b"ID3" and len(data) >= 10:
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        return data[10 + size:]
    return data


def rec_live(station, minutes, outdir, title=""):
    """ライブは medialist を数秒おきに Python 側で取りに行き、新しいセグメントを
    順に書き足す（ffmpeg に URL を渡すと medialist の取得で失敗するため）。"""
    token, _ = authorize()
    base = playlist_base(station, timefree=False)
    url = f"{base}?station_id={station}&l=15&lsid={secrets.token_hex(16)}&type=b"
    out = out_path(outdir, station, now_str(), title)
    target = minutes * 60
    log(f"ライブ録音開始 {station} {minutes}分 -> {out}")
    seen, got, errors = set(), 0.0, 0
    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, "live.aac")
        with open(raw, "wb") as f:
            while got < target:
                try:
                    for inf, seg in media_segments(token, url):
                        if seg in seen:
                            continue
                        seen.add(seg)
                        f.write(strip_id3(http(seg)[0]))
                        got += float(inf[8:].split(",")[0])
                    errors = 0
                except (OSError, StopIteration, ValueError) as e:
                    errors += 1
                    log("ライブ取得エラー", e)
                    if errors >= 5:
                        break
                    token, _ = authorize()  # トークン切れの可能性があるので取り直す
                time.sleep(2)
        ok = got > 0 and subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", raw, "-t", str(target)] + FF_COPY + [out]
        ).returncode == 0
    if ok:
        fix_sbr(out)
    log("完了" if ok and got >= target else "失敗" if not ok else "途中まで保存", out)
    return ok


def rec_timefree(station, ft, to, outdir, title="", progress=None):
    """番組全体のセグメント一覧を集めて1本のプレイリストにし、ffmpeg 1回で保存する
    （重複セグメントを除き、中間ファイルや再結合を挟まない）。"""
    ft, to = ft.ljust(14, "0"), to.ljust(14, "0")
    token, _ = authorize()
    base = playlist_base(station, timefree=True)
    lsid = secrets.token_hex(16)
    t0 = dt.datetime.strptime(ft, "%Y%m%d%H%M%S")
    t1 = dt.datetime.strptime(to, "%Y%m%d%H%M%S")
    total = int((t1 - t0).total_seconds())
    out = out_path(outdir, station, ft, title)
    log(f"タイムフリー録音 {station} {ft}-{to} -> {out}")
    report = progress or (lambda p: None)

    segs, seen, seek = [], set(), t0
    while seek < t1:
        q = urllib.parse.urlencode({
            "station_id": station, "start_at": ft, "ft": ft, "end_at": to, "to": to,
            "seek": seek.strftime("%Y%m%d%H%M%S"), "l": CHUNK_SEC, "lsid": lsid, "type": "c",
        })
        for attempt in range(3):
            try:
                chunk = media_segments(token, f"{base}?{q}")
                break
            except (OSError, StopIteration) as e:
                if attempt == 2:
                    log("プレイリスト取得失敗", seek, e)
                    return False
                time.sleep(3)
        segs += [s for s in chunk if s[1] not in seen]
        seen.update(u for _, u in chunk)
        seek += dt.timedelta(seconds=CHUNK_SEC)
        report(0.1 * min(1.0, (seek - t0).total_seconds() / total))  # 一覧取得は全体の1割
    if not segs:
        log("セグメントが見つかりません")
        return False

    with tempfile.TemporaryDirectory() as tmp:
        m3u8, part = os.path.join(tmp, "all.m3u8"), os.path.join(tmp, "out.m4a")
        with open(m3u8, "w") as f:
            f.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:0\n")
            f.writelines(f"{inf}\n{u}\n" for inf, u in segs)
            f.write("#EXT-X-ENDLIST\n")  # 無いとライブ扱いで終わらない
        cmd = ["ffmpeg", "-loglevel", "error", "-y",
               "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
               "-i", m3u8, "-t", str(total)] + FF_COPY + [part]
        ok = ffmpeg_progress(cmd, total, lambda p: report(0.1 + 0.9 * p))
        if ok:
            fix_sbr(part)
            shutil.move(part, out)
    log("完了" if ok else "失敗", out)
    return ok


# ---------------- HE-AAC の明示 ----------------
AAC_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350]
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}


def _boxes(buf, start, end):
    i = start
    while i + 8 <= end:
        size = struct.unpack(">I", buf[i:i + 4])[0]
        if size < 8 or i + size > end:
            return
        yield i, size, bytes(buf[i + 4:i + 8])
        i += size


def _find_esds(buf, start, end, path):
    """moov 以下から mp4a/esds を探す。見つかれば (祖先のオフセット一覧, esds, mp4a) を返す。"""
    for off, size, typ in _boxes(buf, start, end):
        if typ in CONTAINERS:
            r = _find_esds(buf, off + 8, off + size, path + [off])
        elif typ == b"stsd":
            r = _find_esds(buf, off + 16, off + size, path + [off])
        elif typ == b"mp4a":
            r = next(((path + [off], o, off) for o, _, t in _boxes(buf, off + 36, off + size)
                      if t == b"esds"), None)
        else:
            continue
        if r:
            return r
    return None


def _desc(tag, payload):
    n = len(payload)
    return bytes([tag, 0x80 | (n >> 21) & 0x7F, 0x80 | (n >> 14) & 0x7F, 0x80 | (n >> 7) & 0x7F, n & 0x7F]) + payload


def _read_desc(buf, i):
    tag, i, n = buf[i], i + 1, 0
    for _ in range(4):
        b = buf[i]; i += 1
        n = (n << 7) | (b & 0x7F)
        if not b & 0x80:
            break
    return tag, i, n  # タグ, 中身の先頭, 中身の長さ


def decoded_format(path):
    """ffmpeg のデコーダーが実際に出す (サンプルレート, チャンネル数)。"""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                        "-show_entries", "stream=sample_rate,channels", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    try:
        rate, ch = r.stdout.strip().split(",")[:2]
        return int(rate), int(ch)
    except ValueError:
        return None


def fix_sbr(path):
    """暗黙の SBR（HE-AAC）を m4a 内の AudioSpecificConfig に明示的に書き込む。
    音声データには一切触れない。すでに明示済み・SBR なしのファイルは何もしない。"""
    with open(path, "rb") as f:
        buf = bytearray(f.read())
    top = {t: (o, s) for o, s, t in _boxes(buf, 0, len(buf))}
    if b"moov" not in top or b"mdat" not in top or top[b"moov"][0] < top[b"mdat"][0]:
        return False  # moov が先頭のファイルは stco のずれ補正が要るので対象外（ffmpeg の既定は末尾）
    mo, ms = top[b"moov"]
    found = _find_esds(buf, mo + 8, mo + ms, [mo])
    if not found:
        return False
    ancestors, eo, mp4a = found
    esize = struct.unpack(">I", buf[eo:eo + 4])[0]
    # esds: ES_Descriptor > DecoderConfigDescriptor > DecoderSpecificInfo(=ASC)
    tag, p, n = _read_desc(buf, eo + 12)
    flags = buf[p + 2]
    q = p + 3 + (2 if flags & 0x80 else 0)
    q += 1 + buf[q] if flags & 0x40 else 0
    q += 2 if flags & 0x20 else 0
    es_head = bytes(buf[p:q])
    tag, d, dn = _read_desc(buf, q)
    if tag != 4:
        return False
    dcd_head = bytes(buf[d:d + 13])
    tag, a, an = _read_desc(buf, d + 13)
    if tag != 5 or an < 2:
        return False
    asc, rest = bytes(buf[a:a + an]), bytes(buf[a + an:d + dn])
    after_dcd = bytes(buf[d + dn:p + n])
    aot, sr_idx, ch = asc[0] >> 3, ((asc[0] & 7) << 1) | (asc[1] >> 7), (asc[1] >> 3) & 0xF
    if aot != 2 or sr_idx >= len(AAC_RATES):
        return False  # LC 以外（明示済みなど）は触らない
    fmt = decoded_format(path)
    if not fmt or fmt[0] != AAC_RATES[sr_idx] * 2 or fmt[0] not in AAC_RATES:
        return False  # SBR が入っていない
    ext_aot = 29 if ch == 1 and fmt[1] == 2 else 5  # PS 付き（HE-AAC v2）なら 29
    bits = (ext_aot << 20) | (sr_idx << 16) | (ch << 12) | (AAC_RATES.index(fmt[0]) << 8) | (2 << 3)
    new_asc = (bits << 7).to_bytes(4, "big")  # 25bit を左詰め
    payload = buf[eo + 8:eo + 12] + _desc(3, es_head + _desc(4, dcd_head + _desc(5, new_asc) + rest) + after_dcd)
    new_esds = struct.pack(">I", 8 + len(payload)) + b"esds" + payload
    delta = len(new_esds) - esize
    buf[eo:eo + esize] = new_esds
    for off in ancestors:  # moov〜mp4a のサイズを更新
        struct.pack_into(">I", buf, off, struct.unpack(">I", buf[off:off + 4])[0] + delta)
    struct.pack_into(">I", buf, mp4a + 32, fmt[0] << 16)  # サンプルエントリのサンプルレート
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(buf)
    os.replace(tmp, path)
    return True


# ---------------- 設定・自動録音 ----------------
def load_config(path=DEFAULT_CONFIG):
    cfg = {"output_dir": "./recordings", "rules": [], "reservations": []}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def save_config(cfg, path=DEFAULT_CONFIG):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def resolve_outdir(cfg, config_path=DEFAULT_CONFIG):
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), cfg["output_dir"])


def done_path(outdir):
    return os.path.join(outdir, ".done.json")


def load_done(outdir):
    try:
        with open(done_path(outdir)) as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def mark_done(outdir, key):
    done = load_done(outdir) | {key}
    os.makedirs(outdir, exist_ok=True)
    with open(done_path(outdir), "w") as f:
        json.dump(sorted(done), f)


def prog_key(station, ft):
    return f"{station}_{ft}"


def matches(rule, prog):
    text = (prog["title"] + " " + prog["pfm"] + " " + prog["info"]).lower()
    return any(k.lower() in text for k in rule["keywords"] if k)


def pending_targets(cfg, done):
    """放送が終わっていて未録音の対象番組（キーワード一致 + 予約）を返す。"""
    now, seen, out = now_str(), set(done), []
    for r in cfg.get("reservations", []):
        if r["to"] <= now and prog_key(r["station"], r["ft"]) not in seen:
            seen.add(prog_key(r["station"], r["ft"]))
            out.append(r)
    for rule in cfg.get("rules", []):
        for st in rule["stations"]:
            for p in weekly_programs(st):
                key = prog_key(st, p["ft"])
                if key not in seen and p["to"] <= now and matches(rule, p):
                    seen.add(key)
                    out.append(p)
    return out


def auto(config_path):
    cfg = load_config(config_path)
    outdir = resolve_outdir(cfg, config_path)
    for p in pending_targets(cfg, load_done(outdir)):
        if rec_timefree(p["station"], p["ft"], p["to"], outdir, p["title"]):
            mark_done(outdir, prog_key(p["station"], p["ft"]))


# ---------------- CLI ----------------
def main():
    ap = argparse.ArgumentParser(description="radiko 録音ツール")
    ap.add_argument("-o", "--outdir", default=os.path.join(HERE, "recordings"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ui"); p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    sub.add_parser("stations")
    p = sub.add_parser("programs"); p.add_argument("station"); p.add_argument("date", nargs="?")
    p = sub.add_parser("live"); p.add_argument("station"); p.add_argument("minutes", type=float)
    p = sub.add_parser("timefree"); p.add_argument("station")
    p.add_argument("ft", help="開始 YYYYMMDDHHMM"); p.add_argument("to", help="終了 YYYYMMDDHHMM")
    p = sub.add_parser("auto"); p.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    a = ap.parse_args()

    try:
        if a.cmd == "ui":
            import ui
            ui.serve(a.port, open_browser=not a.no_browser)
        elif a.cmd == "stations":
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
    except RadikoError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
