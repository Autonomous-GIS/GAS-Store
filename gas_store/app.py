from flask import (
    Flask,
    request,
    jsonify,
    send_from_directory,
    render_template,
)
import sqlite3
import json
import os
from pathlib import Path

from gas_server.gas_store import gas_store

BASE_DIR = Path(__file__).resolve().parent.parent
STORE_DIR = BASE_DIR / "gas_store"

DB_PATH = str(STORE_DIR / "gas_store.db")

API_PATH = "/store/api/gas"

@app.route("/store")
def gas_store_index():
    return send_from_directory(
        STORE_DIR / "templates",
        "index.html"
    )

@app.route("/store/api/gas")
def gas_store_api():
    service = (request.args.get("SERVICE", "") or "").upper()
    req = (request.args.get("REQUEST", "") or "").lower()
    name = request.args.get("name") or request.args.get("agent")

    if service and service != "GAS":
        return jsonify(ok=False, error="Invalid SERVICE"), 400

    if req == "getcapabilities":
        return jsonify(
            service="GAS",
            version="1.0.0",
            request="GetCapabilities",
            agents=list_agents(DB_PATH),
        )

    if req == "describeagent":
        if not name:
            return jsonify(ok=False, error="Missing name"), 400

        detail = load_detail(DB_PATH, name)

        if detail is None:
            return jsonify(ok=False, error="Agent not found"), 404

        return jsonify(detail)

    return jsonify(ok=False, error="Invalid REQUEST"), 400

@app.route("/store/api/gas/register", methods=["POST"])
def gas_register():
    """
    Register every agent from a remote GAS GetCapabilities URL
    into the local gas_store database.
    """

    data = request.get_json(silent=True) or {}

    url = (
        data.get("url")
        or request.args.get("url")
        or ""
    ).strip()

    if not url:
        return jsonify(
            ok=False,
            error="Missing 'url'."
        ), 400

    try:
        names = gas_store.register_server(url, DB_PATH)

    except Exception as exc:
        return jsonify(
            ok=False,
            error=f"Registration failed: {exc}"
        ), 502

    return jsonify(
        ok=True,
        registered=names,
        count=len(names),
    )

@app.route("/store/api/gas/search")
def gas_search():
    """
    SQL-backed search over gas_store.db
    """

    if not os.path.exists(DB_PATH):
        return jsonify(
            ok=False,
            error=f"Database not found: {DB_PATH}"
        ), 503

    q = (request.args.get("q") or "").strip()

    field = (
        request.args.get("field") or ""
    ).strip().lower()

    where = []
    params = []

    if q:
        like = f"%{q}%"

        if field == "skills":

            text_parts = [
                "a.skills LIKE ?"
            ]

            params.append(like)

        elif field in _FIELD_MAP:

            cols = _FIELD_MAP[field]

            text_parts = [
                f"a.{c} LIKE ?"
                for c in cols
            ]

            params.extend(
                [like] * len(cols)
            )

        else:

            text_parts = [
                "a.agent_info LIKE ?"
            ]

            params.append(like)

        where.append(
            "(" + " OR ".join(text_parts) + ")"
        )

    for f in _BOOL_FILTERS:

        val = request.args.get(f)

        if val is not None and val != "":
            where.append(f"a.{f} = ?")
            params.append(val)

    sql = "SELECT a.name FROM agents a"

    if where:
        sql += " WHERE " + " AND ".join(where)

    sql += " ORDER BY a.name"

    conn = sqlite3.connect(DB_PATH)

    try:
        names = [
            row[0]
            for row in conn.execute(sql, params).fetchall()
        ]

    finally:
        conn.close()

    return jsonify(
        ok=True,
        query=q,
        count=len(names),
        agents=[
            {
                "name": n,
                "describeUrl": _describe_url(n),
            }
            for n in names
        ],
    )