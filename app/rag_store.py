import hashlib
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterator

from .paths import HOT_DB_PATH, KNOWLEDGE_DB_PATH, RAG_DB_PATH, RAG_MODELS_DIR
from .settings import get_settings


os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "20")


DEFAULT_MODEL = "Qwen/Qwen3-Embedding-0.6B"
DEFAULT_DIMENSION = 512
DEFAULT_RERANKER_MODEL = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
MAX_RERANK_CANDIDATES = 20
QUERY_INSTRUCTION = (
    "Instruct: Given a Chinese QQ chat query, retrieve relevant passages that answer the query\n"
    "Query: "
)

_MODEL_LOCK = threading.Lock()
_ENCODE_LOCK = threading.Lock()
_MODEL: Any = None
_MODEL_KEY: tuple[str, str] | None = None
_RERANKER_LOCK = threading.Lock()
_RERANKER: Any = None
_RERANKER_KEY: tuple[str, str] | None = None
_INDEX_LOCK = threading.Lock()
_TASK_LOCK = threading.Lock()
_CANCEL_EVENT = threading.Event()
_TASK: dict[str, Any] = {
    "running": False,
    "status": "idle",
    "progress": 0,
    "processed": 0,
    "total": 0,
    "indexed": 0,
    "skipped": 0,
    "output": [],
}


class RAGUnavailable(RuntimeError):
    pass


class RAGIndexCancelled(RuntimeError):
    pass


def init_rag_db(dimension: int | None = None) -> None:
    dimension = dimension or rag_dimension()
    RAG_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect(load_vector=True) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rag_documents (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source_type TEXT NOT NULL,
              source_id INTEGER NOT NULL,
              event_date TEXT NOT NULL DEFAULT '',
              title TEXT NOT NULL,
              summary TEXT NOT NULL DEFAULT '',
              category TEXT NOT NULL DEFAULT '',
              source TEXT NOT NULL DEFAULT '',
              rank INTEGER NOT NULL DEFAULT 0,
              heat TEXT NOT NULL DEFAULT '',
              source_url TEXT NOT NULL DEFAULT '',
              fetched_at TEXT NOT NULL DEFAULT '',
              content_hash TEXT NOT NULL,
              embedding_model TEXT NOT NULL,
              embedding_dimension INTEGER NOT NULL,
              vectorized INTEGER NOT NULL DEFAULT 1,
              indexed_at TEXT NOT NULL,
              UNIQUE(source_type, source_id)
            );

            CREATE INDEX IF NOT EXISTS idx_rag_documents_source
            ON rag_documents (source_type, source_id);

            CREATE INDEX IF NOT EXISTS idx_rag_documents_date
            ON rag_documents (event_date);

            CREATE TABLE IF NOT EXISTS rag_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            """
        )
        _ensure_fts_table(conn)
        _ensure_document_columns(conn)
        for source_type in ("knowledge", "hot"):
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS rag_vectors_{source_type} "
                f"USING vec0(embedding float[{dimension}] distance_metric=cosine)"
            )


def rag_stats() -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(RAG_DB_PATH),
        "exists": RAG_DB_PATH.exists(),
        "ready": False,
        "document_count": 0,
        "vector_count": 0,
        "knowledge_count": 0,
        "hot_count": 0,
        "model": "",
        "dimension": 0,
        "latest_indexed_at": None,
        "error": "",
        "task": rag_task_snapshot(),
    }
    try:
        with _connect(load_vector=True) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(vectorized = 1) AS vector_count,
                       SUM(source_type = 'knowledge') AS knowledge_count,
                       SUM(source_type = 'hot') AS hot_count,
                       MAX(indexed_at) AS latest_indexed_at
                FROM rag_documents
                """
            ).fetchone()
            meta = dict(conn.execute("SELECT key, value FROM rag_meta").fetchall())
        result.update(
            {
                "document_count": int(row["total"] or 0),
                "vector_count": int(row["vector_count"] or 0),
                "knowledge_count": int(row["knowledge_count"] or 0),
                "hot_count": int(row["hot_count"] or 0),
                "latest_indexed_at": row["latest_indexed_at"],
                "model": meta.get("embedding_model", ""),
                "dimension": int(meta.get("embedding_dimension", "0") or 0),
            }
        )
        build_state = meta.get("build_state", "")
        result["build_state"] = build_state
        result["ready"] = (
            result["document_count"] > 0
            and result["model"] == rag_model_name()
            and result["dimension"] == rag_dimension()
            and build_state == "ready"
        )
        if build_state == "running":
            result["error"] = "RAG 索引正在构建中。"
        elif build_state == "cancelled":
            result["error"] = "上一次 RAG 索引任务已停止，需要重新构建。"
        elif result["document_count"] and not result["ready"]:
            result["error"] = "Embedding 模型或维度已变化，需要完整重建索引。"
    except Exception as exc:
        result["error"] = str(exc)[:300]
    return result


