"""Assign semantic kinds using document structure and typography."""
from __future__ import annotations
import re
from statistics import median
from layout.model import DocIR, Block

R = lambda p: re.compile(p, re.I)
ABSTRACT = R(r"^\W*(abstract|resumen|résumé|resume|zusammenfassung|samenvatting|riassunto|abstrakt|souhrn|súhrn|ملخص|الملخص|要旨|概要|초록|요약)\b")
REFS = R(r"^\W*(references?|bibliography|literature cited|literaturverzeichnis|literatur|références|bibliographie|bibliografia|referenties|literatura|seznam literatury|المراجع|قائمة المراجع|参考文献|참고문헌)\b")
ACK = R(r"^\W*(acknowledg\w*|danksagung|remerciements|ringraziamenti|dankwoord|poděkování|poďakovanie|شكر|謝辞|감사)")
KEYW = R(r"^\W*(key\s?words?|index terms|schlüsselwörter|schlagwörter|mots[- ]clés|parole chiave|trefwoorden|klíčová slova|kľúčové slová|palabras clave|الكلمات المفتاحية|キーワード|키워드)\b")
RIGHTS = R(r"(©|\(c\)|copyright|all rights reserved|licen[cs]e|creative commons|cc[- ]by)")
PUBMETA = R(r"(issn|isbn|doi\b|doi\.org|\bvol(ume)?\.?\s*\d|\bissue\b|journal of|received:|accepted:|published:|proceedings)")
AFFIL = R(r"(universit|institut|department|faculty|college|school of|laborator|hospital|academy|centre|center|@|université|universität|hochschule|جامعة|كلية|大学|研究所|대학)")
NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+){0,4})\s+[A-Z][^\n.:!?]{2,90}$")
CLASSIFICATION = R(r"^\W*(mathematics subject classification|computing classification system)\b")
ADDRESS = R(r"\b(street|road|avenue|postal code|p\.?\s*o\.?\s+box)\b")
AUTHOR_SEPARATOR = R(r",|\s+(?:and|und|et)\s+|،")


def metadata_kind(text):
    if RIGHTS.search(text):
        return "rights"
    return "publication_metadata" if PUBMETA.search(text) else ""


def _pick_title(doc: DocIR):
    """Title = the largest text block on the first content page (must clearly stand out from body text)."""
    for page in doc.pages[:2]:
        cand = [b for b in page.blocks if b.tag not in ("header", "footer", "table", "figure", "ul", "ol", "hr", "aside") and b.size > 0
                and 6 <= len((b.text or "").strip()) <= 300 and b.kind != "equation"]
        if not cand:
            continue
        med = median(b.size for b in cand)
        best = max(cand, key=lambda b: (b.size, -(b.bbox[1] if b.bbox else 0)))
        if best.size >= 1.2 * med:
            return best
    return None


