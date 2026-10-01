from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import shutil
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops
from playwright.sync_api import sync_playwright

VIEWPORT_WIDTH = 540
VIEWPORT_HEIGHT = 960
DEVICE_SCALE_FACTOR = 2
OUTPUT_WIDTH = VIEWPORT_WIDTH * DEVICE_SCALE_FACTOR
PART_HEIGHT = VIEWPORT_HEIGHT * DEVICE_SCALE_FACTOR
# Las capturas usan un viewport móvil fijo. Cada pantalla completa tiene 1920 px
# de alto de salida (1080x1920); solo la última parte se recorta al final real
# de la última canción cuando no llena una pantalla completa.
# Esta es la capacidad de referencia del layout actual; la cantidad total de
# partes es dinámica según el número de canciones de Playlist X.
MAX_ROWS_PER_PART = 18
LAST_PART_BOTTOM_PADDING_CSS = 8


def parse_dt(value: str) -> datetime:
    text = str(value or '').strip()
    if not text:
        raise ValueError('referenceTime is empty')
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def normalize_track(track: dict[str, Any]) -> dict[str, Any]:
    name = str(track.get('name', '')).strip()
    artists = str(track.get('artists', '')).strip()
    raw_duration = track.get('durationMs', track.get('duration_ms', 0))
    duration_ms = int(float(raw_duration or 0))
    image_url = str(track.get('imageUrl') or track.get('image_url') or '').strip()

    if not name:
        raise ValueError('Track without name')
    if duration_ms <= 0:
        raise ValueError(f'Invalid duration for track: {name}')
    if not image_url:
        raise ValueError(f'Missing individual artwork URL for track: {name}')

    return {
        'name': name,
        'artists': artists,
        'duration_ms': duration_ms,
        'image_url': image_url,
        'origin': 'playlist_x',
    }


def build_history_x_only(
    x_tracks: list[dict[str, Any]],
    reference: datetime,
) -> list[dict[str, Any]]:
    if not x_tracks:
        raise ValueError('playlistXTracks is empty')

    rows: list[dict[str, Any]] = []
    current = reference

    # Mantiene la misma convención que el renderer anterior: el bloque X se
    # recorre en reversa para construir Played at de más reciente a más antiguo.
    # No se agregan tracks externos, no se trunca X y no se eliminan duplicados.
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

    return rows


def load_template(path: str | Path) -> str:
    text = Path(path).read_text(encoding='utf-8')
    if 'id="tracks-list"' not in text:
        raise ValueError('Template does not contain #tracks-list')
    return text


def safe_image_html(row: dict[str, Any]) -> str:
    url = str(row.get('image_url', '') or '').strip()
    if url:
        return (
            f'<img class="song-image" src="{escape(url, quote=True)}" '
            'loading="eager" decoding="async" alt="" '
            'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\';">'
            '<div class="song-image-fallback" style="display:none">♫</div>'
        )
    return '<div class="song-image-fallback">♫</div>'


def inject_rows(
    template_html: str,
    rows: list[dict[str, Any]],
    part_number: int,
    total_parts: int,
) -> str:
    rows_html = ''.join(
        '<li class="track-row surface-card surface-card--hover">'
        f'<div class="cover-col">{safe_image_html(row)}</div>'
        '<div class="detail-col notranslate">'
        f'<div class="track-name">{escape(row["name"])}</div>'
        f'<div class="artist-name text-muted">{escape(row["artists"])}</div>'
        '</div>'
        '<div class="played-col text-muted"><span class="played-at">'
        f'{row["played_at"].strftime("%d/%m/%Y, %H:%M")}'
        '</span></div>'
        '</li>'
        for row in rows
    )

    marker = '<ul id="tracks-list" class="track-list notranslate"></ul>'
    replacement = (
        '<ul id="tracks-list" class="track-list notranslate">'
        + rows_html
        + '</ul>'
    )
    result = template_html.replace(marker, replacement, 1)
    if result == template_html:
        raise RuntimeError('Could not locate #tracks-list in template')

    year = rows[0]['played_at'].year if rows else datetime.now().year
    result = result.replace('<span id="current-year"></span>', str(year))

    # Simula una captura tras hacer scroll: en las partes 2 y 3 no repetimos
    # el título ni el espacio superior. La barra superior permanece visible.
    if part_number > 1:
        result = result.replace('<div class="ad-slot"></div>', '', 1)
        title_block = '<div class="text-center" id="title-block">\n          <h2 class="page-title">Recently played Tracks</h2>\n        </div>'
        result = result.replace(title_block, '<div class="continuation-spacer"></div>', 1)
    else:
        # Parte 1: la pantalla inicial incluye el encabezado exactamente como
        # una entrada nueva de Recently played.
        pass

    # El reporte de WhatsApp se concentra exclusivamente en las canciones de
    # Playlist X. No mostramos aviso ni footer para reservar toda la altura
    # del screenshot a las filas que realmente se van a enviar.
    result = result.replace('<div class="info-note-wrapper" id="info-note-wrapper">', '<div class="info-note-wrapper" id="info-note-wrapper" style="display:none">', 1)
    result = result.replace('<footer class="footer">', '<footer class="footer" style="display:none">', 1)

    # El HTML debe ser estático: no dependemos de JS para renderizar las filas.
    import re
    result = re.sub(r'<script\b[^>]*>.*?</script>', '', result, flags=re.S | re.I)
    return result


