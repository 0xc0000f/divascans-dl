#!/usr/bin/env python3
"""divascans-dl — webtoon downloader for divascans.org (free-chapter SSR bypass).

Bypass mechanism (verified, do not change):
  1. free chapters render full image URLs in the server-side HTML of
     /series/<comic|novel>/<slug>/chapter/<n>; the HTML escapes slashes, so
     unescape backslashes BEFORE matching  https://media\\.divascans\\.org/[^"\\ ]+
  2. image fetches MUST send a real browser User-Agent and
     Referer: https://divascans.org/  (Cloudflare answers 404 otherwise)
  3. chapters still behind coinCost are not uploaded server-side at all —
     nothing to bypass.
"""
import io
import json
import os
import re
import threading
import time
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor, ThreadPoolExecutor as ThreadPool

import requests
from flask import Flask, Response, abort, jsonify, redirect, request

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DV_DATA_DIR", "/app/data")
CATALOG_F = os.path.join(APP_ROOT, "catalog.jsonl")
BASE = "https://divascans.org"
UA_PAGE = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
UA_IMG = UA_PAGE
API_KEY = os.environ.get("DV_API_KEY", "")
PORT = int(os.environ.get("PORT", "3000"))
IMG_TIMEOUT = int(os.environ.get("DV_IMG_TIMEOUT", "45"))
DL_WORKERS = int(os.environ.get("DV_WORKERS", "8"))
IMG_TTL = int(os.environ.get("DV_IMG_TTL", "600"))

for _d in ("downloads", "zips", "covers"):
    try:
        os.makedirs(os.path.join(DATA_DIR, _d), exist_ok=True)
    except FileExistsError:  # dangling symlink left by a moved cache
        pass
    if not os.path.isdir(os.path.join(DATA_DIR, _d)):
        log("WARN", f"DATA_DIR/{_d} is not a directory — writes there will fail")

SERIAL_RE = re.compile(r"/series/(comic|novel)/([a-z0-9\-]+)/chapter/([0-9]+)")
SERIES_RE = re.compile(r"/series/(comic|novel)/([a-z0-9\-]+)")
IMG_RE = re.compile(r"https://media\.divascans\.org/[^\"\\ ]+")
SAFE_RE = re.compile(r"[^A-Za-z0-9._\-]_")

CATALOG = {}
for _line in open(CATALOG_F, encoding="utf-8"):
    try:
        _d = json.loads(_line)
    except ValueError:
        continue
    if _d.get("slug"):
        CATALOG[_d["slug"]] = _d

LOCK = threading.Lock()
JOBS = {}
TL = {}          # url -> (expires, status, reason)
TL_MAX = 400


def log(*a):
    print(int(time.time()), *a, flush=True)


# ---------------------------------------------------------------- catalog api

def series_fmt(slug):
    t = (CATALOG.get(slug) or {}).get("type")
    return "novel" if t in ("NOVEL", "WEB_NOVEL") else "comic"


def catalog_row(slug):
    return CATALOG.get(slug) or {}


# --------------------------------------------------------------- free listing

def fetch_chapters(slug, fmt, force=False):
    """Live free/paid chapter list from the public AI endpoint (no key needed)."""
    key = ("chs", slug)
    now = time.time()
    with LOCK:
        hit = TL.get(key)
    if hit and not force and hit[0] > now:
        return hit[1], hit[2]
    out, page, nsfw, counts = [], 1, False, {}
    while True:
        r = requests.get(
            f"{BASE}/api/ai/series/{slug}/chapters",
            params={"page": page, "limit": 100},
            headers={"User-Agent": UA_PAGE},
            timeout=30,
        )
        if r.status_code != 200:
            return r.status_code, {"error": f"ai endpoint {r.status_code}"}
        j = r.json()
        nsfw = nsfw or bool((j.get("series") or {}).get("is_nsfw"))
        counts = j.get("counts") or counts
        for c in j.get("chapters") or []:
            out.append({
                "n": c.get("number"),
                "title": c.get("title") or "",
                "free": bool(c.get("is_free")),
                "ea": bool(c.get("early_access_until")),
                "published_at": c.get("published_at"),
            })
        pg = j.get("pagination") or {}
        if not pg.get("has_more"):
            break
        page += 1
        if page > 40:
            break
    for c in out:
        c["cached"] = chapter_cached(slug, c["n"])
        if not c["free"]:
            c["eta"] = max(1, c["n"] - (counts.get("free") or 0))
    body = {"slug": slug, "fmt": fmt, "chapters": out, "nsfw": nsfw, "counts": counts,
            "total": len(out), "free": sum(1 for c in out if c["free"])}
    with LOCK:
        TL[key] = (now + IMG_TTL, 200, body)
    return 200, body


# ------------------------------------------------------------- SSR url scrape

