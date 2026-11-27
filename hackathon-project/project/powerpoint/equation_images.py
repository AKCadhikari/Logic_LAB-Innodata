"""Recognize explicitly identified equation pictures and preserve the picture object."""
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
import posixpath
import re

from PIL import Image, ImageOps, ImageChops, UnidentifiedImageError
from lxml import etree
import pymupdf
from pptx.oxml.ns import qn
from pptx.enum.shapes import MSO_SHAPE_TYPE

from layout.equations import NUMBER, formula_crop
from layout.formula_ocr import recognise
from layout.math_structure import mathml, signature
from ocr import glm
from ocr.recognition import RecognitionError
from powerpoint.formulas import canonical, equation_prefix, latex_tokens, native_equation
from powerpoint.package import MC, RunEdit, canonical_picture_alternative, parse
from utils import execution_audit as audit

LABEL = re.compile(r"(?iu)\b(?:eq(?:uation)?\.?|formula)\s*(?:no\.?\s*)?\(?([0-9]+(?:\.[0-9]+)*)\)?(?![\d.])")
A14 = "http://schemas.microsoft.com/office/drawing/2010/main"


def media_usage(package):
    """Count real image references, including media shared with masters and notes."""
    counts = Counter()
    parts = set(package.namelist())
    for part in parts:
        if not part.startswith("ppt/") or not part.endswith(".rels"):
            continue
        folder, filename = part.rsplit("/_rels/", 1)
        source = posixpath.join(folder, filename.removesuffix(".rels"))
        references = Counter(value for element in parse(package.read(source)).iter()
                             for attribute, value in element.attrib.items()
                             if attribute in {qn("r:embed"), qn("r:link"), qn("r:id")}) if (
                                 source in parts and source.endswith(".xml")) else Counter()
        for node in parse(package.read(part)):
            target = node.get("Target", "")
            if node.get("Type", "").endswith("/image") and node.get("TargetMode") != "External":
                counts[target.rsplit("/", 1)[-1]] += max(1, references[node.get("Id")])
    return counts


def inspect(slide, shape, verified, source_pdf, usage, settings):
    if shape.shape_type != MSO_SHAPE_TYPE.PICTURE:
        return None
    element = shape._element
    properties = element.find("./" + qn("p:nvPicPr") + "/" + qn("p:cNvPr"))
    if properties is None:
        return None
    labels = set(LABEL.findall(" ".join(properties.get(key, "") for key in ("name", "descr", "title"))))
    if len(labels) != 1:
        return None
    label = next(iter(labels))
    location = {"shape": shape.name, "shape_id": shape.shape_id, "object_type": "equation_picture"}
    choices = [proof for marker, _, proof in verified if marker == label]
    if len(choices) != 1:
        return location, "", "", None, None, "The equation picture label did not identify a unique verified PDF formula."
    proof = choices[0].copy()
    actual = ""
    try:
        media = slide.part.related_part(element.find(".//" + qn("a:blip")).get(qn("r:embed")))
        key = str(media.partname).lstrip("/")
        blob = media.blob
        if len(blob) > 10 * 1024 * 1024:
            raise ValueError("The equation picture exceeds the recognition size limit.")
        with Image.open(BytesIO(blob)) as image:
            if image.width * image.height > 16_000_000 or image.width < 16 or image.height < 8:
                raise ValueError("The picture dimensions do not support reliable equation recognition.")
            if image.mode not in {"L", "RGB"} or image.format not in {"JPEG", "PNG"}:
                raise ValueError("The picture format or transparency requires a layout review.")
            original = image.convert("RGB")
            output_format = image.format
        start = audit.cursor()
        actual = transcribe(original, settings, f"pptx/{key}") or ""
        proof["picture_recognition_ids"] = audit.ids_since(start)
        expected_tex = proof["equation_latex"]
        expected = signature(mathml(expected_tex))
        found = signature(mathml(actual))
        if actual and found is not None and expected == found:
            return None
        old_prefix = equation_prefix(latex_tokens(actual) or [])
        new_prefix = equation_prefix(latex_tokens(expected_tex) or [])
        if not (found is not None and old_prefix and old_prefix == new_prefix):
            raise ValueError("The pictured expression could not be corroborated against the labelled source formula.")
        if (usage[key.rsplit("/", 1)[-1]] != 1 or
                any(abs(getattr(shape, "crop_" + edge)) > .001 for edge in ("top", "bottom", "left", "right"))):
            raise ValueError("The original media is shared or cropped and requires a manual edit.")
        packed = replacement(source_pdf, proof, original, output_format)
        return location, actual, proof["text"], proof, (key, packed), "The original equation image was replaced with the labelled source PDF crop."
    except (AttributeError, KeyError, OSError, RuntimeError, ValueError, UnidentifiedImageError) as exc:
        return location, actual, proof["text"], proof, None, str(exc)


