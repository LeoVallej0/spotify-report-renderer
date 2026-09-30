from __future__ import annotations

import argparse
import csv
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

import pymupdf as fitz
from weasyprint import HTML

TARGET_ROWS = 50
CSS_WIDTH = 1200
CSS_HEIGHT = 1700
PNG_DPI = 192

@dataclass(frozen=True)
class Track:
    name: str
    artists: str
    duration_ms: int
    origin: str = "playlist_x"


def load_template(path: str | Path) -> str:
    text = Path(path).read_text(encoding="utf-8")
    if 'id="tracks-body"' not in text:
        raise ValueError('Template does not contain #tracks-body')
    return text


def select_random_filler(x_tracks: list[Track], random_pool: list[Track], needed: int, seed: int) -> list[Track]:
    if needed <= 0:
        return []
    x_keys = {(t.name, t.artists, t.duration_ms) for t in x_tracks}
    candidates = [t for t in random_pool if (t.name, t.artists, t.duration_ms) not in x_keys]
    if len(candidates) < needed:
        raise ValueError(f"Random pool too small: {len(candidates)} available, {needed} required.")
    rng = random.Random(seed)
    rng.shuffle(candidates)
    chosen: list[Track] = []
    last_artist = None
    skipped: list[Track] = []
    for t in candidates:
        if last_artist == t.artists:
            skipped.append(t)
            continue
        chosen.append(t)
        last_artist = t.artists
        if len(chosen) == needed:
            return chosen
    rng.shuffle(skipped)
    return (chosen + skipped)[:needed]


def build_history_50(x_tracks: list[Track], random_pool: list[Track], reference: datetime, seed: int) -> list[dict]:
    # This POC caps >50 playlists to 50 entries. Confirm the exact production policy later.
    x_tracks = x_tracks[:TARGET_ROWS]
    needed = TARGET_ROWS - len(x_tracks)
    filler = select_random_filler(x_tracks, random_pool, needed, seed)

    rows: list[dict] = []
    current = reference

    # Preserve the existing project logic: reverse output, assign timestamp, then subtract duration.
    for track in reversed(x_tracks):
        rows.append({
            "name": track.name,
            "artists": track.artists,
            "duration_ms": track.duration_ms,
            "origin": "playlist_x",
            "played_at": current,
            "gap_s": 0,
        })
        current -= timedelta(milliseconds=track.duration_ms)

    # Older synthetic history: always before the Playlist X block.
    gap_rng = random.Random(seed + 918273)
    for track in reversed(filler):
        gap_s = gap_rng.randint(0, 20)
        current -= timedelta(seconds=gap_s)
        rows.append({
            "name": track.name,
            "artists": track.artists,
            "duration_ms": track.duration_ms,
            "origin": "random",
            "played_at": current,
            "gap_s": gap_s,
        })
        current -= timedelta(milliseconds=track.duration_ms)

    if len(rows) != TARGET_ROWS:
        raise RuntimeError("History builder did not produce exactly 50 rows.")
    return rows


def build_rows_html(rows: list[dict]) -> str:
    return "".join(
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
        raise RuntimeError("Could not locate #tracks-body in template.")

    # Server renderer cannot execute the JS year updater.
    report_year = rows[0]['played_at'].year
    result = result.replace('<span id="current-year"></span>', str(report_year))

    # Print-only adjustments. The visual template itself remains the source template.
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
    result = result.replace('<body>', renderer_css + '<body>', 1)
    return result


def render_png(html: str, output_png: str | Path, output_pdf: str | Path | None = None) -> dict:
    output_png = Path(output_png)
    output_pdf = Path(output_pdf) if output_pdf else output_png.with_suffix('.pdf')
    output_png.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html, base_url=str(output_png.parent)).write_pdf(str(output_pdf))
    doc = fitz.open(str(output_pdf))
    try:
        if len(doc) != 1:
            raise RuntimeError(f'Expected 1 PDF page, got {len(doc)}')
        page = doc[0]
        pix = page.get_pixmap(dpi=PNG_DPI, alpha=False)
        pix.save(str(output_png))
        return {
            'pages': len(doc),
            'width': pix.width,
            'height': pix.height,
            'png_bytes': output_png.stat().st_size,
            'pdf_bytes': output_pdf.stat().st_size,
        }
    finally:
        doc.close()


def write_csv(rows: list[dict], path: str | Path) -> None:
    with Path(path).open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['orden_salida','origen','track','artist','duration_ms','played_at','gap_s'])
        for i, r in enumerate(rows, 1):
            w.writerow([i, r['origin'], r['name'], r['artists'], r['duration_ms'], r['played_at'].strftime('%d/%m/%Y, %H:%M:%S'), r.get('gap_s',0)])


def synthetic_playlist(n: int) -> list[Track]:
    artists = ['Calvin Harris & Dua Lipa','The Kid LAROI & Justin Bieber','Miley Cyrus','The Weeknd & Ariana Grande','Dua Lipa','Ed Sheeran','Harry Styles','Coldplay','Bad Bunny','SZA']
    titles = ['One Kiss','Stay','Flowers','Save Your Tears','Don’t Start Now','Shape of You','Levitating','As It Was','Blinding Lights','A Sky Full of Stars']
    return [Track(titles[i % 10] + (f' (Remastered {i+1})' if i in (3,8,15) else ''), artists[i % 10], 185000 + ((i*17000)%125000)) for i in range(n)]


def synthetic_random_pool(n: int = 5000) -> list[Track]:
    genres = ['Pop','Dance','Latin','Indie','R&B','Electronic','Rock','Urban']
    return [Track(f'Random Track {i:04d} — {genres[i%8]}', f'Random Artist {i%500:03d}', 31000 + ((i*73421)%300000), 'random') for i in range(n)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--template', default='Index_StatsSpotify_YEAR_DYNAMIC.html')
    ap.add_argument('--outdir', default='production_poc_output')
    args = ap.parse_args()

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    template = load_template(args.template)
    x = synthetic_playlist(30)
    pool = synthetic_random_pool(5000)
    rows = build_history_50(x, pool, datetime(2026,9,30,12,0), seed=5000)
    html = inject_rows(template, rows)
    html_path = out/'report_30_plus_20.html'
    pdf_path = out/'report_30_plus_20.pdf'
    png_path = out/'report_30_plus_20.png'
    html_path.write_text(html,encoding='utf-8')
    result = render_png(html,png_path,pdf_path)
    write_csv(rows,out/'history_30_plus_20.csv')
    print(result)
    print('Saved:', png_path)

if __name__ == '__main__':
    main()