def search_rag(query: str, source_type: str, limit: int = 5) -> list[dict[str, Any]] | None:
    settings = get_settings()
    if settings.get("rag.enabled", "1") != "1":
        return None
    stats = rag_stats()
    if not stats.get("ready"):
        return None

    query = str(query or "").strip()
    if not query:
        return []
    limit = min(20, max(1, int(limit)))
    candidate_limit = max(30, limit * 8)
    try:
        vector = _encode([QUERY_INSTRUCTION + query], settings)[0]
        import sqlite_vec

        with _connect(load_vector=True) as conn:
            vector_table = _vector_table(source_type)
            vector_rows = conn.execute(
                f"""
                SELECT d.*, v.distance
                FROM {vector_table} v
                JOIN rag_documents d ON d.id = v.rowid
                WHERE v.embedding MATCH ? AND k = ?
                ORDER BY v.distance
                """,
                (sqlite_vec.serialize_float32(vector), candidate_limit),
            ).fetchall()
            keyword_scores = _fts_candidates(conn, query, source_type, candidate_limit)
    except Exception:
        return None

    candidates: dict[int, dict[str, Any]] = {}
    for row in vector_rows:
        item = dict(row)
        distance = float(item.pop("distance", 2.0) or 2.0)
        item["_vector_score"] = max(0.0, 1.0 - distance)
        item["_keyword_score"] = keyword_scores.get(int(item["id"]), 0.0)
        candidates[int(item["id"])] = item

    missing_ids = [item_id for item_id in keyword_scores if item_id not in candidates]
    if missing_ids:
        with _connect(load_vector=False) as conn:
            placeholders = ",".join("?" for _ in missing_ids)
            rows = conn.execute(
                f"SELECT * FROM rag_documents WHERE id IN ({placeholders}) AND source_type = ?",
                [*missing_ids, source_type],
            ).fetchall()
        for row in rows:
            item = dict(row)
            item["_vector_score"] = 0.0
            item["_keyword_score"] = keyword_scores.get(int(item["id"]), 0.0)
            candidates[int(item["id"])] = item

    years = _query_years(query)
    min_similarity = _parse_float(settings.get("rag.min_similarity"), 0.30)
    ranked = []
    for item in candidates.values():
        if years and not _matches_year(item, years):
            continue
        if float(item.get("_vector_score") or 0.0) < min_similarity and float(item.get("_keyword_score") or 0.0) <= 0:
            continue
        score = _rerank_score(query, item, source_type)
        if score <= 0:
            continue
        item["_rag_score"] = round(score, 4)
        ranked.append(item)
    ranked.sort(key=lambda item: (item["_rag_score"], item.get("event_date") or ""), reverse=True)
    if reranker_enabled(settings):
        rerank_count = reranker_top_k(settings)
        reranked = _apply_cross_encoder_rerank(query, ranked[:rerank_count], settings)
        if reranked is not None:
            ranked = reranked + ranked[rerank_count:]
    return [_public_item(item) for item in ranked[:limit]]


