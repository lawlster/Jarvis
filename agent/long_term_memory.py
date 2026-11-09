"""Long-term contextual memory (Step 43).

A durable memory store that sits *over* the short-term memory in
``agent/memory.py`` (the last 20 messages in the SQLite
``conversations`` table).  This module never touches the
production database: records live as one JSON file per record
under a caller-chosen directory (default ``data/memory/``),
plus an ``index.json`` that is rebuilt from the record files
on every load.

Record types
-------------
``fact``        a durable statement about the owner, a company,
                or the project.
``preference``  how the owner wants things done.
``decision``    a choice made, with its reasons.
``commitment``  something promised or scheduled, with its status.
``observation`` something noticed — labelled as an observation,
                not a fact.

Every record carries an ``id``, ``type``, ``text``, who stated
it (``stated_by``), when (``stated_at``, ISO 8601), a ``source``
(a conversation turn id, a research-session file, a thesis id —
whatever lets a later read be traced), and ``tags`` (tickers,
topics).  A record can be superseded by a newer record: the old
one is kept, marked ``superseded``, and points at its
replacement.  Nothing is silently deleted.

Recall
-------
``recall`` retrieves records by tag, by type, by date range, and
by keyword search over the text, newest first, with a per-query
cap.  Superseded records are excluded by default.

Context blocks
--------------
``build_context`` pulls the records relevant to a question or a
ticker (tag match plus keyword match) into a small block the
Step 39 orchestrator can hand to the model.  Every record in
the block is labelled with its type and source, and the block
has hard caps on record count and characters.

The store starts empty.  It records only what the caller
supplies — nothing here is pre-seeded with the owner's real
facts, preferences or decisions.

Nothing in this module is a recommendation, a trade, a send, or
a schedule.  It records and recalls; it does not act.
"""

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MEMORY_DIR = BASE_DIR / "data" / "memory"

RECORD_TYPES = ("fact", "preference", "decision", "commitment",
                "observation")

INDEX_FILENAME = "index.json"

DEFAULT_RECALL_LIMIT = 20
DEFAULT_CONTEXT_RECORDS = 5
DEFAULT_CONTEXT_MAX_CHARS = 2000

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*")

# Small stopword set so the context scorer does not reward filler
# words.  Kept deliberately short: only the words that carry no
# topical meaning in a research question.
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "what", "whats",
    "what's", "how", "why", "when", "where", "who", "which", "that",
    "this", "these", "those", "it", "its", "of", "on", "in", "to",
    "for", "and", "or", "with", "about", "me", "my", "i", "you",
    "your", "please", "tell", "give", "show", "do", "does", "did",
    "can", "could", "should", "would", "is", "be", "been", "has",
    "have", "had", "there", "here", "from", "by", "at", "as", "an",
    "we", "us", "our", "they", "them", "their", "he", "she", "his",
    "her", "his", "not", "no", "yes", "if", "but", "so", "than",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow_iso():
    """Current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _coerce_datetime(value):
    """Coerce an ISO string / datetime to an aware datetime, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _normalise_tags(tags):
    """Normalise a tags value to a de-duplicated list of strings."""
    if not tags:
        return []
    if isinstance(tags, str):
        tags = [tags]
    seen = set()
    out = []
    for tag in tags:
        if tag is None:
            continue
        cleaned = str(tag).strip()
        if not cleaned or cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        out.append(cleaned)
    return out


def _tokens(text):
    """Lower-cased content tokens of a question, minus stopwords."""
    if not text:
        return []
    found = []
    seen = set()
    for token in _TOKEN_RE.findall(text.lower()):
        if token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        found.append(token)
    return found


def _memory_dir(memory_dir):
    return Path(memory_dir) if memory_dir else DEFAULT_MEMORY_DIR


def _record_filename(record_id):
    return f"{record_id}.json"


# ---------------------------------------------------------------------------
# Record creation
# ---------------------------------------------------------------------------