def transcribe(image, settings, reference):
    if not settings.ocr_enabled:
        return None
    candidate = []
    base = ImageOps.autocontrast(ImageOps.grayscale(image))
    base.thumbnail((1600, 900))
    variants = (ImageOps.expand(base, 8, 255), ImageOps.expand(base.resize(
            (max(1, base.width * 3 // 4), max(1, base.height * 3 // 4)), Image.Resampling.LANCZOS), 16, 255))
    if settings.ocr_backend in {"automatic", "glm-ocr"} or settings.formula_glm_on_other_engines:
        ready = glm.model_info(settings)["ready"]
    else:
        ready = False
    if ready:
        configured = replace(settings, glm_ocr_tokens=min(768, settings.glm_ocr_tokens),
                             glm_ocr_timeout_s=min(45, settings.glm_ocr_timeout_s), glm_ocr_keep_alive="2m")
        for normalized in variants:
            buffer = BytesIO()
            normalized.save(buffer, format="PNG")
            try:
                response = glm.recognise_image(buffer.getvalue(), 0, configured, "formula", input_reference=reference)
                if not response.issues and len(response.blocks) == 1 and response.blocks[0].kind == "equation":
                    candidate.append(NUMBER.sub("", response.blocks[0].text).strip())
            except (RecognitionError, OSError, RuntimeError, ValueError):
                break
    if settings.formula_pix2tex_checkpoint:
        for index, variant in enumerate(variants[:1] if candidate else variants, 1):
            buffer = BytesIO()
            variant.save(buffer, format="PNG")
            text = recognise(buffer.getvalue(), settings, f"{reference}/pix2tex/{index}")
            if text:
                candidate.append(NUMBER.sub("", text).strip())
    signatures = [signature(mathml(value)) for value in candidate]
    return candidate[0] if len(signatures) >= 2 and signatures[0] is not None and len(set(signatures)) == 1 else None


def replacement(path, proof, original, output_format):
    with pymupdf.open(path) as pdf:
        page = pdf[proof["page"] - 1]
        if page.rotation:
            raise ValueError("The PDF equation is on a rotated page and requires a crop review.")
        _, box = formula_crop(page, proof["bbox"], proof.get("equation_number"))
        if box.is_empty or box.width > 1400 or box.height > 1400:
            raise ValueError("The source equation crop is invalid.")
        raster = page.get_pixmap(dpi=min(440, int(3000 * 72 / max(box.width, box.height))),
                                 clip=box, alpha=False, colorspace=pymupdf.csRGB)
        pixels = Image.frombytes("RGB", (raster.width, raster.height), raster.samples)
    ink = ImageChops.difference(pixels, Image.new("RGB", pixels.size, "white")).convert("L")
    content = ink.point(lambda value: 255 if value > 40 else 0).getbbox()
    if not content:
        raise ValueError("The verified equation crop has no visible ink.")
    # Only replace conventional dark-on-light equation pictures. Keep the original aspect and footprint.
    corners = [original.getpixel(point) for point in ((0, 0), (0, original.height - 1),
               (original.width - 1, 0), (original.width - 1, original.height - 1))]
    if any(min(color) < 240 for color in corners):
        raise ValueError("The original equation picture has a non-white background.")
    extent = max(1000, min(2500, max(original.size)))
    canvas = tuple(max(1, round(edge * extent / max(original.size))) for edge in original.size)
    fitted = ImageOps.contain(pixels.crop(content), tuple(max(1, int(edge * .94)) for edge in canvas))
    if fitted.width < canvas[0] * .3 or fitted.height < canvas[1] * .3:
        raise ValueError("The verified source crop does not fit the existing equation footprint.")
    background = Image.new("RGB", canvas, "white")
    background.paste(fitted, ((canvas[0]-fitted.width)//2, (canvas[1]-fitted.height)//2))
    buffer = BytesIO()
    background.save(buffer, output_format, quality=97)
    return buffer.getvalue()


def editable_picture(slide, shape, proof):
    """Use the Office math choice with the verified image as the compatibility fallback."""
    original = shape._element
    if original.getparent().tag != qn("p:spTree") or shape.rotation or shape.height < 300000:
        return None
    tokens = latex_tokens(proof["equation_latex"])
    # Convert only a simple horizontal expression whose native text can be checked after rendering.
    if not tokens or not all(len(token) == 1 for token in tokens):
        return None
    math = native_equation(proof["equation_latex"], tokens)
    if math is None:
        return None
    font = min(2200, int(shape.height / 127) // 2, int(shape.width / 127) // len(tokens))
    if font < 800:
        return None
    for run in math.iter(qn("m:r")):
        props = etree.Element(qn("a:rPr"), sz=str(font))
        etree.SubElement(props, qn("a:latin"), typeface="Cambria Math")
        run.insert(0, props)

    namespaces = {prefix: qn(prefix + ":node")[1:].split("}", 1)[0] for prefix in ("p", "a", "m", "r")}
    alt = etree.Element(f"{{{MC}}}AlternateContent", nsmap={**namespaces, "mc": MC, "a14": A14})
    choice = etree.SubElement(alt, f"{{{MC}}}Choice", Requires="a14")
    sp = etree.SubElement(choice, qn("p:sp"))
    nonvisual = etree.SubElement(sp, qn("p:nvSpPr"))
    nonvisual.append(deepcopy(original.find(".//" + qn("p:cNvPr"))))
    etree.SubElement(nonvisual, qn("p:cNvSpPr"), txBox="1")
    etree.SubElement(nonvisual, qn("p:nvPr"))
    props = etree.SubElement(sp, qn("p:spPr"))
    original_props = original.find(qn("p:spPr"))
    transform = original_props.find(qn("a:xfrm"))
    geometry = original_props.find(qn("a:prstGeom"))
    if transform is None or geometry is None or geometry.get("prst") != "rect":
        return None
    props.extend((deepcopy(transform), deepcopy(geometry)))
    etree.SubElement(etree.SubElement(props, qn("a:solidFill")), qn("a:srgbClr"), val="FFFFFF")
    body = etree.SubElement(sp, qn("p:txBody"))
    etree.SubElement(body, qn("a:bodyPr"), lIns="0", tIns="0", rIns="0", bIns="0", anchor="ctr")
    etree.SubElement(body, qn("a:lstStyle"))
    paragraph = etree.SubElement(body, qn("a:p"))
    etree.SubElement(paragraph, qn("a:pPr"), algn="ctr")
    zone = etree.SubElement(paragraph, f"{{{A14}}}m")
    etree.SubElement(zone, qn("m:oMathPara")).append(math)
    fallback = etree.SubElement(alt, f"{{{MC}}}Fallback")
    fallback_shape = deepcopy(sp)
    fallback_shape.remove(fallback_shape.find(qn("p:txBody")))
    fill = deepcopy(original.find(qn("p:blipFill")))
    fill.tag = qn("a:blipFill")
    fallback_props = fallback_shape.find(qn("p:spPr"))
    fallback_props.replace(fallback_props.find(qn("a:solidFill")), fill)
    fallback.append(fallback_shape)
    # Exclusive canonicalization omits namespaces used only inside Requires attribute values.
    xml = canonical_picture_alternative(alt)
    root = slide._element.getroottree()
    location = []
    node = original
    while node is not slide._element:
        parent = node.getparent()
        location.append(parent.index(node))
        node = parent
    if not location or "xmlns:a14" not in xml.split("Requires=", 1)[0]:
        # Keep the QName binding on Choice even if canonicalization did not retain it there.
        return None
    return RunEdit(root.getelementpath(original), canonical(original), xml, qn("p:pic"),
                   position=tuple(reversed(location))), tokens