def start_rag_index(full_rebuild: bool = False) -> dict[str, Any]:
    with _TASK_LOCK:
        if _TASK.get("running"):
            return rag_task_snapshot_locked()
        _CANCEL_EVENT.clear()
        _TASK.clear()
        _TASK.update(
            {
                "running": True,
                "status": "loading_model",
                "progress": 0,
                "processed": 0,
                "total": 0,
                "indexed": 0,
                "skipped": 0,
                "full_rebuild": bool(full_rebuild),
                "started_at": _now_text(),
                "finished_at": None,
                "eta_seconds": None,
                "output": ["正在加载 Embedding 模型，首次运行需要从 Hugging Face 下载模型。"],
                "error": "",
            }
        )
    threading.Thread(target=_run_index_task, args=(full_rebuild,), daemon=True).start()
    return rag_task_snapshot()


def stop_rag_index() -> dict[str, Any]:
    with _TASK_LOCK:
        if not _TASK.get("running"):
            return rag_task_snapshot_locked()
        _CANCEL_EVENT.set()
        _TASK["status"] = "stopping"
        _append_task_output_locked("正在在当前批次结束后停止 RAG 索引任务。")
        return rag_task_snapshot_locked()


def rag_task_snapshot() -> dict[str, Any]:
    with _TASK_LOCK:
        return rag_task_snapshot_locked()


def rag_task_snapshot_locked() -> dict[str, Any]:
    result = dict(_TASK)
    result["output"] = list(_TASK.get("output") or [])
    return result


def build_rag_index(full_rebuild: bool, progress: Callable[[int, int, int, int, int, str], None]) -> None:
    settings = get_settings()
    dimension = rag_dimension(settings)
    model_name = rag_model_name(settings)
    _encode([QUERY_INSTRUCTION + "测试向量模型"], settings)

    with _INDEX_LOCK:
        init_rag_db(dimension)
        with _connect(load_vector=True) as conn:
            current = dict(conn.execute("SELECT key, value FROM rag_meta").fetchall())
            schema_changed = (
                current.get("embedding_model") not in {None, "", model_name}
                or int(current.get("embedding_dimension", dimension) or dimension) != dimension
            )
            if full_rebuild or schema_changed:
                _reset_index(conn, dimension)
            _set_meta(conn, "embedding_model", model_name)
            _set_meta(conn, "embedding_dimension", str(dimension))
            _set_meta(conn, "build_state", "running")

        hot_cutoff = (date.today() - timedelta(days=rag_hot_vector_days(settings))).isoformat()
        knowledge_total = _source_count(KNOWLEDGE_DB_PATH, "knowledge_items")
        hot_vector_total = _source_count(HOT_DB_PATH, "hot_items", "event_date >= ?", (hot_cutoff,))
        hot_catalog_total = _source_count(HOT_DB_PATH, "hot_items")
        total = knowledge_total + hot_vector_total
        processed = indexed = skipped = 0
        started = time.monotonic()
        for rows in _source_batches(KNOWLEDGE_DB_PATH, "knowledge_items"):
            _raise_if_cancelled()
            changed, vector_items = _changed_rows("knowledge", rows, model_name, dimension, lambda _: True)
            vectors = _encode([_document_text(item) for item in vector_items], settings) if vector_items else []
            _save_batch("knowledge", changed, vector_items, vectors, model_name, dimension)
            processed += len(rows)
            indexed += len(vector_items)
            skipped += len(rows) - len(vector_items)
            elapsed = max(0.1, time.monotonic() - started)
            eta = int((elapsed / processed) * max(0, total - processed)) if processed else 0
            progress(processed, total, indexed, skipped, eta, f"知识库向量：{processed}/{total}，新建或更新 {indexed}，跳过 {skipped}")

        cataloged = 0
        for rows in _source_batches(HOT_DB_PATH, "hot_items"):
            _raise_if_cancelled()
            hot_vector_count = sum(
                str(item.get("event_date") or "") >= hot_cutoff
                for item in rows
            )
            changed, vector_items = _changed_rows(
                "hot",
                rows,
                model_name,
                dimension,
                lambda item: str(item.get("event_date") or "") >= hot_cutoff,
            )
            vectors = _encode([_document_text(item) for item in vector_items], settings) if vector_items else []
            _save_batch("hot", changed, vector_items, vectors, model_name, dimension)
            cataloged += len(rows)
            processed += hot_vector_count
            indexed += len(vector_items)
            skipped += hot_vector_count - len(vector_items)
            elapsed = max(0.1, time.monotonic() - started)
            eta = int((elapsed / processed) * max(0, total - processed)) if processed else 0
            progress(
                processed,
                total,
                indexed,
                skipped,
                eta,
                f"热点关键词目录：{cataloged}/{hot_catalog_total}；近 {rag_hot_vector_days(settings)} 天向量：{processed}/{total}",
            )

        with _connect(load_vector=False) as conn:
            _set_meta(conn, "embedding_model", model_name)
            _set_meta(conn, "embedding_dimension", str(dimension))
            _set_meta(conn, "last_build_at", _now_text())
            _set_meta(conn, "build_state", "ready")


