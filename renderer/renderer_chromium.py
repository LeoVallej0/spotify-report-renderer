from __future__ import annotations

import argparse
import csv
import json
import random
import re
import shutil
import os
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any

from PIL import Image
from playwright.sync_api import sync_playwright

TARGET_ROWS = 50
VIEWPORT_WIDTH = 1445
VIEWPORT_HEIGHT = 2047
DEVICE_SCALE_FACTOR = 2
OUTPUT_WIDTH = 2890
CROP_HEIGHT_PX = 3090


def parse_dt(value: str) -> datetime:
    text = str(value or '').strip()
    if not text:
        raise ValueError('referenceTime is empty')
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def normalize_track(track: dict[str, Any], origin: str) -> dict[str, Any]:
    name = str(track.get('name', '')).strip()
    artists = str(track.get('artists', '')).strip()
    raw_duration = track.get('durationMs', track.get('duration_ms', 0))
    duration_ms = int(float(raw_duration or 0))

    if not name:
        raise ValueError('Track without name')
    if duration_ms <= 0:
        raise ValueError(f'Invalid duration for track: {name}')

    return {
        'name': name,
        'artists': artists,
        'duration_ms': duration_ms,
        'origin': origin,
    }


def track_key(track: dict[str, Any]) -> tuple[str, str, int]:
    return (
        str(track['name']),
        str(track['artists']),
        int(track['duration_ms']),
    )


def choose_filler(
    x_tracks: list[dict[str, Any]],
    pool: list[dict[str, Any]],
    needed: int,
    seed: int,
) -> list[dict[str, Any]]:
    if needed <= 0:
        return []

    # Filler is independent of X: overlap with X is allowed. The only
    # protected data is X itself; it is never filtered, reordered, or edited.
    unique: dict[tuple[str, str, int], dict[str, Any]] = {}

    for track in pool:
        unique.setdefault(track_key(track), track)

    candidates = list(unique.values())
    if len(candidates) < needed:
        raise ValueError(
            f'Random pool contains only {len(candidates)} eligible tracks; '
            f'{needed} required.'
        )

    rng = random.Random(seed)
    rng.shuffle(candidates)

    # The anti-consecutive-artist rule applies ONLY to filler tracks.
    chosen: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    last_artist = None

    for track in candidates:
        artist = track['artists']
        if chosen and artist == last_artist:
            deferred.append(track)
            continue

        chosen.append(track)
        last_artist = artist
        if len(chosen) == needed:
            return chosen

    rng.shuffle(deferred)
    for track in deferred:
        chosen.append(track)
        if len(chosen) == needed:
            return chosen

    raise RuntimeError('Could not select the requested filler tracks.')


def random_gap_seconds(rng: random.Random) -> int:
    """Variable, non-uniform synthetic listening gaps for filler only."""
    p = rng.random()

    if p < 0.55:
        return rng.randint(15, 120)       # 15 s – 2 min
    if p < 0.85:
        return rng.randint(121, 300)      # ~2 – 5 min
    if p < 0.97:
        return rng.randint(301, 900)      # 5 – 15 min
    return rng.randint(901, 1800)         # 15 – 30 min, occasional


def build_history_50(
    x_tracks: list[dict[str, Any]],
    pool: list[dict[str, Any]],
    reference: datetime,
    seed: int,
) -> list[dict[str, Any]]:
    # Playlist X is the authoritative block.
    # When X exceeds the 50-row target, keep the last 50 source tracks.
    if len(x_tracks) > TARGET_ROWS:
        x_tracks = x_tracks[-TARGET_ROWS:]

    needed = TARGET_ROWS - len(x_tracks)
    filler = choose_filler(x_tracks, pool, needed, seed + 271828)

    rows: list[dict[str, Any]] = []
    current = reference

    # STRICT BLOCK: Playlist X is never reordered, filtered, de-duplicated,
    # artist-collapsed, or given synthetic gaps. Timestamp first, duration second.
    for track in reversed(x_tracks):
        rows.append({
            'name': track['name'],
            'artists': track['artists'],
            'duration_ms': track['duration_ms'],
            'origin': 'playlist_x',
            'played_at': current,
            'gap_s': 0,
        })
        current -= timedelta(milliseconds=track['duration_ms'])

    # SYNTHETIC BLOCK: filler lies strictly before X in time. Only this block
    # gets variable gaps, so its timestamps do not look like uninterrupted playback.
    gap_rng = random.Random(seed + 314159)
    for track in reversed(filler):
        gap_s = random_gap_seconds(gap_rng)
        current -= timedelta(seconds=gap_s)

        rows.append({
            'name': track['name'],
            'artists': track['artists'],
            'duration_ms': track['duration_ms'],
            'origin': 'random',
            'played_at': current,
            'gap_s': gap_s,
        })

        current -= timedelta(milliseconds=track['duration_ms'])

    if len(rows) != TARGET_ROWS:
        raise RuntimeError(f'Expected {TARGET_ROWS} rows, got {len(rows)}')

    return rows


