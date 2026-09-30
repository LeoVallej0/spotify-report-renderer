from __future__ import annotations

import argparse
import csv
import json
import random
import re
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

import pymupdf as fitz
from weasyprint import HTML

TARGET_ROWS = 50
CSS_WIDTH = 1200
CSS_HEIGHT = 1700
PNG_DPI = 192


def parse_dt(value: str) -> datetime:
    value = str(value or '').strip()
    if not value:
        raise ValueError('referenceTime is empty')
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def normalize_track(t: dict, origin: str) -> dict:
    name = str(t.get('name', '')).strip()
    artists = str(t.get('artists', '')).strip()
    duration = int(float(t.get('durationMs', t.get('duration_ms', 0)) or 0))
    if not name:
        raise ValueError('Track without name')
    if duration <= 0:
        raise ValueError(f'Invalid duration for track: {name}')
    return {'name': name, 'artists': artists, 'duration_ms': duration, 'origin': origin}


def choose_filler_from_payload(x_tracks: list[dict], filler_tracks: list[dict], needed: int) -> list[dict]:
    if needed <= 0:
        return []
    if len(filler_tracks) < needed:
        raise ValueError(f'Payload contains only {len(filler_tracks)} filler tracks; {needed} required.')

    x_keys = {(t['name'], t['artists'], t['duration_ms']) for t in x_tracks}
    chosen = []
    seen = set()
    last_artist = None
    deferred = []

    for t in filler_tracks:
        key = (t['name'], t['artists'], t['duration_ms'])
        if key in x_keys or key in seen:
            continue
        if t['artists'] == last_artist:
            deferred.append(t)
            continue
        chosen.append(t)
        seen.add(key)
        last_artist = t['artists']
        if len(chosen) == needed:
            return chosen

    for t in deferred:
        key = (t['name'], t['artists'], t['duration_ms'])
        if key in x_keys or key in seen:
            continue
        chosen.append(t)
        seen.add(key)
        if len(chosen) == needed:
            return chosen

    raise ValueError(f'Unable to build {needed} unique filler tracks from payload.')


def build_history_50(x_tracks: list[dict], filler_tracks: list[dict], reference: datetime, seed: int) -> list[dict]:
    # Production policy for this stage: latest 50 tracks of Playlist X are represented.
    # The table is rendered newest-first, matching the current template logic.
    if len(x_tracks) > TARGET_ROWS:
        x_tracks = x_tracks[-TARGET_ROWS:]

    needed = TARGET_ROWS - len(x_tracks)
    filler = choose_filler_from_payload(x_tracks, filler_tracks, needed)

    rows: list[dict] = []
    current = reference

    # Preserve project logic: reverse X, assign timestamp, then subtract duration.
    for t in reversed(x_tracks):
        rows.append({
            'name': t['name'],
            'artists': t['artists'],
            'duration_ms': t['duration_ms'],
            'origin': 'playlist_x',
            'played_at': current,
            'gap_s': 0,
        })
        current -= timedelta(milliseconds=t['duration_ms'])

    # Older synthetic history precedes Playlist X in time and follows it in the table.
    gap_rng = random.Random(seed + 918273)
    for t in reversed(filler):
        gap_s = gap_rng.randint(0, 20)
        current -= timedelta(seconds=gap_s)
        rows.append({
            'name': t['name'],
            'artists': t['artists'],
            'duration_ms': t['duration_ms'],
            'origin': 'random',
            'played_at': current,
            'gap_s': gap_s,
        })
        current -= timedelta(milliseconds=t['duration_ms'])

    if len(rows) != TARGET_ROWS:
        raise RuntimeError(f'Expected {TARGET_ROWS} rows, got {len(rows)}')
    return rows


def load_template(path: str) -> str:
    text = Path(path).read_text(encoding='utf-8')
    if 'id="tracks-body"' not in text:
        raise ValueError('Template does not contain #tracks-body')
    return text