def rag_model_name(settings: dict[str, str] | None = None) -> str:
    settings = settings or get_settings()
    return settings.get("rag.embedding_model", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def rag_dimension(settings: dict[str, str] | None = None) -> int:
    settings = settings or get_settings()
    try:
        value = int(settings.get("rag.embedding_dimension", str(DEFAULT_DIMENSION)))
    except (TypeError, ValueError):
        value = DEFAULT_DIMENSION
    return value if value in {256, 512, 1024} else DEFAULT_DIMENSION


def rag_hot_vector_days(settings: dict[str, str] | None = None) -> int:
    settings = settings or get_settings()
    try:
        value = int(settings.get("rag.hot_vector_days", "30"))
    except (TypeError, ValueError):
        value = 30
    return min(180, max(7, value))


def reranker_enabled(settings: dict[str, str] | None = None) -> bool:
    settings = settings or get_settings()
    return settings.get("rag.reranker_enabled", "1").strip() == "1"


def reranker_model_name(settings: dict[str, str] | None = None) -> str:
    settings = settings or get_settings()
    return settings.get("rag.reranker_model", DEFAULT_RERANKER_MODEL).strip() or DEFAULT_RERANKER_MODEL


def reranker_top_k(settings: dict[str, str] | None = None) -> int:
    settings = settings or get_settings()
    try:
        value = int(settings.get("rag.reranker_top_k", str(MAX_RERANK_CANDIDATES)))
    except (TypeError, ValueError):
        value = MAX_RERANK_CANDIDATES
    return min(MAX_RERANK_CANDIDATES, max(1, value))


def _run_index_task(full_rebuild: bool) -> None:
    from .db import log_event

    try:
        build_rag_index(full_rebuild, _update_task_progress)
        with _TASK_LOCK:
            _TASK.update(
                {
                    "running": False,
                    "status": "done",
                    "progress": 100,
                    "finished_at": _now_text(),
                    "eta_seconds": 0,
                }
            )
            _append_task_output_locked("RAG 索引构建完成。")
        log_event("info", "rag", "RAG index build finished", str(rag_stats())[:800])
    except RAGIndexCancelled:
        with _TASK_LOCK:
            _TASK.update(
                {
                    "running": False,
                    "status": "cancelled",
                    "finished_at": _now_text(),
                    "eta_seconds": None,
                }
            )
            _append_task_output_locked("RAG 索引任务已停止，现有索引不会被标记为可用。")
        with _connect(load_vector=False) as conn:
            _set_meta(conn, "build_state", "cancelled")
        log_event("info", "rag", "RAG index build cancelled")
    except Exception as exc:
        with _TASK_LOCK:
            _TASK.update(
                {
                    "running": False,
                    "status": "error",
                    "finished_at": _now_text(),
                    "error": str(exc)[:800],
                }
            )
            _append_task_output_locked(f"失败：{exc}")
        log_event("error", "rag", "RAG index build failed", repr(exc)[:800])


def _update_task_progress(processed: int, total: int, indexed: int, skipped: int, eta_seconds: int, message: str) -> None:
    with _TASK_LOCK:
        _TASK.update(
            {
                "status": "indexing",
                "processed": processed,
                "total": total,
                "indexed": indexed,
                "skipped": skipped,
                "progress": min(99, int(processed * 100 / max(1, total))),
                "eta_seconds": eta_seconds,
            }
        )
        _append_task_output_locked(message)


def _append_task_output_locked(message: str) -> None:
    output = _TASK.setdefault("output", [])
    output.append(message)
    _TASK["output"] = output[-80:]


@contextmanager
def _connect(load_vector: bool) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(RAG_DB_PATH, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        if load_vector:
            import sqlite_vec

            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_fts_table(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(title, summary, category, source, tokenize='trigram')"
        )
    except sqlite3.Error:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(title, summary, category, source, tokenize='unicode61')"
        )


def _reset_index(conn: sqlite3.Connection, dimension: int) -> None:
    conn.execute("DROP TABLE IF EXISTS rag_vectors")
    conn.execute("DROP TABLE IF EXISTS rag_vectors_knowledge")
    conn.execute("DROP TABLE IF EXISTS rag_vectors_hot")
    conn.execute("DELETE FROM rag_fts")
    conn.execute("DELETE FROM rag_documents")
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'rag_documents'")
    for source_type in ("knowledge", "hot"):
        conn.execute(
            f"CREATE VIRTUAL TABLE rag_vectors_{source_type} "
            f"USING vec0(embedding float[{dimension}] distance_metric=cosine)"
        )


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO rag_meta(key, value) VALUES (?, ?)", (key, value))


def _raise_if_cancelled() -> None:
    if _CANCEL_EVENT.is_set():
        raise RAGIndexCancelled()


def _source_count(path: Any, table: str, where: str = "", values: tuple[Any, ...] = ()) -> int:
    if not path.exists():
        return 0
    conn = sqlite3.connect(path)
    try:
        clause = f" WHERE {where}" if where else ""
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}{clause}", values).fetchone()[0])
    finally:
        conn.close()


