"""Live face recognition for the drone video pipeline. Runs on the laptop next to MediaMTX.

  Pi Zero --WebRTC--> laptop MediaMTX "cam" --> stream.py --> laptop MediaMTX "faces" --> Pi 4 / browsers
                                                   |  YOLO11n face detector + SFace recognizer
                                                   +--> POST {SITE_URL}/api/sightings (names for the live site)

    python -m face_id.stream
    SITE_URL=http://<PI4_IP>:3000 RES=1280x720 python -m face_id.stream

Every option is an environment variable or the matching --flag (see --help). It reads the
same MediaMTX path drone_detect.py does, so both can run at once. Setup: face_id/SETUP.md.
"""
import argparse
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.request
from collections import Counter, deque
from pathlib import Path

# Low-latency RTSP for OpenCV's FFmpeg backend; must be set before the first VideoCapture.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from .faceid import UNKNOWN, FaceID  # noqa: E402

GREEN, RED, GREY, WHITE = (0, 200, 0), (0, 0, 255), (160, 160, 160), (255, 255, 255)
ENCODERS = ["libx264", "libopenh264", "h264_nvenc", "h264_qsv", "h264_amf", "h264_mf"]
ENCODER_OPTS = {
    # Browsers' WebRTC wants H.264 without B-frames; baseline where the encoder supports it.
    "libx264": ["-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "baseline"],
    "h264_nvenc": ["-preset", "p1", "-tune", "ll", "-profile:v", "baseline"],
    "h264_qsv": ["-preset", "veryfast"],
}


def env(name, default):
    return os.environ.get(name, default)


def has_gstreamer():
    return "GStreamer:                   YES" in cv2.getBuildInformation()


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


# ---------------------------------------------------------------- input

class FrameSource(threading.Thread):
    """Keeps only the newest frame so a slow model never builds up delay. Reconnects on its own."""

    def __init__(self, src):
        super().__init__(daemon=True)
        self.src = src
        self.lock = threading.Lock()
        self.frame, self.seq, self.last_time = None, 0, 0.0

    def open(self):
        src = self.src
        if src.isdigit():
            return cv2.VideoCapture(int(src))
        if "!" in src:
            return cv2.VideoCapture(src, cv2.CAP_GSTREAMER)
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
        if not cap.isOpened() and src.startswith("rtsp://") and has_gstreamer():
            # Fedora's system OpenCV: same decode path drone_detect.py uses.
            cap = cv2.VideoCapture(
                f"rtspsrc location={src} latency=0 protocols=tcp ! rtph264depay ! h264parse ! openh264dec ! "
                "videoconvert ! video/x-raw,format=BGR ! appsink drop=true max-buffers=1 sync=false",
                cv2.CAP_GSTREAMER)
        return cap

    def run(self):
        is_file = Path(self.src).is_file()
        while True:
            cap = self.open()
            if not cap.isOpened():
                time.sleep(1)
                continue
            print(f"Input connected: {self.src}")
            delay = 1 / (cap.get(cv2.CAP_PROP_FPS) or 30) if is_file else 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                with self.lock:
                    self.frame, self.seq, self.last_time = frame, self.seq + 1, time.time()
                if delay:
                    time.sleep(delay)
            cap.release()
            print("Input lost, reconnecting...")
            time.sleep(1)

    def latest(self):
        with self.lock:
            return self.frame, self.seq, self.last_time


# ---------------------------------------------------------------- output

class FFmpegSink:
    """Pipes raw frames into ffmpeg, which publishes H.264 over RTSP to MediaMTX."""

    def __init__(self, url, size, fps, bitrate, encoder):
        self.url, self.size, self.fps, self.bitrate = url, size, fps, bitrate
        self.ffmpeg = env("FFMPEG", shutil.which("ffmpeg") or "ffmpeg")
        if encoder == "auto":
            listed = subprocess.run([self.ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
            self.candidates = [e for e in ENCODERS if f" {e} " in listed]
        else:
            self.candidates = [encoder]
        if not self.candidates:
            raise SystemExit("ffmpeg has no H.264 encoder (install one with libx264 or libopenh264)")
        self.proc, self.started = None, 0.0

    def start(self):
        enc = self.candidates[0]
        w, h = self.size
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(self.fps), "-i", "-",
               "-an", "-c:v", enc, *ENCODER_OPTS.get(enc, []),
               "-b:v", str(self.bitrate), "-g", str(self.fps), "-bf", "0", "-pix_fmt", "yuv420p",
               "-f", "rtsp", "-rtsp_transport", "tcp", self.url]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        self.started = time.time()
        print(f"Publishing {w}x{h}@{self.fps} via ffmpeg/{enc} -> {self.url}")

    def write(self, frame):
        if self.proc is None or self.proc.poll() is not None:
            if self.proc is not None:
                if time.time() - self.started < 3 and len(self.candidates) > 1:
                    print(f"  encoder {self.candidates[0]} failed, trying {self.candidates[1]}")
                    self.candidates.pop(0)
                else:
                    print("  publisher exited (is MediaMTX running?), retrying in 2 s")
                    time.sleep(2)
            self.start()
        try:
            self.proc.stdin.write(frame.tobytes())
        except (BrokenPipeError, OSError):
            self.proc.kill()


class GStreamerSink:
    """Fallback without ffmpeg: OpenCV + GStreamer over RTMP, as drone_detect.py does on Fedora."""

    def __init__(self, url, size, fps, bitrate):
        host_path = url.split("://", 1)[1].split("/", 1)
        host = host_path[0].split(":")[0]
        self.url = f"rtmp://{host}:1935/{host_path[1]}"
        self.pipeline = ("appsrc ! videoconvert ! video/x-raw,format=I420 ! "
                         f"openh264enc bitrate={bitrate} complexity=low ! h264parse ! "
                         f"flvmux streamable=true ! rtmpsink location={self.url} sync=false")
        self.size, self.fps, self.writer = size, fps, None

    def write(self, frame):
        if self.writer is None or not self.writer.isOpened():
            self.writer = cv2.VideoWriter(self.pipeline, cv2.CAP_GSTREAMER, 0, self.fps, self.size, True)
            print(f"Publishing via GStreamer/RTMP -> {self.url}" if self.writer.isOpened()
                  else "  GStreamer publisher failed to open, retrying")
            if not self.writer.isOpened():
                time.sleep(2)
                return
        self.writer.write(frame)


def make_sink(args, size):
    mode = args.out_mode
    if mode == "auto":
        mode = ("ffmpeg" if shutil.which(env("FFMPEG", "ffmpeg")) else
                "gstreamer" if has_gstreamer() else "none")
    if mode == "ffmpeg":
        return FFmpegSink(args.output, size, args.fps, args.bitrate, args.encoder)
    if mode == "gstreamer":
        return GStreamerSink(args.output, size, args.fps, args.bitrate)
    print("WARNING: no ffmpeg and no GStreamer in OpenCV - not publishing (use SHOW=1 to preview)")
    return None


# ---------------------------------------------------------------- tracking + recognition

class Track:
    def __init__(self, tid, box, now):
        self.id, self.box = tid, box
        self.first_seen = self.last_seen = now
        self.hits, self.since_recog = 1, 10**9
        self.votes = deque(maxlen=7)  # (name, score) from recent recognitions
        self.announced_name = None

    @property
    def name(self):
        if not self.votes:
            return "?"
        return Counter(n for n, _ in self.votes).most_common(1)[0][0]

    @property
    def score(self):
        scores = [s for n, s in self.votes if n == self.name]
        return sum(scores) / len(scores) if scores else 0.0


class FaceTracker:
    """IoU tracker: stable IDs, recognition every few frames, and a majority vote over recent names
    so a single bad frame doesn't flip someone to Unknown."""

    def __init__(self, fid, recog_every, min_face, keep_s=1.0, confirm_hits=2):
        self.fid, self.recog_every, self.min_face = fid, recog_every, min_face
        self.keep_s, self.confirm_hits = keep_s, confirm_hits
        self.tracks, self.next_id = [], 1
        self.events = []

    def update(self, frame, now):
        dets = self.fid.detect(frame)
        pairs = sorted(((iou(t.box, d[:4]), ti, di) for ti, t in enumerate(self.tracks)
                        for di, d in enumerate(dets)), reverse=True)
        used_t, used_d = set(), set()
        for score, ti, di in pairs:
            if score < 0.3 or ti in used_t or di in used_d:
                continue
            t = self.tracks[ti]
            t.box, t.last_seen, t.hits = dets[di][:4], now, t.hits + 1
            used_t.add(ti)
            used_d.add(di)
        for di, d in enumerate(dets):
            if di not in used_d:
                self.tracks.append(Track(self.next_id, d[:4], now))
                self.next_id += 1

        for t in self.tracks:
            if t.last_seen != now:
                continue
            t.since_recog += 1
            x1, y1, x2, y2 = t.box
            if min(x2 - x1, y2 - y1) >= self.min_face and (t.since_recog >= self.recog_every or len(t.votes) < 3):
                t.votes.append(self.fid.recognize(frame, t.box))
                t.since_recog = 0
            if t.hits >= self.confirm_hits and t.votes and t.name != t.announced_name:
                kind = "entered" if t.announced_name is None else "identified"
                self.event(kind, t, now)
                t.announced_name = t.name

        self.expire(now)

    def expire(self, now):
        for t in [t for t in self.tracks if now - t.last_seen > self.keep_s]:
            if t.announced_name is not None:
                self.event("left", t, now)
            self.tracks.remove(t)

    def event(self, kind, t, now):
        dur = now - t.first_seen
        sign = {"entered": "+", "identified": "=", "left": "-"}[kind]
        extra = f" after {dur:.0f}s" if kind == "left" else f" ({t.score:.2f})"
        print(f"{sign} {t.name} #{t.id} {kind}{extra}")
        self.events.append({"event": kind, "name": t.name, "track": t.id, "score": round(t.score, 3),
                            "duration_s": round(dur, 1), "time": int(now * 1000)})


# ---------------------------------------------------------------- live-site reporting

class SightingsReporter(threading.Thread):
    """POSTs who is in view to the live site every half second. Never blocks the video loop."""

    def __init__(self, site_url):
        super().__init__(daemon=True)
        self.url = site_url.rstrip("/") + "/api/sightings"
        self.q = queue.Queue(maxsize=1)
        self.pending_events, self.last_error = [], 0.0

    def submit(self, present, events, signal=True):
        try:
            events = self.q.get_nowait()["events"] + events  # replace an unsent snapshot, keep its events
        except queue.Empty:
            pass
        self.q.put({"present": present, "events": events, "signal": signal})

    def run(self):
        while True:
            payload = self.q.get()
            payload["events"] = self.pending_events + payload["events"]
            req = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=2).read()
                self.pending_events = []
            except OSError as e:
                self.pending_events = payload["events"][-50:]
                if time.time() - self.last_error > 30:
                    print(f"  live site unreachable ({self.url}): {e}")
                    self.last_error = time.time()
            time.sleep(0.5)


# ---------------------------------------------------------------- drawing

def banner(frame, text, color=RED):
    h, w = frame.shape[:2]
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1.0, 2)
    x, y = (w - tw) // 2, h // 2
    cv2.rectangle(frame, (x - 12, y - th - 12), (x + tw + 12, y + 12), (0, 0, 0), -1)
    cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 2)
    return frame