def load_template(path: str | Path) -> str:
    text = Path(path).read_text(encoding='utf-8')
    if 'id="tracks-body"' not in text:
        raise ValueError('Template does not contain #tracks-body')
    return text


def inject_rows(template_html: str, rows: list[dict[str, Any]]) -> str:
    rows_html = ''.join(
        f'<tr data-v-b34d8fe2="">'
        f'<td data-v-b34d8fe2="">{escape(row["name"])}</td>'
        f'<td data-v-b34d8fe2="">{escape(row["artists"])}</td>'
        f'<td data-v-b34d8fe2="">{row["played_at"].strftime("%d/%m/%Y, %H:%M")}</td>'
        f'</tr>'
        for row in rows
    )

    pattern = r'<tbody id="tracks-body" class="notranslate" data-v-b34d8fe2="">.*?</tbody>'
    replacement = (
        '<tbody id="tracks-body" class="notranslate" data-v-b34d8fe2="">'
        + rows_html
        + '</tbody>'
    )
    result, count = re.subn(pattern, replacement, template_html, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError('Could not locate #tracks-body in template')

    # The server renderer cannot execute the browser year updater.
    result = result.replace(
        '<span id="current-year"></span>',
        str(rows[0]['played_at'].year),
    )

    # Remove JavaScript/control UI. The report itself is already pure HTML/CSS.
    result = re.sub(r'<script\b[^>]*>.*?</script>', '', result, flags=re.S | re.I)
    # Remove the outer control panel conservatively. It is outside the capture area.
    result = re.sub(r'\s*<div id="control-panel"[\s\S]*?(?=</body>)', '', result, count=1, flags=re.I)

    # Browser-renderer CSS. We keep the source template intact and only ensure
    # the original desktop navigation/layout is visible in the headless browser.
    renderer_css = '''<style>
html, body { margin: 0 !important; padding: 0 !important; }
.navbar.fixed-top { position: static !important; }
.navbar-expand-sm .navbar-collapse { display: flex !important; flex-basis: auto !important; }
.navbar-expand-sm .navbar-nav { flex-direction: row !important; }
.navbar-expand-sm .navbar-toggler { display: none !important; }
#control-panel { display: none !important; }
</style>'''
    return result.replace('<body>', renderer_css + '<body>', 1)


def render_chromium(html: str, png_path: Path) -> dict[str, Any]:
    png_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path = png_path.with_name(png_path.stem + '_raw.png')

    with sync_playwright() as playwright:
        executable = (
            os.environ.get('CHROMIUM_EXECUTABLE')
            or shutil.which('chromium')
            or shutil.which('google-chrome')
        )
        launch_kwargs = {
            'headless': True,
            'args': [
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
            ],
        }
        if executable:
            launch_kwargs['executable_path'] = executable
        else:
            launch_kwargs['channel'] = 'chromium'

        browser = playwright.chromium.launch(**launch_kwargs)
        try:
            page = browser.new_page(
                viewport={
                    'width': VIEWPORT_WIDTH,
                    'height': VIEWPORT_HEIGHT,
                },
                device_scale_factor=DEVICE_SCALE_FACTOR,
            )
            page.set_content(html, wait_until='load', timeout=30000)
            page.wait_for_timeout(250)

            # Capture ONLY the report area. This avoids the viewport's unused
            # bottom space and gives us a stable physical image size at DPR=2.
            capture = page.locator('[data-capture-area="true"]').first
            if capture.count() != 1:
                raise RuntimeError(
                    'Expected exactly one [data-capture-area="true"] element.'
                )

            capture.screenshot(
                path=str(raw_path),
                scale='device',
            )
        finally:
            browser.close()

    image = Image.open(raw_path).convert('RGB')

    if image.width != OUTPUT_WIDTH:
        raise RuntimeError(
            f'Unexpected high-resolution width: {image.width}; expected {OUTPUT_WIDTH}'
        )

    if image.height < CROP_HEIGHT_PX:
        raise RuntimeError(
            f'Captured report is only {image.height}px tall; crop requires {CROP_HEIGHT_PX}px.'
        )

    # Fixed crop, never resampling the report. The crop is based on the current
    # validated 50-row template: content ends around y=3052px, leaving a small
    # clean bottom margin before the final cut at y=3090px.
    cropped = image.crop((0, 0, OUTPUT_WIDTH, CROP_HEIGHT_PX))
    cropped.save(png_path, format='PNG', optimize=True)

    return {
        'raw_width': image.width,
        'raw_height': image.height,
        'width': cropped.width,
        'height': cropped.height,
        'device_scale_factor': DEVICE_SCALE_FACTOR,
        'crop_bottom_px': CROP_HEIGHT_PX,
        'png_bytes': png_path.stat().st_size,
        'raw_png_bytes': raw_path.stat().st_size,
    }


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow([
            'orden_salida', 'origen', 'track', 'artist',
            'duration_ms', 'played_at', 'gap_s'
        ])
        for index, row in enumerate(rows, 1):
            writer.writerow([
                index,
                row['origin'],
                row['name'],
                row['artists'],
                row['duration_ms'],
                row['played_at'].isoformat(),
                row.get('gap_s', 0),
            ])


def validate_rows(rows: list[dict[str, Any]], x_count: int, needed: int) -> dict[str, Any]:
    if len(rows) != TARGET_ROWS:
        raise AssertionError(f'Expected {TARGET_ROWS} rows, got {len(rows)}')

    x_rows = [r for r in rows if r['origin'] == 'playlist_x']
    random_rows = [r for r in rows if r['origin'] == 'random']

    if len(x_rows) != x_count:
        raise AssertionError(f'Expected {x_count} official rows, got {len(x_rows)}')
    if len(random_rows) != needed:
        raise AssertionError(f'Expected {needed} random rows, got {len(random_rows)}')

    if any(r['gap_s'] != 0 for r in x_rows):
        raise AssertionError('Playlist X contains a synthetic gap.')

    random_gaps = [r['gap_s'] for r in random_rows]
    if random_rows and not any(g > 120 for g in random_gaps):
        raise AssertionError('Random block did not produce a visibly variable gap > 120 s.')

    # Official block must be contiguous by duration.
    for a, b in zip(x_rows, x_rows[1:]):
        expected = a['played_at'] - timedelta(milliseconds=a['duration_ms'])
        if expected.replace(second=0, microsecond=0) != b['played_at'].replace(second=0, microsecond=0):
            # Comparison at minute precision because displayed timestamps omit seconds.
            raise AssertionError('Playlist X timing is not duration-strict at minute precision.')

    # The first row must use the exact reference timestamp at display precision.
    return {
        'rows': len(rows),
        'playlist_x_rows': len(x_rows),
        'random_rows': len(random_rows),
        'random_gap_min_s': min(random_gaps) if random_gaps else 0,
        'random_gap_max_s': max(random_gaps) if random_gaps else 0,
        'random_gap_over_120_count': sum(g > 120 for g in random_gaps),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('payload')
    parser.add_argument('--template', default='renderer/templates/report_template.html')
    parser.add_argument('--outdir', default='artifacts/real_render_test')
    args = parser.parse_args()

    payload = json.loads(Path(args.payload).read_text(encoding='utf-8'))
    if payload.get('type') != 'real_render_test':
        raise ValueError('Expected payload type=real_render_test')

    x_tracks = [normalize_track(t, 'playlist_x') for t in payload.get('playlistXTracks', [])]
    filler_tracks = [normalize_track(t, 'random') for t in payload.get('fillerTracks', [])]
    if not x_tracks:
        raise ValueError('playlistXTracks is empty')

    reference = parse_dt(payload.get('referenceTime'))
    seed = int(payload.get('seed', 5000))
    rows = build_history_50(x_tracks, filler_tracks, reference, seed)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    template = load_template(args.template)
    html = inject_rows(template, rows)

    html_path = outdir / 'report_real.html'
    png_path = outdir / 'report_real.png'
    csv_path = outdir / 'history_real.csv'
    html_path.write_text(html, encoding='utf-8')

    render_result = render_chromium(html, png_path)
    validation = validate_rows(rows, len(x_tracks[-TARGET_ROWS:]), max(0, TARGET_ROWS - len(x_tracks[-TARGET_ROWS:])))
    write_csv(rows, csv_path)

    manifest = {
        'renderer': 'chromium-headless',
        'type': payload.get('type'),
        'recordId': payload.get('recordId', ''),
        'playlistName': payload.get('playlistName', ''),
        'referenceTime': payload.get('referenceTime', ''),
        'playlistXInputRows': len(x_tracks),
        'officialRowsUsed': validation['playlist_x_rows'],
        'randomRowsUsed': validation['random_rows'],
        'outputRows': len(rows),
        'viewport': [VIEWPORT_WIDTH, VIEWPORT_HEIGHT],
        'deviceScaleFactor': DEVICE_SCALE_FACTOR,
        'outputSize': [OUTPUT_WIDTH, CROP_HEIGHT_PX],
        'crop': {
            'x': 0,
            'y': 0,
            'width': OUTPUT_WIDTH,
            'height': CROP_HEIGHT_PX,
        },
        'render': render_result,
        'validation': validation,
    }
    (outdir / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print('CHROMIUM REAL RENDER OK')


if __name__ == '__main__':
    main()
