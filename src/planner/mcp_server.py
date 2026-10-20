"""MCP-Server: gibt Claude enge, aber echte Werkzeuge auf den Planer-Stand.

Jedes Werkzeug ruft dieselben Funktionen auf wie die CLI (`tasks.add_task`, `proposals.*`, …) –
Change-Log, Nie-verloren-Garantie, Bereichsgewichte und die „Vorschlag nur nach Bestätigung“-Regel
gelten also unverändert. Quelle/Akteur stehen auf "claude", damit im Change-Log sichtbar bleibt,
was von Claude stammt.

Datenschutz: Mails und Sprachnachrichten dürfen laut Nutzer nie bei Claude landen. Die Lese-Werkzeuge
hier blenden deshalb alles mit `source` in {mail, gmail, voice} aus – auch abgeleitete Titel.

Netz: läuft nur im Heimnetz/Tailscale, niemals am Router offen. Start verlangt MCP_TOKEN in .env;
ohne Token startet der Server nicht (siehe `build_app`).
"""

from __future__ import annotations

from contextlib import closing
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import areas, proposals, tasks
from .config import Settings, load_settings
from .db import connect
from .parsing import parse_deadline, parse_estimate

SOURCE = "claude"
ACTOR = "claude"
# Diese Quellen können Mail-/Sprachinhalte widerspiegeln (z. B. im Titel) – nie an Claude zurückgeben.
RESTRICTED_SOURCES = ("mail", "gmail", "voice")

mcp = MCPServer("planner", instructions="Werkzeuge auf Jonahs echten Planer-Stand (Kalender/Tasks).")


def _area_id(conn, path: str | None) -> int | None:
    return areas.ensure_path(conn, path, actor=ACTOR) if path else None


@mcp.tool()
def add_task(
    title: str,
    deadline: str | None = None,
    area: str | None = None,
    estimate: str | None = None,
    boost: float = 1.0,
) -> dict[str, Any]:
    """Legt eine echte Aufgabe an – landet automatisch im nächsten Planungslauf im Kalender/in Google Tasks.

    Nur für Dinge, die wirklich erledigt werden müssen (Jonah hat es im Chat so gesagt). Für alles
    Freiwillige/Unsichere lieber `propose` nutzen, das braucht erst seine Bestätigung.

    deadline: z. B. "2026-10-20", "20.10." oder "20.10. 18:00". estimate: z. B. "90", "45m", "2h".
    area: Bereichspfad, z. B. "Uni/Modul-X" (wird bei Bedarf angelegt).
    boost: manueller Prioritätsfaktor (1.0 = normal, >1 wichtiger).
    """
    settings = load_settings()
    with closing(connect(settings.db_path)) as conn:
        try:
            dl = parse_deadline(deadline) if deadline else None
            est = parse_estimate(estimate) if estimate else None
        except ValueError as exc:
            return {"error": str(exc)}
        area_id = _area_id(conn, area)
        task_id = tasks.add_task(
            conn, title=title, source=SOURCE, actor=ACTOR, area_id=area_id, deadline=dl, estimate_min=est
        )
        if task_id is None:
            return {"error": "Aufgabe existiert schon."}
        if boost != 1.0:
            conn.execute("UPDATE tasks SET priority_boost = ? WHERE id = ?", (boost, task_id))
            conn.commit()
        return {"task_id": task_id, "title": title, "deadline": dl, "area": area}