def chapter_images(slug, fmt, n, force=False):
    """Verified mechanism: SSR HTML -> backslash-unescape -> regex media urls."""
    key = ("imgs", slug, n)
    now = time.time()
    with LOCK:
        hit = TL.get(key)
    if hit and not force and hit[0] > now:
        return hit[1], hit[2]
    url = f"{BASE}/series/{fmt}/{slug}/chapter/{n}"
    try:
        r = requests.get(url, headers={"User-Agent": UA_PAGE, "Referer": BASE + "/"},
                         timeout=40)
    except requests.RequestException as e:
        return 502, {"error": f"ssr fetch failed: {e}"}
    if r.status_code != 200:
        return r.status_code, {"error": f"ssr {r.status_code}"}
    html = r.text.replace("\\", "")
    urls, seen = [], set()
    for u in IMG_RE.findall(html):
        if u not in seen:
            seen.add(u)
            urls.append(u)
    # SSR lists full pages (p-<uuid>, w=720) AND vertical slice tiles
    # (s-<uuid>, w=240) of the same page — only full pages belong in the zip.
    full = re.compile(r"/(?:\d{1,4}-)?p-[0-9a-f-]{36}\.webp$|/chapters/\d+/\d+\.webp$")
    keep = [u for u in urls if full.search(u)]
    if not keep:
        keep = [u for u in urls if not re.search(r"/(?:\d{1,4}-)?s-[0-9a-f-]{36}\.webp$", u)]
    body = {"slug": slug, "n": n, "images": keep, "listed": len(urls)}
    with LOCK:
        TL[key] = (now + (3600 if urls else 120), 200, body)
    return 200, body


# ------------------------------------------------------- roleascann mirror
# DivaScans frees chapters slowly; the roleascann/snapvip mirror carries them
# fresh. cdna.snapvip.xyz/<mirror_slug>/<chapter>/<N>.webp are vertical slices
# of the whole chapter strip (N=0.., no API, open hotlink): HEAD-probe until
# the first 404, download, stitch back into page images, cache like any other.

MIRROR_F = os.path.join(DATA_DIR, "mirror_map.jsonl")
MIRROR_BAKED = os.path.join(APP_ROOT, "mirror_map.jsonl")
MIRROR_UA = {"User-Agent": UA_IMG, "Referer": "https://roliascann.com/"}
_mirror = {}
_mirror_loaded = 0.0


def _mirror_load(force=False):
    global _mirror, _mirror_loaded
    now = time.time()
    if not force and now - _mirror_loaded < 600:
        return
    merged = {}
    for f in (MIRROR_BAKED, MIRROR_F):
        try:
            for line in open(f, encoding="utf-8"):
                d = json.loads(line)
                s = (d.get("mirror_slug") or "").strip()
                if s and re.fullmatch(r"[a-z0-9][a-z0-9._-]*", s):
                    merged[d["slug"]] = (s, int(d.get("mirror_max_ch") or 0))
        except (OSError, ValueError):
            continue
    if merged:
        _mirror = merged
    _mirror_loaded = now


def _mirror_for(slug, n):
    _mirror_load()
    r = _mirror.get(slug)
    return r[0] if r and r[1] >= n else None


def _mirror_slices(ms, n):
    key = ("mir", ms, n)
    now = time.time()
    with LOCK:
        hit = TL.get(key)
    if hit and hit[0] > now:
        return hit[1]
    urls, miss = [], 0
    for i in range(500):
        u = f"https://cdna.snapvip.xyz/{ms}/{n}/{i}.webp"
        code = -1
        for _ in range(2):  # one retry: a transient miss must not truncate the chapter
            try:
                h = requests.head(u, headers=MIRROR_UA, timeout=12, allow_redirects=True)
                code = h.status_code
            except requests.RequestException:
                code = -1
            if code == 200:
                break
        if code == 200:
            urls.append(u)
            miss = 0
        else:
            miss += 1
            if miss >= 3:
                break
    with LOCK:
        TL[key] = (now + (21600 if urls else 600), urls, None)
    return urls