def _source_batches(path: Any, table: str, batch_size: int = 128):
    if not path.exists():
        return
    last_id = 0
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        while True:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE id > ? ORDER BY id LIMIT ?",
                (last_id, batch_size),
            ).fetchall()
            if not rows:
                break
            yield [dict(row) for row in rows]
            last_id = int(rows[-1]["id"])
    finally:
        conn.close()


def _changed_rows(
    source_type: str,
    rows: list[dict[str, Any]],
    model: str,
    dimension: int,
    should_vectorize: Callable[[dict[str, Any]], bool],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not rows:
        return [], []
    ids = [int(row["id"]) for row in rows]
    with _connect(load_vector=False) as conn:
        placeholders = ",".join("?" for _ in ids)
        existing = conn.execute(
            f"SELECT source_id, content_hash, embedding_model, embedding_dimension, vectorized FROM rag_documents WHERE source_type = ? AND source_id IN ({placeholders})",
            [source_type, *ids],
        ).fetchall()
    known = {int(row["source_id"]): dict(row) for row in existing}
    changed: list[dict[str, Any]] = []
    vector_items: list[dict[str, Any]] = []
    for row in rows:
        row = _normalize_source_item(source_type, row)
        row["content_hash"] = _content_hash(row)
        old = known.get(int(row["id"]))
        vectorized = should_vectorize(row)
        content_changed = not old or old["content_hash"] != row["content_hash"]
        vector_changed = vectorized and (
            content_changed
            or old["embedding_model"] != model
            or int(old["embedding_dimension"]) != dimension
            or not int(old["vectorized"])
        )
        state_changed = bool(old) and bool(int(old["vectorized"])) != vectorized
        if content_changed or vector_changed or state_changed:
            changed.append(row)
        if vector_changed:
            vector_items.append(row)
    return changed, vector_items


def _normalize_source_item(source_type: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "event_date": str(row.get("event_date") or ""),
        "title": str(row.get("title") or "").strip(),
        "summary": str(row.get("summary") or "").strip(),
        "category": str(row.get("category") or "").strip(),
        "source": str(row.get("source") or ("知识库" if source_type == "knowledge" else "热点")).strip(),
        "source_url": str(row.get("source_url") or "").strip(),
        "fetched_at": str(row.get("fetched_at") or ""),
        "rank": int(row.get("rank") or 0),
        "heat": str(row.get("heat") or ""),
    }


def _content_hash(item: dict[str, Any]) -> str:
    text = "\n".join(str(item.get(key) or "") for key in ("event_date", "title", "summary", "category", "source", "source_url"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _document_text(item: dict[str, Any]) -> str:
    return (
        f"日期：{item['event_date']}\n"
        f"标题：{item['title']}\n"
        f"摘要：{item['summary']}\n"
        f"类别：{item['category']}\n"
        f"来源：{item['source']}"
    )


def _encode(texts: list[str], settings: dict[str, str]) -> list[list[float]]:
    if not texts:
        return []
    model = _load_model(settings)
    dimension = rag_dimension(settings)
    batch_size = max(1, min(64, int(settings.get("rag.embedding_batch_size", "16") or 16)))
    with _ENCODE_LOCK:
        vectors = model.encode(texts, batch_size=batch_size, normalize_embeddings=False, show_progress_bar=False)
    import numpy as np

    vectors = np.asarray(vectors, dtype=np.float32)[:, :dimension]
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors = vectors / np.maximum(norms, 1e-12)
    return vectors.tolist()


def _load_model(settings: dict[str, str]):
    global _MODEL, _MODEL_KEY
    model_name = rag_model_name(settings)
    device = settings.get("rag.embedding_device", "auto").strip().lower() or "auto"
    key = (model_name, device)
    with _MODEL_LOCK:
        if _MODEL is not None and _MODEL_KEY == key:
            return _MODEL
        try:
            from huggingface_hub import snapshot_download
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RAGUnavailable("缺少 sentence-transformers，请重新运行启动脚本安装依赖。") from exc
        RAG_MODELS_DIR.mkdir(parents=True, exist_ok=True)
        os.environ["HF_HUB_DISABLE_XET"] = "1"
        _append_model_status("使用普通 HTTP 下载模型，连接中断时会自动断点续传。")
        snapshot_path = snapshot_download(
            repo_id=model_name,
            cache_dir=str(RAG_MODELS_DIR),
            max_workers=1,
        )
        _append_model_status("Embedding 模型下载完成，正在加载模型。")
        kwargs: dict[str, Any] = {}
        if device != "auto":
            kwargs["device"] = device
        _MODEL = SentenceTransformer(snapshot_path, **kwargs)
        _MODEL_KEY = key
        return _MODEL


def _apply_cross_encoder_rerank(
    query: str,
    items: list[dict[str, Any]],
    settings: dict[str, str],
) -> list[dict[str, Any]] | None:
    if not items:
        return []
    try:
        model = _load_reranker(settings)
        pairs = [(query, _document_text(item)) for item in items]
        scores = model.predict(
            pairs,
            batch_size=max(1, min(20, int(settings.get("rag.reranker_batch_size", "4") or 4))),
            show_progress_bar=False,
        )
    except Exception:
        return None

    for item, raw_score in zip(items, scores):
        cross_score = _normalize_cross_encoder_score(float(raw_score))
        item["_cross_encoder_score"] = cross_score
        item["_rag_score"] = round(float(item["_rag_score"]) * 0.35 + cross_score * 0.65, 4)
    items.sort(key=lambda item: (item["_rag_score"], item.get("event_date") or ""), reverse=True)
    return items


def _load_reranker(settings: dict[str, str]):
    global _RERANKER, _RERANKER_KEY
    model_name = reranker_model_name(settings)
    device = settings.get("rag.embedding_device", "auto").strip().lower() or "auto"
    key = (model_name, device)
    with _RERANKER_LOCK:
        if _RERANKER is not None and _RERANKER_KEY == key:
            return _RERANKER
        try:
            from huggingface_hub import snapshot_download
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            raise RAGUnavailable("missing sentence-transformers") from exc
        RAG_MODELS_DIR.mkdir(parents=True, exist_ok=True)
        snapshot_path = snapshot_download(
            repo_id=model_name,
            cache_dir=str(RAG_MODELS_DIR),
            max_workers=1,
        )
        kwargs: dict[str, Any] = {"max_length": 512, "trust_remote_code": True}
        if device != "auto":
            kwargs["device"] = device
        _RERANKER = CrossEncoder(snapshot_path, **kwargs)
        _RERANKER_KEY = key
        return _RERANKER


def _normalize_cross_encoder_score(value: float) -> float:
    if 0.0 <= value <= 1.0:
        return value
    return 1.0 / (1.0 + pow(2.718281828459045, -value))


def _append_model_status(message: str) -> None:
    with _TASK_LOCK:
        if _TASK.get("running"):
            _append_task_output_locked(message)


def _save_batch(
    source_type: str,
    items: list[dict[str, Any]],
    vector_items: list[dict[str, Any]],
    vectors: list[list[float]],
    model: str,
    dimension: int,
) -> None:
    if not items:
        return
    import sqlite_vec

    vectors_by_id = {int(item["id"]): vector for item, vector in zip(vector_items, vectors)}
    with _connect(load_vector=True) as conn:
        vector_table = _vector_table(source_type)
        for item in items:
            vector = vectors_by_id.get(int(item["id"]))
            vectorized = vector is not None
            old = conn.execute(
                "SELECT id FROM rag_documents WHERE source_type = ? AND source_id = ?",
                (source_type, item["id"]),
            ).fetchone()
            if old:
                doc_id = int(old["id"])
                conn.execute(f"DELETE FROM {vector_table} WHERE rowid = ?", (doc_id,))
                conn.execute("DELETE FROM rag_fts WHERE rowid = ?", (doc_id,))
                conn.execute(
                    """
                    UPDATE rag_documents SET event_date=?, title=?, summary=?, category=?, source=?, rank=?, heat=?, source_url=?, fetched_at=?,
                      content_hash=?, embedding_model=?, embedding_dimension=?, vectorized=?, indexed_at=? WHERE id=?
                    """,
                    (
                        item["event_date"], item["title"], item["summary"], item["category"], item["source"], item["rank"], item["heat"],
                        item["source_url"], item["fetched_at"], item["content_hash"], model, dimension, int(vectorized), _now_text(), doc_id,
                    ),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO rag_documents
                      (source_type, source_id, event_date, title, summary, category, source, rank, heat, source_url, fetched_at,
                       content_hash, embedding_model, embedding_dimension, vectorized, indexed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_type, item["id"], item["event_date"], item["title"], item["summary"], item["category"],
                        item["source"], item["rank"], item["heat"], item["source_url"], item["fetched_at"], item["content_hash"], model, dimension, int(vectorized), _now_text(),
                    ),
                )
                doc_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO rag_fts(rowid, title, summary, category, source) VALUES (?, ?, ?, ?, ?)",
                (doc_id, item["title"], item["summary"], item["category"], item["source"]),
            )
            if vectorized:
                conn.execute(
                    f"INSERT INTO {vector_table}(rowid, embedding) VALUES (?, ?)",
                    (doc_id, sqlite_vec.serialize_float32(vector)),
                )


def _fts_candidates(conn: sqlite3.Connection, query: str, source_type: str, limit: int) -> dict[int, float]:
    terms = _fts_terms(query)
    if not terms:
        return {}
    expression = " OR ".join(f'"{term}"' for term in terms)
    try:
        rows = conn.execute(
            """
            SELECT d.id, bm25(rag_fts) AS rank
            FROM rag_fts
            JOIN rag_documents d ON d.id = rag_fts.rowid
            WHERE rag_fts MATCH ? AND d.source_type = ?
            ORDER BY rank
            LIMIT ?
            """,
            (expression, source_type, limit),
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {int(row["id"]): 1.0 / (1.0 + abs(float(row["rank"] or 0.0))) for row in rows}


def _fts_terms(query: str) -> list[str]:
    values = re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,20}|(?:19|20)\d{2}|[\u4e00-\u9fff]{3,8}", query)
    ignored = {"什么", "怎么", "最近有啥", "有什么", "怎么样", "知识库", "告诉我"}
    return list(dict.fromkeys(value for value in values if value not in ignored))[:8]


def _rerank_score(query: str, item: dict[str, Any], source_type: str) -> float:
    query_lower = query.lower()
    title = str(item.get("title") or "")
    content_lower = f"{title} {item.get('summary', '')} {item.get('category', '')} {item.get('source', '')}".lower()
    vector_score = float(item.get("_vector_score") or 0.0)
    keyword_score = float(item.get("_keyword_score") or 0.0)
    metadata_score = 0.0
    exact_terms = re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,20}|(?:19|20)\d{2}", query_lower)
    if exact_terms and all(term.lower() in content_lower for term in exact_terms):
        metadata_score += 0.5
    if title and title.lower() in query_lower:
        metadata_score += 0.3
    if source_type == "hot":
        age = _age_days(str(item.get("event_date") or ""))
        metadata_score += max(0.0, 0.4 - min(0.4, age / 90.0))
    return vector_score * 0.7 + keyword_score * 0.2 + metadata_score * 0.1


def _query_years(text: str) -> set[int]:
    years = {int(value) for value in re.findall(r"(?:19|20)\d{2}", text)}
    for value in re.findall(r"(?<!\d)(\d{2})\s*年", text):
        number = int(value)
        years.add(2000 + number if number <= 79 else 1900 + number)
    return years


def _matches_year(item: dict[str, Any], years: set[int]) -> bool:
    text = f"{item.get('event_date', '')} {item.get('title', '')} {item.get('summary', '')}"
    item_years = {int(value) for value in re.findall(r"(?:19|20)\d{2}", text)}
    item_years.update(2010 + int(value) for value in re.findall(r"(?i)\bS(\d{1,2})\b", text))
    return bool(item_years & years)


def _age_days(value: str) -> int:
    try:
        return max(0, (date.today() - date.fromisoformat(value[:10])).days)
    except ValueError:
        return 9999


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_date": item.get("event_date", ""),
        "title": item.get("title", ""),
        "summary": item.get("summary", ""),
        "category": item.get("category", ""),
        "source": item.get("source", ""),
        "rank": int(item.get("rank") or 0),
        "heat": item.get("heat", ""),
        "source_url": item.get("source_url", ""),
        "fetched_at": item.get("fetched_at", ""),
        "rag_score": item.get("_rag_score", 0),
        "vector_score": round(float(item.get("_vector_score") or 0.0), 4),
        "keyword_score": round(float(item.get("_keyword_score") or 0.0), 4),
        "cross_encoder_score": round(float(item.get("_cross_encoder_score") or 0.0), 4),
    }


def _now_text() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _parse_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _vector_table(source_type: str) -> str:
    if source_type not in {"knowledge", "hot"}:
        raise ValueError("invalid RAG source type")
    return f"rag_vectors_{source_type}"


def _ensure_document_columns(conn: sqlite3.Connection) -> None:
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(rag_documents)")}
    if "rank" not in columns:
        conn.execute("ALTER TABLE rag_documents ADD COLUMN rank INTEGER NOT NULL DEFAULT 0")
    if "heat" not in columns:
        conn.execute("ALTER TABLE rag_documents ADD COLUMN heat TEXT NOT NULL DEFAULT ''")
    if "vectorized" not in columns:
        conn.execute("ALTER TABLE rag_documents ADD COLUMN vectorized INTEGER NOT NULL DEFAULT 1")
