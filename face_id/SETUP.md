# Face recognition in the drone pipeline

This adds names to the drone's live video. The laptop finds faces in the Pi Zero's feed, names the people it knows, and publishes an annotated stream. The live site on the Pi 4 shows that stream along with a list of who is in view.

```
Pi Zero W (camera)        Laptop (192.168.0.196)                                Pi 4 (live site)
------------------        -----------------------------------------------       -------------------------
MediaMTX :8889/cam  --->  MediaMTX path "cam"                                   server.js :3000
   WebRTC                   |                                                     index.html
                            +--> drone_detect.py --> path "detect"  (unchanged)     - video iframe (VIDEO_URL)
                            |                                                       - "Recognized" panel
                            +--> face_id/stream.py --> path "faces" ----WebRTC--->      (polls /api/sightings)
                                   1. YOLO11n face detector (ONNX Runtime)
                                   2. SFace recognizer vs. known_faces/
                                   3. tracker: stable IDs, name voting
                                   +-- POST /api/sightings (names, entered/left) ------^
```

- The Pi Zero doesn't change. `stream.py` reads the same MediaMTX path as `drone_detect.py`, so the Pi still serves only one viewer.
- `drone_detect.py` doesn't change either. The face service publishes to its own path, `faces`, so you can run both, or only one of them.
- The laptop needs only `onnxruntime`, `numpy` and OpenCV. It doesn't need torch or Ultralytics.

## 1. Files the laptop needs

`face_id/models/` and `face_id/known_faces/` are gitignored, so copy them over yourself:

| File | Where it comes from | Needed? |
|---|---|---|
| `face_id/models/face_det.onnx` | Training PC: `python -m face_id.export` | Yes |
| `face_id/models/face_recognition_sface_2021dec.onnx` | Downloads itself on first run | Yes (automatic) |
| `face_id/models/lnsoni_sface.npy` | Training PC; generated on the first `scan`/`identify` | Recommended. It sets each person's match threshold. Without it, the default threshold (0.363) is used |
| `face_id/known_faces/<Name>/*.jpg` | Photos of each person to recognize | Yes, or everyone is "Unknown" |

From the training PC (PowerShell; the destination folder must already exist on the laptop):

```powershell
scp -r face_id\models face_id\known_faces <user>@192.168.0.196:~/FPVcognitinon/face_id/
```

## 2. Laptop setup (Fedora ThinkPad)

Get the code:

```bash
cd ~ && git clone https://github.com/mileskeffer/FPVcognitinon.git   # or: cd ~/FPVcognitinon && git pull
cd ~/FPVcognitinon && git checkout yolo-training
```

Reuse the venv from the `drone_detect.py` setup. It already has Fedora's OpenCV and `onnxruntime`:

```bash
source ~/drone_ai_env/bin/activate
pip install onnxruntime numpy     # no-ops if already installed
python -c "import cv2; cv2.FaceRecognizerSF; print('SFace OK', cv2.__version__)"
```

To publish the annotated video, install `ffmpeg` (preferred):

```bash
sudo dnf install ffmpeg-free      # or RPM Fusion's ffmpeg, which includes libx264
ffmpeg -hide_banner -encoders | grep -E "libx264|libopenh264"   # need at least one
```

Without `ffmpeg`, the script falls back to OpenCV + GStreamer over RTMP (`openh264enc`), the same route `drone_detect.py` uses. Force either route with `OUT_MODE=ffmpeg` or `OUT_MODE=gstreamer`.

## 3. Add the people to recognize

Put photos in `face_id/known_faces/`, one folder per person. The folder name is the name shown:

```
face_id/known_faces/
  Miles/   front.jpg  left.jpg  outdoors.jpg
  Sam/     sam1.png  sam2.png
```

- Use 3–5 photos per person, with different angles and lighting. One clear face per photo.
- Screenshots and phone photos both work. The face only needs to be clearly visible.
- Or capture photos from a webcam: `python -m face_id.faceid enroll Miles` (SPACE saves a photo, Q quits).
- Check a photo before flying: `python -m face_id.faceid identify some_photo.jpg`
- Restart `stream.py` after changing `known_faces/`. The photos are loaded at startup.

## 4. Run it

Three terminals on the laptop (the second is optional):

```bash
# 1. MediaMTX (pulls the Pi's feed into path "cam")
cd ~/mediamtx && ./mediamtx

# 2. Optional: the existing object detector
source ~/drone_ai_env/bin/activate && cd ~ && python drone_detect.py

# 3. Face recognition, sending names to the Pi 4's site
source ~/drone_ai_env/bin/activate && cd ~/FPVcognitinon
SITE_URL=http://<PI4_IP>:3000 python -m face_id.stream
```

Healthy output:

```
  Miles: 4 photo(s), match threshold 0.502
Detector: face_det.onnx  |  known: Miles
Reporting names to http://<PI4_IP>:3000/api/sightings
Publishing 640x480@30 via ffmpeg/libx264 -> rtsp://127.0.0.1:8554/faces
Input connected: rtsp://127.0.0.1:8554/cam
+ Miles #1 entered (0.61)
+ Unknown #2 entered (0.12)
in 24.8 fps | detect+recognize 38 ms | faces 2 | known in view ['Miles']
- Unknown #2 left after 9s
```

Watch it directly at `http://192.168.0.196:8889/faces`.

The video shows `NO SIGNAL FROM PI` before the first frame and `STREAM STALLED Ns` if frames stop. It reconnects by itself, the same as `drone_detect.py`.

## 5. Live site on the Pi 4

The site needs two settings: which stream to show (`VIDEO_URL`) and to accept names from the laptop (built into `server.js`).

