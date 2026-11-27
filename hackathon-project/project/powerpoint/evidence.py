"""Bounded, slide-aware retrieval over source passages."""
from collections import Counter, defaultdict
from math import isfinite, log, sqrt
import re

from rapidfuzz import fuzz, process

from comparison.differ import STOPWORDS, ws_lower
from utils import execution_audit as audit, ollama


def terms(text):
    return {word for word in re.findall(r"[^\W_]+", text.casefold()) if word not in STOPWORDS and len(word) > 2}


class EvidenceIndex:
    def __init__(self, units, settings=None):
        self.units = units
        self.normalized = [unit.norm for unit in units]
        self.keywords = defaultdict(list)
        self.page_terms = defaultdict(set)
        self.vectors = []
        self.embedding_settings = settings if settings and settings.ollama_enabled and settings.amend_embedding_model else None
        self.query_cache = {}
        for index, unit in enumerate(units):
            tokens = terms(unit.text)
            self.page_terms[unit.page].update(tokens)
            for token in tokens:
                self.keywords[token].append(index)
        if self.embedding_settings and self.units:
            try:
                for offset in range(0, len(self.units), 24):
                    self.vectors.extend(self._embed([unit.text[:1200] for unit in self.units[offset:offset + 24]]))
                if len({len(vector) for vector in self.vectors}) != 1:
                    raise ValueError("The embedding vector shape changed.")
            except (TypeError, KeyError, ValueError, ollama.OllamaError):
                self.vectors = []

    def _embed(self, passages):
        settings = self.embedding_settings
        payload = {"model": settings.amend_embedding_model, "input": passages, "truncate": True}
        with audit.execution("pptx_evidence_retrieval", "ollama", settings.amend_embedding_model,
                             input_type="source_passages", input_sha256=audit.digest(payload),
                             input_reference="pptx_evidence_index") as event:
            result = ollama.request_json(settings.ollama_url, "/api/embed", payload, min(45, settings.ollama_timeout_s))
            vectors = result["embeddings"]
            if len(vectors) != len(passages):
                raise ValueError("The embedding response omitted passages.")
            output = []
            for vector in vectors:
                if not 2 <= len(vector) <= 4096 or any(not isinstance(value, (float, int)) or not isfinite(value) for value in vector):
                    raise ValueError("Invalid embedding vector.")
                norm = sqrt(sum(value * value for value in vector))
                if not norm or not isfinite(norm):
                    raise ValueError("Invalid embedding norm.")
                output.append([value / norm for value in vector])
            if event:
                event.update(validation="passed", downstream=[{"consumer": "pptx_source_shortlist", "vectors": len(output)}])
            return output

    def _related(self, query):
        if not self.vectors:
            return []
        if query not in self.query_cache:
            try:
                vector, = self._embed([query[:1200]])
                if len(vector) != len(self.vectors[0]):
                    raise ValueError("The query vector shape differs from the catalogue.")
                scores = sorted(enumerate(self.vectors), key=lambda item: (
                    -sum(a * b for a, b in zip(vector, item[1])), item[0]))
                self.query_cache[query] = [index for index, _ in scores[:12]]
            except (TypeError, KeyError, ValueError, ollama.OllamaError):
                self.vectors = []
                return []
        return self.query_cache[query]

    def shortlist(self, text, context, hints):
        weights = Counter()
        context_terms = terms(context)
        for token in terms(text):
            hits = self.keywords.get(token, [])
            weight = 1 + log((1 + len(self.units)) / (1 + len(hits)))
            for index in hits:
                weights[index] += weight
        fuzzy = [index for _, _, index in process.extract(ws_lower(text), self.normalized,
                 scorer=fuzz.partial_ratio, limit=24, score_cutoff=45)]
        selected = {**dict.fromkeys(fuzzy), **dict.fromkeys(index for index, _ in weights.most_common(18)),
                    **dict.fromkeys(self._related(text))}
        return [self.units[index] for index in sorted(selected, key=lambda index: (
            index not in fuzzy, -weights[index],
            self.units[index].page not in hints,
            -len(context_terms & terms(" ".join(self.units[index].section))),
            -len(context_terms & self.page_terms[self.units[index].page])))]

    def corroborated_pages(self, notes, reported):
        """A note paragraph must independently match a PDF passage before boosting its page."""
        checks = [ws_lower(line) for line in notes.splitlines() if len(terms(line)) >= 5]
        return {unit.page for unit in self.units if (not reported or unit.page in reported)
                and any(len(terms(unit.text)) >= 5 and fuzz.token_ratio(note, unit.norm) >= 92 for note in checks)}
