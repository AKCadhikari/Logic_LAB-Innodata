"""Render and compare presentation slides with a local LibreOffice process."""
from __future__ import annotations

import html
import shutil
import subprocess
from contextlib import ExitStack
from pathlib import Path

import pymupdf


def renderer() -> str | None:
    executable = shutil.which("soffice")
    if executable:
        return executable
    installed = Path(r"C:\Program Files\LibreOffice\program\soffice.exe")
    return str(installed) if installed.is_file() else None


def compare_slides(decks: dict[str, Path], out: Path, executable: str) -> dict:
    out.mkdir(parents=True, exist_ok=False)
    rendered = {}
    with ExitStack() as stack:
        for label, source in decks.items():
            folder = out / label
            folder.mkdir()
            profile = (out / "libreoffice-profile").as_uri()
            result = subprocess.run([executable, f"-env:UserInstallation={profile}", "--headless",
                                     "--convert-to", "pdf", "--outdir", str(folder), str(source.resolve())],
                                    capture_output=True, text=True, errors="replace", timeout=180)
            pdf = folder / (source.stem + ".pdf")
            if result.returncode or not pdf.is_file():
                raise ValueError(f"LibreOffice could not render {label}: {result.stderr or result.stdout}")
            rendered[label] = stack.enter_context(pymupdf.open(pdf))
        rows, comparisons = [], []
        for index in range(max(len(doc) for doc in rendered.values())):
            cells, pixmaps = [], {}
            for label, document in rendered.items():
                if index >= len(document):
                    cells.append("<td>Missing slide</td>")
                    continue
                pix = document[index].get_pixmap(dpi=88, alpha=False, colorspace=pymupdf.csRGB)
                pixmaps[label] = pix
                image = f"{label}/slide-{index + 1:03d}.png"
                pix.save(out / image)
                cells.append(f'<td><a href="{image}"><img loading="lazy" src="{image}" '
                             f'alt="{html.escape(label)} slide {index + 1}"></a></td>')
            expected, actual = pixmaps.get("expected"), pixmaps.get("actual")
            match = bool(expected and actual and expected.width == actual.width and expected.height == actual.height
                         and expected.samples == actual.samples)
            comparisons.append({"slide": index + 1, "expected_matches_actual": match,
                                "available": list(pixmaps)})
            rows.append(f'<tr><th scope="row">{index + 1}<br>{"Match" if match else "Different"}</th>'
                        + "".join(cells) + "</tr>")
    headings = "".join(f'<th scope="col">{html.escape(label.title())}</th>' for label in decks)
    page = ('<!doctype html><meta charset="utf-8"><title>Slide comparison</title>'
            '<style>body{font:15px system-ui;background:#f8f5ee;color:#23332f;margin:24px}'
            'table{width:100%;border-collapse:collapse;table-layout:fixed}'
            'td,th{padding:8px;border-bottom:1px solid #d1d4c9;vertical-align:top}th:first-child{width:84px}'
            'img{width:100%;border:1px solid #d1d4c9}</style>'
            '<h1>Slide comparison</h1><p>Rendered locally with LibreOffice at 88 DPI.</p>'
            f'<table><thead><tr><th scope="col">Slide</th>{headings}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')
    (out / "index.html").write_text(page, encoding="utf-8")
    return {"renderer": "LibreOffice", "matched_slides": sum(row["expected_matches_actual"] for row in comparisons),
            "slides": comparisons, "report": "visual/index.html"}