```bash
cd ~/FPVcognitinon && git pull && npm install
VIDEO_URL=http://192.168.0.196:8889/faces node server.js
```

Open `http://<PI4_IP>:3000`. Below the ping table, the **Recognized** panel shows:
- who is in view now (green for known people, gold for Unknown),
- the last 10 entered/left events,
- a status line: *N face(s) in view*, *No video from drone*, *Face service offline*, or *Face service not connected*.

Without `VIDEO_URL`, the site shows the raw feed (`http://192.168.0.196:8889/cam`) as before.

### Optional: relay the video through the Pi 4

By default, every browser pulls the video straight from the laptop. To have viewers connect to the Pi 4 instead, so the laptop sends only one copy, run MediaMTX on the Pi 4 with this path:

```yaml
paths:
  faces:
    source: whep://192.168.0.196:8889/faces/whep
```

Then start the site with `VIDEO_URL=http://<PI4_IP>:8889/faces`.

### Firewall

- Laptop: the same ports as before (`8889/tcp`, `8189/udp`). The face service only makes outgoing requests to the Pi 4.
- Pi 4: `3000/tcp` for the site, and `8889/tcp` + `8189/udp` if it relays video.

## Options

Set these as environment variables (as for `drone_detect.py`) or as flags (`python -m face_id.stream --help`).

| Variable | Default | What it does |
|---|---|---|
| `INPUT` | `rtsp://127.0.0.1:8554/cam` | Source: stream URL, webcam index (`0`), video file, or a GStreamer pipeline |
| `OUTPUT` | `rtsp://127.0.0.1:8554/faces` | MediaMTX path to publish to. Set to `.../detect` to replace `drone_detect.py`'s feed on an unchanged site (don't run both then) |
| `SITE_URL` | unset | Live site that receives names, e.g. `http://192.168.0.50:3000`. Unset means no reporting |
| `RES` | `640x480` | Output size. **Must match the camera's aspect ratio** |
| `FPS` / `BITRATE` | `30` / `1500000` | Output stream |
| `OUT_MODE` | `auto` | `ffmpeg`, `gstreamer`, or `none` |
| `ENCODER` | `auto` | ffmpeg encoder. Auto tries `libx264`, `libopenh264`, `h264_nvenc`, `h264_qsv`, `h264_amf`, `h264_mf` |
| `CONF` | `0.5` | Face detector confidence. Lower it to catch more, smaller faces |
| `RECOG_EVERY` | `5` | Re-check each tracked face's identity every N frames |
| `MIN_FACE` | `32` | Faces smaller than this (in pixels) get a box but no name |
| `ORT_THREADS` | all cores | ONNX Runtime threads |
| `SHOW` | `0` | `1` opens a local preview window |

**Speed:** the detector runs at the size it was exported at. The default 640 keeps distant faces. For more fps on the ThinkPad's CPU, re-export at 416 on the training PC (`python -m face_id.export --imgsz 416`) and copy `face_det.onnx` over again.

## API (server.js)

`POST /api/sightings`, sent by `stream.py` every 0.5 s:

```json
{
  "present": [{ "track": 1, "name": "Miles", "score": 0.61 }],
  "events":  [{ "event": "entered", "name": "Miles", "track": 1, "score": 0.61, "duration_s": 0.2, "time": 1791123814368 }],
  "signal": true
}
```

`event` is `entered`, `identified` (an Unknown track turned into a known name) or `left`. `signal` is `false` while the face service is running but receiving no video.

`GET /api/sightings` returns `{ present, log, signal, updated, age_ms }`. `log` holds the last 50 events, newest first, and `age_ms` is the time since the last update (`null` if the service has never connected).

`GET /api/config` returns `{ video_url }`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No detector in face_id/models` | Copy `face_det.onnx` to the laptop (step 1) |
| Everyone is `Unknown` | Check `known_faces/` and the `Name: N photo(s)` lines at startup. Add clearer, more varied photos, and test with `faceid identify` |
| Wrong person named | Add more photos of both people. The startup threshold line shows how strict each match is |
| `publisher exited (is MediaMTX running?)` | Start MediaMTX first. The script retries every 2 s |
| `ffmpeg has no H.264 encoder` | Install an ffmpeg with `libx264` or `libopenh264`, or use `OUT_MODE=gstreamer` |
| Stays on `NO SIGNAL FROM PI` | Check `http://192.168.0.196:8889/cam` plays. On Fedora's OpenCV, the RTSP input falls back to a GStreamer/openh264 pipeline automatically |
| Panel says *Face service not connected* | `SITE_URL` not set, or the laptop can't reach `<PI4_IP>:3000` (the script logs `live site unreachable`) |
| Panel says *Face service offline* | `stream.py` stopped or lost the network more than 5 s ago |
| Low fps | Re-export at `--imgsz 416`, raise `RECOG_EVERY`, or run without `drone_detect.py` |

## Retraining the detector (training PC)

The detector is YOLO11n, fine-tuned on WIDER FACE plus composites built from the LNSONI dataset (`dataset/`). To retrain on the Windows RTX machine:

```powershell
python -m face_id.prepare_wider     # WIDER FACE annotations -> YOLO labels
python -m face_id.build_dataset     # LNSONI composites + data.yaml
python -m face_id.train --name face_det_v2
python -m face_id.export            # -> face_id/models/face_det.onnx, then copy it to the laptop
```

The current weights are from epoch 5 of a 50-epoch run (validation mAP50 0.595 on real and synthetic faces). They work well on clear, close faces. A full run will do better on small and distant faces. Before that run, raise `MIN_SIZE` in `prepare_wider.py` to about 12 px: WIDER's tiny crowd faces overflowed the 3060's 6 GB of GPU memory last time.