def build_rows_html(rows: list[dict]) -> str:
    return ''.join(
        f'<tr data-v-b34d8fe2="">'
        f'<td data-v-b34d8fe2="">{escape(r["name"])}</td>'
        f'<td data-v-b34d8fe2="">{escape(r["artists"])}</td>'
        f'<td data-v-b34d8fe2="">{r["played_at"].strftime("%d/%m/%Y, %H:%M")}</td>'
        f'</tr>'
        for r in rows
    )


def inject_rows(template_html: str, rows: list[dict]) -> str:
    rows_html = build_rows_html(rows)
    pattern = r'<tbody id="tracks-body" class="notranslate" data-v-b34d8fe2="">.*?</tbody>'
    repl = f'<tbody id="tracks-body" class="notranslate" data-v-b34d8fe2="">{rows_html}</tbody>'
    result, count = re.subn(pattern, repl, template_html, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError('Could not locate #tracks-body in template')

    # Server-side renderer cannot execute the browser year updater.
    report_year = rows[0]['played_at'].year
    result = result.replace('<span id="current-year"></span>', str(report_year))

    renderer_css = f'''<style>
@page {{ size: {CSS_WIDTH}px {CSS_HEIGHT}px; margin: 0; }}
#control-panel {{ display: none !important; }}
.navbar.fixed-top {{ position: static !important; }}
.navbar-expand-sm .navbar-collapse {{ display: flex !important; flex-basis: auto !important; }}
.navbar-expand-sm .navbar-nav {{ flex-direction: row !important; }}
.navbar-expand-sm .navbar-toggler {{ display: none !important; }}
html, body {{ margin: 0 !important; padding: 0 !important; }}
tr {{ page-break-inside: avoid !important; }}
</style>'''
    return result.replace('<body>', renderer_css + '<body>', 1)


def render_png(html: str, png_path: Path, pdf_path: Path) -> dict:
    png_path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html, base_url=str(png_path.parent)).write_pdf(str(pdf_path))
    doc = fitz.open(str(pdf_path))
    try:
        if len(doc) != 1:
            raise RuntimeError(f'Expected 1 PDF page, got {len(doc)}')
        page = doc[0]
        pix = page.get_pixmap(dpi=PNG_DPI, alpha=False)
        pix.save(str(png_path))
        return {
            'pages': len(doc),
            'width': pix.width,
            'height': pix.height,
            'png_bytes': png_path.stat().st_size,
            'pdf_bytes': pdf_path.stat().st_size,
        }
    finally:
        doc.close()


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['orden_salida','origen','track','artist','duration_ms','played_at','gap_s'])
        for i, r in enumerate(rows, 1):
            w.writerow([
                i, r['origin'], r['name'], r['artists'], r['duration_ms'],
                r['played_at'].isoformat(), r.get('gap_s', 0)
            ])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('payload', help='Path to JSON payload')
    ap.add_argument('--template', default='renderer/templates/report_template.html')
    ap.add_argument('--outdir', default='artifacts/real_render_test')
    args = ap.parse_args()

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

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    template = load_template(args.template)
    html = inject_rows(template, rows)

    html_path = out / 'report_real.html'
    pdf_path = out / 'report_real.pdf'
    png_path = out / 'report_real.png'
    csv_path = out / 'history_real.csv'
    manifest_path = out / 'manifest.json'

    html_path.write_text(html, encoding='utf-8')
    result = render_png(html, png_path, pdf_path)
    write_csv(rows, csv_path)

    manifest = {
        'type': payload.get('type'),
        'recordId': payload.get('recordId', ''),
        'playlistName': payload.get('playlistName', ''),
        'referenceTime': payload.get('referenceTime', ''),
        'playlistXInputRows': len(x_tracks),
        'randomFillerRows': len(filler_tracks),
        'outputRows': len(rows),
        'result': result,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    print('REAL RENDER OK')


if __name__ == '__main__':
    main()
