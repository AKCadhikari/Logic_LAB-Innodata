"""Independently transcribe a crop with an explicitly provisioned local model."""
from io import BytesIO
import json
from pathlib import Path
from subprocess import TimeoutExpired, run
import sys

from PIL import Image, ImageOps

from utils import execution_audit as audit


def recognise(image, settings, reference):
    if not settings.formula_pix2tex_checkpoint:
        return None
    checkpoint = Path(settings.formula_pix2tex_checkpoint).expanduser().resolve()
    with audit.execution("equation_transcription", "pix2tex", "LaTeX-OCR",
                         input_type="image/png", input_sha256=audit.digest(image),
                         input_reference=reference) as event:
        if not checkpoint.is_file():
            if event is not None:
                event.update(validation="unavailable", error="The configured checkpoint is missing.")
            return None
        try:
            result = run([settings.formula_pix2tex_python or sys.executable, "-m", "layout.formula_ocr",
                          str(checkpoint)], cwd=Path(__file__).resolve().parents[1],
                         input=image, capture_output=True, timeout=75, check=False)
            if result.returncode:
                raise ValueError("The local pix2tex runtime returned an error.")
            text = json.loads(result.stdout.splitlines()[-1])
            if not isinstance(text, str) or not 1 <= len(text) <= 3500:
                raise ValueError("pix2tex returned no valid transcription.")
        except (IndexError, OSError, TimeoutExpired, ValueError) as exc:
            if event is not None:
                event.update(validation="unavailable", error=str(exc))
            return None
        if event is not None:
            event.update(validation="candidate")
        return text


def read_checkpoint(checkpoint):
    # Run in a separate process because the upstream model loader changes cwd.
    from argparse import Namespace
    checkpoint = Path(checkpoint).resolve(strict=True)
    from pix2tex.cli import LatexOCR
    model = LatexOCR(Namespace(config="settings/config.yaml", checkpoint=str(checkpoint),
                               no_cuda=True, no_resize=False, temperature=0.05))
    with Image.open(BytesIO(sys.stdin.buffer.read())) as image:
        image = ImageOps.autocontrast(ImageOps.grayscale(image))
        image.thumbnail((1800, 1200))
        print(json.dumps(model(ImageOps.expand(image, border=6, fill=255))))


if __name__ == "__main__":
    read_checkpoint(sys.argv[1])
