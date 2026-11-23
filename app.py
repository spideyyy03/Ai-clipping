#!/usr/bin/env python3
"""
Clip Finder server. Paste a YouTube link, get cut vertical clips back.

Standard library only. Needs yt-dlp and ffmpeg on the machine (the Dockerfile has both).

Env vars (all optional):
  PORT               default 8000
  APP_TOKEN          if set, every request needs ?key=<token>. SET THIS when hosting publicly.
  ANTHROPIC_API_KEY  if set, Claude writes a hook and caption per clip
  YT_COOKIES         contents of a cookies.txt, for when YouTube blocks the host's IP
  MAX_VIDEO_MINUTES  default 90
  CONCURRENCY        default 1 (jobs at once)
  DATA_DIR           default /tmp/clip-jobs
"""
import json, os, re, subprocess, sys, threading, time, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
CLIPPER = os.environ.get("CLIPPER", os.path.join(ROOT, "..", "skills", "clip", "clipper.py"))
DATA = os.environ.get("DATA_DIR", "/tmp/clip-jobs")
TOKEN = os.environ.get("APP_TOKEN", "")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MAX_MIN = int(os.environ.get("MAX_VIDEO_MINUTES", "90"))
SLOT = threading.Semaphore(int(os.environ.get("CONCURRENCY", "1")))
JOBS = {}
YT = re.compile(r"^https?://((www|m)\.)?(youtube\.com|youtu\.be)/", re.I)


def explain(err):
    e = err or ""
    if "Sign in to confirm" in e or "not a bot" in e:
        return "YouTube blocked this server. Set YT_COOKIES (see README) and try again."
    if "does not pass filter" in e:
        return "That video is longer than %d minutes." % MAX_MIN
    if "Private video" in e or "unavailable" in e:
        return "That video is private or unavailable."
    return (e.strip().splitlines() or ["Download failed."])[-1][:200]


def fetch_media(url, d):
    """Download video + captions. Returns (video_path, srt_path). Tests replace this."""
    cmd = ["yt-dlp", "--no-playlist", "--match-filter", "duration<=%d" % (MAX_MIN * 60),
           "-f", "bv*[height<=720]+ba/b[height<=720]/b", "--merge-output-format", "mp4",
           "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en", "--convert-subs", "srt",
           "-o", os.path.join(d, "video.%(ext)s"), url]
    if os.environ.get("YT_COOKIES"):
        cf = os.path.join(d, "cookies.txt")
        open(cf, "w").write(os.environ["YT_COOKIES"])
        cmd[1:1] = ["--cookies", cf]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    video = os.path.join(d, "video.mp4")
    if r.returncode or not os.path.exists(video):
        raise RuntimeError(explain(r.stderr))
    srts = sorted(f for f in os.listdir(d) if f.endswith(".srt"))
    if not srts:
        raise RuntimeError("This video has no English captions, so there is nothing to find clips in.")
    return video, os.path.join(d, srts[0])


