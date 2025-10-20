#!/usr/bin/env python3
import http.server
import json
import os
import pathlib
import subprocess
import tempfile
import urllib.parse
import uuid
import threading
import time

ROOT = pathlib.Path(__file__).parent
WEB = ROOT / "webapp"
DATA = ROOT / "annotations"
DATA.mkdir(exist_ok=True)
TRANSCODE_DIR = WEB / "transcoded"
TRANSCODE_DIR.mkdir(exist_ok=True)

# In-memory job store for transcode progress
JOBS = {}

def _run_transcode_job(job_id, in_path: str, out_path: pathlib.Path, duration_hint: float | None):
    job = JOBS.get(job_id)
    if not job:
        return
    # Probe input duration if not provided
    duration = duration_hint
    if duration is None:
        try:
            out = subprocess.run([
                'ffprobe','-v','quiet','-print_format','json','-show_format', in_path
            ], capture_output=True, check=True)
            info = json.loads(out.stdout.decode('utf-8','ignore'))
            fmt = info.get('format') or {}
            if 'duration' in fmt:
                duration = float(fmt['duration'])
        except Exception:
            duration = None
    job['duration'] = duration

    cmd = [
        'ffmpeg','-y','-hide_banner','-loglevel','error',
        '-i', in_path,
        '-movflags','+faststart',
        '-c:v','libx264','-profile:v','main','-pix_fmt','yuv420p','-preset','veryfast','-crf','23',
        '-c:a','aac','-b:a','128k',
        '-progress','pipe:1','-nostats',
        str(out_path)
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    except FileNotFoundError:
        job.update(status='error', message='ffmpeg not found. Please install ffmpeg.')
        try:
            os.unlink(in_path)
        except Exception:
            pass
        return

    try:
        last_time_ms = 0
        if proc.stdout:
            for line in proc.stdout:
                line = line.strip()
                if line.startswith('out_time_ms='):
                    try:
                        tms = int(line.split('=',1)[1])
                        last_time_ms = tms
                        if duration and duration > 0:
                            job['progress'] = max(0.0, min(1.0, (tms/1000000.0)/duration))
                        else:
                            # unknown duration: show growing fraction (heuristic)
                            job['progress'] = min(0.99, job.get('progress', 0.0) + 0.01)
                    except Exception:
                        pass
                elif line.startswith('progress='):
                    # continue or end
                    if line.endswith('end'):
                        break
        rc = proc.wait()
        if rc == 0:
            job.update(status='done', url=f"/transcoded/{out_path.name}", progress=1.0)
        else:
            err = ''
            try:
                err = proc.stderr.read() if proc.stderr else ''
            except Exception:
                pass
            job.update(status='error', message=(err[-4000:] if err else f'ffmpeg exit code {rc}'))
    finally:
        try:
            os.unlink(in_path)
        except Exception:
            pass


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/annotations":
            qs = urllib.parse.parse_qs(parsed.query)
            vid = (qs.get("video_id") or [None])[0]
            if not vid:
                self.send_error(400, "video_id required")
                return
            p = DATA / f"{vid}.json"
            if not p.exists():
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"{}")
                return
            data = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/api/transcode-status":
            qs = urllib.parse.parse_qs(parsed.query)
            jid = (qs.get('job') or [None])[0]
            if not jid or jid not in JOBS:
                self.send_error(404, "job not found")
                return
            payload = JOBS[jid].copy()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode('utf-8'))
            return

        # serve static files from webapp/
        if self.path == "/":
            return http.server.SimpleHTTPRequestHandler.do_GET(self)

        return http.server.SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/annotations":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                self.send_error(400, "invalid json")
                return
            vid = data.get("videoId")
            if not vid:
                self.send_error(400, "videoId required")
                return
            (DATA / f"{vid}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b"{\"ok\":true}")
            return

        if parsed.path == "/api/probe-duration":
            # Accept multipart/form-data with field name 'file'
            ctype = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in ctype:
                self.send_error(400, "multipart/form-data required")
                return
            try:
                import cgi
                fs = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={
                    'REQUEST_METHOD': 'POST',
                    'CONTENT_TYPE': ctype,
                })
                field = fs['file'] if 'file' in fs else None
                if not field or not getattr(field, 'file', None):
                    self.send_error(400, "file field missing")
                    return
                with tempfile.NamedTemporaryFile(delete=False) as tmp:
                    tmp.write(field.file.read())
                    tmp_path = tmp.name
                # Run ffprobe
                try:
                    out = subprocess.run([
                        'ffprobe','-v','quiet','-print_format','json','-show_format','-show_streams', tmp_path
                    ], capture_output=True, check=True)
                except FileNotFoundError:
                    os.unlink(tmp_path)
                    self.send_error(501, "ffprobe not found. Please install ffmpeg.")
                    return
                except subprocess.CalledProcessError as e:
                    os.unlink(tmp_path)
                    msg = e.stderr.decode('utf-8', 'ignore') if e.stderr else str(e)
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "ffprobe_failed", "message": msg[-4000:]}).encode('utf-8'))
                    return
                finally:
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
                try:
                    info = json.loads(out.stdout.decode('utf-8', 'ignore'))
                    duration = None
                    # Prefer format.duration
                    if isinstance(info, dict):
                        fmt = info.get('format') or {}
                        if 'duration' in fmt:
                            duration = float(fmt['duration'])
                        else:
                            # fallback: max stream duration
                            streams = info.get('streams') or []
                            for st in streams:
                                d = st.get('duration')
                                if d is not None:
                                    duration = max(duration or 0.0, float(d))
                    if duration is None:
                        raise ValueError('duration not found')
                except Exception:
                    self.send_error(400, "failed to parse ffprobe output")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"duration": duration}).encode('utf-8'))
                return
            except Exception as e:
                # Generic fallback when multipart parsing or processing fails unexpectedly
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "bad_request", "message": str(e)}).encode('utf-8'))
                return

        if parsed.path == "/api/transcode":
            # Transcode uploaded video to H.264 MP4 for browser playback
            ctype = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in ctype:
                self.send_error(400, "multipart/form-data required")
                return
            try:
                import cgi
                fs = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={
                    'REQUEST_METHOD': 'POST',
                    'CONTENT_TYPE': ctype,
                })
                field = fs['file'] if 'file' in fs else None
                if not field or not getattr(field, 'file', None):
                    self.send_error(400, "file field missing")
                    return
                with tempfile.NamedTemporaryFile(delete=False, suffix=pathlib.Path(field.filename or 'in').suffix) as tmp:
                    tmp.write(field.file.read())
                    in_path = tmp.name
                out_name = f"{uuid.uuid4().hex}.mp4"
                out_path = TRANSCODE_DIR / out_name
                # ffmpeg transcode to H.264 + AAC, faststart for progressive playback
                try:
                    subprocess.run([
                        'ffmpeg','-y','-i', in_path,
                        '-movflags','+faststart',
                        '-c:v','libx264','-profile:v','main','-pix_fmt','yuv420p','-preset','veryfast','-crf','23',
                        '-c:a','aac','-b:a','128k',
                        str(out_path)
                    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                except FileNotFoundError:
                    os.unlink(in_path)
                    self.send_error(501, "ffmpeg not found. Please install ffmpeg.")
                    return
                except subprocess.CalledProcessError as e:
                    os.unlink(in_path)
                    msg = e.stderr.decode('utf-8', 'ignore') if e.stderr else str(e)
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "ffmpeg_failed", "message": msg[-4000:]}).encode('utf-8'))
                    return
                finally:
                    try:
                        os.unlink(in_path)
                    except Exception:
                        pass
                # Optionally probe duration of output
                duration = None
                try:
                    out = subprocess.run([
                        'ffprobe','-v','quiet','-print_format','json','-show_format', str(out_path)
                    ], capture_output=True, check=True)
                    info = json.loads(out.stdout.decode('utf-8','ignore'))
                    fmt = info.get('format') or {}
                    if 'duration' in fmt:
                        duration = float(fmt['duration'])
                except Exception:
                    pass
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                payload = {"url": f"/transcoded/{out_name}"}
                if duration:
                    payload["duration"] = duration
                self.wfile.write(json.dumps(payload).encode('utf-8'))
                return
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "bad_request", "message": str(e)}).encode('utf-8'))
                return

        if parsed.path == "/api/transcode-start":
            # Start async transcode with progress polling
            ctype = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in ctype:
                self.send_error(400, "multipart/form-data required")
                return
            try:
                import cgi
                fs = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={
                    'REQUEST_METHOD': 'POST',
                    'CONTENT_TYPE': ctype,
                })
                field = fs['file'] if 'file' in fs else None
                if not field or not getattr(field, 'file', None):
                    self.send_error(400, "file field missing")
                    return
                with tempfile.NamedTemporaryFile(delete=False, suffix=pathlib.Path(field.filename or 'in').suffix) as tmp:
                    tmp.write(field.file.read())
                    in_path = tmp.name
                out_name = f"{uuid.uuid4().hex}.mp4"
                out_path = TRANSCODE_DIR / out_name

                # Duration hint via ffprobe (best-effort)
                duration_hint = None
                try:
                    out = subprocess.run([
                        'ffprobe','-v','quiet','-print_format','json','-show_format', in_path
                    ], capture_output=True, check=True)
                    info = json.loads(out.stdout.decode('utf-8','ignore'))
                    fmt = info.get('format') or {}
                    if 'duration' in fmt:
                        duration_hint = float(fmt['duration'])
                except Exception:
                    pass

                job_id = uuid.uuid4().hex
                JOBS[job_id] = { 'status': 'running', 'progress': 0.0, 'message': '', 'url': None, 'duration': duration_hint }
                th = threading.Thread(target=_run_transcode_job, args=(job_id, in_path, out_path, duration_hint), daemon=True)
                th.start()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"job": job_id}).encode('utf-8'))
                return
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "bad_request", "message": str(e)}).encode('utf-8'))
                return

        self.send_error(404, "not found")

    def translate_path(self, path):
        # serve files from webapp folder
        path = super().translate_path(path)
        # Replace cwd with WEB to pin serving root
        rel = os.path.relpath(path, os.getcwd())
        return str(WEB / rel)


if __name__ == "__main__":
    os.chdir(WEB)
    port = int(os.environ.get("PORT", "8000"))
    http.server.test(HandlerClass=Handler, port=port)
