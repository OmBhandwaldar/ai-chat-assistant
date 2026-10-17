"""Hybrid retrieval over maintenance report chunks: BM25 + dense + RRF fusion.

Facts needed to judge an SOP breach sometimes live only in a repair
report's free text (e.g. "contained about 7 hours after discovery" — SR-02
has no structured column for this). search_reports() finds the right
chunk by combining two independent retrievers so neither's blind spot
loses a result: DuckDB FTS (BM25, exact wording) and ChromaDB (dense,
paraphrase-tolerant), fused by reciprocal rank fusion (Q9, Q10, Q13).

build_report_index() populates `report_chunks` (DuckDB, for FTS + text)
and the `reports` ChromaDB collection (dense vectors) once; subsequent
calls are a no-op unless force=True or the two stores drift out of sync.

See PLAN.md Phase 3.2, TECH_STACK.md section on hybrid retrieval.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

from backend.models.schemas import Evidence, ToolResult

_CHROMA_PATH = Path(__file__).resolve().parents[1] / "chroma_db"
_COLLECTION_NAME = "reports"
_EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"
_WINDOW_TOKENS = 400
_OVERLAP_TOKENS = 50
_RRF_K = 60

_embedder: Any = None
_chroma_clients: dict[str, Any] = {}


def _get_embedder() -> Any:
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer

        _embedder = SentenceTransformer(_EMBED_MODEL_NAME)
    return _embedder


def _get_chroma_collection(chroma_path: Path | str | None = None) -> Any:
    import chromadb

    path = str(chroma_path or _CHROMA_PATH)
    if path not in _chroma_clients:
        _chroma_clients[path] = chromadb.PersistentClient(path=path)
    return _chroma_clients[path].get_or_create_collection(_COLLECTION_NAME)


def _chunk_text(text: str, window: int = _WINDOW_TOKENS, overlap: int = _OVERLAP_TOKENS) -> list[str]:
    """Split text into ~window-token windows with overlap-token overlap,
    using the embedding model's own tokenizer so chunk boundaries line up
    with what gets embedded."""
    tokenizer = _get_embedder().tokenizer
    ids = tokenizer.encode(text, add_special_tokens=False)
    if not ids:
        return []
    step = window - overlap
    chunks: list[str] = []
    i = 0
    while True:
        window_ids = ids[i : i + window]
        chunks.append(tokenizer.decode(window_ids))
        if i + window >= len(ids):
            break
        i += step
    return chunks


def _ensure_fts_index(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("INSTALL fts")
    conn.execute("LOAD fts")
    conn.execute("PRAGMA create_fts_index('report_chunks', 'chunk_id', 'text', overwrite=1)")


def build_report_index(
    conn: duckdb.DuckDBPyConnection,
    chroma_path: Path | str | None = None,
    force: bool = False,
) -> int:
    """Chunk every maintenance report, embed the chunks, and index them in
    both report_chunks (DuckDB, FTS) and the `reports` ChromaDB collection.

    Safe to call on every request: a no-op once both stores already hold
    the same number of chunks, unless force=True.
    """
    collection = _get_chroma_collection(chroma_path)
    chunk_count = conn.execute("SELECT count(*) FROM report_chunks").fetchone()[0]
    vector_count = collection.count()

    if chunk_count > 0 and chunk_count == vector_count and not force:
        _ensure_fts_index(conn)
        return chunk_count

    conn.execute("DELETE FROM report_chunks")
    existing_ids = collection.get()["ids"]
    if existing_ids:
        collection.delete(ids=existing_ids)

    reports = conn.execute(
        "SELECT report_path, asset_id, maintenance_id, area_id, started_at, body FROM maintenance_reports"
    ).fetchall()

    chunk_rows: list[tuple] = []
    chunk_ids: list[str] = []
    texts: list[str] = []
    metadatas: list[dict] = []
    for report_path, asset_id, maintenance_id, area_id, started_at, body in reports:
        for idx, piece in enumerate(_chunk_text(body)):
            chunk_id = f"{maintenance_id}#{idx}"
            chunk_rows.append((chunk_id, report_path, asset_id, maintenance_id, area_id, started_at, idx, piece))
            chunk_ids.append(chunk_id)
            texts.append(piece)
            metadatas.append({
                "asset_id": asset_id,
                "area_id": area_id or "",
                "maintenance_id": maintenance_id,
                "started_at": int(started_at.timestamp()) if started_at else 0,
            })

    if chunk_rows:
        conn.executemany(
            """
            INSERT INTO report_chunks
                (chunk_id, report_path, asset_id, maintenance_id, area_id, started_at, chunk_index, text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            chunk_rows,
        )

        embedder = _get_embedder()
        embeddings = embedder.encode(
            texts, normalize_embeddings=True, batch_size=64, show_progress_bar=False
        ).tolist()

        batch_size = 500
        for i in range(0, len(chunk_ids), batch_size):
            collection.add(
                ids=chunk_ids[i : i + batch_size],
                embeddings=embeddings[i : i + batch_size],
                documents=texts[i : i + batch_size],
                metadatas=metadatas[i : i + batch_size],
            )

    _ensure_fts_index(conn)
    return len(chunk_rows)


