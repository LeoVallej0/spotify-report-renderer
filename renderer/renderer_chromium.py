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

    # IMPORTANTE: imageUrl/image_url deben pertenecer al track.
    # Nunca usar coverUrl/cover_url aquí: esos campos pueden ser la portada
    # de la playlist y producirían la misma imagen en todas las filas.
    image_url = str(
        track.get('imageUrl')
        or track.get('image_url')
        or ''
    ).strip()

    return {
        'name': name,
        'artists': artists,
        'duration_ms': duration_ms,
        'image_url': image_url,
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
    # Playlist X is the authoritative block. NEVER truncate, reorder,
    # de-duplicate, filter, or otherwise alter its source list.
    # The current Recently played page is capped at 50 visible entries.
    # If X itself exceeds that cap, stop instead of changing X.
    if len(x_tracks) > TARGET_ROWS:
        raise ValueError(
            f'Playlist X contains {len(x_tracks)} tracks, which exceeds the '
            f'50-track report limit. X was not modified.'
        )

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
            'image_url': track.get('image_url', ''),
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
            'image_url': track.get('image_url', ''),
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
    if 'id="tracks-list"' not in text:
        raise ValueError('Template does not contain #tracks-list')
    return text


def inject_rows(template_html: str, rows: list[dict[str, Any]]) -> str:
    def image_html(row: dict[str, Any]) -> str:
        url = str(row.get('image_url', '') or '').strip()
        if url:
            # Fallback is generated by CSS/DOM if the remote image cannot load.
            return (
                f'<img class="song-image" src="{escape(url, quote=True)}" '
                f'loading="eager" decoding="async" alt="" '
                f'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\';">'
                f'<div class="song-image-fallback" style="display:none">♫</div>'
            )
        return '<div class="song-image-fallback">♫</div>'

    rows_html = ''.join(
        f'<li class="track-row surface-card surface-card--hover">'
        f'<div class="cover-col">{image_html(row)}</div>'
        f'<div class="detail-col notranslate">'
        f'<div class="track-name">{escape(row["name"])}</div>'
        f'<div class="artist-name text-muted">{escape(row["artists"])}</div>'
        f'</div>'
        f'<div class="played-col text-muted"><span class="played-at">'
        f'{row["played_at"].strftime("%d/%m/%Y, %H:%M")}'
        f'</span></div>'
        f'</li>'
        for row in rows
    )

    pattern = r'<ul id="tracks-list" class="track-list notranslate"></ul>'
    replacement = (
        '<ul id="tracks-list" class="track-list notranslate">'
        + rows_html
        + '</ul>'
    )
    result, count = re.subn(pattern, replacement, template_html, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError('Could not locate #tracks-list in template')

    year = rows[0]['played_at'].year if rows else datetime.now().year
    result = result.replace('<span id="current-year"></span>', str(year))

    # The report is static HTML. Remove scripts so Chromium only renders the final state.
    result = re.sub(r'<script\b[^>]*>.*?</script>', '', result, flags=re.S | re.I)
    return result


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
            page.wait_for_timeout(400)
            page.evaluate("document.fonts && document.fonts.ready")
            page.evaluate("Promise.all(Array.from(document.images).map(i => i.complete ? Promise.resolve() : new Promise(r => { i.addEventListener('load', r, {once:true}); i.addEventListener('error', r, {once:true}); })))")
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

    # Preserve the complete current-layout report at native DPR=2. The new
    # card layout is taller than the old table, so the final height is taken
    # directly from the capture instead of using the old fixed 3090px crop.
    image.save(png_path, format='PNG', optimize=True)

    return {
        'raw_width': image.width,
        'raw_height': image.height,
        'width': image.width,
        'height': image.height,
        'device_scale_factor': DEVICE_SCALE_FACTOR,
        'crop_bottom_px': None,
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
    validation = validate_rows(rows, len(x_tracks), max(0, TARGET_ROWS - len(x_tracks)))
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
        'outputSize': [OUTPUT_WIDTH, render_result['height']],
        'crop': None,
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