@mcp.tool()
def propose(
    title: str,
    kind: str = "event",
    start: str | None = None,
    end: str | None = None,
    deadline: str | None = None,
    estimate: str | None = None,
    area: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Legt einen Vorschlag an – kommt NUR nach Bestätigung durch Jonah in den Kalender.

    kind "event" braucht start+end mit Uhrzeit (z. B. "5.11. 13:30"); kind "task" braucht eine Deadline.
    Nutze das für alles, was nicht sicher eine Pflichtaufgabe ist.
    """
    settings = load_settings()
    with closing(connect(settings.db_path)) as conn:
        try:
            s = parse_deadline(start) if start else None
            e = parse_deadline(end) if end else None
            if kind == "event" and not (s and e and "T" in s and "T" in e):
                return {"error": "Termin braucht start und end mit Uhrzeit, z. B. '5.11. 13:30'"}
            dl = parse_deadline(deadline) if deadline else None
            est = parse_estimate(estimate) if estimate else None
        except ValueError as exc:
            return {"error": str(exc)}
        area_id = _area_id(conn, area)
        try:
            pid = proposals.add_proposal(
                conn, title=title, kind=kind, source=SOURCE, actor=ACTOR, start=s, end=e,
                deadline=dl, estimate_min=est, area_id=area_id, note=note,
            )
        except ValueError as exc:
            return {"error": str(exc)}
        if pid is None:
            return {"error": "Vorschlag existiert schon."}
        return {"proposal_id": pid, "title": title, "kind": kind}


@mcp.tool()
def decide_proposal(proposal_id: int, accept: bool) -> dict[str, Any]:
    """Bestätigt oder verwirft einen Vorschlag – nur aufrufen, wenn Jonah im Chat zugestimmt hat."""
    settings = load_settings()
    with closing(connect(settings.db_path)) as conn:
        try:
            if accept:
                task_id = proposals.accept(conn, proposal_id, actor=ACTOR)
                return {"proposal_id": proposal_id, "accepted": True, "task_id": task_id}
            proposals.reject(conn, proposal_id, actor=ACTOR)
            return {"proposal_id": proposal_id, "accepted": False}
        except KeyError:
            return {"error": f"Vorschlag #{proposal_id} gibt es nicht."}
        except ValueError as exc:
            return {"error": str(exc)}


@mcp.tool()
def set_task_status(task_id: int, status: str) -> dict[str, Any]:
    """Setzt eine Aufgabe auf "done" oder "dropped". Die Blocker verschwinden beim nächsten Planungslauf."""
    settings = load_settings()
    with closing(connect(settings.db_path)) as conn:
        try:
            tasks.set_status(conn, task_id, status, actor=ACTOR)
        except KeyError:
            return {"error": f"Aufgabe #{task_id} gibt es nicht."}
        except ValueError as exc:
            return {"error": str(exc)}
        return {"task_id": task_id, "status": status}


def _visible_tasks(conn) -> list[dict[str, Any]]:
    return [
        {
            "id": t["id"], "title": t["title"], "deadline": t["deadline"],
            "estimate_min": t["estimate_min"], "area": areas.path_of(conn, t["area_id"]) or None,
            "source": t["source"],
        }
        for t in tasks.open_tasks(conn) if t["source"] not in RESTRICTED_SOURCES
    ]


@mcp.tool()
def list_open_tasks() -> list[dict[str, Any]]:
    """Offene Aufgaben (ohne alles, was aus Mail/Sprachnachricht stammt – siehe Datenschutzregel)."""
    settings = load_settings()
    with closing(connect(settings.db_path)) as conn:
        return _visible_tasks(conn)


@mcp.tool()
def list_pending_proposals() -> list[dict[str, Any]]:
    """Vorschläge, die noch auf Bestätigung warten (ohne Mail-/Sprach-Herkunft)."""
    settings = load_settings()
    with closing(connect(settings.db_path)) as conn:
        return [
            {
                "id": r["id"], "title": r["title"], "kind": r["kind"], "start": r["start"],
                "end": r["end"], "deadline": r["deadline"], "note": r["note"],
            }
            for r in proposals.pending(conn) if r["source"] not in RESTRICTED_SOURCES
        ]


@mcp.tool()
def area_tree() -> list[dict[str, Any]]:
    """Bereichsbaum mit Gewichten – zur Einordnung, wohin eine neue Aufgabe gehört."""
    settings = load_settings()
    with closing(connect(settings.db_path)) as conn:
        return [{"depth": d, "id": i, "name": n, "weight": w} for d, i, n, w in areas.tree(conn)]


class _BearerAuth(BaseHTTPMiddleware):
    """Einfacher statischer Bearer-Token-Check – bewusst kein OAuth (Einzelnutzer, nur im Tailnet)."""

    def __init__(self, app, token: str):
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        if request.headers.get("authorization") != f"Bearer {self._token}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def build_app(settings: Settings) -> Starlette:
    if not settings.mcp_token:
        raise ValueError(
            "MCP_TOKEN fehlt in .env – ohne Token startet der Server nicht "
            "(sonst könnte jeder im Tailnet deinen Planer ändern)."
        )
    # Eigene statt der SDK-Voreinstellung: die automatische DNS-Rebinding-Prüfung kennt nur
    # 127.0.0.1/localhost/::1 – bindet der Server stattdessen an die Tailscale-IP, bräuchte es das hier.
    security = TransportSecuritySettings(
        allowed_hosts=[f"{settings.mcp_host}:*"], allowed_origins=[f"http://{settings.mcp_host}:*"]
    )
    app = mcp.streamable_http_app(host=settings.mcp_host, transport_security=security)
    app.add_middleware(_BearerAuth, token=settings.mcp_token)
    return app


def run(settings: Settings | None = None) -> None:
    import uvicorn

    settings = settings or load_settings()
    uvicorn.run(build_app(settings), host=settings.mcp_host, port=settings.mcp_port, log_level="info")
