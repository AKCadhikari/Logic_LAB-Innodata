"""Semantic `kind` attribute (bonus marks in the brief) + per-block language tagging."""
from __future__ import annotations
import re
from layout.model import DocIR, Block
from utils.textutil import detect_lang, script_counts

R = lambda p: re.compile(p, re.I)
ABSTRACT = R(r"^\W*(abstract|resumen|résumé|resume|zusammenfassung|samenvatting|riassunto|abstrakt|souhrn|súhrn|ملخص|الملخص|要旨|概要|초록|요약)\b")
REFS = R(r"^\W*(references?|bibliography|literature cited|literaturverzeichnis|literatur|références|bibliographie|bibliografia|referenties|literatura|seznam literatury|المراجع|قائمة المراجع|参考文献|참고문헌)\b")
ACK = R(r"^\W*(acknowledg\w*|danksagung|remerciements|ringraziamenti|dankwoord|poděkování|poďakovanie|شكر|謝辞|감사)")
KEYW = R(r"^\W*(key\s?words?|index terms|schlüsselwörter|schlagwörter|mots[- ]clés|parole chiave|trefwoorden|klíčová slova|kľúčové slová|palabras clave|الكلمات المفتاحية|キーワード|키워드)\b")
RIGHTS = R(r"(©|\(c\)|copyright|all rights reserved|licen[cs]e|creative commons|cc[- ]by)")
PUBMETA = R(r"(issn|isbn|doi\b|doi\.org|\bvol(ume)?\.?\s*\d|\bissue\b|journal of|received:|accepted:|published:|proceedings)")
AFFIL = R(r"(universit|institut|department|faculty|college|school of|laborator|hospital|academy|centre|center|@|université|universität|hochschule|جامعة|كلية|大学|研究所|대학)")


def _pick_title(doc: DocIR):
    """Title = the largest text block on the first content page (must clearly stand out from body text)."""
    for page in doc.pages[:2]:
        cand = [b for b in page.blocks if b.tag not in ("header", "footer", "table", "figure", "ul", "ol", "hr", "aside") and b.size > 0
                and 6 <= len((b.text or "").strip()) <= 300 and b.kind != "equation"]
        if not cand:
            continue
        sizes = sorted(b.size for b in cand)
        med = sizes[len(sizes) // 2]
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
                    if nb.tag == "p" and nb.size and abs(nb.size - chosen.size) < 0.1 * chosen.size and nb.bbox and chosen.bbox and 0 <= nb.bbox[1] - chosen.bbox[3] < 1.5 * chosen.size:
                        chosen.text = (chosen.text + " " + nb.text).strip(); chosen.bbox = (min(chosen.bbox[0], nb.bbox[0]), chosen.bbox[1], max(chosen.bbox[2], nb.bbox[2]), nb.bbox[3])
                        chosen.spans = []; page.blocks.remove(nb); break
                break
    seen_aff = False
    for pi, page in enumerate(doc.pages):
        for b in page.blocks:
            if b.tag in ("header", "footer"):
                b.kind = "publication_metadata"; continue
            if b.tag in ("table", "figure"):
                b.kind = "figure_caption" if False else b.kind
                continue
            txt = b.text or " ".join(c.text for c in b.children)
            if b is chosen:
                title_done = True; section = "front"; continue
            if b.tag.startswith("h"):
                if b is chosen:
                    title_done = True; section = "front"; continue
                if chosen is None and pi == 0 and not title_done:
                    b.tag, b.kind, title_done = "h1", "title", True
                    section = "front"; continue
                b.kind = "section_heading"
                head = txt.strip()
                if ABSTRACT.match(head): section = "abstract"; b.kind = "abstract"
                elif REFS.match(head): section = "references"
                elif ACK.match(head): section = "acknowledgment"; b.kind = "acknowledgment"
                else: section = "body"
                continue
            if b.tag == "aside":
                continue
            items = b.children if b.tag in ("ul", "ol") else [b]
            for it in items:
                t = (it.text or "").strip()
                if b.kind == "figure_caption" or it.kind == "figure_caption":
                    it.kind = "figure_caption"; continue
                if KEYW.match(t): k = "keywords"
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
    # per-block language (cross-script differences, or long Latin blocks differing from the doc language)
    for page in doc.pages:
        for b in page.blocks:
            items = b.children if b.tag in ("ul", "ol") else [b]
            for it in items:
                t = it.text or ""
                if len(t) < 30 or it.tag in ("table", "figure"):
                    continue
                sc = script_counts(t); tot = sum(sc.values()) or 1
                lg = detect_lang(t)
                if lg == "und" or lg == doc.lang:
                    continue
                cross = (lg in ("ar", "ja", "ko")) != (doc.lang in ("ar", "ja", "ko")) or (lg in ("ar", "ja", "ko") and doc.lang in ("ar", "ja", "ko") and lg != doc.lang)
                if cross and tot >= 20:
                    it.lang = lg
                elif not cross and len(t) >= 150 and lg != doc.lang and doc.lang not in ("ar", "ja", "ko"):
                    it.lang = lg
