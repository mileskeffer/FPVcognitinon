# Drone AI Video Pipeline

Live video from a Raspberry Pi Zero W camera, YOLO object detection with tracking on a ThinkPad, and a WebRTC viewer anyone on the network can open.

## How it fits together

```
Pi Zero W + IMX219                ThinkPad T14s (Fedora)                          Browser
-----------------                 -----------------------------------------------  -------
MediaMTX (camera, hw H.264)       MediaMTX pulls Pi feed over WebRTC (WHEP)
  WebRTC only, RTSP off    ---->    path "cam"  --> drone_detect.py
  :8889/cam                                         (decode, YOLO11n, tracking)
                                                    --> MediaMTX path "detect"  ---> :8889/detect
```

- The Pi only captures and hardware-encodes. All AI work happens on the laptop.
- The Pi serves exactly one WebRTC viewer: the laptop's MediaMTX. Everyone else watches through the laptop.
- The laptop pulls from the Pi, so the laptop's IP doesn't matter to the Pi.

## Addresses

| Device | IP |
|---|---|
| Raspberry Pi (`RPI-Cam`, user `camera`) | `192.168.0.192` |
| ThinkPad | `192.168.0.196` |
| D-Link router | `192.168.0.1` |

Reserve both IPs in the router's DHCP settings so they don't move after a reboot.

## Viewing

| What | URL |
|---|---|
| Raw Pi camera, relayed by the laptop | `http://localhost:8889/cam` |
| YOLO + tracking feed (on the laptop) | `http://localhost:8889/detect` |
| YOLO + tracking feed (any other device) | `http://192.168.0.196:8889/detect` |

Avoid opening `http://192.168.0.192:8889/cam` (the Pi directly) during a demo. Every direct viewer adds another encrypted stream for the Zero W to push.

---

## Raspberry Pi setup

MediaMTX lives at `/home/camera/mediamtx` (the binary) with its config at `/home/camera/mediamtx.yml`.

### Camera path in `~/mediamtx.yml`

RTSP is disabled in this config (`rtsp: no`) to keep load on the Pi down. Under `paths:` there is exactly one `cam:` block:

```yaml
paths:
  cam:
    source: rpiCamera
    rpiCameraWidth: 640
    rpiCameraHeight: 480
    rpiCameraFPS: 30
    rpiCameraBitrate: 800000
    rpiCameraIDRPeriod: 30
    rpiCameraH264Profile: baseline
```

For 720p, use `1280` / `720` and bitrate `1500000`. YAML does not allow duplicate keys, so there must only be one `cam:`.

### systemd service

`/etc/systemd/system/mediamtx.service`:

```ini
[Unit]
After=network-online.target
Wants=network-online.target

[Service]
User=camera
WorkingDirectory=/home/camera
ExecStart=/home/camera/mediamtx
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mediamtx
journalctl -u mediamtx -f      # look for "using hardware H264 encoder" and "stream is available and online"
```

Running it as a service matters: anything started from an SSH session dies when the Wi-Fi drops the SSH connection.

### Wi-Fi tweak

Wi-Fi power save off, which made SSH and the stream far more responsive:

```bash
sudo nmcli connection modify "$(nmcli -t -f NAME connection show --active | grep -v lo | head -1)" 802-11-wireless.powersave 2
```

---

## ThinkPad setup

### System packages

```bash
sudo dnf install python3-opencv gstreamer1-plugins-good gstreamer1-plugins-bad-free gstreamer1-plugin-openh264
```

`openh264` comes from Fedora's Cisco repo. If it's missing: `sudo dnf config-manager setopt fedora-cisco-openh264.enabled=1`.

### Python environment

The venv must use Fedora's OpenCV, because pip's `opencv-python` is built **without GStreamer** and fails silently.

```bash
python3 -m venv --system-site-packages ~/drone_ai_env
source ~/drone_ai_env/bin/activate
pip install onnxruntime ultralytics          # ultralytics is only needed to export the model
pip uninstall -y opencv-python opencv-python-headless opencv-contrib-python opencv-contrib-python-headless
```

Check it (the path must be under `/usr/lib64`, not inside the venv):

```bash
python -c "import cv2; print(cv2.__file__)"
python -c "import cv2; print(cv2.getBuildInformation())" | grep -i gstreamer    # GStreamer: YES
```

Any later `pip install` can sneak `opencv-python` back in. Re-run the uninstall line if GStreamer goes back to `NO`.

### Model

```bash
cd ~
yolo export model=yolo11n.pt format=onnx imgsz=416
```

This produces `~/yolo11n.onnx`. The script reads the input size and class names from the file.

### MediaMTX on the laptop

