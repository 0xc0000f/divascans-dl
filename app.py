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
import zipfile
from concurrent.futures import ThreadPoolExecutor

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

os.makedirs(os.path.join(DATA_DIR, "downloads"), exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "zips"), exist_ok=True)

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
    out, page = [], 1
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
    body = {"slug": slug, "fmt": fmt, "chapters": out,
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
    body = {"slug": slug, "n": n, "images": urls}
    with LOCK:
        TL[key] = (now + (3600 if urls else 120), 200, body)
    return 200, body


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


def get_chapter_files(slug, n, force=False):
    """Return (ordered list of (img_url, local_path), missing_count)."""
    st, body = chapter_images(slug, series_fmt(slug), n, force=force)
    if st != 200:
        raise RuntimeError(body.get("error", "ssr failed"))
    urls = body["images"]
    pairs = [(u, page_path(slug, n, u)) for u in urls]
    todo, seenp = [], set()
    for u, p in pairs:
        if p not in seenp and not (os.path.exists(p) and os.path.getsize(p) > 0):
            seenp.add(p)
            todo.append((u, p))
    missing = 0
    if todo:
        with ThreadPoolExecutor(max_workers=DL_WORKERS) as ex:
            for _, ok in ex.map(lambda t: fetch_image(*t), todo):
                if not ok:
                    missing += 1
    return pairs, len(urls), missing


# ------------------------------------------------------------------- zip jobs

def build_zip(slug, chapters, job):
    row = catalog_row(slug)
    zname = f"{row.get('title') or slug}.zip"
    zpath = os.path.join(DATA_DIR, "zips", job["key"] + ".zip")
    report = {"series": row.get("title") or slug, "chapters": {}}
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as zf:
        for idx, n in enumerate(chapters, 1):
            if job.get("stop"):
                break
            job["state"] = f"ch {n} ({idx}/{len(chapters)})"
            try:
                pairs, total, missing = get_chapter_files(slug, n)
            except RuntimeError as e:
                report["chapters"][str(n)] = {"error": str(e)}
                job["errors"].append(f"ch{n}: {e}")
                continue
            if total == 0:
                report["chapters"][str(n)] = {"error": "no images (not free yet?)"}
                job["errors"].append(f"ch{n}: no images")
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
            d = chapter_dir(slug, n)
            if got == total:
                try:
                    open(os.path.join(d, ".complete"), "w").write(str(got))
                except OSError:
                    pass
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
    threading.Thread(target=job_worker, args=(key, slug, sorted(ns)), daemon=True).start()
    return jsonify({"key": key, "chapters": len(ns)})


@app.get("/api/job/<key>")
def api_job(key):
    j = JOBS.get(key)
    if not j:
        abort(404)
    return jsonify({k: j[k] for k in ("status", "state", "done", "total", "done_pages", "errors")})


@app.get("/api/job/<key>/zip")
def api_job_zip(key):
    j = JOBS.get(key)
    if not j or j["status"] != "done":
        abort(404)
    fn = os.path.basename(j.get("zname") or "download.zip")
    data = open(j["zpath"], "rb").read()
    return Response(data, mimetype="application/zip",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fn}"})


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
    if st != 200:
        return jsonify(body), st
    return jsonify(body)


@app.get("/img/<path:url>")
def proxy_img(url):
    """Proxy image fetch so the browser is never the one talking to Cloudflare.

    With ?slug=&n= the response comes from (or fills) the real chapter cache
    directory, so previewing also populates downloads/<slug>/ch<n>/.
    """
    if not url.startswith("https://media.divascans.org/"):
        abort(403)
    slug = request.args.get("slug") or "stream"
    n = request.args.get("n") or "0"
    dst = page_path(slug, n, url)
    p, ok = fetch_image(url, dst)
    if not ok:
        abort(404)
    return Response(open(p, "rb").read(), mimetype="image/webp",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/cover/<slug>")
def cover(slug):
    """Stream the series og-image through us (needs browser UA, browser img can't send Referer)."""
    if not re.fullmatch(r"[a-z0-9\-]+", slug):
        abort(404)
    r = requests.get(f"{BASE}/series/{series_fmt(slug)}/{slug}/opengraph-image",
                     headers={"User-Agent": UA_PAGE, "Referer": BASE + "/"},
                     timeout=30)
    if r.status_code != 200:
        abort(404)
    return Response(r.content, mimetype=r.headers.get("Content-Type", "image/png"),
                    headers={"Cache-Control": "public, max-age=86400"})


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


PAGE = """<!doctype html><html lang=th><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>divascans-dl</title>
<style>
body{font-family:system-ui,sans-serif;background:#12141a;color:#e7eaf0;margin:0;padding:24px}
.wrap{max-width:900px;margin:auto}
h1{font-size:22px;margin:0 0 4px}
.sub{color:#8996a3;font-size:13px;margin-bottom:18px}
input[type=text]{width:100%;box-sizing:border-box;padding:11px 12px;border-radius:8px;border:1px solid #2a2f3a;background:#1b1f28;color:#e7eaf0;font-size:15px}
button{background:#3d6df0;border:0;color:#fff;padding:10px 14px;border-radius:8px;font-size:14px;cursor:pointer;margin-top:8px}
button.ghost{background:#262c38}
button:disabled{opacity:.45;cursor:default}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.card{display:flex;gap:12px;padding:12px;background:#1b1f28;border-radius:10px;margin-top:8px;cursor:pointer;align-items:center}
.card:hover{background:#222835}
.card img{width:48px;height:60px;object-fit:cover;border-radius:6px;background:#0d0f14}
.meta{color:#8996a3;font-size:12px}
.pill{background:#262c38;border-radius:20px;padding:2px 8px;font-size:11px;color:#9fb3d1;margin-left:6px}
.pill.f{color:#7fe0a0}.pill.p{color:#e6b06b}
.rows{margin-top:14px;border-top:1px solid #242a35}
.ep{display:flex;align-items:center;gap:10px;padding:9px 4px;border-bottom:1px solid #1d222c}
.ep input{width:16px;height:16px}
.ep .num{width:56px;color:#9fb3d1;font-variant-numeric:tabular-nums}
.prog{margin:14px 0;background:#1b1f28;border-radius:8px;height:26px;position:relative;display:none}
.prog i{position:absolute;left:0;top:0;bottom:0;background:#3d6df0;border-radius:8px;width:0}
.prog span{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:12px}
.err{color:#f28b82;font-size:12px;margin:6px 0;white-space:pre-wrap}
a.zipbtn{background:#2f9e63;border-radius:8px;padding:10px 14px;color:#fff;text-decoration:none;font-size:14px;display:inline-block;margin-top:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;margin-top:12px}
.grid img{width:100%;border-radius:6px;background:#0d0f14;min-height:40px}
.back{color:#7fa5f5;cursor:pointer;font-size:13px;margin-bottom:8px;display:inline-block}
.chip{font-size:11px;background:#262c38;border-radius:6px;padding:2px 7px;color:#9fb3d1;cursor:pointer}
</style>
<div class=wrap>
<h1>divascans-dl</h1>
<div class=sub>วางลิงก์ตอน / ลิงก์เรื่อง หรือค้นชื่อ — ตอนฟรี (รวมที่เว็บปลดฟรีแล้ว) โหลดได้ครบทุกหน้า</div>
<input id=q placeholder="เช่น https://divascans.org/series/comic/love-was-cruel/chapter/12 หรือ love was cruel">
<div class=bar><button onclick=search()>ค้นหา</button>
<button class=ghost onclick=openUrl()>เปิดจากลิงก์</button>
<span id=st style="color:#8996a3;font-size:12px"></span></div>
<div id=out></div>
<div class=prog id=prog><i id=pb></i><span id=pt></span></div>
<div id=err class=err></div>
</div>
<script>
const $=id=>document.getElementById(id);
let CUR=null, TIMER=null;
async function status(){try{const j=await(await fetch('/api/status')).json();
 $('st').textContent=`${j.series} เรื่อง · cache ${j.cached_series} · เหลือ ${j.disk_free_gb} GB · ${j.api_key?'มี feed key':'ไม่มี feed key'}`}catch(e){}}
status();
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function slugOf(u){const m=(u||'').match(/\\/series\\/(?:comic|novel)\\/([a-z0-9-]+)(?:\\/chapter\\/(\\d+))?/);
 return m?{slug:m[1],n:m[2]}:null}
 async function search(){const q=$('q').value.trim(); if(!q)return;
 const s=slugOf(q); if(s&&s.slug){openSeries(s.slug,s.n);return}
 const j=await(await fetch('/api/series?q='+encodeURIComponent(q))).json();
 let h='';
 (j.items||[]).forEach(t=>{h+=`<div class=card onclick="openSeries('${t.slug}')"><img loading=lazy src="/cover/${t.slug}" onerror="this.style.opacity=.15">
 <div><b>${esc(t.title||t.slug)}</b><div class=meta>${t.type} · ${t.total} ตอน<span class="pill f">ฟรี ${t.free}</span><span class="pill p">จ่าย ${t.paid}</span></div></div></div>`});
 $('out').innerHTML=h||'<p class=meta>ไม่พบเรื่อง</p>';
}
function openUrl(){const s=slugOf($('q').value); if(!s){$('err').textContent='วางลิงก์ divascans.org/series/... ก่อน';return}
 openSeries(s.slug,s.n)}
async function openSeries(slug,pre){
 CUR=slug; const j=await(await fetch('/api/series/'+slug)).json();
 if(j.error){$('out').innerHTML='<p class=meta>'+esc(j.error)+'</p>';return}
 const c=j.catalog||{};
 let h=`<span class=back onclick=search()>← เปลี่ยนเรื่อง</span>
 <div class=card style=cursor:default><img loading=lazy src="/cover/${slug}" onerror="this.style.opacity=.15">
 <div><b>${esc(c.title||slug)}</b><div class=meta>${c.type||''} · ${j.total} ตอน · ฟรี ${j.free}<br><span class=meta>${slug}</span></div></div></div>
 <div class=bar style=margin-top:12px><button onclick="dl()">⬇ โหลดตอนที่เลือกเป็น ZIP</button>
 <span class=chip onclick=selAll(1)>เลือกทั้งหมด</span>
 <span class=chip onclick=selFree()>เลือกตอนฟรี</span>
 <span class=chip onclick=selAll(0)>ล้าง</span>
 <button class=ghost id=stop onclick=stopjob() style=display:none>หยุด</button></div>
 <div class=rows>`;
 j.chapters.forEach(e=>{const t=e.free?'<span class="pill f">ฟรี</span>':'<span class="pill p">จ่าย</span>';
 const ck=e.cached?`<span class="pill f">🗃 ${e.cached}</span>`:'';
 h+=`<div class=ep data-free=${e.free?1:0}><input type=checkbox class=ch value=${e.n}>
 <span class=num>ch ${e.n}</span><span style=flex:1>${esc(e.title)||'—'} ${t}${ck}${e.ea?'<span class=pill>early access</span>':''}</span>
 <span class=chip onclick="view(${e.n})">ดูรูป</span></div>`});
 $('out').innerHTML=h+'</div>';
 if(pre){const el=[...document.querySelectorAll('.ch')].find(x=>x.value==pre); if(el){el.checked=true;dl()}}
}
function selAll(v){document.querySelectorAll('.ch').forEach(x=>x.checked=!!v)}
function selFree(){document.querySelectorAll('.ep').forEach(r=>{const i=r.querySelector('.ch'); if(i)i.checked=r.dataset.free=='1'})}
async function view(n){$('err').textContent='';
 document.querySelectorAll('.prev').forEach(x=>x.remove());
 const j=await(await fetch(`/api/chapter/${CUR}/${n}`)).json();
 const im=j.images||[];
 const box=document.querySelector('.rows')||$('out');
 if(!im.length){box.insertAdjacentHTML('afterend',`<p class="meta prev">ch ${n}: ไม่มีรปูใน SSR — ตอนน ้ียังไม่ ปลดฟรี (ไฟล ์ยัง ไม่ถูกอั ปขึ้ นเซิร ์ฟเวอร ์)</p>`);return}
 box.insertAdjacentHTML('afterend',`<div class="meta prev" style=margin-top:10px>ch ${n}: ${im.length} หน้ า (cached ${j.cached||0})</div>
 <div class="grid prev">`+im.map(u=>`<img loading=lazy src="/img/${encodeURIComponent(u)}?slug=${CUR}&n=${n}" onerror="this.style.opacity=.2">`).join('')+`</div>`);
}
let JOBKEY=null;
async function dl(){const sel=[...document.querySelectorAll('.ch:checked')].map(x=>+x.value);
 if(!sel.length){$('err').textContent='เลือกตอนก่่อน';return}
 $('err').textContent=''; $('prog').style.display='block'; $('stop').style.display='inline-block';
 document.querySelectorAll('.zipbtn').forEach(x=>x.remove());
 const j=await(await fetch('/api/chapters',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({slug:CUR,chapters:sel})})).json();
 if(j.error){$('err').textContent=j.error;return}
 JOBKEY=j.key; clearInterval(TIMER); TIMER=setInterval(()=>poll(j.key),900);
}
async function poll(k){try{const j=await(await fetch('/api/job/'+k)).json();
 const pct=j.total?Math.round(j.done/j.total*100):0;
 $('pb').style.width=pct+'%'; $('pt').textContent=`${j.done}/${j.total} ตอน · ${j.done_pages} หน้ า · ${j.state}`;
 if(j.errors.length)$('err').textContent=j.errors.slice(-6).join('\\n');
 if(j.status!=='running'){clearInterval(TIMER); $('stop').style.display='none';
  if(j.status==='done')$('prog').insertAdjacentHTML('afterend',
    `<a class=zipbtn href="/api/job/${k}/zip">📦 ดาวน์โหลด ZIP</a>`);
  else $('err').textContent='โหลดไม่ ส ำเร็จ: '+j.state}}catch(e){}}
async function stopjob(){if(JOBKEY)await fetch('/api/job/'+JOBKEY+'/stop',{method:'POST'})}
</script>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
