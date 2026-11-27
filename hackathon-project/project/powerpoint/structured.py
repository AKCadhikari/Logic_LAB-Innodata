"""Source-backed PowerPoint tables and hyperlink planning."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit

from pptx.oxml.ns import qn
from rapidfuzz import fuzz

from comparison.differ import ws_lower
from powerpoint.package import REL, RunEdit, parse


def source_tables(ir):
    for page in ir.pages:
        for block in page.blocks:
            if block.tag == "table" and block.cells:
                grid = {}
                for cell in block.cells:
                    grid[cell.row, cell.col] = cell
                yield page, block, grid


def table_matches(table, sources):
    if len(table.rows) < 2 or len(table.columns) < 2:
        return []
    headers = {ws_lower(table.cell(0, i).text): i for i in range(len(table.columns))
               if table.cell(0, i).text.strip()}
    rows = {ws_lower(table.cell(i, 0).text): i for i in range(1, len(table.rows)) if table.cell(i, 0).text.strip()}
    if len(headers) != len(table.columns) or len(rows) != len(table.rows) - 1:
        return []
    found = []
    for page, block, grid in sources:
        source_headers = {ws_lower(cell.text): j for (i, j), cell in grid.items() if i == 0 and cell.text.strip()}
        source_rows = {ws_lower(cell.text): i for (i, j), cell in grid.items() if i and j == 0 and cell.text.strip()}
        if not (headers.keys() <= source_headers.keys() and rows.keys() <= source_rows.keys()):
            continue
        if len(source_headers) != sum(i == 0 and bool(cell.text.strip()) for (i, _), cell in grid.items()):
            continue
        if len(source_rows) != sum(i > 0 and j == 0 and bool(cell.text.strip()) for (i, j), cell in grid.items()):
            continue
        found.append((page, block, {(ri, ci): grid.get((source_rows[row], source_headers[col]))
                      for row, ri in rows.items() for col, ci in headers.items() if ci}))
    return found


def chart_discrepancies(chart, sources):
    """Compare a cached chart series against a uniquely matching source table."""
    categories = [str(category.label) for category in chart.plots[0].categories]
    series = [(str(item.name), list(item.values)) for item in chart.series]
    if len(categories) < 2 or not series or any(len(values) != len(categories) for _, values in series):
        return None
    if len({ws_lower(item) for item in categories}) != len(categories):
        return None
    matching = []
    for page, block, grid in sources:
        rows = {ws_lower(cell.text): row for (row, col), cell in grid.items() if row > 0 and col == 0 and cell.text}
        cols = {ws_lower(cell.text): col for (row, col), cell in grid.items() if row == 0 and col > 0 and cell.text}
        if len(rows) != sum(i > 0 and j == 0 and bool(cell.text) for (i, j), cell in grid.items()):
            continue
        if all(ws_lower(name) in cols for name, _ in series) and all(ws_lower(value) in rows for value in categories):
            matching.append((page, block, grid, rows, cols))
    if len(matching) != 1:
        return None
    page, block, grid, rows, cols = matching[0]
    changes = []
    for name, values in series:
        for label, value in zip(categories, values):
            cell = grid.get((rows[ws_lower(label)], cols[ws_lower(name)]))
            if cell is None or value is None:
                continue
            try:
                expected = Decimal(cell.text)
                if not expected.is_finite() or expected == Decimal(str(value)):
                    continue
            except InvalidOperation:
                continue
            changes.append((name, label, str(value), cell.text,
                            {"page": page.n, "bbox": cell.bbox or block.bbox, "text": cell.text,
                             "extraction": block.source, "table_row": cell.row + 1, "table_column": cell.col + 1}))
    return changes


def external_url(value):
    try:
        parts = urlsplit(value)
        return bool(parts.scheme in {"http", "https"} and parts.hostname and not parts.username
                    and not parts.password and not re.search(r"[\s\x00-\x1f]", value))
    except ValueError:
        return False


def source_links(ir):
    result = defaultdict(list)
    for page in ir.pages:
        for block in page.blocks:
            values = [span.link for span in block.spans if span.link]
            if block.source == "native" and page.diag.get("health") == "healthy":
                values += [url.rstrip(".,;)") for url in re.findall(r"https?://[^\s<>]+", block.text)]
            for value in dict.fromkeys(values):
                if external_url(value):
                    result[value].append({"page": page.n, "bbox": block.bbox, "text": value, "extraction": block.source})
    return dict(result)


def hyperlink_edits(package, part, drawing, candidates):
    path = PurePosixPath(part)
    rels = str(path.parent / "_rels" / (path.name + ".rels"))
    if rels not in package.namelist():
        return []
    root = parse(package.read(rels))
    relationships = {relation.get("Id"): relation for relation in root if relation.tag == f"{{{REL}}}Relationship"
                     and relation.get("TargetMode") == "External" and relation.get("Type", "").endswith("/hyperlink")}
    links = defaultdict(list)
    for pointer in drawing.iter(qn("a:hlinkClick"), qn("a:hlinkMouseOver")):
        links[pointer.get(qn("r:id"))].append(pointer)
    results = []
    for identifier, pointers in links.items():
        relation = relationships.get(identifier)
        if relation is None:
            continue
        value = relation.get("Target", "")
        if value in candidates or not external_url(value):
            continue
        visible = [pointer.getparent().getparent().find(qn("a:t")) for pointer in pointers]
        labels = [node.text.strip() if node is not None and node.text else "" for node in visible]
        prefix_has_path = len(urlsplit(value).path.strip("/")) >= 5
        matching = [new for new in candidates if urlsplit(new).hostname == urlsplit(value).hostname
                    and (new.startswith(value.rstrip("/") + "/") or fuzz.ratio(new, value) >= 90)
                    and (all(label == new for label in labels)
                         or (prefix_has_path and new.startswith(value.rstrip("/") + "/")
                             and all(label == value for label in labels)))]
        if len(matching) == 1:
            new, = matching
            text_nodes = {node for node, label in zip(visible, labels) if label == value}
            text_edits = [RunEdit(drawing.getroottree().getelementpath(node), node.text, node.text.replace(value, new, 1))
                          for node in text_nodes]
            results.append((rels, RunEdit(root.getroottree().getelementpath(relation), value, new, relation.tag, "Target"),
                            candidates[new][0], pointers[0], text_edits))
    return results
