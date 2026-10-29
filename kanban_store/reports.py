"""Reports: what agents post back on a card.

A report is a progress update or a final result (``kind``) with a Markdown
summary and an ordered list of assets. Asset types are special-cased because
each renders differently in the UI:

* ``mermaid``  — Mermaid diagram source (text), rendered with mermaid.js
* ``plotly``   — a Plotly figure (JSON: ``data``, ``layout``, ``config``),
                 rendered interactively with plotly.js
* ``snapshot`` — a raster image: PNG, JPEG, GIF or WebP (screenshots, plots
                 saved by matplotlib, camera frames, …)
* ``svg``      — a generic SVG drawing, shown as an image (never inlined:
                 served with a sandboxing CSP, so scripts in it cannot run)
* ``file``     — anything else, offered as a download

Asset bodies live on disk, content-addressed by SHA-256 under the assets
directory (``KANBAN_ASSETS_DIR``, default ``<db dir>/kanban_assets``), so the
same figure posted twice is stored once. Only metadata is in SQLite.

A result report goes through review: ``submitted`` → ``accepted`` or
``changes_requested``; a newer result on the same card marks older submitted
ones ``superseded``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

ASSET_TYPES: tuple[str, ...] = ("mermaid", "plotly", "snapshot", "svg", "file")
REPORT_KINDS: tuple[str, ...] = ("progress", "result")
REVIEW_STATES: tuple[str, ...] = ("submitted", "accepted", "changes_requested", "superseded")
# Types the board can show as a picture on a card or in the gallery.
VISUAL_TYPES: tuple[str, ...] = ("snapshot", "svg", "plotly", "mermaid")

MAX_ASSET_BYTES = int(os.environ.get("KANBAN_ASSET_MAX_BYTES", str(20 * 1024 * 1024)))
MAX_ASSETS_PER_REPORT = int(os.environ.get("KANBAN_ASSETS_PER_REPORT", "24"))
MAX_MERMAID_BYTES = 256 * 1024

_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
_SVG_ROOT = re.compile(rb"^\s*(<\?xml[^>]*>\s*)?(<!--.*?-->\s*)*(<!DOCTYPE[^>]*>\s*)?<svg[\s>]", re.S | re.I)


@dataclass
class ReportAsset:
    id: int
    report_id: int
    position: int
    type: str
    caption: str
    mime: str
    sha256: str
    size: int
    filename: str
    url: str = ""

    def to_public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Report:
    id: int
    task_id: str
    session_id: str | None
    author: str
    kind: str
    title: str
    summary_md: str
    review_status: str | None
    review_comment: str | None
    reviewed_at: str | None
    created_at: str
    assets: list[ReportAsset] = field(default_factory=list)

    def to_public(self) -> dict[str, Any]:
        d = asdict(self)
        d["assets"] = [a.to_public() for a in self.assets]
        return d


def asset_url(asset_id: int) -> str:
    return f"/api/assets/{asset_id}"


def sniff_image(data: bytes) -> str | None:
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def prepare_asset(spec: dict[str, Any]) -> tuple[str, str, bytes, str, str]:
    """Validate one asset spec and return ``(type, caption, body, mime, filename)``.

    ``spec`` keys: ``type`` (required), ``caption``, and the body as either
    ``text`` (mermaid source, Plotly JSON or SVG markup), ``figure`` (a Plotly
    figure as a dict) or ``data`` (bytes: images, SVG, files); ``filename``
    for files. Raises ``ValueError`` with a message an agent can act on.
    """
    type_ = str(spec.get("type") or "").strip().lower()
    if type_ in ("image", "png", "jpg", "jpeg", "screenshot", "photo"):
        type_ = "snapshot"
    if type_ in ("chart", "plot"):
        type_ = "plotly"
    if type_ not in ASSET_TYPES:
        raise ValueError(f"asset type must be one of {', '.join(ASSET_TYPES)}, got {spec.get('type')!r}")
    caption = str(spec.get("caption") or "").strip()[:500]
    filename = os.path.basename(str(spec.get("filename") or "").strip())[:200]
    data: bytes | None = spec.get("data")
    text = spec.get("text")
    figure = spec.get("figure")

    if type_ == "mermaid":
        src = text if isinstance(text, str) else (data.decode("utf-8") if data else "")
        src = src.strip()
        # agents often wrap the diagram in a ```mermaid fence
        m = re.match(r"^```\s*mermaid\s*\n(.*?)\n?```\s*$", src, re.S)
        if m:
            src = m.group(1).strip()
        if not src:
            raise ValueError("mermaid asset needs the diagram source")
        body = src.encode("utf-8")
        if len(body) > MAX_MERMAID_BYTES:
            raise ValueError(f"mermaid source too large ({len(body)} bytes, max {MAX_MERMAID_BYTES})")
        return type_, caption, body, "text/plain; charset=utf-8", filename or "diagram.mmd"

    if type_ == "plotly":
        if figure is None:
            raw = text if isinstance(text, str) else (data.decode("utf-8") if data else "")
            try:
                figure = json.loads(raw) if raw.strip() else None
            except json.JSONDecodeError as e:
                raise ValueError(f"plotly asset is not valid JSON: {e}") from None
        if hasattr(figure, "to_plotly_json"):  # a plotly.graph_objects.Figure
            figure = figure.to_plotly_json()
        if not isinstance(figure, dict) or not isinstance(figure.get("data"), list):
            raise ValueError('plotly asset must be a figure object with a "data" list '
                             '(e.g. fig.to_json() from plotly)')
        body = json.dumps(figure, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
        _check_size(body, "plotly figure")
        return type_, caption, body, "application/json", filename or "figure.json"

    if type_ == "svg":
        body = text.encode("utf-8") if isinstance(text, str) else (data or b"")
        if not body:
            raise ValueError("svg asset needs the SVG markup or file")
        if not _SVG_ROOT.match(body[:4096]):
            raise ValueError("svg asset does not look like an SVG document (root element must be <svg>)")
        _check_size(body, "svg")
        return type_, caption, body, "image/svg+xml", filename or "drawing.svg"

    if type_ == "snapshot":
        body = data or b""
        if not body:
            raise ValueError("snapshot asset needs image bytes (path or data_base64)")
        mime = sniff_image(body)
        if mime is None:
            if _SVG_ROOT.match(body[:4096]):
                raise ValueError("this is an SVG — post it with type 'svg'")
            raise ValueError("snapshot must be a PNG, JPEG, GIF or WebP image")
        _check_size(body, "snapshot")
        ext = mime.split("/")[1].replace("jpeg", "jpg")
        return type_, caption, body, mime, filename or f"snapshot.{ext}"

    # file
    body = data if data is not None else (text.encode("utf-8") if isinstance(text, str) else b"")
    if not body:
        raise ValueError("file asset needs content (path or data_base64)")
    _check_size(body, "file")
    return type_, caption, body, "application/octet-stream", filename or "attachment.bin"


def _check_size(body: bytes, what: str) -> None:
    if len(body) > MAX_ASSET_BYTES:
        raise ValueError(f"{what} too large ({len(body)} bytes, max {MAX_ASSET_BYTES}; "
                         "KANBAN_ASSET_MAX_BYTES)")


class ReportsMixin:
    """Report methods for :class:`kanban_store.Store` (uses its connection,
    lock and history helpers)."""

    # set by Store.__init__
    assets_dir: Path

    # ------------------------------------------------------------------ files

    def _asset_path(self, sha: str) -> Path:
        return self.assets_dir / sha[:2] / sha

    def _write_blob(self, body: bytes) -> str:
        sha = hashlib.sha256(body).hexdigest()
        path = self._asset_path(sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(body)
            tmp.replace(path)
        return sha

    def asset_bytes(self, asset: ReportAsset) -> bytes:
        return self._asset_path(asset.sha256).read_bytes()

    def asset_file(self, asset: ReportAsset) -> Path:
        return self._asset_path(asset.sha256)

    # ------------------------------------------------------------------ writes

    def create_report(
        self,
        task_id: str,
        title: str,
        *,
        author: str,
        kind: str = "progress",
        summary_md: str = "",
        session_id: str | None = None,
        assets: Iterable[dict[str, Any]] = (),
    ) -> Report:
        """Store a report and its assets; writes a ``report`` history row.
        Asset specs are validated before anything is written (see
        :func:`prepare_asset`)."""
        from .store import _now, normalize_choice  # local: avoid import cycle

        title = (title or "").strip()
        if not title:
            raise ValueError("report title must not be empty")
        kind = normalize_choice(kind, REPORT_KINDS, "report kind")
        specs = list(assets or [])
        if len(specs) > MAX_ASSETS_PER_REPORT:
            raise ValueError(f"too many assets ({len(specs)}, max {MAX_ASSETS_PER_REPORT})")
        prepared = []
        for i, spec in enumerate(specs):
            try:
                prepared.append(prepare_asset(spec))
            except ValueError as e:
                raise ValueError(f"assets[{i}]: {e}") from None
        shas = [self._write_blob(p[2]) for p in prepared]
        ts = _now()
        with self._lock:
            if not self._task_exists(task_id):
                raise KeyError(task_id)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if kind == "result":
                    self._conn.execute(
                        "UPDATE reports SET review_status='superseded' "
                        "WHERE task_id=? AND kind='result' AND review_status='submitted'",
                        (task_id,),
                    )
                cur = self._conn.execute(
                    """INSERT INTO reports (task_id, session_id, author, kind, title, summary_md,
                                            review_status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (task_id, (session_id or "").strip() or None, author or "agent", kind,
                     title[:300], summary_md or "", "submitted" if kind == "result" else None, ts),
                )
                rid = cur.lastrowid
                for pos, ((type_, caption, body, mime, filename), sha) in enumerate(zip(prepared, shas)):
                    self._conn.execute(
                        """INSERT INTO report_assets (report_id, position, type, caption, mime,
                                                      sha256, size, filename, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (rid, pos, type_, caption, mime, sha, len(body), filename, ts),
                    )
                n = len(prepared)
                note = (f"{'result' if kind == 'result' else 'progress'} report: {title}"
                        + (f" ({n} asset{'s' if n != 1 else ''})" if n else ""))
                self._insert_history(task_id, ts, author or "agent", "report", None, None, note)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        r = self.get_report(rid)
        assert r is not None
        return r

    def review_report(
        self, report_id: int, decision: str, *, actor: str, comment: str = "",
    ) -> Report:
        """Accept a result report (card → done) or request changes (card →
        in_progress, and the comment goes to the agent as feedback)."""
        from .store import _now

        decision = str(decision or "").strip().lower().replace(" ", "_")
        if decision in ("accept", "accepted", "approve"):
            decision = "accepted"
        elif decision in ("changes", "request_changes", "changes_requested", "reject"):
            decision = "changes_requested"
        else:
            raise ValueError("decision must be 'accept' or 'changes'")
        comment = (comment or "").strip()
        if decision == "changes_requested" and not comment:
            raise ValueError("say what should change (comment)")
        r = self.get_report(report_id)
        if r is None:
            raise KeyError(report_id)
        if r.kind != "result":
            raise ValueError("only result reports are reviewed")
        ts = _now()
        with self._lock:
            self._conn.execute(
                "UPDATE reports SET review_status=?, review_comment=?, reviewed_at=? WHERE id=?",
                (decision, comment or None, ts, report_id),
            )
        task = self.get_task(r.task_id, history_limit=0)
        if decision == "accepted":
            if task and task.status != "done":
                self.move_task(r.task_id, "done", actor=actor,
                               comment=f"accepted: {r.title}" + (f" — {comment}" if comment else ""))
        else:
            self.send_input(
                f"Changes requested on your result “{r.title}”: {comment}",
                author=actor, task_id=r.task_id, kind="feedback",
                session_id=r.session_id if r.session_id and self.get_session(r.session_id) else None,
                report_id=r.id,
            )
            if task and task.status in ("results", "testing", "done"):
                self.move_task(r.task_id, "in_progress", actor=actor,
                               comment=f"changes requested: {comment}")
        out = self.get_report(report_id)
        assert out is not None
        return out

    # ------------------------------------------------------------------ reads

    def get_report(self, report_id: int) -> Report | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM reports WHERE id=?", (int(report_id),)).fetchone()
            if not row:
                return None
            rep = self._row_to_report(row)
            rep.assets = self._assets_for([rep.id]).get(rep.id, [])
        return rep

    def get_asset(self, asset_id: int) -> ReportAsset | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM report_assets WHERE id=?", (int(asset_id),)
            ).fetchone()
        return self._row_to_asset(row) if row else None

    def list_reports(self, task_id: str) -> list[Report]:
        """A card's reports, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reports WHERE task_id=? ORDER BY id DESC", (task_id,)
            ).fetchall()
            reps = [self._row_to_report(r) for r in rows]
            by_report = self._assets_for([r.id for r in reps])
        for r in reps:
            r.assets = by_report.get(r.id, [])
        return reps

    def list_project_assets(
        self, project_id: str, *, kind: str | None = "result", limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Visual assets of a project's reports, newest first — the gallery."""
        sql = """SELECT a.*, r.title AS report_title, r.kind AS report_kind,
                        r.review_status, r.created_at AS report_created_at,
                        r.author AS report_author, t.id AS task_id, t.title AS task_title,
                        t.status AS task_status
                 FROM report_assets a
                 JOIN reports r ON r.id = a.report_id
                 JOIN tasks t ON t.id = r.task_id
                 WHERE t.project_id = ? AND a.type IN ({})""".format(",".join("?" * len(VISUAL_TYPES)))
        params: list[Any] = [project_id, *VISUAL_TYPES]
        if kind:
            sql += " AND r.kind = ?"
            params.append(kind)
        sql += " ORDER BY a.id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            a = self._row_to_asset(row).to_public()
            a.update(
                report_title=row["report_title"], report_kind=row["report_kind"],
                review_status=row["review_status"], report_created_at=row["report_created_at"],
                report_author=row["report_author"], task_id=row["task_id"],
                task_title=row["task_title"], task_status=row["task_status"],
            )
            out.append(a)
        return out

    def report_summary(self, task_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Per card: report count, latest report kind/review and the first
        visual asset of the latest report that has one (card thumbnail)."""
        if not task_ids:
            return {}
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            for i in range(0, len(task_ids), 500):
                chunk = task_ids[i:i + 500]
                ph = ",".join("?" * len(chunk))
                for r in self._conn.execute(
                    f"""SELECT task_id, COUNT(*) AS n, MAX(id) AS last_id FROM reports
                        WHERE task_id IN ({ph}) GROUP BY task_id""", chunk,
                ).fetchall():
                    last = self._conn.execute(
                        "SELECT kind, review_status FROM reports WHERE id=?", (r["last_id"],)
                    ).fetchone()
                    out[r["task_id"]] = {
                        "reports": r["n"], "last_kind": last["kind"],
                        "review_status": last["review_status"], "thumb": None,
                    }
                vt = ",".join("?" * len(VISUAL_TYPES))
                for r in self._conn.execute(
                    f"""SELECT r.task_id, a.id, a.type, a.caption FROM report_assets a
                        JOIN reports r ON r.id = a.report_id
                        WHERE r.task_id IN ({ph}) AND a.type IN ({vt})
                        ORDER BY r.id DESC,
                                 CASE WHEN a.type IN ('snapshot', 'svg') THEN 0 ELSE 1 END,
                                 a.position ASC""",
                    (*chunk, *VISUAL_TYPES),
                ).fetchall():
                    info = out.get(r["task_id"])
                    if info is not None and info["thumb"] is None:
                        info["thumb"] = {"asset_id": r["id"], "type": r["type"],
                                         "caption": r["caption"], "url": asset_url(r["id"])}
        return out

    # ------------------------------------------------------------------ rows

    def _assets_for(self, report_ids: list[int]) -> dict[int, list[ReportAsset]]:
        if not report_ids:
            return {}
        ph = ",".join("?" * len(report_ids))
        rows = self._conn.execute(
            f"SELECT * FROM report_assets WHERE report_id IN ({ph}) ORDER BY report_id, position",
            report_ids,
        ).fetchall()
        out: dict[int, list[ReportAsset]] = {}
        for row in rows:
            out.setdefault(row["report_id"], []).append(self._row_to_asset(row))
        return out

    @staticmethod
    def _row_to_asset(row: Any) -> ReportAsset:
        return ReportAsset(
            id=row["id"], report_id=row["report_id"], position=row["position"],
            type=row["type"], caption=row["caption"], mime=row["mime"], sha256=row["sha256"],
            size=row["size"], filename=row["filename"], url=asset_url(row["id"]),
        )

    @staticmethod
    def _row_to_report(row: Any) -> Report:
        return Report(
            id=row["id"], task_id=row["task_id"], session_id=row["session_id"],
            author=row["author"], kind=row["kind"], title=row["title"],
            summary_md=row["summary_md"], review_status=row["review_status"],
            review_comment=row["review_comment"], reviewed_at=row["reviewed_at"],
            created_at=row["created_at"],
        )