```bash
mkdir -p ~/mediamtx && cd ~/mediamtx
wget https://github.com/bluenviron/mediamtx/releases/download/v1.21.1/mediamtx_v1.21.1_linux_amd64.tar.gz
tar xzf mediamtx_v1.21.1_linux_amd64.tar.gz
cat >> mediamtx.yml <<'EOF'
  cam:
    source: whep://192.168.0.192:8889/cam/whep
EOF
```

That `cam:` entry makes the laptop's MediaMTX pull the Pi's WebRTC stream. The stock config already lets local apps publish other paths like `detect`.

### Firewall (so other devices can view)

```bash
sudo firewall-cmd --add-port=8889/tcp --add-port=8189/udp --permanent
sudo firewall-cmd --reload
```

---

## Running it

Pi: nothing to do, the service starts on boot.

ThinkPad, two terminals:

```bash
# Terminal 1
cd ~/mediamtx && ./mediamtx
```

```bash
# Terminal 2
source ~/drone_ai_env/bin/activate
cd ~
python drone_detect.py
```

Healthy output:

```
Model yolo11n.onnx: 416x416, 80 classes, 8 threads
Publishing annotated feed over rtmp: http://localhost:8889/detect
  connected.
+ person #1 entered
in 30.0 fps | read 0 ms  infer 14 ms  track 0.3 ms | tracking 1 | seen {'person': 1}
```

### Options (environment variables, no file edits)

| Variable | Default | Example | What it does |
|---|---|---|---|
| `RES` | `640x480` | `RES=1280x720` | Output size. **Must match the camera's aspect ratio** or the image gets stretched |
| `BITRATE` | `1500000` | `BITRATE=2000000` | Output bitrate for the `/detect` stream |
| `CLASSES` | all | `CLASSES=person` | Only detect/track these classes (comma list) |
| `MODEL` | `yolo11n.onnx` | `MODEL=yolo11s.onnx` | Swap models |
| `OUT_MODE` | auto | `OUT_MODE=rtmp` | Force how it publishes to MediaMTX |

Example for 720p, people only:

```bash
RES=1280x720 BITRATE=2000000 CLASSES=person python drone_detect.py
```

---

## What `drone_detect.py` does

1. **Input:** reads the Pi feed from the laptop's MediaMTX (`rtsp://127.0.0.1:8554/cam`) through GStreamer, decoding with openh264. Reconnects by itself if the feed drops.
2. **Detection:** YOLO11n on ONNX Runtime directly (8 threads, about 14 ms per frame). No Ultralytics or torch at runtime; they added about 40 ms of overhead per frame.
3. **Tracking:** ByteTrack-style tracker (about 0.3 ms per frame):
   - Persistent IDs (`person #3`), color per ID, motion trails
   - A new object needs 3 strong detections in a row before it gets an ID (no flicker IDs)
   - IDs survive about 1.5 s out of view and through people crossing
   - HUD: `now:` (on screen) and `seen:` (unique IDs since start)
   - Terminal events: `+ person #7 entered`, `- person #7 left after 12s`
4. **Output:** publishes the annotated video to the laptop's MediaMTX path `detect` at a steady 30 fps (RTMP, since Fedora's GStreamer lacks `rtspclientsink`). MediaMTX serves it as WebRTC.
5. **Status overlays:** `NO SIGNAL FROM PI` before the first frame, `STREAM STALLED Ns` if frames stop, so a dead feed never just looks frozen.

---

## Lessons from getting here

| Symptom | Cause | Fix |
|---|---|---|
| Black screen, `GStreamer pipeline failed to open` | pip's `opencv-python` has no GStreamer | Fedora `python3-opencv` + venv with `--system-site-packages` |
| YOLO at ~55 ms per frame | Ultralytics/torch overhead | Call ONNX Runtime directly, 8 threads |
| Stream freezes after network blip | Sender ran in the SSH session and died with it | Run MediaMTX as a systemd service |
| `0 packets` arriving | Laptop changed IP after a reboot (`.100` to `.196`) | Laptop pulls from Pi now; reserve IPs in DHCP |
| Network dropped when video started | Raw UDP bursts of huge datagrams over weak Wi-Fi | MediaMTX WebRTC/RTSP, which use MTU-sized packets |
| `no element "rtspclientsink"` | Fedora's `gstreamer1-rtsp-server` doesn't ship that element | Script auto-falls back to RTMP |
| Pi config error `mapping key "cam" already defined` | Two `cam:` blocks in the YAML | Keep exactly one |
| Service `status=203/EXEC` | Wrong binary path in the unit file | `ExecStart=/home/camera/mediamtx` |
| Feed looks squished | `RES` aspect ratio doesn't match the camera | 4:3 camera with 640x480, 16:9 with 1280x720 / 1920x1080 |
