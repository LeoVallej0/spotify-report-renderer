from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime, timezone

tracks = []
for i in range(30):
    tracks.append({
        "name": f"Playlist X Track {i+1:02d}",
        "artists": "Example Artist",
        "durationMs": 180000,
        "imageUrl": "https://i.scdn.co/image/ab67616d000048518104def19a9fb7d075955144",
    })

filler = []
for i in range(20):
    filler.append({
        "name": f"Random Track {i+1:02d}",
        "artists": f"Random Artist {i%7+1}",
        "durationMs": 195000,
        "imageUrl": "https://i.scdn.co/image/ab67616d000048518104def19a9fb7d075955144",
    })

payload = {
    "type": "real_render_test",
    "generationMode": "prueba",
    "recordId": "fixture",
    "playlistName": "Fixture",
    "playlistUrl": "",
    "referenceTime": datetime.now(timezone.utc).isoformat(),
    "callbackUrl": "https://example.invalid/callback",
    "seed": 5000,
    "playlistXTracks": tracks,
    "fillerTracks": filler,
}
Path("fixture_payload.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
print("fixture_payload.json created")
