"""
vector_baseline.py — the plain vector-RAG baseline for the Phase-9 graph-vs-vector
comparison. Deliberately a COMPETENT baseline (the fairness charter), never a strawman:

  * Same raw LER text the graph was built from (data/raw/*.txt), keyed by LER number,
    over the exact same corpus the graph indexes (the 833 records in out/).
  * A STRONG local embedder (BAAI/bge-large-en-v1.5) and a WEAK one (all-MiniLM-L6-v2)
    run side by side, so the comparison's structural verdicts can be shown embedder-
    INVARIANT rather than an artifact of one model choice.
  * Standard fixed-size overlapping chunks; numpy brute-force cosine (the corpus is only
    a few thousand chunks, so exact search is instant and needs no ANN index).
  * Feeds the SAME answer.answer() as the graph through the SAME `Evidence` contract, so
    the only variable in the head-to-head is RETRIEVAL, not the answer model or prompt.

Everything is local and free to (re)build, so a clone reproduces the baseline without
paying. The embeddings cache under out/vector/ (git-ignored; regenerating them costs
only compute, never API dollars).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np


from .retrieve import Evidence  # the shared, retriever-agnostic hand-off to answer.py

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = REPO_ROOT / "data" / "raw"
OUT_DIR = REPO_ROOT / "out"
CACHE_DIR = REPO_ROOT / "out" / "vector"

# Embedders: one strong, one weak. bge-v1.5's documented retrieval recipe puts a short
# instruction on the QUERY side only (passages are embedded raw); MiniLM takes raw text.
# Both are local open-weights models, so the baseline is permanently reproducible.
MODELS = {
    "bge-large": {"hf": "BAAI/bge-large-en-v1.5",
                  "query_prefix": "Represent this sentence for searching relevant passages: "},
    "minilm":    {"hf": "sentence-transformers/all-MiniLM-L6-v2",
                  "query_prefix": ""},
}

# Chunk configs (words, overlap). MEDIUM is the primary config and fits BOTH models'
# context windows (MiniLM truncates at 256 tokens ~= ~190 words), so the embedder-
# invariance check is clean. SMALL/LARGE drive the chunk-size ablation — LARGE exceeds
# MiniLM's window (so MiniLM truncates it; bge-large does not), which is itself reported.
CHUNK_CONFIGS = {
    "small":  {"words": 90,  "overlap": 15},
    "medium": {"words": 180, "overlap": 27},
    "large":  {"words": 330, "overlap": 50},
}

DEFAULT_MODEL = "bge-large"
DEFAULT_CONFIG = "medium"
DEFAULT_K = 8
DEFAULT_THRESHOLD = 0.0   # refusal gate; TUNED and swept as a curve in compare.py


# --------------------------------------------------------------------------- #
# corpus -> chunks (same raw text + same corpus the graph was built from)
# --------------------------------------------------------------------------- #
def accession_to_ler() -> dict:
    """Map ADAMS accession -> LER number from the extracted records, so each raw text
    file is keyed by the SAME LER number the graph uses (the fair shared corpus)."""
    m = {}
    for f in glob.glob(str(OUT_DIR / "*.json")):
        try:
            r = json.load(open(f))
        except Exception:
            continue
        acc = (r.get("identity") or {}).get("accession_number")
        ler = r.get("ler_number")
        if acc and ler:
            m[acc] = ler
    return m


_WS = re.compile(r"\s+")


def chunk_words(text: str, size: int, overlap: int) -> list[str]:
    """Fixed-size overlapping word windows (deterministic, model-agnostic sizing)."""
    words = _WS.sub(" ", text).strip().split(" ")
    if not words or words == [""]:
        return []
    step = max(1, size - overlap)
    out = []
    for i in range(0, len(words), step):
        piece = words[i:i + size]
        if piece:
            out.append(" ".join(piece))
        if i + size >= len(words):
            break
    return out


@dataclass
class Chunk:
    ler: str
    text: str


def build_chunks(config: str) -> list[Chunk]:
    cfg = CHUNK_CONFIGS[config]
    acc2ler = accession_to_ler()
    chunks: list[Chunk] = []
    for acc, ler in sorted(acc2ler.items()):
        p = RAW_DIR / f"{acc}.txt"
        if not p.exists():
            continue
        for piece in chunk_words(p.read_text(errors="ignore"), cfg["words"], cfg["overlap"]):
            chunks.append(Chunk(ler=ler, text=piece))
    return chunks


# --------------------------------------------------------------------------- #
# the index: embed once (cached), answer cosine top-k by numpy brute force
# --------------------------------------------------------------------------- #
class VectorIndex:
    def __init__(self, model: str = DEFAULT_MODEL, config: str = DEFAULT_CONFIG,
                 rebuild: bool = False, verbose: bool = False):
        if model not in MODELS:
            raise ValueError(f"unknown model {model!r} (have {list(MODELS)})")
        if config not in CHUNK_CONFIGS:
            raise ValueError(f"unknown config {config!r} (have {list(CHUNK_CONFIGS)})")
        self.model_key = model
        self.config = config
        self.spec = MODELS[model]
        self.verbose = verbose
        self._encoder = None
        self.chunks: list[Chunk] = []
        self.emb: np.ndarray | None = None
        self._load_or_build(rebuild)

    @property
    def cache(self) -> Path:
        return CACHE_DIR / self.model_key / self.config

    @staticmethod
    def _device() -> str:
        import torch
        if torch.backends.mps.is_available():   # Apple-silicon GPU
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
        return "cpu"

    def _encoder_(self):
        if self._encoder is None:
            from sentence_transformers import SentenceTransformer
            dev = self._device()
            if self.verbose:
                print(f"  loading encoder {self.spec['hf']} on {dev} ...", flush=True)
            self._encoder = SentenceTransformer(self.spec["hf"], device=dev)
        return self._encoder

    def _embed(self, texts, is_query=False) -> np.ndarray:
        enc = self._encoder_()
        if is_query and self.spec["query_prefix"]:
            texts = [self.spec["query_prefix"] + t for t in texts]
        vecs = enc.encode(texts, normalize_embeddings=True, batch_size=128,
                          show_progress_bar=self.verbose)
        return np.asarray(vecs, dtype=np.float32)

    def _load_or_build(self, rebuild: bool) -> None:
        embp, metap = self.cache / "emb.npy", self.cache / "chunks.jsonl"
        if not rebuild and embp.exists() and metap.exists():
            self.emb = np.load(embp)
            self.chunks = [Chunk(**json.loads(l))
                           for l in metap.read_text().splitlines() if l.strip()]
            return
        if self.verbose:
            print(f"  building index [{self.model_key}/{self.config}] ...", flush=True)
        self.chunks = build_chunks(self.config)
        self.emb = self._embed([c.text for c in self.chunks], is_query=False)
        self.cache.mkdir(parents=True, exist_ok=True)
        np.save(embp, self.emb)
        with metap.open("w") as f:
            for c in self.chunks:
                f.write(json.dumps({"ler": c.ler, "text": c.text}) + "\n")
        if self.verbose:
            print(f"  cached {len(self.chunks)} chunks -> {self.cache}", flush=True)

    def search(self, question: str, k: int = DEFAULT_K) -> list[dict]:
        """Top-k chunks by cosine (normalized dot product). Exact, brute-force."""
        q = self._embed([question], is_query=True)[0]
        sims = self.emb @ q
        n = len(sims)
        k = min(k, n)
        if k <= 0:
            return []
        idx = np.argpartition(-sims, k - 1)[:k]
        idx = idx[np.argsort(-sims[idx])]
        return [{"ler": self.chunks[i].ler, "text": self.chunks[i].text,
                 "score": float(sims[i])} for i in idx]

    def ranked_lers(self, question: str, k_chunks: int = 200) -> list[tuple[str, float]]:
        """Distinct LERs ranked by their best chunk score — the input to recall@k
        curves (how deep vector must retrieve to cover an expected LER set)."""
        hits = self.search(question, k=min(k_chunks, len(self.chunks)))
        best: dict[str, float] = {}
        for h in hits:
            if h["ler"] not in best or h["score"] > best[h["ler"]]:
                best[h["ler"]] = h["score"]
        return sorted(best.items(), key=lambda kv: -kv[1])


# --------------------------------------------------------------------------- #
# the retriever seam — same contract as GraphRetriever.retrieve()
# --------------------------------------------------------------------------- #
class VectorRetriever:
    def __init__(self, model: str = DEFAULT_MODEL, config: str = DEFAULT_CONFIG,
                 k: int = DEFAULT_K, threshold: float = DEFAULT_THRESHOLD,
                 index: VectorIndex | None = None):
        self.index = index or VectorIndex(model, config)
        self.k = k
        self.threshold = threshold

    def retrieve(self, question: str) -> Evidence:
        hits = self.index.search(question, self.k)
        anchors = {"model": self.index.model_key, "config": self.index.config,
                   "k": self.k, "top_score": (hits[0]["score"] if hits else None)}
        # refusal gate: if nothing is similar enough, decline (the vector analog of the
        # graph's structural refusal — but a tuned threshold, swept as a curve in compare.py).
        if not hits or hits[0]["score"] < self.threshold:
            return Evidence("vector", anchors,
                            "(no sufficiently similar passages found in the corpus)",
                            empty=True)
        order: list[str] = []
        by_ler: dict[str, list[dict]] = {}
        for h in hits:
            by_ler.setdefault(h["ler"], []).append(h)
            if h["ler"] not in order:
                order.append(h["ler"])
        blocks = []
        for ler in order:
            joined = "\n    [...]\n    ".join(h["text"] for h in by_ler[ler])
            blocks.append(f"[LER {ler}] (top similarity {by_ler[ler][0]['score']:.2f})\n    {joined}")
        text = ("Retrieved passages from the source LER reports "
                "(top-k by semantic similarity):\n\n" + "\n\n".join(blocks))
        return Evidence("vector", anchors, text, node_keys=[],
                        lers=[{"key": l, "source": "vector"} for l in order])


# --------------------------------------------------------------------------- #
# CLI: build/cache the indexes, or test a single query
# --------------------------------------------------------------------------- #
def build_all(models=None, configs=None, rebuild=False) -> None:
    models = models or list(MODELS)
    configs = configs or list(CHUNK_CONFIGS)
    for m in models:
        for c in configs:
            idx = VectorIndex(m, c, rebuild=rebuild, verbose=True)
            print(f"[{m}/{c}] {len(idx.chunks)} chunks, dim={idx.emb.shape[1]}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Vector-RAG baseline: build the index or query it.")
    p.add_argument("--build", action="store_true", help="build/cache embeddings")
    p.add_argument("--all", action="store_true", help="build every model x config")
    p.add_argument("--rebuild", action="store_true", help="ignore cache and re-embed")
    p.add_argument("--model", default=DEFAULT_MODEL, choices=list(MODELS))
    p.add_argument("--config", default=DEFAULT_CONFIG, choices=list(CHUNK_CONFIGS))
    p.add_argument("--k", type=int, default=DEFAULT_K)
    p.add_argument("--query", help="a question to retrieve for (prints top-k chunks)")
    args = p.parse_args(argv)

    if args.all:
        build_all(rebuild=args.rebuild)
        return 0
    if args.build:
        VectorIndex(args.model, args.config, rebuild=args.rebuild, verbose=True)
        return 0
    if args.query:
        r = VectorRetriever(args.model, args.config, k=args.k)
        ev = r.retrieve(args.query)
        print(f"[{args.model}/{args.config}] k={args.k}  top_score={ev.anchors.get('top_score')}")
        print("retrieved LERs:", ", ".join(ev.ler_keys()) or "(none)")
        print("\n" + ev.text[:2000])
        return 0
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