def assign_kinds(doc: DocIR) -> None:
    section = "front"
    title_done = False
    chosen = _pick_title(doc)
    if chosen is not None:
        chosen.tag, chosen.kind = "h1", "title"
        # a wrapped title continues on the next block with the same size
        for page in doc.pages[:2]:
            if chosen in page.blocks:
                i = page.blocks.index(chosen)
                for nb in page.blocks[i + 1:i + 3]:
                    if not nb.bbox or not chosen.bbox:
                        continue
                    centers_close = abs(sum(nb.bbox[::2]) - sum(chosen.bbox[::2])) < page.width * 0.05
                    similar_size = abs(nb.size - chosen.size) < 0.1 * chosen.size
                    short_centered = (centers_close and 0.78 * chosen.size <= nb.size
                                      and 1 <= len(nb.text.split()) <= 7 and not nb.text.endswith((".", ":")))
                    if (nb.tag in {"p", "h1", "h2"} and nb.writing_mode == chosen.writing_mode
                            and (centers_close and similar_size and len(nb.text.split()) <= 15 or short_centered)
                            and not AFFIL.search(nb.text)
                            and not AUTHOR_SEPARATOR.search(nb.text) and not ABSTRACT.match(nb.text)
                            and 0 <= nb.bbox[1] - chosen.bbox[3] < 1.5 * chosen.size):
                        chosen.original_text = "\n".join((chosen.original_text or chosen.text, nb.original_text or nb.text))
                        chosen.line_boxes = [*(chosen.line_boxes or [chosen.bbox]), *(nb.line_boxes or [nb.bbox])]
                        chosen.text = (chosen.text + " " + nb.text).strip(); chosen.bbox = (min(chosen.bbox[0], nb.bbox[0]), chosen.bbox[1], max(chosen.bbox[2], nb.bbox[2]), nb.bbox[3])
                        chosen.spans = []; page.blocks.remove(nb); break
                break
    seen_aff = False
    for pi, page in enumerate(doc.pages):
        for b in page.blocks:
            if b.tag in ("header", "footer"):
                b.kind = metadata_kind(b.text) or "publication_metadata"; continue
            if b.tag in ("table", "figure"):
                b.kind = "figure_caption" if False else b.kind
                continue
            txt = b.text or " ".join(c.text for c in b.children)
            number = NUMBERED_HEADING.fullmatch(txt.strip()) if b.tag == "p" and b.source == "ocr" else None
            if number and len(txt.split()) <= 12 and b.kind != "equation":
                b.tag = f"h{2 + number[1].count('.')}"
            if b.tag == "p" and len(txt.split()) <= 3 and (REFS.match(txt) or ACK.match(txt)) and len(txt) < 32:
                b.tag = "h2"
            elif section == "references" and not (REFS.match(txt) or ACK.match(txt)):
                if b.tag in ("p", "h2", "h3", "h4", "h5", "h6", "ul", "ol"):
                    b.kind = "reference_entry"
                    if b.tag not in ("ul", "ol"):
                        b.tag = "p"
                    continue
            if b is chosen:
                title_done = True; section = "front"; continue
            if pi == 0 and section == "front" and b.tag in {"p", "h2", "h3", "h4", "h5", "h6"}:
                front_kind = metadata_kind(txt) if len(txt) < 300 else ""
                if not front_kind and title_done and len(txt) < 300 and (AFFIL.search(txt) or ADDRESS.search(txt)):
                    front_kind = "affiliation"
                    seen_aff = True
                if (not front_kind and title_done and not seen_aff and len(txt) < 180
                        and AUTHOR_SEPARATOR.search(txt) and not txt.endswith(".")
                        and sum(char.isupper() for char in txt) >= sum(char.islower() for char in txt)):
                    front_kind = "byline"
                if front_kind:
                    b.tag, b.kind = "p", front_kind
                    continue
            if section == "front" and ABSTRACT.fullmatch(txt.strip()) and b.tag == "p":
                b.tag = "h2"
            if b.tag.startswith("h"):
                if b is chosen:
                    title_done = True; section = "front"; continue
                if chosen is None and pi == 0 and not title_done and not number and not ABSTRACT.match(txt):
                    b.tag, b.kind, title_done = "h1", "title", True
                    section = "front"; continue
                b.kind = "section_heading"
                head = txt.strip()
                if ABSTRACT.match(head): section = "abstract"; b.kind = "abstract"
                elif REFS.match(head): section = "references"
                elif ACK.match(head): section = "acknowledgment"; b.kind = "acknowledgment"
                else: section = "body"
                continue
            if b.tag == "aside" or b.kind == "equation":
                continue
            items = b.children if b.tag in ("ul", "ol") else [b]
            for it in items:
                t = (it.text or "").strip()
                if b.kind == "figure_caption" or it.kind == "figure_caption":
                    it.kind = "figure_caption"; continue
                if KEYW.match(t) or CLASSIFICATION.match(t): k = "keywords"
                elif ABSTRACT.match(t) and section in ("front", "abstract"): k = "abstract"; section = "abstract"
                elif section == "references": k = "reference_entry"
                elif section == "abstract": k = "abstract"
                elif section == "acknowledgment": k = "acknowledgment"
                elif RIGHTS.search(t) and len(t) < 400: k = "rights"
                elif section == "front" and PUBMETA.search(t) and len(t) < 300: k = "publication_metadata"
                elif section == "front" and AFFIL.search(t) and len(t) < 300: k = "affiliation"; seen_aff = True
                elif section == "front" and pi == 0 and title_done and not seen_aff and len(t) < 160 and not re.search(r"\d", t) and not t.endswith(".") and re.search(r",| and | und | et |،| و ", t): k = "byline"
                else: k = "body"
                it.kind = k
            if b.tag in ("ul", "ol"):
                b.kind = items[0].kind if items else "body"