def write_hooks(clips):
    if not API_KEY:
        return
    items = "\n\n".join("id %d: %s" % (c["rank"], c["text"][:700]) for c in clips)
    prompt = ("Write a hook and caption for each short-form clip below. Return ONLY a JSON array of "
              '{"id":number,"hook":string,"caption":string}. hook: at most 8 words, true to the clip. '
              "caption: one sentence. Never invent numbers, names or claims not in the clip text.\n\n" + items)
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({"model": "claude-sonnet-5-5", "max_tokens": 1500,
                         "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        txt = json.load(urllib.request.urlopen(req, timeout=60))["content"][0]["text"]
        by = {d["id"]: d for d in json.loads(re.search(r"\[.*\]", txt, re.S).group(0))}
    except Exception:
        return
    for c in clips:
        d = by.get(c["rank"])
        if d:
            c["hook"], c["caption"] = str(d.get("hook", c["hook"])), str(d.get("caption", c["caption"]))


def run_job(jid, url, count, mx):
    job, d = JOBS[jid], os.path.join(DATA, jid)
    os.makedirs(d, exist_ok=True)
    try:
        with SLOT:
            job.update(status="working", message="Downloading video and captions…")
            video, srt = fetch_media(url, d)
            job["message"] = "Finding the best moments…"
            out = os.path.join(d, "clips")
            r = subprocess.run([sys.executable, CLIPPER, srt, "--video", video, "--count", str(count),
                                "--max", str(mx), "--min", str(min(20, mx // 2)), "--out", out],
                               capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError((r.stderr.strip().splitlines() or ["Clipping failed."])[-1])
            clips = json.load(open(os.path.join(out, "clips.json")))
            for c in clips:
                c["hook"] = " ".join(c["first_line"].rstrip(".?!").split()[:8])
                c["caption"] = c["first_line"]
            write_hooks(clips)
            job["message"] = "Cutting clips…"
            r = subprocess.run(["bash", os.path.join(out, "cut.sh")], capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError("Cutting failed: " + r.stderr[-200:])
            os.remove(video)
        job["clips"] = [{"rank": c["rank"], "start": c["start"], "end": c["end"], "score": c["score"],
                         "hook": c["hook"], "caption": c["caption"],
                         "url": "/files/%s/%s" % (jid, c["file"])} for c in clips]
        job.update(status="done", message="")
    except Exception as e:
        job.update(status="error", message=str(e))


def sweeper():
    while True:
        time.sleep(600)
        for jid in list(JOBS):
            if time.time() - JOBS[jid]["t"] > 7200:
                subprocess.run(["rm", "-rf", os.path.join(DATA, jid)])
                JOBS.pop(jid, None)


PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Clip Finder</title>
<style>:root{color-scheme:light dark;--a:#2f4bff}*{box-sizing:border-box}
body{margin:0;font:16px/1.5 system-ui,sans-serif;padding:max(16px,env(safe-area-inset-top)) 16px 48px}
main{max-width:560px;margin:0 auto}h1{font-size:32px;letter-spacing:-.03em;margin:8px 0}
input,select,button{font:inherit;width:100%;padding:12px;border-radius:10px;border:1px solid #8885;background:transparent;color:inherit}
label{display:block;font-weight:600;font-size:14px;margin:14px 0 6px}.r{display:flex;gap:10px}.r>div{flex:1}
.go{background:var(--a);color:#fff;border:0;font-weight:800;margin-top:16px;padding:15px}.go:disabled{opacity:.5}
.c{border:1px solid #8885;border-radius:14px;padding:14px;margin-top:14px}.c video{width:100%;max-height:70vh;border-radius:10px;background:#000}
.h{font-size:20px;font-weight:800;line-height:1.15;margin:10px 0 4px}.m{opacity:.7;font-size:14px;margin:0}
.b{display:flex;gap:8px;margin-top:10px}.b a,.b button{flex:1;text-align:center;text-decoration:none;color:inherit;padding:10px;border:1px solid #8885;border-radius:8px;font-weight:600;font-size:14px}
.chk{display:flex;gap:10px;align-items:flex-start;font-size:14px;margin-top:14px}.chk input{width:auto;margin-top:4px}
#s{margin-top:16px}.e{color:#d33}</style></head><body><main>
<h1>Clip Finder</h1><p class="m">Paste a YouTube link. Get vertical clips with captions.</p>
<label for="u">YouTube link</label><input id="u" type="url" inputmode="url" placeholder="https://youtu.be/…">
<div class="r"><div><label for="n">Clips</label><select id="n"><option>3</option><option selected>5</option><option>8</option></select></div>
<div><label for="x">Longest clip</label><select id="x"><option value="30">30 sec</option><option value="45">45 sec</option><option value="60" selected>60 sec</option></select></div></div>
<label class="chk"><input type="checkbox" id="ok"><span>I own this video or have permission to clip it.</span></label>
<button class="go" id="g">Find clips</button><div id="s"></div><div id="o"></div></main>
<script>
var K=new URLSearchParams(location.search).get("key")||"",$=function(i){return document.getElementById(i)};
function q(p){return p+(K?(p.indexOf("?")>-1?"&":"?")+"key="+encodeURIComponent(K):"")}
function t(s){s=Math.round(s);return Math.floor(s/60)+":"+String(s%60).padStart(2,"0")}
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]})}
function say(m,e){$("s").innerHTML=m?'<span class="'+(e?"e":"")+'">'+esc(m)+"</span>":""}
async function poll(id){
 var j=await (await fetch(q("/api/jobs/"+id))).json();
 if(j.status==="working"){say(j.message);return setTimeout(function(){poll(id)},2500)}
 $("g").disabled=false;
 if(j.status!=="done"){say(j.message||"Something went wrong.",true);return}
 say("");$("o").innerHTML=j.clips.map(function(c){var u=q(c.url);
  return '<div class="c"><video src="'+u+'" controls playsinline preload="metadata"></video><p class="h">'+esc(c.hook)+
  '</p><p class="m">'+esc(c.caption)+'</p><p class="m">'+t(c.start)+" to "+t(c.end)+" in the video</p>"+
  '<div class="b"><a href="'+u+(u.indexOf("?")>-1?"&":"?")+'dl=1" download>Download</a><button data-t="'+esc(c.hook+"\\n"+c.caption)+'">Copy text</button></div></div>'}).join("");
 [].forEach.call(document.querySelectorAll("[data-t]"),function(b){b.onclick=function(){try{navigator.clipboard.writeText(b.dataset.t);b.textContent="Copied"}catch(e){}}})
}
$("g").onclick=async function(){
 $("o").innerHTML="";if(!$("ok").checked){say("Confirm you have permission to clip this video.",true);return}
 $("g").disabled=true;say("Starting…");
 var r=await fetch(q("/api/jobs"),{method:"POST",body:JSON.stringify({url:$("u").value.trim(),count:+$("n").value,max:+$("x").value,ok:true})});
 var j=await r.json();if(!r.ok){$("g").disabled=false;say(j.error,true);return}poll(j.id)}
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _authed(self, qs):
        return not TOKEN or qs.get("key", [""])[0] == TOKEN

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/":
            b = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            return self.wfile.write(b)
        if not self._authed(qs):
            return self._json(401, {"error": "Add ?key=YOUR_TOKEN to the address."})
        m = re.match(r"^/api/jobs/([0-9a-f]{12})$", u.path)
        if m:
            j = JOBS.get(m.group(1))
            return self._json(200, j) if j else self._json(404, {"error": "Unknown job."})
        m = re.match(r"^/files/([0-9a-f]{12})/(clip_\d\d\.mp4)$", u.path)
        p = os.path.join(DATA, m.group(1), "clips", m.group(2)) if m else ""
        if p and os.path.isfile(p):
            return self._file(p, "dl" in qs)
        self._json(404, {"error": "Not found."})

    def _file(self, p, dl):
        size = os.path.getsize(p)
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else end
            else:
                start = max(0, size - int(m.group(2)))
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        if dl:
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % os.path.basename(p))
        self.end_headers()
        with open(p, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                buf = f.read(min(65536, left))
                if not buf:
                    break
                try:
                    self.wfile.write(buf)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(buf)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/jobs":
            return self._json(404, {"error": "Not found."})
        if not self._authed(parse_qs(u.query)):
            return self._json(401, {"error": "Add ?key=YOUR_TOKEN to the address."})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            url, count, mx = str(body["url"]).strip(), int(body["count"]), int(body["max"])
        except Exception:
            return self._json(400, {"error": "Bad request."})
        if not YT.match(url):
            return self._json(400, {"error": "Paste a youtube.com or youtu.be link."})
        if not body.get("ok"):
            return self._json(400, {"error": "Confirm you have permission to clip this video."})
        if sum(1 for j in JOBS.values() if j["status"] == "working") >= 3:
            return self._json(429, {"error": "Busy. Try again in a few minutes."})
        jid = uuid.uuid4().hex[:12]
        JOBS[jid] = {"id": jid, "status": "working", "message": "Queued…", "clips": [], "t": time.time()}
        threading.Thread(target=run_job, args=(jid, url, max(1, min(count, 10)), max(20, min(mx, 90))),
                         daemon=True).start()
        self._json(200, {"id": jid})


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    threading.Thread(target=sweeper, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    print("Clip Finder on http://0.0.0.0:%d%s" % (port, "  (token required)" if TOKEN else "  (NO TOKEN SET)"))
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
