# FPVcognitinon

Hackathon project for displaying an FPV camera stream and collecting Pi telemetry.

## Run the dashboard

Install dependencies and start the Express server:

```bash
npm install
node server.js
```

Find the server computer's LAN address on Linux with:

```bash
hostname -I
```

The dashboard embeds the camera page at `http://192.168.0.196:8889/cam`. That camera service must already be running and reachable from the viewing device.

## Feeder ping monitor

The feeder in `feeder/status.py` repeatedly pings the configured devices, calculates the average response time, and submits the results to the Express server. The dashboard reads those results from `GET /api/stats` and places each device in the ping table on `index.html`.

Install the Python dependency if needed:

```bash
python3 -m pip install requests
```

Run the feeder from the project directory:

```bash
python3 feeder/status.py
```

By default it pings:

```text
192.168.0.192 -> pi-zero
192.168.0.196 -> my-laptop
```

Configure different targets and display names with comma-separated options:

```bash
python3 feeder/status.py \
	--targets 192.168.0.192,192.168.0.196 \
	--names pi-zero,my-laptop
```

The number of names should match the number of target addresses. The feeder currently posts to `http://localhost:3000/api/stats`, so run it on the same machine as the Express server unless `status.py` is updated to support a remote server URL.

The server stores the latest submitted list in memory. Restarting the server clears the table data until the feeder submits the next batch.

## Dashboard features

- Displays the embedded camera stream.
- Measures and displays the browser-to-Express-server ping every two seconds.
- Displays average ping values returned by `GET /api/stats`.
- Provides forward, back, left, right, rotate-left, and rotate-right controls.
- Arrow keys control movement; `Q` rotates left and `E` rotates right.

The movement buttons currently send commands to `POST /api/control` and log them in the server terminal. They do not control motors until `/api/control` is connected to the Pi's motor or flight-controller API.

## API

`POST /api/control`

```json
{ "command": "forward" }
```

Valid commands are `forward`, `back`, `left`, `right`, `rotate-left`, and `rotate-right`.

`POST /api/stats`

```json
{ "ping_times": [{ "name": "my laptop", "time": 0.5 }] }
```

`GET /api/stats` returns the latest submitted ping values.