def annotate(frame, tracks, hud):
    for t in tracks:
        x1, y1, x2, y2 = t.box
        name = t.name
        color = GREY if name == "?" else RED if name == UNKNOWN else GREEN
        label = f"{name} #{t.id}" + (f" {t.score:.2f}" if name not in ("?", UNKNOWN) else "")
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        ty = max(th + 6, y1 - 6)
        cv2.rectangle(frame, (x1, ty - th - 6), (x1 + tw + 6, ty + 4), color, -1)
        cv2.putText(frame, label, (x1 + 3, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.6, WHITE, 2)
    cv2.putText(frame, hud, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 4)
    cv2.putText(frame, hud, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, WHITE, 1)
    return frame


# ---------------------------------------------------------------- main

def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default=env("INPUT", "rtsp://127.0.0.1:8554/cam"),
                    help="INPUT: stream URL, webcam index, video file, or GStreamer pipeline")
    ap.add_argument("--output", default=env("OUTPUT", "rtsp://127.0.0.1:8554/faces"),
                    help="OUTPUT: MediaMTX path to publish to (WebRTC at http://<laptop>:8889/faces)")
    ap.add_argument("--out-mode", default=env("OUT_MODE", "auto"), choices=["auto", "ffmpeg", "gstreamer", "none"])
    ap.add_argument("--res", default=env("RES", "640x480"), help="RES: output size, match the camera aspect")
    ap.add_argument("--fps", type=int, default=int(env("FPS", 30)))
    ap.add_argument("--bitrate", type=int, default=int(env("BITRATE", 1500000)))
    ap.add_argument("--encoder", default=env("ENCODER", "auto"), help=f"ENCODER: auto picks from {ENCODERS}")
    ap.add_argument("--site-url", default=env("SITE_URL", ""),
                    help="SITE_URL: live site to send names to, e.g. http://192.168.0.50:3000")
    ap.add_argument("--conf", type=float, default=float(env("CONF", 0.5)), help="CONF: face detector confidence")
    ap.add_argument("--recog-every", type=int, default=int(env("RECOG_EVERY", 5)),
                    help="RECOG_EVERY: re-recognize each tracked face every N frames")
    ap.add_argument("--min-face", type=int, default=int(env("MIN_FACE", 32)),
                    help="MIN_FACE: faces smaller than this (px) are tracked but not named")
    ap.add_argument("--show", action="store_true", default=env("SHOW", "0") == "1", help="SHOW=1: local preview")
    return ap.parse_args()


