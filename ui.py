"""radirec の操作画面（ブラウザUI）。`python3 radirec.py ui` で起動する。

127.0.0.1 でのみ待ち受けるので、同じPCのブラウザからしか開けない。
起動している間は30分おきに自動録音（キーワード・予約）もチェックする。
"""
import itertools
import json
import mimetypes
import os
import queue
import re
import threading
import time
import traceback
import unicodedata
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import radirec as rr

AUTO_INTERVAL = 30 * 60
AUDIO_EXTS = (".m4a", ".mp3")
HTML = os.path.join(rr.HERE, "ui.html")

lock = threading.Lock()
jobs = []  # 新しい順
job_ids = itertools.count(1)
tf_queue = queue.Queue()  # タイムフリーは1件ずつ順番に処理
program_cache = {}  # station -> (取得時刻, 番組一覧)
PROGRAM_TTL = 10 * 60
state = {"area": None, "stations": [], "error": None, "auto_last": None, "auto_next": None}


def cfg():
    return rr.load_config()


def outdir():
    return rr.resolve_outdir(cfg())


# ---------------- ジョブ ----------------
def new_job(kind, station, title, ft="", to="", minutes=None):
    job = {"id": next(job_ids), "kind": kind, "station": station, "title": title,
           "ft": ft, "to": to, "minutes": minutes, "status": "queued",
           "progress": 0.0, "error": "", "created": rr.now_str()}
    with lock:
        jobs.insert(0, job)
        del jobs[200:]
    return job


def active_keys():
    with lock:
        return {rr.prog_key(j["station"], j["ft"]) for j in jobs
                if j["kind"] == "timefree" and j["status"] in ("queued", "running")}


def enqueue_timefree(station, ft, to, title):
    if rr.prog_key(station, ft) in active_keys():
        return None
    job = new_job("timefree", station, title, ft, to)
    tf_queue.put(job)
    return job


def run_job(job, fn):
    job["status"] = "running"
    try:
        ok = fn()
        job["status"] = "done" if ok else "failed"
        if not ok:
            job["error"] = "ffmpeg が失敗しました（ログを確認）"
    except Exception as e:  # noqa: BLE001 - UIに理由を出すため全部拾う
        traceback.print_exc()
        job["status"], job["error"] = "failed", str(e)
    if job["status"] == "done":
        job["progress"] = 1.0


def timefree_worker():
    while True:
        job = tf_queue.get()

        def fn(job=job):
            d = outdir()
            ok = rr.rec_timefree(job["station"], job["ft"], job["to"], d, job["title"],
                                 progress=lambda p: job.update(progress=p))
            if ok:
                rr.mark_done(d, rr.prog_key(job["station"], job["ft"]))
            return ok
        run_job(job, fn)


def start_live(station, minutes, title):
    job = new_job("live", station, title, ft=rr.now_str(), minutes=minutes)
    start = time.time()

    def ticker():
        while job["status"] in ("queued", "running"):
            job["progress"] = min(0.99, (time.time() - start) / (minutes * 60))
            time.sleep(2)

    def fn():
        threading.Thread(target=ticker, daemon=True).start()
        return rr.rec_live(station, minutes, outdir(), title)
    threading.Thread(target=run_job, args=(job, fn), daemon=True).start()
    return job


def start_stream(sid, minutes, title, label=None):
    st = rr.find_stream(cfg(), sid)
    if not st:
        raise ValueError(f"海外局 {sid} が登録されていません")
    job = new_job("stream", sid, title or st["name"], ft=rr.now_str(), minutes=round(minutes, 1))
    job["label"] = label or "海外局ライブ"

    def fn():
        return rr.rec_stream(st, minutes, outdir(), title, progress=lambda p: job.update(progress=p))
    threading.Thread(target=run_job, args=(job, fn), daemon=True).start()
    return job


started_schedules = set()  # (予約のキー, 開始時刻) — 同じ回を二重に始めないため


def schedule_key(sc):
    return json.dumps({k: sc.get(k) for k in ("stream", "time", "minutes", "days", "date")}, sort_keys=True)


def schedule_loop():
    while True:
        try:
            for sc, start, left in rr.due_schedules(cfg()):
                key = (schedule_key(sc), start.isoformat())
                if key in started_schedules:
                    continue
                started_schedules.add(key)
                rr.log(f"予約録音を開始 {sc['stream']} {sc['time']} 残り{left:.0f}分")
                start_stream(sc["stream"], left, sc.get("title", ""), label="予約録音")
        except Exception as e:  # noqa: BLE001
            rr.log("予約チェック失敗:", e)
        time.sleep(15)