def new_record(record_type, text, stated_by=None, stated_at=None,
               source=None, source_kind=None, tags=None):
    """Build a new memory record dict (not yet saved).

    ``record_type`` must be one of ``RECORD_TYPES``; ``text`` must
    be non-empty.  ``stated_at`` defaults to now (UTC).  ``source``
    is whatever lets the record be traced later — a conversation
    turn id, a research-session file path, a thesis id.  Returns
    the record dict with status ``active``.
    """
    if record_type not in RECORD_TYPES:
        raise ValueError(
            f"unknown record type {record_type!r}; expected one of "
            f"{', '.join(RECORD_TYPES)}")
    if not text or not str(text).strip():
        raise ValueError("record text must not be empty")
    stated = _coerce_datetime(stated_at)
    stated_iso = stated.isoformat() if stated else _utcnow_iso()
    return {
        "id": uuid.uuid4().hex,
        "type": record_type,
        "text": str(text).strip(),
        "stated_by": stated_by,
        "stated_at": stated_iso,
        "source": source,
        "source_kind": source_kind,
        "tags": _normalise_tags(tags),
        "status": "active",
        "superseded_by": None,
    }


# ---------------------------------------------------------------------------
# Storage: one JSON file per record, plus a rebuilt index
# ---------------------------------------------------------------------------

