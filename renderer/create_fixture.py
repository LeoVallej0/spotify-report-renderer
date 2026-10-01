from pathlib import Path
import json
from datetime import datetime, timezone
import urllib.parse

def svg_data(label, hue):
    svg = f"""<svg xmlns=\"http://www.w3.org/2000/svg\" width=300 height=300><defs><linearGradient id=\"g\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"1\"><stop offset=\"0\" stop-color=\"hsl({hue},70%,88%)\"/><stop offset=\"1\" stop-color=\"hsl({(hue+55)%360},70%,65%)\"/></linearGradient></defs><rect width=\"300\" height=\"300\" rx=24 fill=\"url(#g)\"/><text x=\"150\" y=\"145\" text-anchor=\"middle\" font-family=\"Arial\" font-size=\"44\" font-weight=\"700\" fill=\"#1a3a26\">{label}</text><text x=\"150\" y=\"190\" text-anchor=\"middle\" font-family=\"Arial\" font-size=\"18\" fill=\"#2c3e50\">TRACK COVER</text></svg>"""
    return "data:image/svg+xml;charset=utf-8," + urllib.parse.quote(svg)

tracks = []
for i in range(50):
    tracks.append({
        "name": f"Playlist X Track {i+1:02d}",
        "artists": f"Artist {i%9+1}",
        "durationMs": 180000 + (i%7)*7000,
        "imageUrl": svg_data(f"{i+1:02d}", i*23%360),
    })

payload = {
    "type": "real_render_test",
    "generationMode": "prueba",
    "recordId": "fixture",
    "playlistName": "Playlist X Fixture 50",
    "playlistUrl": "https://open.spotify.com/playlist/example",
    "referenceTime": datetime.now(timezone.utc).isoformat(),
    "callbackUrl": "https://example.invalid/callback",
    "seed": 5000,
    "playlistXTracks": tracks,
    "fillerTracks": [],
}
Path("fixture_payload.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
print("fixture_payload.json created: 50 X tracks, random=0")
