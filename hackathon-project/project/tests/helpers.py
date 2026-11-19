def make_pdf(path, pages):
    """pages: list of callables(page) that draw on a fresh A4 page."""
    import pymupdf
    d = pymupdf.open()
    for draw in pages:
        draw(d.new_page(width=595, height=842))
    d.save(str(path)); d.close()
    return path


def paragraph(page, y, text, size=11, x=60, w=475, bold=False):
    page.insert_textbox((x, y, x + w, y + 140), text, fontsize=size, fontname="hebo" if bold else "helv")
