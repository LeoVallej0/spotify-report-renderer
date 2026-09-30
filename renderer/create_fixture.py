from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def track(i: int, origin: str) -> dict:
    artists = [
        'Artist Alpha', 'Artist Beta', 'Artist Gamma', 'Artist Alpha',
        'Artist Delta', 'Artist Beta', 'Artist Epsilon', 'Artist Zeta',
    ]
    return {
        'name': f'{"Official" if origin == "playlist_x" else "Random"} Track {i:02d}',
        'artists': artists[i % len(artists)],
        'durationMs': 180000 + ((i * 17321) % 150000),
    }

x = [track(i, 'playlist_x') for i in range(30)]
random_pool = [
    {
        'name': f'Random Pool Track {i:04d}',
        'artists': f'Pool Artist {i % 300:03d}',
        'durationMs': 45000 + ((i * 73421) % 260000),
    }
    for i in range(2000)
]

payload = {
    'type': 'real_render_test',
    'recordId': 'FIXTURE_30_20',
    'playlistName': 'Chromium renderer test',
    'referenceTime': '2026-09-30T12:00:00',
    'seed': 20260930,
    'playlistXTracks': x,
    'fillerTracks': random_pool,
}
Path('fixture_payload.json').write_text(
    json.dumps(payload, ensure_ascii=False, indent=2),
    encoding='utf-8',
)
print('fixture_payload.json created: 30 official + pool 2000')