def _stitch(paths, dst_base, maxh=20000):
    """Join vertical slices into page images of at most maxh px (JPEG caps at
    65535; ~20k px ≈ one native DivaScans page). Returns the written paths."""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    w = max(Image.open(p).width for p in paths)
    outs, y, canvas = [], 0, None

    def flush(idx):
        nonlocal canvas, y
        if canvas is not None:
            dst = f"{dst_base}.{idx:03d}.jpg" if outs or idx > 1 else f"{dst_base}.jpg"
            canvas.save(dst, "JPEG", quality=80, optimize=True)
            outs.append(dst)
            canvas.close()
            canvas, y = None, 0

    idx = 1
    for p in paths:
        im = Image.open(p).convert("RGB")
        if im.width != w:
            im = im.resize((w, max(1, im.height * w // im.width)))
        if canvas is None:
            canvas = Image.new("RGB", (w, min(im.height, maxh)), (255, 255, 255))
            y = 0
        if y + im.height > maxh and y > 0:
            flush(idx)
            idx += 1
            canvas = Image.new("RGB", (w, min(im.height, maxh)), (255, 255, 255))
            y = 0
        if canvas.height < y + im.height:
            bigger = Image.new("RGB", (w, y + im.height), (255, 255, 255))
            bigger.paste(canvas.crop((0, 0, w, y)), (0, 0))
            canvas.close()
            canvas = bigger
        canvas.paste(im, (0, y))
        y += im.height
        im.close()
    flush(idx)
    return outs


def _mirror_pages(slug, n, fresh):
    """Locked chapter fallback: mirror slices -> local page files.

    Returns (pairs, total, missing) like get_chapter_files, or None if this
    chapter is not on the mirror. Stitched into one jpg per chapter when
    possible; falls back to raw slices if Pillow can't hold the strip.
    """
    ms = _mirror_for(slug, n)
    if not ms:
        return None
    urls = _mirror_slices(ms, n)
    if not urls:
        return None
    d = chapter_dir(slug, n)
    os.makedirs(d, exist_ok=True)
    todo = [(u, os.path.join(d, f"mirror-{i:04d}.webp")) for i, u in enumerate(urls)]
    got, missing = [], 0
    with ThreadPoolExecutor(max_workers=DL_WORKERS) as ex:
        for (u, p), (_, ok) in zip(todo, ex.map(lambda t: fetch_image(*t), todo)):
            if ok:
                got.append(p)
                if fresh is not None:
                    fresh.append(p)
            else:
                missing += 1
    if not got:
        return None
    try:
        pages = _stitch(got, os.path.join(d, f"mirror-{n:03d}-page"))
        if fresh is not None:
            fresh.extend(pages)
        return ([(f"mirror-slices/{n}/{i + 1:03d}.jpg", p) for i, p in enumerate(pages)],
                len(pages), missing)
    except Exception:  # noqa: BLE001 — huge strip: serve slices as pages
        return ([(f"mirror-slices/{n}/{i + 1:03d}.webp", p) for i, p in enumerate(got)],
                len(got), missing)


# ------------------------------------------------------------------- fetching

def page_path(slug, n, img_url):
    name = SAFE_RE.sub("_", img_url.rsplit("/", 1)[-1])[:150]
    return os.path.join(DATA_DIR, "downloads", slug, f"ch{n}", name)


def fetch_image(img_url, dst):
    """Download with browser UA + referer (Cloudflare hotlink guard on new scheme).

    Defensive: any failure -> (dst, False); the .part temp file is always cleaned.
    """
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            return dst, True
        part = f"{dst}.{os.getpid()}.{threading.get_ident()}.part"
        try:
            r = requests.get(img_url, headers={"User-Agent": UA_IMG, "Referer": BASE + "/"},
                             stream=True, timeout=(10, IMG_TIMEOUT))
            if r.status_code != 200:
                return dst, False
            with open(part, "wb") as fh:
                for chunk in r.iter_content(65536):
                    fh.write(chunk)
            if not os.path.exists(part) or os.path.getsize(part) == 0:
                return dst, False
            os.replace(part, dst)
            return dst, True
        finally:
            try:
                os.remove(part)
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001
        log("img fetch error", img_url[:90], repr(e)[:120])
        return dst, False


def chapter_dir(slug, n):
    return os.path.join(DATA_DIR, "downloads", slug, f"ch{n}")


def chapter_cached(slug, n):
    d = chapter_dir(slug, n)
    if not os.path.isdir(d):
        return 0
    return len([f for f in os.listdir(d) if not f.startswith(".") and not f.endswith(".part")])


def get_chapter_files(slug, n, force=False, fresh=None):
    """Return (ordered list of (img_url, local_path), missing_count).

    Newly downloaded page paths are appended to `fresh` so the job can
    delete them after the zip is served (nothing accumulates on disk).
    """
    st, body = chapter_images(slug, series_fmt(slug), n, force=force)
    if st != 200:
        raise RuntimeError(body.get("error", "ssr failed"))
    urls = body["images"]
    if not urls:
        mir = _mirror_pages(slug, n, fresh)
        if mir:
            return mir
    pairs = [(u, page_path(slug, n, u)) for u in urls]
    todo, seenp = [], set()
    for u, p in pairs:
        if p not in seenp and not (os.path.exists(p) and os.path.getsize(p) > 0):
            seenp.add(p)
            todo.append((u, p))
    missing = 0
    if todo:
        with ThreadPoolExecutor(max_workers=DL_WORKERS) as ex:
            results = zip(todo, ex.map(lambda t: fetch_image(*t), todo))
            for (u, p), (_, ok) in results:
                if ok:
                    if fresh is not None:
                        fresh.append(p)
                else:
                    missing += 1
    return pairs, len(urls), missing


# --------------------------------------------------------- premium unlock ETA
# divascans frees the oldest premium chapter on each new publication
# (free_on_next_release). /api/v1/releases/series gives published_count and
# free_count, so (n - free_count) updates remain before chapter n turns free.

_WIN_TTL, _win_cache = 300, {}
_win_lock = threading.Lock()


def premium_unlock_in(slug, n):
    """How many more releases until chapter n is free (None = unknown)."""
    if not API_KEY or n > 100000:
        return None
    now = time.time()
    with _win_lock:
        hit = _win_cache.get(slug)
        if hit and now - hit[1] >= _WIN_TTL:
            hit = None
        if hit:
            pub, free = hit[0]
        else:
            try:
                r = requests.get(f"{BASE}/api/v1/releases/series", params={"series": slug},
                                 headers={"Authorization": "Bearer " + API_KEY,
                                          "User-Agent": UA_PAGE}, timeout=15)
                d = r.json()["data"][0]
                pub, free = d["published_count"], d["free_count"]
                _win_cache[slug] = ((pub, free), now)
            except Exception as e:  # noqa: BLE001
                log("releases/series error", slug, repr(e)[:100])
                return None
    return n - free if free < pub and n <= pub else None


# ------------------------------------------------------------------- zip jobs

def build_zip(slug, chapters, job):
    row = catalog_row(slug)
    zname = f"{row.get('title') or slug}.zip"
    zpath = os.path.join(DATA_DIR, "zips", job["key"] + ".zip")
    report = {"series": row.get("title") or slug, "chapters": {}}
    fresh = job.setdefault("tmp", [])
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as zf:
        for idx, n in enumerate(chapters, 1):
            if job.get("stop"):
                break
            job["state"] = f"ch {n} ({idx}/{len(chapters)})"
            try:
                pairs, total, missing = get_chapter_files(slug, n, fresh=fresh)
            except RuntimeError as e:
                report["chapters"][str(n)] = {"error": str(e)}
                job["errors"].append(f"ch{n}: {e}")
                continue
            if total == 0:
                unlock = premium_unlock_in(slug, n)
                if unlock:
                    err = f"not free yet — unlocks free after ~{unlock} more update(s)"
                else:
                    err = "no images (not free yet?)"
                report["chapters"][str(n)] = {"error": err}
                job["errors"].append(f"ch{n}: {err}")
                continue
            got = 0
            for i, (u, p) in enumerate(pairs, 1):
                if os.path.exists(p) and os.path.getsize(p) > 0:
                    zf.write(p, f"ch{int(n):03d}/{i:03d}-{os.path.basename(p)}")
                    got += 1
            job["done_pages"] += got
            job["done"] += 1
            info = {"pages": got}
            if total - got:
                info["missing"] = total - got
                job["errors"].append(f"ch{n}: {total - got} pages missing")
            report["chapters"][str(n)] = info
    with zipfile.ZipFile(zpath, "a") as zf:
        zf.writestr("_download_report.json", json.dumps(report, ensure_ascii=False, indent=1))
    job["zpath"] = zpath
    job["zname"] = zname
    job["state"] = "done"


def job_worker(key, slug, chapters):
    job = JOBS[key]
    try:
        build_zip(slug, chapters, job)
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        log("zip job failed", key, repr(e))
        job["status"] = "failed"
        job["errors"].append(str(e))
        job["state"] = "failed"


# ----------------------------------------------------------------------- http

app = Flask(__name__)


@app.get("/")
def home():
    return redirect("/ui", code=302)


@app.get("/ui")
def ui():
    return PAGE


@app.get("/api/status")
def api_status():
    du = os.statvfs(DATA_DIR)
    return jsonify({
        "ok": True,
        "series": len(CATALOG),
        "cached_series": len(os.listdir(os.path.join(DATA_DIR, "downloads"))),
        "api_key": bool(API_KEY),
        "jobs": sum(1 for j in JOBS.values() if j["status"] == "running"),
        "disk_free_gb": round(du.f_bavail * du.f_frsize / 1e9, 1),
    })


@app.get("/api/series")
def api_series():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"items": []})
    m = SERIES_RE.search(q)
    if m:
        q = m.group(2)
    ql = q.lower()
    out = []
    for slug, r in CATALOG.items():
        title = (r.get("title") or "").lower()
        if ql in title or ql in slug:
            out.append({"slug": slug, "title": r.get("title"), "type": r.get("type"),
                        "total": r.get("total", 0), "free": r.get("free", 0),
                        "paid": r.get("paid", 0)})
        if len(out) >= 40:
            break
    return jsonify({"items": out})


_DESC_TTL, _desc_cache = 3600, {}


def series_desc(slug):
    """Series synopsis from the SSR page's og:description (cached)."""
    now = time.time()
    hit = _desc_cache.get(slug)
    if hit and hit[0] > now:
        return hit[1]
    desc = ""
    try:
        r = requests.get(f"{BASE}/series/{series_fmt(slug)}/{slug}",
                         headers={"User-Agent": UA_PAGE}, timeout=30)
        m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)',
                      r.text) or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description', r.text)
        if m:
            import html as _h
            desc = _h.unescape(m.group(1)).strip()
    except Exception as e:  # noqa: BLE001
        log("desc error", slug, repr(e)[:100])
    _desc_cache[slug] = (now + _DESC_TTL, desc)
    return desc


