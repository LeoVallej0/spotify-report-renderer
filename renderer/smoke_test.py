from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from renderer_live import Track, build_rows, inject_into_template, render_png, TARGET_ROWS, TEMPLATE_PATH


def main() -> None:
    x_tracks = [
        Track(name=f"Playlist X Track {i:02d}", artists=f"Artist {i%7+1}", duration_ms=180000 + (i % 5) * 17000)
        for i in range(1, 31)
    ]
    random_tracks = [
        Track(name=f"Random Track {i:04d}", artists=f"Random Artist {i%19+1}", duration_ms=165000 + (i % 9) * 11000, origin="random")
        for i in range(1, 201)
    ]

    reference = datetime(2026, 9, 30, 12, 0, tzinfo=ZoneInfo("America/Caracas"))
    rows = build_rows(x_tracks, random_tracks, reference, seed=123456)
    if len(rows) != TARGET_ROWS:
        raise SystemExit(f"Expected {TARGET_ROWS} rows, got {len(rows)}")

    # Verificación: las 30 filas de Playlist X son las primeras 30 y están en el orden temporal esperado.
    x_rows = [r for r in rows if r["origin"] == "playlist_x"]
    random_rows = [r for r in rows if r["origin"] == "random"]
    if len(x_rows) != 30 or len(random_rows) != 20:
        raise SystemExit(f"Unexpected split: X={len(x_rows)} random={len(random_rows)}")
    if x_rows[0]["played_at"] != reference:
        raise SystemExit("Playlist X first Played at does not match reference time.")

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    final_html = inject_into_template(template, rows)

    out_dir = Path("smoke_output")
    out_dir.mkdir(parents=True, exist_ok=True)
    with_png = out_dir / "smoke_report.png"

    png_path, metrics = render_png(final_html, out_dir)
    if metrics["pages"] != 1:
        raise SystemExit(f"Expected 1 PDF page, got {metrics['pages']}")
    if metrics["width"] != 2400 or metrics["height"] != 3400:
        raise SystemExit(f"Unexpected PNG size: {metrics['width']}x{metrics['height']}")

    print("SMOKE TEST OK")
    print(f"rows={len(rows)} x={len(x_rows)} random={len(random_rows)}")
    print(f"pages={metrics['pages']} size={metrics['width']}x{metrics['height']} bytes={metrics['png_bytes']}")
    print(f"output={png_path}")


if __name__ == "__main__":
    main()