def save_record(record, memory_dir=None):
    """Write a record dict as ``{id}.json``; return the path.

    The record dict is updated in place with its ``file_path``.
    The index is rebuilt after the write.
    """
    directory = _memory_dir(memory_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if not record.get("id"):
        record["id"] = uuid.uuid4().hex
    path = directory / _record_filename(record["id"])
    record["file_path"] = str(path)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    _rebuild_index(directory)
    return str(path)


def load_record(path):
    """Load a record JSON file; return the record dict."""
    with open(path, encoding="utf-8") as handle:
        record = json.load(handle)
    if not isinstance(record, dict) or not record.get("id"):
        raise ValueError(f"not a memory record: {path}")
    return record


def load_record_by_id(record_id, memory_dir=None):
    """Load a record by id; return the record dict, or None."""
    path = _memory_dir(memory_dir) / _record_filename(record_id)
    if not path.exists():
        return None
    return load_record(path)


def _read_all(memory_dir):
    """Read every record file in the directory.

    Returns ``(records, skipped)`` where ``skipped`` is a list of
    file names that could not be read as records (corrupt JSON,
    wrong shape, or the index file itself is never a record).
    Corrupt files are skipped, never allowed to crash the store.
    """
    directory = _memory_dir(memory_dir)
    records = []
    skipped = []
    if not directory.exists():
        return records, skipped
    for path in sorted(directory.glob("*.json")):
        if path.name == INDEX_FILENAME:
            continue
        try:
            record = load_record(path)
        except (ValueError, OSError, json.JSONDecodeError):
            skipped.append(path.name)
            continue
        records.append(record)
    return records, skipped


def _rebuild_index(directory):
    """Rebuild ``index.json`` from the record files on disk."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    records, skipped = _read_all(directory)
    index = {
        "record_count": len(records),
        "skipped_files": skipped,
        "records": [
            {
                "id": record.get("id"),
                "type": record.get("type"),
                "stated_at": record.get("stated_at"),
                "tags": record.get("tags") or [],
                "status": record.get("status") or "active",
            }
            for record in sorted(
                records,
                key=lambda r: r.get("stated_at") or "",
                reverse=True)
        ],
    }
    (directory / INDEX_FILENAME).write_text(
        json.dumps(index, indent=2), encoding="utf-8")
    return index


def load_store(memory_dir=None):
    """Load the whole store: records, skipped files, and counts.

    Returns ``{"records": [...], "skipped": [...],
    "record_count": n, "active_count": n}``.  Records are newest
    first by ``stated_at``.  The index file is rebuilt as a side
    effect so it never goes stale.
    """
    directory = _memory_dir(memory_dir)
    records, skipped = _read_all(directory)
    records.sort(key=lambda r: r.get("stated_at") or "", reverse=True)
    _rebuild_index(directory)
    active = [r for r in records if r.get("status") != "superseded"]
    return {
        "records": records,
        "skipped": skipped,
        "record_count": len(records),
        "active_count": len(active),
    }


def list_records(memory_dir=None, include_superseded=False):
    """All records, newest first by ``stated_at``.

    Superseded records are excluded unless ``include_superseded``
    is True.
    """
    store = load_store(memory_dir)
    records = store["records"]
    if not include_superseded:
        records = [r for r in records
                   if r.get("status") != "superseded"]
    return records


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------

def recall(memory_dir=None, tag=None, record_type=None, since=None,
           until=None, keyword=None, limit=DEFAULT_RECALL_LIMIT,
           include_superseded=False):
    """Retrieve records matching the given filters, newest first.

    Filters combine (AND): ``tag`` (case-insensitive match against
    a record's tags), ``record_type`` (must be a known type),
    ``since`` / ``until`` (ISO date or datetime bounds on
    ``stated_at`` — ``since`` inclusive, ``until`` exclusive),
    ``keyword`` (case-insensitive substring over the record text
    and tags).  ``limit`` caps the number returned (``None`` means
    no cap).  Superseded records are excluded unless
    ``include_superseded`` is True.
    """
    if record_type is not None and record_type not in RECORD_TYPES:
        raise ValueError(
            f"unknown record type {record_type!r}; expected one of "
            f"{', '.join(RECORD_TYPES)}")
    since_dt = _coerce_datetime(since)
    until_dt = _coerce_datetime(until)
    keyword_lower = keyword.strip().lower() if keyword else None
    tag_lower = tag.strip().lower() if tag else None

    out = []
    for record in list_records(memory_dir,
                               include_superseded=include_superseded):
        if record_type is not None and record.get("type") != record_type:
            continue
        if tag_lower is not None:
            tags = [str(t).lower()
                    for t in (record.get("tags") or [])]
            if tag_lower not in tags:
                continue
        if since_dt is not None or until_dt is not None:
            stated = _coerce_datetime(record.get("stated_at"))
            if stated is None:
                continue
            if since_dt is not None and stated < since_dt:
                continue
            if until_dt is not None and stated >= until_dt:
                continue
        if keyword_lower is not None:
            haystack = " ".join(
                [record.get("text") or ""] +
                [str(t) for t in (record.get("tags") or [])]).lower()
            if keyword_lower not in haystack:
                continue
        out.append(record)
    if limit is not None:
        out = out[:limit]
    return out


# ---------------------------------------------------------------------------
# Correction: supersede, never silently delete
# ---------------------------------------------------------------------------

def supersede_record(old_id, new_record, memory_dir=None):
    """Supersede the record ``old_id`` with ``new_record``.

    ``new_record`` is a record dict built by :func:`new_record`
    (it is saved first).  The old record is kept, marked
    ``superseded``, and given ``superseded_by`` pointing at the new
    record's id.  Returns ``(old_record, new_record)``.  Raises
    ``KeyError`` if no record with ``old_id`` exists, and
    ``ValueError`` if the old record is already superseded.
    """
    directory = _memory_dir(memory_dir)
    old = load_record_by_id(old_id, directory)
    if old is None:
        raise KeyError(f"no memory record with id {old_id!r}")
    if old.get("status") == "superseded":
        raise ValueError(
            f"record {old_id!r} is already superseded by "
            f"{old.get('superseded_by')!r}")
    new_path = save_record(new_record, directory)
    new_record["file_path"] = new_path
    old["status"] = "superseded"
    old["superseded_by"] = new_record["id"]
    save_record(old, directory)
    return old, new_record


# ---------------------------------------------------------------------------
# Context blocks for the orchestrator
# ---------------------------------------------------------------------------

def _score_record(record, query_tokens, ticker):
    """Relevance score of a record for a question / ticker.

    Whole-word matching only: a query token scores when it equals
    a tag, or when it appears as a whole word in the record text.
    Substring matching is deliberately not used — a short token
    such as ``we`` must not match inside ``weak``.
    """
    score = 0
    tags_lower = [str(t).lower()
                  for t in (record.get("tags") or [])]
    if ticker and ticker.strip().lower() in tags_lower:
        score += 3
    text_words = set(_TOKEN_RE.findall(
        (record.get("text") or "").lower()))
    for token in query_tokens:
        if token in tags_lower:
            score += 2
        elif token in text_words:
            score += 1
    return score


def _format_record(record):
    """One labelled line for a record inside a context block."""
    source = record.get("source") or "source not recorded"
    stated_by = record.get("stated_by") or "not recorded"
    stated_at = record.get("stated_at") or "date not recorded"
    return (
        f"[{record.get('type', 'fact')}] {record.get('text', '')} "
        f"(stated by {stated_by}, {stated_at}; source: {source})"
    )


def build_context(question=None, ticker=None, memory_dir=None,
                  max_records=DEFAULT_CONTEXT_RECORDS,
                  max_chars=DEFAULT_CONTEXT_MAX_CHARS):
    """Build a context block from the records relevant to a
    question and/or ticker.

    Relevance is tag match (a ticker tag scores highest) plus
    keyword match against the question's content words.  Only
    active records are used — superseded records never reach the
    model.  Returns ``{"block": str, "records": [...],
    "record_count": n, "truncated": bool, "query": {...}}``.

    ``block`` is empty when nothing is relevant.  ``truncated``
    is True when the record or character cap cut something off.
    """
    if not question and not ticker:
        return {
            "block": "",
            "records": [],
            "record_count": 0,
            "truncated": False,
            "query": {"question": question, "ticker": ticker},
        }
    query_tokens = _tokens(question)
    ticker_clean = ticker.strip() if ticker else None

    scored = []
    for record in list_records(memory_dir):
        score = _score_record(record, query_tokens, ticker_clean)
        if score > 0:
            scored.append((score, record.get("stated_at") or "", record))
    # Highest score first; ties broken newest first.
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

    chosen = []
    truncated = False
    for _score, _stated, record in scored:
        if len(chosen) >= max_records:
            truncated = True
            break
        chosen.append(record)

    lines = [_format_record(record) for record in chosen]
    block = "\n".join(lines)
    if len(block) > max_chars:
        block = block[:max_chars].rstrip()
        truncated = True
    return {
        "block": block,
        "records": chosen,
        "record_count": len(chosen),
        "truncated": truncated,
        "query": {"question": question, "ticker": ticker_clean},
    }


def context_block(question=None, ticker=None, memory_dir=None,
                  max_records=DEFAULT_CONTEXT_RECORDS,
                  max_chars=DEFAULT_CONTEXT_MAX_CHARS):
    """The context block text only (see :func:`build_context`)."""
    return build_context(question, ticker, memory_dir,
                         max_records=max_records,
                         max_chars=max_chars)["block"]


# ---------------------------------------------------------------------------
# Thin store class (convenience wrapper over the functions above)
# ---------------------------------------------------------------------------

class MemoryStore:
    """A long-term memory store rooted at one directory."""

    def __init__(self, memory_dir=None):
        self.memory_dir = _memory_dir(memory_dir)

    def add(self, record_type, text, **kwargs):
        """Create and save a record; return it."""
        record = new_record(record_type, text, **kwargs)
        save_record(record, self.memory_dir)
        return record

    def recall(self, **kwargs):
        """See :func:`recall`."""
        return recall(self.memory_dir, **kwargs)

    def list(self, include_superseded=False):
        """See :func:`list_records`."""
        return list_records(self.memory_dir,
                            include_superseded=include_superseded)

    def supersede(self, old_id, new_record):
        """See :func:`supersede_record`."""
        return supersede_record(old_id, new_record, self.memory_dir)

    def context(self, question=None, ticker=None, **kwargs):
        """See :func:`build_context`."""
        return build_context(question, ticker, self.memory_dir,
                             **kwargs)

    def load_store(self):
        """See :func:`load_store`."""
        return load_store(self.memory_dir)