@app.get("/api/series/<slug>")
def api_one_series(slug):
    if slug not in CATALOG and not re.fullmatch(r"[a-z0-9\-]+", slug):
        abort(404)
    fmt = series_fmt(slug)
    st, body = fetch_chapters(slug, fmt, force=request.args.get("refresh") == "1")
    if st != 200:
        return jsonify(body), st
    body["catalog"] = catalog_row(slug)
    body["fmt"] = fmt
    body["desc"] = series_desc(slug)
    return jsonify(body)


@app.post("/api/chapters")
def api_download():
    j = request.get_json(force=True, silent=True) or {}
    slug = j.get("slug") or ""
    if slug not in CATALOG:
        m = SERIES_RE.search(slug)
        slug = m.group(2) if m else ""
    if slug not in CATALOG:
        return jsonify({"error": f"unknown slug: {slug}"}), 404
    ns = [int(n) for n in (j.get("chapters") or []) if str(n).isdigit()]
    if not ns:
        return jsonify({"error": "no chapters selected"}), 400
    key = f"{int(time.time()*1000)%10**13:x}{os.getpid()%997:x}"
    JOBS[key] = {"key": key, "slug": slug, "status": "running", "state": "starting",
                 "done": 0, "total": len(ns), "done_pages": 0, "errors": [], "stop": False}
    try:
        sweep_stale()
    except Exception as e:  # noqa: BLE001
        log("sweep error", repr(e)[:100])
    threading.Thread(target=job_worker, args=(key, slug, sorted(ns)), daemon=True).start()
    return jsonify({"key": key, "chapters": len(ns)})


@app.get("/api/job/<key>")
def api_job(key):
    j = JOBS.get(key)
    if not j:
        abort(404)
    return jsonify({k: j[k] for k in ("status", "state", "done", "total", "done_pages", "errors")})


def _rm_tree_file(p):
    try:
        os.remove(p)
        d = os.path.dirname(p)
        for _ in range(2):
            try:
                os.rmdir(d)
                d = os.path.dirname(d)
            except OSError:
                break
    except OSError:
        pass


