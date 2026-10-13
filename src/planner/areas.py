"""Bereichsbaum (z. B. „Uni/Modul X“) mit Gewichten für die Priorisierung.

Das Gewicht eines Bereichs gilt direkt für ihn; wer unter „Uni“ einen Unterbereich anlegt,
setzt dessen Gewicht explizit (Standard 1.0) und überschreibt damit den Elternwert.
"""

from __future__ import annotations

import sqlite3

from . import changelog


def _split(path: str) -> list[str]:
    parts = [p.strip() for p in path.split("/") if p.strip()]
    if not parts:
        raise ValueError("leerer Bereichspfad")
    return parts


def find_path(conn: sqlite3.Connection, path: str) -> int | None:
    parent: int | None = None
    for name in _split(path):
        row = conn.execute(
            "SELECT id FROM areas WHERE parent_id IS ? AND name = ? COLLATE NOCASE", (parent, name)
        ).fetchone()
        if row is None:
            return None
        parent = int(row["id"])
    return parent


def ensure_path(conn: sqlite3.Connection, path: str, *, actor: str) -> int:
    """Legt fehlende Knoten des Pfads an (Gewicht 1.0) und gibt die ID des letzten zurück."""
    parent: int | None = None
    for name in _split(path):
        row = conn.execute(
            "SELECT id FROM areas WHERE parent_id IS ? AND name = ? COLLATE NOCASE", (parent, name)
        ).fetchone()
        if row is None:
            cur = conn.execute("INSERT INTO areas(name, parent_id) VALUES (?, ?)", (name, parent))
            conn.commit()
            parent = int(cur.lastrowid)
            changelog.log(conn, actor=actor, action="area.created", entity="area",
                          entity_id=parent, detail={"name": name})
        else:
            parent = int(row["id"])
    assert parent is not None
    return parent


def set_weight(conn: sqlite3.Connection, area_id: int, weight: float, *, actor: str) -> None:
    if weight <= 0:
        raise ValueError("Gewicht muss > 0 sein")
    old = conn.execute("SELECT weight FROM areas WHERE id = ?", (area_id,)).fetchone()
    if old is None:
        raise KeyError(area_id)
    conn.execute("UPDATE areas SET weight = ? WHERE id = ?", (weight, area_id))
    conn.commit()
    changelog.log(conn, actor=actor, action="area.weight", entity="area", entity_id=area_id,
                  detail={"from": old["weight"], "to": weight})


def weight_of(conn: sqlite3.Connection, area_id: int | None) -> float:
    """Gewicht des Bereichs; Aufgaben ohne Bereich zählen neutral (1.0)."""
    if area_id is None:
        return 1.0
    row = conn.execute("SELECT weight FROM areas WHERE id = ?", (area_id,)).fetchone()
    return float(row["weight"]) if row else 1.0


def path_of(conn: sqlite3.Connection, area_id: int | None) -> str:
    names: list[str] = []
    current = area_id
    while current is not None:
        row = conn.execute("SELECT name, parent_id FROM areas WHERE id = ?", (current,)).fetchone()
        if row is None:
            break
        names.append(row["name"])
        current = row["parent_id"]
    return "/".join(reversed(names))


def tree(conn: sqlite3.Connection) -> list[tuple[int, int, str, float]]:
    """(Tiefe, id, Name, Gewicht) in Baumreihenfolge."""
    rows = conn.execute("SELECT id, name, parent_id, weight FROM areas ORDER BY name COLLATE NOCASE").fetchall()
    children: dict[int | None, list] = {}
    for r in rows:
        children.setdefault(r["parent_id"], []).append(r)
    out: list[tuple[int, int, str, float]] = []

    def walk(parent: int | None, depth: int) -> None:
        for r in children.get(parent, []):
            out.append((depth, int(r["id"]), r["name"], float(r["weight"])))
            walk(int(r["id"]), depth + 1)

    walk(None, 0)
    return out