def main():
    args = parse_args()
    size = tuple(int(v) for v in args.res.lower().split("x"))

    fid = FaceID(conf=args.conf)
    print(f"Detector: {fid.det.weights.name}  |  known: {', '.join(fid.people) or 'nobody yet'}")
    tracker = FaceTracker(fid, args.recog_every, args.min_face)
    source = FrameSource(args.input)
    source.start()
    sink = make_sink(args, size)
    reporter = None
    if args.site_url:
        reporter = SightingsReporter(args.site_url)
        reporter.start()
        print(f"Reporting names to {reporter.url}")

    out_lock = threading.Lock()
    out = {"frame": banner(np.zeros((size[1], size[0], 3), np.uint8), "NO SIGNAL FROM PI")}

    def publish():
        # Steady output rate regardless of model speed, so the WebRTC player never starves.
        nxt = time.time()
        while True:
            with out_lock:
                frame = out["frame"]
            _, _, last = source.latest()
            if last and time.time() - last > 1.5:
                frame = banner(frame.copy(), f"STREAM STALLED {time.time() - last:.0f}s")
            if sink:
                sink.write(frame)
            nxt += 1 / args.fps
            time.sleep(max(0.0, nxt - time.time()))
            if time.time() - nxt > 1:
                nxt = time.time()

    threading.Thread(target=publish, daemon=True).start()

    last_seq, n, t_det, t_report, t_stat = 0, 0, 0.0, 0.0, time.time()
    while True:
        frame, seq, last = source.latest()
        if frame is None or seq == last_seq:
            now = time.time()
            if now - (last or 0) > 1.5 and now - t_report >= 0.5:
                # No video: people can't be "in view", and the site should say the feed is down.
                tracker.expire(now)
                if reporter:
                    reporter.submit([], tracker.events, signal=False)
                tracker.events = []
                t_report = now
            time.sleep(0.003)
            continue
        last_seq = seq
        now = time.time()
        frame = cv2.resize(frame, size) if (frame.shape[1], frame.shape[0]) != size else frame.copy()

        t0 = time.perf_counter()
        tracker.update(frame, now)
        t_det += time.perf_counter() - t0
        n += 1

        shown = [t for t in tracker.tracks if t.hits >= tracker.confirm_hits and t.last_seen == now]
        known = sorted({t.name for t in shown if t.name not in ("?", UNKNOWN)})
        hud = f"faces {len(shown)}" + (f" | {', '.join(known)}" if known else "")
        annotated = annotate(frame, shown, hud)
        with out_lock:
            out["frame"] = annotated

        if reporter and (now - t_report >= 0.5 or tracker.events):
            present = [{"track": t.id, "name": t.name, "score": round(t.score, 3)} for t in shown if t.name != "?"]
            reporter.submit(present, tracker.events)
            tracker.events = []
            t_report = now
        elif not reporter:
            tracker.events = []

        if args.show:
            cv2.imshow("faces", annotated)
            if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                break

        if now - t_stat >= 5:
            print(f"in {n / (now - t_stat):.1f} fps | detect+recognize {t_det / n * 1000:.0f} ms | "
                  f"faces {len(shown)} | known in view {known or '-'}")
            n, t_det, t_stat = 0, 0.0, now


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
