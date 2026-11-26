"""Build a page-faithful presentation from a validated PDF extraction."""
from io import BytesIO

import pymupdf
from pptx import Presentation
from pptx.util import Inches

from diagnostics.profile import block_text
from powerpoint.language import shape_paragraph
from utils.textutil import xml_safe


def render_presentation(ir, output):
    presentation = Presentation()
    with pymupdf.open(ir.path) as source:
        if len(source) != len(ir.pages):
            raise ValueError("The source PDF and reconstruction have different page counts.")
        first = source[0].rect
        presentation.slide_width = Inches(max(1, 10 * first.width / max(first.width, first.height)))
        presentation.slide_height = Inches(max(1, 10 * first.height / max(first.width, first.height)))
        for page, structured in zip(source, ir.pages):
            slide = presentation.slides.add_slide(presentation.slide_layouts[6])
            rect = page.rect
            scale = 1600 / max(rect.width, rect.height)
            png = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
            fit = min(presentation.slide_width / rect.width, presentation.slide_height / rect.height)
            width, height = round(fit * rect.width), round(fit * rect.height)
            slide.shapes.add_picture(BytesIO(png), (presentation.slide_width - width) // 2,
                                     (presentation.slide_height - height) // 2, width=width, height=height)
            notes = slide.notes_slide.notes_text_frame
            if notes is not None:
                notes.text = xml_safe("\n\n".join(filter(None, map(block_text, structured.blocks))))
                for paragraph in notes.paragraphs:
                    shape_paragraph(paragraph._p)
    presentation.save(output)
    return {"slides": len(presentation.slides), "status": "generated", "applied": [], "review": []}