def save_streams(streams):
    c, seen, out = cfg(), set(), []
    for st in streams:
        name, url = st.get("name", "").strip(), st.get("url", "").strip()
        if not name or not url.startswith(("http://", "https://")):
            continue
        sid = st.get("id") or rr.safe(name).lower()[:24] or "stream"
        base, n = sid, 2
        while sid in seen:
            sid, n = f"{base}{n}", n + 1
        seen.add(sid)
        out.append({"id": sid, "name": name, "url": url, "tz": st.get("tz") or ""})
    c["streams"] = out
    rr.save_config(c)


def save_schedules(schedules):
    c, out = cfg(), []
    ids = {s["id"] for s in c["streams"]}
    for sc in schedules:
        if sc.get("stream") not in ids or not re.fullmatch(r"\d{1,2}:\d{2}", sc.get("time", "")):
            continue
        item = {"stream": sc["stream"], "time": sc["time"], "minutes": max(1, int(sc.get("minutes") or 60)),
                "title": sc.get("title", "").strip()}
        if sc.get("date"):
            item["date"] = sc["date"].replace("-", "")
        elif sc.get("days"):
            item["days"] = sorted({int(d) for d in sc["days"] if 0 <= int(d) <= 6})
        else:
            continue
        out.append(item)
    c["schedules"] = out
    rr.save_config(c)


def auto_check():
    state["auto_last"] = rr.now_str()
    try:
        for p in rr.pending_targets(cfg(), rr.load_done(outdir())):
            enqueue_timefree(p["station"], p["ft"], p["to"], p["title"])
    except Exception as e:  # noqa: BLE001
        rr.log("自動チェック失敗:", e)


def auto_loop():
    time.sleep(5)
    while True:
        auto_check()
        state["auto_next"] = time.time() + AUTO_INTERVAL
        time.sleep(AUTO_INTERVAL)


def load_stations():
    try:
        _, area = rr.authorize()
        state.update(area=area, stations=[{"id": i, "name": n} for i, n in rr.stations(area)],
                     error=None)
    except Exception as e:  # noqa: BLE001
        state["error"] = f"radiko に接続できません: {e}"


# ---------------- API ----------------
def api_state():
    c = cfg()
    if not state["stations"]:
        load_stations()
    with lock:
        js = [dict(j) for j in jobs]
    nxt = state["auto_next"]
    return {"area": state["area"], "stations": state["stations"], "error": state["error"],
            "jobs": js, "rules": c["rules"], "reservations": c["reservations"],
            "streams": c["streams"], "schedules": c["schedules"],
            "auto_last": state["auto_last"],
            "auto_next_min": max(0, int((nxt - time.time()) / 60)) if nxt else None}


def api_programs(station):
    c, done, active = cfg(), rr.load_done(outdir()), active_keys()
    reserved = {rr.prog_key(r["station"], r["ft"]) for r in c["reservations"]}
    rules = [r for r in c["rules"] if station in r["stations"]]
    out = []
    for p in rr.weekly_programs(station):
        key = rr.prog_key(station, p["ft"])
        p["recorded"] = key in done
        p["active"] = key in active
        p["reserved"] = key in reserved
        p["matched"] = any(rr.matches(r, p) for r in rules)
        out.append(p)
    return {"now": rr.now_str(), "programs": out}


def cached_programs(station):
    """週間番組表を局ごとに10分キャッシュする（検索で全局を何度も取りに行かないため）。"""
    with lock:
        hit = program_cache.get(station)
    if hit and time.time() - hit[0] < PROGRAM_TTL:
        return [dict(p) for p in hit[1]]
    progs = list(rr.weekly_programs(station))
    with lock:
        program_cache[station] = (time.time(), progs)
    return [dict(p) for p in progs]


def norm(text):
    """全角・半角や大文字・小文字の違いを無視して比べるための正規化。"""
    return unicodedata.normalize("NFKC", text).lower()


def api_search(query):
    words = [norm(w) for w in query.split() if w]
    if not words:
        return {"now": rr.now_str(), "programs": []}
    if not state["stations"]:
        load_stations()
    c, done, active = cfg(), rr.load_done(outdir()), active_keys()
    reserved = {rr.prog_key(r["station"], r["ft"]) for r in c["reservations"]}
    names = {s["id"]: s["name"] for s in state["stations"]}
    ids = list(names)
    with ThreadPoolExecutor(max_workers=6) as ex:
        tables = list(ex.map(lambda sid: (sid, cached_programs(sid)), ids))
    out = []
    for sid, progs in tables:
        rules = [r for r in c["rules"] if sid in r["stations"]]
        for p in progs:
            text = norm(" ".join((p["title"], p["pfm"], p["info"], sid, names.get(sid, ""))))
            if all(w in text for w in words):  # スペース区切りは AND（局名でも絞れる）
                key = rr.prog_key(sid, p["ft"])
                p.update(recorded=key in done, active=key in active, reserved=key in reserved,
                         matched=any(rr.matches(r, p) for r in rules))
                out.append(p)
    out.sort(key=lambda p: p["ft"])
    return {"now": rr.now_str(), "programs": out[:300]}


