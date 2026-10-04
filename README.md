# FPVcognitinon
hackathon project for HD:CR 2026 decicated to processing FPV drone camera data and utilizing facial recognition software with it

## Stream from another device

1. On the computer running the server, install dependencies and start it:

	```bash
	npm install
	npm start
	```

2. For a phone or another computer to share its camera, serve the app over HTTPS. Browsers block camera access on a normal LAN HTTP address. Set the certificate paths before starting:

	```bash
	HTTPS_KEY=/path/to/key.pem HTTPS_CERT=/path/to/cert.pem npm start
	```

3. Find the server computer's local IP address. On Linux, use `hostname -I`.

4. Open the sender page on the camera device and the viewer page on the viewing device, using the same room name:

	```text
	https://SERVER_IP:3000/sender.html?room=drone1
	https://SERVER_IP:3000/viewer.html?room=drone1
	```

The sender device captures its camera with WebRTC. The viewer receives the video peer-to-peer. The server only exchanges WebRTC setup messages. Both devices must be able to reach the server, and a TURN server may be required when they are on different networks.

## Face recognition

The laptop can name the people in the drone's video. `face_id/stream.py` reads the Pi's feed from MediaMTX, finds faces with a YOLO11n face detector, matches them against photos in `face_id/known_faces/`, publishes an annotated stream to the MediaMTX path `faces`, and sends the names to this site's **Recognized** panel.

Setup for the laptop and the Pi 4: [face_id/SETUP.md](face_id/SETUP.md).