def balanced_slices(count: int) -> list[tuple[int, int]]:
    if count <= 0:
        return []

    # La capacidad está basada en la altura real del layout móvil V16.
    # No se modifica el conjunto X: solo se decide dónde empieza cada captura.
    part_count = (count + MAX_ROWS_PER_PART - 1) // MAX_ROWS_PER_PART
    slices: list[tuple[int, int]] = []
    for index in range(part_count):
        start_idx = index * MAX_ROWS_PER_PART
        end_idx = min(count, start_idx + MAX_ROWS_PER_PART)
        slices.append((start_idx, end_idx))
    return slices


def wait_for_images(page) -> None:
    page.wait_for_timeout(250)
    page.evaluate("document.fonts && document.fonts.ready")
    page.evaluate("Promise.all(Array.from(document.images).map(i => i.complete ? Promise.resolve() : new Promise(r => { i.addEventListener('load', r, {once:true}); i.addEventListener('error', r, {once:true}); })))")
    page.wait_for_timeout(250)
    failed = page.locator('img.song-image').evaluate_all(
        "els => els.map((img, idx) => ({idx, complete: img.complete, naturalWidth: img.naturalWidth, src: img.src})).filter(x => !x.complete || x.naturalWidth === 0)"
    )
    if failed:
        sample = failed[:5]
        raise RuntimeError(f'Individual track artwork failed to load: {sample}')


def launch_browser(playwright):
    executable = os.environ.get('CHROMIUM_EXECUTABLE') or shutil.which('chromium') or shutil.which('google-chrome')
    kwargs = {
        'headless': True,
        'args': ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
    }
    if executable:
        kwargs['executable_path'] = executable
    else:
        kwargs['channel'] = 'chromium'
    return playwright.chromium.launch(**kwargs)


def render_parts(
    html_parts: list[str],
    rows_per_part: list[int],
    outdir: Path,
) -> dict[str, Any]:
    part_paths: list[Path] = []
    part_heights: list[int] = []

    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        try:
            total_parts = len(html_parts)
            for index, html in enumerate(html_parts, start=1):
                page = browser.new_page(
                    viewport={'width': VIEWPORT_WIDTH, 'height': VIEWPORT_HEIGHT},
                    device_scale_factor=DEVICE_SCALE_FACTOR,
                )
                try:
                    page.set_content(html, wait_until='load', timeout=30000)
                    wait_for_images(page)

                    is_last = index == total_parts
                    is_full_last = rows_per_part[index - 1] >= MAX_ROWS_PER_PART
                    path = outdir / f'report_part_{index}.png'

                    if is_last and not is_full_last:
                        # La última captura termina exactamente después de la última
                        # fila, con un pequeño margen natural. Nunca cambia el ancho
                        # ni la geometría de las pantallas anteriores.
                        bottom_css = page.locator('#tracks-list').evaluate(
                            "el => Math.ceil(el.getBoundingClientRect().bottom)"
                        )
                        crop_css_height = min(
                            VIEWPORT_HEIGHT,
                            max(1, int(bottom_css + LAST_PART_BOTTOM_PADDING_CSS))
                        )
                        page.screenshot(
                            path=str(path),
                            full_page=False,
                            clip={
                                'x': 0,
                                'y': 0,
                                'width': VIEWPORT_WIDTH,
                                'height': crop_css_height,
                            },
                            scale='device',
                        )
                        expected_height = crop_css_height * DEVICE_SCALE_FACTOR
                    else:
                        page.screenshot(
                            path=str(path),
                            full_page=False,
                            scale='device',
                        )
                        expected_height = PART_HEIGHT

                    img = Image.open(path).convert('RGB')
                    if img.size != (OUTPUT_WIDTH, expected_height):
                        raise RuntimeError(
                            f'Part {index} has size {img.size}; expected {(OUTPUT_WIDTH, expected_height)}'
                        )
                    part_paths.append(path)
                    part_heights.append(img.height)
                finally:
                    page.close()
        finally:
            browser.close()

    master_height = sum(part_heights)
    canvas = Image.new('RGB', (OUTPUT_WIDTH, master_height), 'white')
    y = 0
    for path, part_height in zip(part_paths, part_heights):
        img = Image.open(path).convert('RGB')
        canvas.paste(img, (0, y))
        y += part_height

    master_path = outdir / 'report_real.png'
    canvas.save(master_path, format='PNG', optimize=True)

    return {
        'width': canvas.width,
        'height': canvas.height,
        'part_width': OUTPUT_WIDTH,
        'part_full_height': PART_HEIGHT,
        'part_heights': part_heights,
        'parts': total_parts,
        'master_bytes': master_path.stat().st_size,
        'part_files': [str(p.name) for p in part_paths],
    }


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['orden_salida', 'origen', 'track', 'artist', 'duration_ms', 'played_at', 'gap_s', 'image_url'])
        for index, row in enumerate(rows, 1):
            writer.writerow([
                index, row['origin'], row['name'], row['artists'], row['duration_ms'],
                row['played_at'].isoformat(), row.get('gap_s', 0), row.get('image_url', ''),
            ])