def api_recordings():
    d = outdir()
    if not os.path.isdir(d):
        return []
    files = []
    for name in os.listdir(d):
        if name.endswith(AUDIO_EXTS):
            st = os.stat(os.path.join(d, name))
            files.append({"name": name, "size": st.st_size, "mtime": st.st_mtime})
    return sorted(files, key=lambda f: f["name"], reverse=True)


def set_reservation(body, on):
    c = cfg()
    key = rr.prog_key(body["station"], body["ft"])
    c["reservations"] = [r for r in c["reservations"] if rr.prog_key(r["station"], r["ft"]) != key]
    if on:
        c["reservations"].append({k: body[k] for k in ("station", "ft", "to", "title")})
    rr.save_config(c)


def save_rules(rules):
    c = cfg()
    c["rules"] = [{"stations": [s for s in r["stations"] if s],
                   "keywords": [k for k in r["keywords"] if k]}
                  for r in rules if r.get("stations") and r.get("keywords")]
    rr.save_config(c)


def delete_recording(name):
    path = os.path.join(outdir(), os.path.basename(name))
    if path.endswith(AUDIO_EXTS) and os.path.isfile(path):
        os.remove(path)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        try:
            if u.path == "/":
                with open(HTML, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/state":
                self.send_json(api_state())
            elif u.path == "/api/programs":
                self.send_json(api_programs(q["station"]))
            elif u.path == "/api/search":
                self.send_json(api_search(q.get("q", "")))
            elif u.path == "/api/recordings":
                self.send_json(api_recordings())
            elif u.path.startswith("/rec/"):
                self.send_file(urllib.parse.unquote(u.path[5:]), download="dl" in q)
            else:
                self.send_error(404)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send_error(415)  # 他サイトからのフォーム送信を弾く
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            if self.path == "/api/timefree":
                job = enqueue_timefree(body["station"], body["ft"], body["to"], body.get("title", ""))
                self.send_json({"ok": True, "queued": bool(job)})
            elif self.path == "/api/live":
                start_live(body["station"], float(body["minutes"]), body.get("title", ""))
                self.send_json({"ok": True})
            elif self.path == "/api/stream/rec":
                start_stream(body["stream"], float(body["minutes"]), body.get("title", ""))
                self.send_json({"ok": True})
            elif self.path == "/api/streams":
                save_streams(body["streams"])
                self.send_json({"ok": True})
            elif self.path == "/api/stream/probe":
                info = rr.probe_stream(body["url"])
                self.send_json({"ok": bool(info), "codec": info and info[0], "kbps": info and info[1]})
            elif self.path == "/api/schedules":
                save_schedules(body["schedules"])
                self.send_json({"ok": True})
            elif self.path == "/api/reserve":
                set_reservation(body, body.get("on", True))
                self.send_json({"ok": True})
            elif self.path == "/api/rules":
                save_rules(body["rules"])
                self.send_json({"ok": True})
            elif self.path == "/api/auto/run":
                threading.Thread(target=auto_check, daemon=True).start()
                self.send_json({"ok": True})
            elif self.path == "/api/recordings/delete":
                delete_recording(body["name"])
                self.send_json({"ok": True})
            elif self.path == "/api/reconnect":
                load_stations()
                self.send_json({"ok": True})
            else:
                self.send_error(404)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)

    def send_file(self, name, download=False):
        path = os.path.join(outdir(), os.path.basename(name))
        if not (path.endswith(AUDIO_EXTS) and os.path.isfile(path)):
            return self.send_error(404)
        size = os.path.getsize(path)
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else max(0, size - int(b))
            end = int(b) if a and b else end
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "audio/mp4")
        self.send_header("Accept-Ranges", "bytes")
        if download:
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + urllib.parse.quote(os.path.basename(path)))
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


def serve(port=8765, open_browser=True):
    threading.Thread(target=timefree_worker, daemon=True).start()
    threading.Thread(target=auto_loop, daemon=True).start()
    threading.Thread(target=schedule_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    rr.log(f"radirec UI: {url}  （終了は Ctrl+C）")
    if open_browser:
        threading.Timer(1, webbrowser.open, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