def _drop_job_files(j):
    """Ephemeral storage: after the zip was served (or aged out), delete
    the zip and every page this job downloaded. Pre-existing cache stays."""
    zp = j.pop("zpath", None)
    if zp:
        _rm_tree_file(zp)
    for p in j.pop("tmp", []):
        _rm_tree_file(p)


_T0 = time.time()


def sweep_stale():
    """Delete zip artifacts of abandoned jobs (never fetched) older than 1h.

    Also catches artifacts orphaned by a container restart (JOBS lost):
    zips older than 1h, and chapter dirs created since boot and untouched
    for 1h (pre-existing cache dirs are older than boot, never touched).
    """
    now = time.time()
    for j in list(JOBS.values()):
        zp = j.get("zpath")
        if zp and j["status"] != "running" and os.path.exists(zp) \
                and now - os.path.getmtime(zp) > 3600:
            _drop_job_files(j)
    zdir = os.path.join(DATA_DIR, "zips")
    try:
        for de in os.scandir(zdir):
            if de.is_file() and now - de.stat().st_mtime > 3600:
                _rm_tree_file(de.path)
    except OSError:
        pass
    ddir = os.path.join(DATA_DIR, "downloads")
    try:
        for sslug in os.scandir(ddir):
            if not sslug.is_dir() or sslug.stat().st_mtime <= _T0 - 60:
                continue  # series dir pre-dates this process: old cache, leave alone
            for chd in os.scandir(sslug.path):
                if not (chd.is_dir() and re.fullmatch(r"ch\d+", chd.name)
                        and _T0 - 60 < chd.stat().st_mtime < now - 3600):
                    continue
                for dpf in os.walk(chd.path, topdown=False):
                    for f in dpf[2]:
                        _rm_tree_file(os.path.join(dpf[0], f))
                    try:
                        os.rmdir(dpf[0])
                    except OSError:
                        pass
    except OSError:
        pass


@app.get("/api/job/<key>/zip")
def api_job_zip(key):
    j = JOBS.get(key)
    if not j or j["status"] != "done" or not j.get("zpath"):
        abort(404)
    fn = os.path.basename(j.get("zname") or "download.zip")
    ascii_fn = re.sub(r"[^A-Za-z0-9._ -]", "", fn)[:70].strip() or "download.zip"
    quoted = urllib.parse.quote(fn)
    data = open(j["zpath"], "rb").read()
    _drop_job_files(j)
    return Response(data, mimetype="application/zip",
                    headers={"Content-Disposition":
                             f'attachment; filename="{ascii_fn}"; filename*=UTF-8\'\'{quoted}'})


@app.post("/api/job/<key>/stop")
def api_job_stop(key):
    j = JOBS.get(key)
    if not j:
        abort(404)
    j["stop"] = True
    return jsonify({"ok": True})


@app.get("/api/chapter/<slug>/<int:n>")
def api_chapter(slug, n):
    if slug not in CATALOG:
        abort(404)
    st, body = chapter_images(slug, series_fmt(slug), n, force=request.args.get("refresh") == "1")
    body["cached"] = chapter_cached(slug, n)
    if st == 200 and not body.get("images"):
        body["mirror"] = bool(_mirror_for(slug, n))
    if st != 200:
        return jsonify(body), st
    return jsonify(body)


@app.get("/img/<path:url>")
def proxy_img(url):
    """Proxy image fetch so the browser is never the one talking to Cloudflare.

    Nothing is stored on disk here (owner request): if the page is already in
    the chapter cache we serve that, otherwise we stream from the origin.
    """
    if not url.startswith("https://media.divascans.org/"):
        abort(403)
    slug = request.args.get("slug") or "stream"
    n = request.args.get("n") or "0"
    dst = page_path(slug, n, url)
    try:
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            data = open(dst, "rb").read()
        else:
            r = requests.get(url, headers={"User-Agent": UA_IMG, "Referer": BASE + "/"},
                             timeout=(10, IMG_TIMEOUT))
            if r.status_code != 200 or not r.content:
                abort(404)
            data = r.content
    except Exception as e:  # noqa: BLE001
        log("img proxy error", url[:90], repr(e)[:100])
        abort(404)
    return Response(data, mimetype="image/webp",
                    headers={"Cache-Control": "public, max-age=86400"})


def _store_cover(slug, data, ext):
    try:
        with open(os.path.join(DATA_DIR, "covers", f"{slug}.{ext}"), "wb") as fh:
            fh.write(data)
        ent = sorted((de.stat().st_mtime, de.path) for de in os.scandir(os.path.join(DATA_DIR, "covers")) if de.is_file())
        for _, old in ent[:-3000]:  # ~500 MB ceiling on the cover cache
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        pass