def validate(rows: list[dict[str, Any]], x_count: int, image_count: int) -> dict[str, Any]:
    if len(rows) != x_count:
        raise AssertionError(f'Expected {x_count} X rows, got {len(rows)}')
    if any(r['origin'] != 'playlist_x' for r in rows):
        raise AssertionError('Found a non-X row in the report')
    if any(r['gap_s'] != 0 for r in rows):
        raise AssertionError('Playlist X has non-zero synthetic gaps')
    if image_count != len(rows):
        raise AssertionError(
            f'Every Playlist X row must have individual artwork. Found {image_count}/{len(rows)}.'
        )
    for a, b in zip(rows, rows[1:]):
        expected = a['played_at'] - timedelta(milliseconds=a['duration_ms'])
        if expected.replace(second=0, microsecond=0) != b['played_at'].replace(second=0, microsecond=0):
            raise AssertionError('X timing is not duration-strict at minute precision')
    return {
        'rows': len(rows),
        'playlist_x_rows': len(rows),
        'random_rows': 0,
        'tracks_with_artwork': image_count,
        'tracks_without_artwork': len(rows) - image_count,
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

    filler = payload.get('fillerTracks') or []
    if filler:
        raise ValueError('V16 es X-only: fillerTracks debe estar vacío u omitido')

    x_tracks = [normalize_track(t) for t in payload.get('playlistXTracks', [])]
    if not x_tracks:
        raise ValueError('playlistXTracks is empty')

    reference = parse_dt(payload.get('referenceTime'))
    rows = build_history_x_only(x_tracks, reference)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    template = load_template(args.template)

    slices = balanced_slices(len(rows))
    html_parts: list[str] = []
    used_rows_for_part: list[int] = []
    for part_number, (start, end) in enumerate(slices, start=1):
        subset = rows[start:end]
        used_rows_for_part.append(len(subset))
        html_parts.append(inject_rows(template, subset, part_number, len(slices)))

    # The exact same row set appears in CSV/master; only the visual split changes.
    html_path = outdir / 'report_real.html'
    html_path.write_text(html_parts[0], encoding='utf-8')
    csv_path = outdir / 'history_real.csv'
    write_csv(rows, csv_path)

    image_count = sum(1 for r in rows if r.get('image_url'))
    render_result = render_parts(html_parts, used_rows_for_part, outdir)
    validation = validate(rows, len(x_tracks), image_count)

    manifest = {
        'renderer': 'chromium-headless',
        'formatVersion': 'V17_X_ONLY_TRACK_COVERS_DYNAMIC_MOBILE',
        'type': payload.get('type'),
        'recordId': payload.get('recordId', ''),
        'playlistName': payload.get('playlistName', ''),
        'referenceTime': payload.get('referenceTime', ''),
        'playlistXInputRows': len(x_tracks),
        'officialRowsUsed': len(rows),
        'randomRowsUsed': 0,
        'outputRows': len(rows),
        'viewport': [VIEWPORT_WIDTH, VIEWPORT_HEIGHT],
        'deviceScaleFactor': DEVICE_SCALE_FACTOR,
        'outputSize': [render_result['width'], render_result['height']],
        'partSize': [render_result['part_width'], render_result['part_full_height']],
        'parts': render_result['parts'],
        'partRowCounts': used_rows_for_part,
        'partMaxRows': MAX_ROWS_PER_PART,
        'partHeights': render_result['part_heights'],
        'render': render_result,
        'validation': validation,
    }
    (outdir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print('CHROMIUM V17 X-ONLY TRACK COVERS DYNAMIC-PART RENDER OK')


if __name__ == '__main__':
    main()
