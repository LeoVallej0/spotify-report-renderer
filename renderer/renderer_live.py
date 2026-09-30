from __future__ import annotations

import base64
import hashlib
import html as html_lib
import json
import os
import random
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import fitz
import requests
from weasyprint import HTML

TARGET_ROWS = 50
CSS_WIDTH = 1200
CSS_HEIGHT = 1700
PNG_DPI = 192
SPOTIFY_EMBED_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/154.0 Safari/537.36"
)

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "report_template.html"


@dataclass(frozen=True)
class Track:
    name: str
    artists: str
    duration_ms: int
    origin: str = "playlist_x"


def fail(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def extract_playlist_id(value: str) -> str:
    value = str(value or "").strip()
    m = re.search(r"spotify\.com/(?:embed/)?playlist/([A-Za-z0-9]+)", value, re.I)
    if m:
        return m.group(1)
    m = re.match(r"spotify:playlist:([A-Za-z0-9]+)$", value, re.I)
    return m.group(1) if m else ""


def fetch_embed(playlist_id: str) -> dict[str, Any]:
    url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    r = requests.get(
        url,
        headers={
            "User-Agent": SPOTIFY_EMBED_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://open.spotify.com/",
        },
        timeout=30,
    )
    r.raise_for_status()
    m = re.search(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>([\s\S]*?)</script>', r.text, re.I)
    if not m:
        m = re.search(r'<script[^>]*__NEXT_DATA__[^>]*>([\s\S]*?)</script>', r.text, re.I)
    if not m:
        raise RuntimeError("No se encontró __NEXT_DATA__ en el Embed de Spotify.")
    return json.loads(m.group(1))


def find_track_list(obj: Any) -> list[Any] | None:
    if isinstance(obj, dict):
        tl = obj.get("trackList")
        if isinstance(tl, list):
            return tl
        for value in obj.values():
            result = find_track_list(value)
            if result is not None:
                return result
    elif isinstance(obj, list):
        for value in obj:
            result = find_track_list(value)
            if result is not None:
                return result
    return None


def find_entity(obj: Any) -> dict[str, Any] | None:
    if isinstance(obj, dict):
        if obj.get("type") == "playlist":
            return obj
        if obj.get("name") and isinstance(obj.get("trackList"), list) and obj.get("coverArt"):
            return obj
        for value in obj.values():
            result = find_entity(value)
            if result is not None:
                return result
    elif isinstance(obj, list):
        for value in obj:
            result = find_entity(value)
            if result is not None:
                return result
    return None


def extract_artists(track: Any) -> str:
    if not isinstance(track, dict):
        return ""
    if isinstance(track.get("subtitle"), str):
        return track["subtitle"].strip()
    artists = track.get("artists")
    if isinstance(artists, list):
        vals = []
        for artist in artists:
            if isinstance(artist, dict):
                vals.append(str(artist.get("name") or artist.get("title") or "").strip())
            else:
                vals.append(str(artist).strip())
        return ", ".join(x for x in vals if x)
    if isinstance(track.get("artist"), str):
        return track["artist"].strip()
    return ""


def extract_duration_ms(track: Any) -> int:
    if not isinstance(track, dict):
        return 0
    for key in ("duration", "durationMs", "duration_ms", "durationMillis", "durationMilliseconds"):
        value = track.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return round(value)
    value = track.get("duration")
    if isinstance(value, dict):
        nested = value.get("totalMilliseconds") or value.get("milliseconds") or 0
        if isinstance(nested, (int, float)) and nested > 0:
            return round(nested)
    return 0


def parse_playlist(url: str) -> tuple[str, list[Track]]:
    pid = extract_playlist_id(url)
    if not pid:
        raise RuntimeError(f"URL de playlist inválida: {url}")
    data = fetch_embed(pid)
    track_list = find_track_list(data)
    if not track_list:
        raise RuntimeError(f"No se encontraron tracks para playlist {pid}.")
    entity = find_entity(data) or {}
    name = str(entity.get("name") or "Playlist").strip()

    tracks: list[Track] = []
    for raw in track_list:
        if not raw:
            continue
        track = raw.get("track") if isinstance(raw, dict) and isinstance(raw.get("track"), dict) else raw
        if not isinstance(track, dict):
            continue
        name_t = str(track.get("title") or track.get("name") or "").strip()
        if not name_t:
            continue
        tracks.append(
            Track(
                name=name_t,
                artists=extract_artists(track),
                duration_ms=extract_duration_ms(track),
            )
        )
    if not tracks:
        raise RuntimeError(f"La playlist {pid} no contiene tracks utilizables.")
    return name, tracks


def track_key(t: Track) -> tuple[str, str, int]:
    return (t.name, t.artists, t.duration_ms)


def stable_seed(record_id: str, date_text: str) -> int:
    digest = hashlib.sha256(f"{record_id}|{date_text}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def choose_filler(x_tracks: list[Track], pool: list[Track], needed: int, seed: int) -> list[Track]:
    if needed <= 0:
        return []
    x_keys = {track_key(t) for t in x_tracks}
    unique: dict[tuple[str, str, int], Track] = {}
    for t in pool:
        key = track_key(t)
        if key not in x_keys:
            unique.setdefault(key, t)
    candidates = list(unique.values())
    if len(candidates) < needed:
        raise RuntimeError(
            f"La playlist random no tiene suficientes canciones únicas: "
            f"{len(candidates)} disponibles, {needed} necesarias."
        )
    rng = random.Random(seed)
    rng.shuffle(candidates)

    chosen: list[Track] = []
    deferred: list[Track] = []
    last_artist = None
    for t in candidates:
        if t.artists and t.artists == last_artist:
            deferred.append(t)
            continue
        chosen.append(t)
        last_artist = t.artists
        if len(chosen) == needed:
            return chosen
    rng.shuffle(deferred)
    for t in deferred:
        if len(chosen) == needed:
            break
        chosen.append(t)
    return chosen[:needed]


def build_rows(
    x_tracks: list[Track],
    random_tracks: list[Track],
    reference: datetime,
    seed: int,
) -> list[dict[str, Any]]:
    # Producción: si X supera 50, usamos las 50 últimas de X antes de invertir,
    # lo que conserva el significado de "la parte más reciente" de la secuencia.
    x_tracks = x_tracks[-TARGET_ROWS:]
    filler = choose_filler(x_tracks, random_tracks, TARGET_ROWS - len(x_tracks), seed)

    rows: list[dict[str, Any]] = []
    current = reference

    # Igual que el Colab original: timestamp primero, resta después.
    for track in reversed(x_tracks):
        rows.append(
            {
                "name": track.name,
                "artists": track.artists,
                "duration_ms": track.duration_ms,
                "origin": "playlist_x",
                "played_at": current,
                "gap_s": 0,
            }
        )
        current -= timedelta(milliseconds=max(track.duration_ms, 0))

    # Historial sintético anterior a X.
    gap_rng = random.Random(seed + 918273)
    for track in reversed(filler):
        gap_s = gap_rng.randint(0, 20)
        current -= timedelta(seconds=gap_s)
        rows.append(
            {
                "name": track.name,
                "artists": track.artists,
                "duration_ms": track.duration_ms,
                "origin": "random",
                "played_at": current,
                "gap_s": gap_s,
            }
        )
        current -= timedelta(milliseconds=max(track.duration_ms, 0))

    if len(rows) != TARGET_ROWS:
        raise RuntimeError(f"Se esperaban {TARGET_ROWS} filas y se generaron {len(rows)}.")
    return rows


def rows_html(rows: list[dict[str, Any]]) -> str:
    parts = []
    for r in rows:
        parts.append(
            '<tr data-v-b34d8fe2="">'
            f'<td data-v-b34d8fe2="">{html_lib.escape(r["name"])}</td>'
            f'<td data-v-b34d8fe2="">{html_lib.escape(r["artists"])}</td>'
            f'<td data-v-b34d8fe2="">{r["played_at"].strftime("%d/%m/%Y, %H:%M")}</td>'
            '</tr>'
        )
    return "".join(parts)


def inject_into_template(template_html: str, rows: list[dict[str, Any]]) -> str:
    pattern = r'<tbody id="tracks-body" class="notranslate" data-v-b34d8fe2="">.*?</tbody>'
    replacement = (
        '<tbody id="tracks-body" class="notranslate" data-v-b34d8fe2="">'
        + rows_html(rows)
        + '</tbody>'
    )
    result, count = re.subn(pattern, replacement, template_html, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError("No se encontró #tracks-body en la plantilla.")

    result = result.replace('<span id="current-year"></span>', str(rows[0]["played_at"].year))

    css = f'''<style>
@page {{ size: {CSS_WIDTH}px {CSS_HEIGHT}px; margin: 0; }}
html, body {{ margin: 0 !important; padding: 0 !important; }}
tr {{ break-inside: avoid !important; page-break-inside: avoid !important; }}
.navbar.fixed-top {{ position: static !important; }}
.navbar-expand-sm .navbar-collapse {{ display: flex !important; flex-basis: auto !important; }}
.navbar-expand-sm .navbar-nav {{ flex-direction: row !important; }}
.navbar-expand-sm .navbar-toggler {{ display: none !important; }}
</style>'''
    return result.replace("<body>", css + "<body>", 1)


def render_png(html: str, workdir: Path) -> tuple[Path, dict[str, int]]:
    html_path = workdir / "report.html"
    pdf_path = workdir / "report.pdf"
    png_path = workdir / "report.png"
    html_path.write_text(html, encoding="utf-8")

    HTML(string=html, base_url=str(workdir)).write_pdf(str(pdf_path))
    doc = fitz.open(str(pdf_path))
    try:
        if len(doc) != 1:
            raise RuntimeError(f"El PDF automático produjo {len(doc)} páginas; se esperaba 1.")
        pix = doc[0].get_pixmap(dpi=PNG_DPI, alpha=False)
        pix.save(str(png_path))
        return png_path, {
            "pages": len(doc),
            "width": pix.width,
            "height": pix.height,
            "png_bytes": png_path.stat().st_size,
        }
    finally:
        doc.close()


def callback(callback_url: str, secret: str, payload: dict[str, Any]) -> None:
    sep = "&" if "?" in callback_url else "?"
    url = f"{callback_url}{sep}key={secret}"
    r = requests.post(url, json=payload, timeout=120)
    r.raise_for_status()


def parse_payload() -> dict[str, Any]:
    raw = os.environ.get("PAYLOAD_JSON", "").strip()
    if not raw and len(sys.argv) > 1:
        raw = Path(sys.argv[1]).read_text(encoding="utf-8")
    if not raw:
        raise RuntimeError("No se recibió PAYLOAD_JSON.")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("PAYLOAD_JSON no es un objeto JSON.")
    return payload


def process_job(job: dict[str, Any], random_url: str, callback_url: str, secret: str) -> None:
    record_id = str(job.get("recordId") or "").strip()
    playlist_url = str(job.get("playlistUrl") or "").strip()
    reference_date = str(job.get("referenceDate") or "").strip()
    reference_time = str(job.get("referenceTime") or "").strip()
    timezone_name = str(job.get("timezone") or "UTC").strip()
    seed = int(job.get("seed") or stable_seed(record_id, reference_date))

    if not record_id or not playlist_url or not reference_date or not reference_time:
        raise RuntimeError("Trabajo automático incompleto.")

    try:
        _, x_tracks = parse_playlist(playlist_url)
        _, random_tracks = parse_playlist(random_url)
        tz = ZoneInfo(timezone_name)
        reference = datetime.fromisoformat(f"{reference_date}T{reference_time}").replace(tzinfo=tz)
        rows = build_rows(x_tracks, random_tracks, reference, seed)
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        final_html = inject_into_template(template, rows)

        with tempfile.TemporaryDirectory(prefix="spotify-render-") as td:
            png_path, metrics = render_png(final_html, Path(td))
            encoded = base64.b64encode(png_path.read_bytes()).decode("ascii")

        callback(
            callback_url,
            secret,
            {
                "type": "render_complete",
                "recordId": record_id,
                "playlistName": str(job.get("playlistName") or "Playlist"),
                "referenceDate": reference_date,
                "referenceTime": reference_time,
                "seed": seed,
                "metrics": metrics,
                "pngBase64": encoded,
            },
        )
        print(f"OK {record_id}: {metrics['width']}x{metrics['height']} bytes={metrics['png_bytes']}")
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        try:
            callback(
                callback_url,
                secret,
                {
                    "type": "render_error",
                    "recordId": record_id,
                    "error": message[:1000],
                },
            )
        finally:
            print(f"ERROR {record_id}: {message}", file=sys.stderr)


def main() -> None:
    payload = parse_payload()
    jobs = payload.get("jobs")
    random_url = str(payload.get("randomPlaylistUrl") or os.environ.get("RANDOM_PLAYLIST_URL") or "").strip()
    callback_url = str(payload.get("callbackUrl") or os.environ.get("CALLBACK_URL") or "").strip()
    secret = os.environ.get("RENDER_CALLBACK_SECRET", "").strip()

    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("No hay jobs para procesar.")
    if not random_url:
        raise RuntimeError("Falta randomPlaylistUrl.")
    if not callback_url:
        raise RuntimeError("Falta callbackUrl.")
    if not secret:
        raise RuntimeError("Falta RENDER_CALLBACK_SECRET.")

    for job in jobs:
        if not isinstance(job, dict):
            continue
        process_job(job, random_url, callback_url, secret)


if __name__ == "__main__":
    main()