def _cover_kind(data):
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _cover_small(data):
    """Downscale any cover to a 480px-wide jpg (~30 KB) so 1787 covers cost
    ~60 MB, not gigabytes. Falls back to the raw bytes when Pillow can't."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.load()
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        if im.width > 480:
            im = im.resize((480, max(1, im.height * 480 // im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=78, optimize=True)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return data


def cover_source(slug):
    """Fetch the real CDN cover_image; fall back to og-image. Always stored
    as a small jpg."""
    src = (CATALOG.get(slug) or {}).get("cover_image")
    if src and src.startswith("https://media.divascans.org/"):
        try:
            r = requests.get(src, headers={"User-Agent": UA_IMG, "Referer": BASE + "/"},
                             timeout=30)
            if r.status_code == 200 and _cover_kind(r.content):
                small = _cover_small(r.content)
                _store_cover(slug, small, "jpg")
                return small, "image/jpeg"
        except requests.RequestException:
            pass
    r = requests.get(f"{BASE}/series/{series_fmt(slug)}/{slug}/opengraph-image",
                     headers={"User-Agent": UA_PAGE, "Referer": BASE + "/"},
                     timeout=40)
    if r.status_code != 200 or not _cover_kind(r.content):
        return None, None
    small = _cover_small(r.content)
    _store_cover(slug, small, "jpg")
    return small, "image/jpeg"


@app.get("/cover/<slug>")
def cover(slug):
    """Series og-image, cached on disk (browser img can't send the Referer CF wants)."""
    if not re.fullmatch(r"[a-z0-9\-]+", slug):
        abort(404)
    cdir = os.path.join(DATA_DIR, "covers")
    os.makedirs(cdir, exist_ok=True)
    ext = next((e for e in ("jpg", "webp", "png")
                if os.path.exists(os.path.join(cdir, f"{slug}.{e}"))), None)
    if ext:
        with open(os.path.join(cdir, f"{slug}.{ext}"), "rb") as fh:
            data = fh.read()
        return Response(data, mimetype=f"image/{'jpeg' if ext == 'jpg' else ext}",
                        headers={"Cache-Control": "public, max-age=86400"})
    data, ctype = cover_source(slug)
    if data is None:
        abort(404)
    return Response(data, mimetype=ctype,
                    headers={"Cache-Control": "public, max-age=86400"})


def warm_covers():
    """Prefetch every series cover at boot (owner asked for real covers on
    cards, not lazy on-demand og-images). CDN jpgs, ~120 KB each, bounded."""
    try:
        cdir = os.path.join(DATA_DIR, "covers")
        have = {de.name[:-4] for de in os.scandir(cdir)
                if de.is_file() and de.name.endswith(".jpg")}
    except OSError:
        have = set()
    todo = [s for s, r in CATALOG.items()
            if s not in have and (r.get("title") or r.get("cover_image"))]
    log(f"cover warmup: {len(todo)} to fetch")

    def one(slug):
        try:
            cover_source(slug)
        except Exception:  # noqa: BLE001
            pass

    with ThreadPool(6) as ex:
        for _ in ex.map(one, todo):
            time.sleep(0.05)
    log("cover warmup: done")


@app.get("/api/feed")
def api_feed():
    """Latest became_free/published events (needs DV_API_KEY)."""
    if not API_KEY:
        return jsonify({"error": "no DV_API_KEY configured"}), 503
    r = requests.get(f"{BASE}/api/v1/releases", params={"limit": 100},
                     headers={"Authorization": "Bearer " + API_KEY, "User-Agent": UA_PAGE},
                     timeout=30)
    if r.status_code != 200:
        return jsonify({"error": f"feed {r.status_code}"}), 502
    out = []
    for e in r.json().get("data", []):
        ch, se = e.get("chapter", {}), e.get("series", {})
        if not (se.get("slug") and ch.get("number")):
            continue
        out.append({"slug": se["slug"], "title": se.get("title"),
                    "n": ch["number"], "event": e.get("event"),
                    "is_free": bool(ch.get("is_free")),
                    "early_access_until": ch.get("early_access_until")})
    return jsonify({"events": out})


PAGE = r"""<!doctype html><html lang=th><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>DIVASCANS DL</title>
<meta name=description content="divascans.org episode ZIP downloader">
<style>
:root{--pink:#34d399;--bg:#0c1013;--card:#151a1f;--line:#263038;
 --ease-out:cubic-bezier(.23,1,.32,1);--ease-in-out:cubic-bezier(.77,0,.175,1)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#eee;
 font-family:'Noto Sans',system-ui,sans-serif}
header{display:flex;gap:12px;align-items:center;padding:14px 20px;border-bottom:1px solid var(--line);
 position:sticky;top:0;background:rgba(12,16,19,.96);backdrop-filter:blur(6px);z-index:5;flex-wrap:wrap}
.logo{font-weight:900;font-size:19px;color:var(--pink);letter-spacing:.04em}
.sub{font-size:12px;color:#8996a3;white-space:nowrap}
input[type=text]{flex:1;min-width:210px;background:var(--card);border:1px solid var(--line);color:#eee;
 padding:11px 14px;border-radius:10px;font-size:14px}
input[type=text]:focus{outline:2px solid var(--pink);outline-offset:-1px}
button{background:linear-gradient(135deg,#10b981,#047857);color:#fff;border:0;padding:11px 18px;
 border-radius:10px;font-weight:700;cursor:pointer;transition:transform 160ms var(--ease-out),filter 160ms ease}
button:active{transform:scale(.97)}
button:disabled{filter:grayscale(.5);opacity:.65;cursor:wait}
.back{color:#888;cursor:pointer;font-size:13px}
main{max-width:920px;margin:0 auto;padding:18px 16px 90px}
.count{float:right;color:#8996a3;font-size:12px;font-weight:400}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:12px;margin-top:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;cursor:pointer;
 transition:transform 160ms var(--ease-out),border-color 160ms ease}
@media (hover:hover) and (pointer:fine){.card:hover{transform:translateY(-2px);border-color:var(--pink)}}
.card img{width:100%;aspect-ratio:3/4;object-fit:cover;display:block;background:#182027}
.card b{display:block;font-size:13px;padding:8px 10px 2px;line-height:1.35}
.card span{display:block;font-size:11px;color:#8996a3;padding:0 10px 10px}
.age{display:inline-block;background:#5a1426;color:#ff94a9;border-radius:4px;padding:1px 5px;font-weight:700}
.tag{display:inline-block;background:#12291f;color:#8dd3b6;border-radius:4px;padding:1px 5px;font-weight:700}
.head{display:flex;gap:14px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin:14px 0}
.head img{width:110px;aspect-ratio:3/4;object-fit:cover;border-radius:10px;flex:none;background:#182027}
.head h2{margin:0 0 6px;font-size:20px}.meta{color:#8dd3b6;font-size:13px;margin:6px 0}
.synopsis{color:#aab5bf;font-size:12px;line-height:1.5;margin-top:9px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0;align-items:center}
.ep{display:grid;grid-template:1fr/48px 54px 1fr auto auto;gap:10px;align-items:center;
 border-top:1px solid var(--line);padding:10px 2px}
.ep .n{font-weight:700;color:var(--pink);text-align:right;white-space:nowrap}
.ep img{width:54px;aspect-ratio:3/4;object-fit:cover;border-radius:5px;background:#182027}
.ep .t{font-size:14px}.ep .coin{font-size:12px;color:#ffd166;min-width:120px;text-align:right}
.ep .free{font-size:12px;color:#7bd88f;min-width:120px;text-align:right}
.ep .got{font-size:11px;color:#7bd88f}.zip{padding:7px 11px;font-size:12px;border-radius:8px}
.err{color:#ffb4ab;background:#2a1317;border:1px solid #5b2730;border-radius:10px;padding:10px 12px}
.frow{display:flex;gap:10px;align-items:center;border-top:1px solid var(--line);padding:9px 2px;cursor:pointer}
.frow:hover .ft{color:var(--pink)}
.frow img{width:34px;aspect-ratio:3/4;object-fit:cover;border-radius:4px;background:#182027}
.ft{font-size:14px;transition:color 160ms ease}.fm{margin-left:auto;font-size:11px;color:#8996a3;white-space:nowrap}
#toast{position:fixed;left:50%;bottom:22px;transform:translate(-50%,140%);opacity:0;background:#131a20;
 border:1px solid #2b3a44;color:#d7e3ea;padding:11px 16px;border-radius:12px;font-size:13px;z-index:9;
 box-shadow:0 8px 30px rgba(0,0,0,.5);transition:transform 400ms var(--ease-out),opacity 400ms var(--ease-out);
 max-width:min(90vw,560px)}
#toast.on{transform:translate(-50%,0);opacity:1}
#toast.bad{border-color:#5b2730;color:#ffb4ab}
.item{opacity:0;transform:translateY(8px);animation:rise 260ms var(--ease-out) forwards}
@keyframes rise{to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){.item{animation:none;opacity:1;transform:none}
 button,.card{transition:none}}
</style></head><body>
<header><div class=logo>DIVASCANS DL</div><span class=sub>Episode ZIP Downloader</span>
<input id=q type=text placeholder="พิมพ์ชื่อเรื่อง divascans…" aria-label="ค้นหาเรื่อง"><button onclick=search()>ค้นหา</button></header>
<main id=out></main><div id=toast role=status></div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>(s+'').replace(/[<>&"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp','"':'&quot'}[c]));
let toastT=0;
function toast(msg,bad){const t=$('toast');t.textContent=msg;t.classList.toggle('bad',!!bad);t.classList.add('on');
 clearTimeout(toastT);toastT=setTimeout(()=>t.classList.remove('on'),4000)}
let LASTQ='';
async function search(){const q=$('q').value.trim();if(!q)return;LASTQ=q;const out=$('out');out.innerHTML='<h3>กำลังค้นหา…</h3>';
 const r=await fetch('/api/series?'+new URLSearchParams({q}));const j=await r.json();
 if(!r.ok||j.error){out.innerHTML=`<div class=err>${esc(j.error||'ค้นหาไม่สำเร็จ')}</div>`;return}
 if(!j.items.length){out.innerHTML='<h3>ไม่พบเรื่อง</h3>';return}
 let h=`<h3>${esc(q)} <span class=count>${j.items.length} เรื่อง</span></h3><div class=grid>`;
 j.items.forEach((x,i)=>{h+=`<div class="item card" style="animation-delay:${Math.min(i,12)*40}ms" onclick="openSeries('${esc(x.slug)}')">
  <img loading=lazy src="/cover/${esc(x.slug)}" onerror="this.hidden=true"><b>${esc(x.title||x.slug)}</b>
  <span>${esc((x.type||'').toLowerCase())}<br><span class=tag>${x.free||0}/${x.total||0} ฟรี</span>${' '}<span class=age>${esc((x.type||'').startsWith('NOVEL')?'NOVEL':'COMIC')}</span></span></div>`});
 out.innerHTML=h+'</div>'}
let SEL=new Set(),EPS=[],CUR='',TITLE='';
function upd(){const n=SEL.size,b=$('dl');if(b){b.disabled=!n;b.textContent=n?`ดาวน์โหลด ${n} ตอน`:'เลือกตอนก่อน'}
 document.querySelectorAll('.epc').forEach(x=>{x.checked=SEL.has(+x.value)})}
function selAll(fn){SEL.clear();EPS.filter(fn).forEach(e=>SEL.add(e.n));upd()}
async function openSeries(slug){const out=$('out');out.innerHTML='<h3>กำลังโหลดรายละเอียด…</h3>';
 const r=await fetch('/api/series/'+encodeURIComponent(slug));const j=await r.json();
 if(!r.ok||j.error){out.innerHTML=`<div class=err>${esc(j.error||'โหลดไม่สำเร็จ')}</div>`;return}
 CUR=slug;TITLE=j.catalog?.title||slug;EPS=j.chapters||[];SEL.clear();
 let h=`<span class=back onclick="search()">← กลับผลการค้นหา</span><div class=head>
  <img src="/cover/${esc(slug)}" onerror="this.hidden=true"><div><h2>${esc(TITLE)}</h2>
  <div class=meta>Diva · ${EPS.length} ตอน · ฟรี ${j.free}${j.nsfw?' · <span class=age>19+</span>':''}</div>
  <div class=meta>มีให้ดาวน์โหลด ${j.chapters.filter(c=>c.free||c.cached).length} ตอน</div>
  <div class=synopsis>${esc(j.desc||'')}</div></div></div>
  <h3>ตอนทั้งหมด <span class=count>${EPS.length}</span></h3>
  <div class=bar><button onclick="selAll(c=>c.free||c.cached)">เลือกตอนฟรี</button>
  <button onclick="selAll(()=>true)">เลือกทั้งหมด</button><button onclick="SEL.clear();upd()">ล้าง</button>
  <button id=dl onclick="dlSel(this)" disabled>เลือกตอนก่อน</button><button id=st onclick=stopJob() hidden>หยุด</button></div>`;
 EPS.forEach(e=>{
  const label=e.title?esc(e.title):'ตอน '+e.n;
  const right=e.cached?'<span class=free>ดาวน์โหลดแล้ว</span>':(e.free?'<span class=free>ฟรี</span>'
   :`<span class=coin>${e.eta?('ฟรีหลังอัปเดต ~'+e.eta+' ครั้ง'):''}</span>`);
  h+=`<div class=ep><label class=n><input class=epc type=checkbox value="${e.n}" onchange="tog(this)">${String(e.n).padStart(2,'0')}</label>
   <img loading=lazy src="/cover/${esc(slug)}" onerror="this.hidden=true"><span class=t>${label}</span>
   <span>${e.cached?'<span class=got>✓ </span>':''}${right}</span>
   <button class=zip onclick="dl([${e.n}],this)">ZIP</button></div>`});
 out.innerHTML=h;upd()}
function tog(x){const n=+x.value;SEL.has(n)?SEL.delete(n):SEL.add(n);upd()}
let JOBK='';
async function dl(ns,btn){btn.disabled=true;const old=btn.textContent;btn.textContent='กำลังโหลด…';
 JOBK='';const st=$('st');
 try{
  const r=await fetch('/api/chapters',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({slug:CUR,chapters:ns})});
  const j=await r.json();if(!r.ok)throw new Error(j.error||'ไม่ได้รับงานโหลด');
  JOBK=j.key;if(st&&btn.id==='dl'){st.hidden=false}
  let job;
  while(true){await new Promise(s=>setTimeout(s,1200));
   job=await(await fetch('/api/job/'+JOBK)).json();
   if(btn.id==='dl')btn.textContent=`โหลดแล้ว ${job.done}/${job.total} ตอน`;
   if(job.status!=='running')break}
  if(job.status!=='done')throw new Error((job.errors||[]).join('; ')||'โหลดไม่สำเร็จ');
  const zr=await fetch('/api/job/'+JOBK+'/zip');
  if(!zr.ok)throw new Error('zip หายไป');
  const blob=await zr.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');
  a.href=url;a.download=(zr.headers.get('Content-Disposition')||'').match(/filename="?([^";]+)/)?.[1]||'download.zip';
  document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),120000);
  toast(`เรียบร้อย — ZIP ${(blob.size/1048576).toFixed(1)}MB อยู่ในเครื่องแล้ว`);
 }catch(e){toast(e.message||'โหลดไม่สำเร็จ',1)}
 finally{if(st){st.hidden=true}btn.disabled=false;btn.textContent=old;upd()}}
function dlSel(btn){const ns=[...SEL].sort((a,b)=>a-b);if(ns.length)dl(ns,btn)}
async function stopJob(){if(JOBK)await fetch('/api/job/'+JOBK+'/stop',{method:'POST'})}
async function home(){const out=$('out');out.innerHTML='<h3>ค้นหาเรื่องจาก divascans แล้วเลือกตอนเพื่อโหลด ZIP ทีละตอนหรือหลายตอน</h3><div id=feed></div>';
 try{const j=await(await fetch('/api/feed')).json();const seen=new Set();let h='<h3 style="margin-top:30px">ล่าสุดจาก divascans</h3>';
  (j.events||[]).forEach(e=>{if(seen.has(e.slug))return;seen.add(e.slug);if(seen.size>14)return;
   h+=`<div class=frow onclick="openSeries('${esc(e.slug)}')"><img loading=lazy src="/cover/${esc(e.slug)}" onerror="this.hidden=true">
    <span class=ft>${esc(e.title||e.slug)}</span><span class=fm>${e.event==='became_free'?'ปลดล็อกฟรี':e.event==='published'?'ออกใหม่':''} · ตอน ${e.n}</span></div>`});
  $('feed').innerHTML=seen.size?h:''}catch(e){}}
$('q').addEventListener('keydown',e=>{if(e.key==='Enter')search()});
home();
</script>"""

if __name__ == "__main__":
    threading.Thread(target=warm_covers, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