def _dedupe_by_report(chunk_ids: list[str]) -> list[str]:
    """Collapse a chunk-ranked list to one entry per report (maintenance_id),
    keeping each report's best (first) rank. Without this, a report with
    several relevant chunks crowds out an equally relevant report that only
    has one, since both retrievers rank chunks, not reports."""
    seen: set[str] = set()
    out: list[str] = []
    for chunk_id in chunk_ids:
        maintenance_id = chunk_id.split("#", 1)[0]
        if maintenance_id not in seen:
            seen.add(maintenance_id)
            out.append(maintenance_id)
    return out


def _rrf_fuse(rank_lists: list[list[str]], k_const: int = _RRF_K) -> list[str]:
    """Reciprocal rank fusion: combine several report-ranked ID lists into
    one, without needing the two retrievers' raw scores to be comparable."""
    scores: dict[str, float] = {}
    for ranks in rank_lists:
        for i, doc_id in enumerate(ranks):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k_const + i + 1)
    return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda kv: -kv[1])]


def _chroma_where(
    well_id: str | None, area_id: str | None, start: date | None, end: date | None
) -> dict | None:
    clauses: list[dict] = []
    if well_id is not None:
        clauses.append({"asset_id": well_id})
    if area_id is not None:
        clauses.append({"area_id": area_id})
    if start is not None:
        clauses.append({"started_at": {"$gte": int(datetime.combine(start, datetime.min.time()).timestamp())}})
    if end is not None:
        clauses.append({"started_at": {"$lte": int(datetime.combine(end, datetime.max.time()).timestamp())}})
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def search_reports(
    conn: duckdb.DuckDBPyConnection,
    query: str,
    well_id: str | None = None,
    area_id: str | None = None,
    start: date | None = None,
    end: date | None = None,
    k: int = 5,
    chroma_path: Path | str | None = None,
) -> ToolResult[dict]:
    """Find the report chunks most relevant to `query`, scoped by well
    and/or area and/or date range (Q9, Q10, Q13)."""
    build_report_index(conn, chroma_path=chroma_path)

    # --- Dense: ChromaDB top-20 ---
    collection = _get_chroma_collection(chroma_path)
    where = _chroma_where(well_id, area_id, start, end)
    query_embedding = _get_embedder().encode([query], normalize_embeddings=True)[0].tolist()
    dense_result = collection.query(query_embeddings=[query_embedding], n_results=20, where=where)
    dense_ids: list[str] = dense_result["ids"][0] if dense_result["ids"] else []

    # --- Sparse: DuckDB FTS (BM25) top-20 ---
    conditions = ["score IS NOT NULL"]
    filter_params: list = []
    if well_id is not None:
        conditions.append("asset_id = ?")
        filter_params.append(well_id)
    if area_id is not None:
        conditions.append("area_id = ?")
        filter_params.append(area_id)
    if start is not None:
        conditions.append("started_at >= ?")
        filter_params.append(start)
    if end is not None:
        conditions.append("started_at <= ?")
        filter_params.append(end)
    where_sql = " AND ".join(conditions)

    sparse_rows = conn.execute(
        f"""
        SELECT chunk_id
        FROM (
            SELECT chunk_id, asset_id, area_id, started_at,
                   fts_main_report_chunks.match_bm25(chunk_id, ?) AS score
            FROM report_chunks
        )
        WHERE {where_sql}
        ORDER BY score DESC
        LIMIT 20
        """,  # noqa: S608 (conditions built from a fixed allow-list of columns above)
        [query, *filter_params],
    ).fetchall()
    sparse_ids = [r[0] for r in sparse_rows]

    # Rank by report, not by chunk — otherwise a report with two relevant
    # chunks crowds out an equally relevant report that only has one.
    fused_ids = _rrf_fuse([_dedupe_by_report(dense_ids), _dedupe_by_report(sparse_ids)])[:k]

    # Each report's best chunk: whichever of its chunks ranked highest in
    # either retriever's raw (non-deduped) list.
    best_chunk_for_report: dict[str, str] = {}
    for chunk_id in [*dense_ids, *sparse_ids]:
        maintenance_id = chunk_id.split("#", 1)[0]
        best_chunk_for_report.setdefault(maintenance_id, chunk_id)

    results: list[dict] = []
    evidence: list[Evidence] = []
    if fused_ids:
        wanted_chunk_ids = [best_chunk_for_report[mid] for mid in fused_ids if mid in best_chunk_for_report]
        detail_rows = conn.execute(
            "SELECT chunk_id, text, maintenance_id, asset_id, started_at FROM report_chunks WHERE chunk_id = ANY(?)",
            [wanted_chunk_ids],
        ).fetchall()
        detail_by_chunk_id = {r[0]: r for r in detail_rows}

        for maintenance_id in fused_ids:
            chunk_id = best_chunk_for_report.get(maintenance_id)
            row = detail_by_chunk_id.get(chunk_id) if chunk_id else None
            if row is None:
                continue
            _, text, m_id, asset_id, started_at = row
            results.append({
                "chunk_id": chunk_id,
                "text": text,
                "maintenance_id": m_id,
                "asset_id": asset_id,
                "started_at": started_at.isoformat() if started_at else None,
            })
            evidence.append(Evidence(source_type="maintenance", source_id=m_id))

    return ToolResult[dict](data={"query": query, "results": results}, evidence=evidence)
