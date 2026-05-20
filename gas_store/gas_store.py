"""Client helpers for the Geospatial Agentic Services (GAS) store.

Uses only the Python standard library (no third-party dependencies).

Endpoints follow the OGC-style KVP convention:
    ?SERVICE=GAS&VERSION=1.0.0&REQUEST=<request>[&name=<agent>]
"""

import datetime
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://www.geospatial-agentic-services.online/"
DEFAULT_DB_PATH = "gas_store.db"


def _fetch_json(base_url, params, timeout):
    """Build a GAS KVP URL, GET it, and return the parsed JSON."""
    url = base_url.rstrip("/") + "/?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError("HTTP %s for %s" % (resp.status, url))
        return json.loads(resp.read().decode("utf-8"))


def get_capabilities(base_url=DEFAULT_BASE_URL, timeout=30):
    """Fetch the GAS GetCapabilities document and return the list of agents.

    Returns a list of dicts: [{"name": ..., "describeUrl": ...}, ...]
    """
    doc = _fetch_json(base_url, {
        "SERVICE": "GAS",
        "VERSION": "1.0.0",
        "REQUEST": "GetCapabilities",
    }, timeout)
    out = []
    for agent in doc.get("agents", []):
        # New schema: agent_id (identifier) + DescribeAgent (link).
        # Legacy schema: name (identifier) + describeUrl.
        identifier = agent.get("agent_id") or agent.get("name")
        describe_url = (agent.get("DescribeAgent")
                        or agent.get("describeUrl")
                        or agent.get("describe_url"))
        out.append({"name": identifier, "describeUrl": describe_url})
    return out


def describe_agent(agent_name, base_url=DEFAULT_BASE_URL, timeout=30):
    """Retrieve the complete DescribeAgent profile for a GAS agent.

    Returns the parsed JSON as a dict with keys:
    profile, skills, operations, provenance_and_reproducibility,
    governance, extensions.
    """
    return _fetch_json(base_url, {
        "SERVICE": "GAS",
        "VERSION": "1.0.0",
        "REQUEST": "DescribeAgent",
        "agent_id": agent_name,
    }, timeout)


def save_agent(agent_name, path=None, base_url=DEFAULT_BASE_URL, timeout=30):
    """Fetch an agent's DescribeAgent profile and write it to a JSON file.

    If ``path`` is omitted, the file is written as ``<agent_name>.json``
    in the current directory. Returns the path that was written.
    """
    if path is None:
        path = agent_name + ".json"
    profile = describe_agent(agent_name, base_url=base_url, timeout=timeout)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(profile, fh, indent=2, ensure_ascii=False)
    return path


# ---------------------------------------------------------------------------
# SQLite storage (one row per agent, full profile kept as a JSON blob)
# ---------------------------------------------------------------------------

# Extracted columns stored as INTEGER. Anything else returned by
# _extracted_columns() defaults to TEXT.
_INTEGER_COLUMNS = {"skill_count", "operation_count"}


def _extracted_column_names():
    """Names of the extracted columns, in stable insertion order.

    Derived from _extracted_columns() (called with an empty profile) so the
    schema and the INSERT statement are always generated from one place --
    add a key there and it flows into the table and the upsert automatically.
    """
    return list(_extracted_columns({}).keys())


def init_db(db_path=DEFAULT_DB_PATH):
    """Create the SQLite database/table if it does not exist.

    The extracted columns between ``describe_url`` and ``fetched_at`` are
    generated from _extracted_column_names(), so the schema stays in sync
    with the extraction logic. Returns an open sqlite3.Connection
    (caller is responsible for closing).
    """
    extracted_ddl = ",\n            ".join(
        "%s %s" % (col, "INTEGER" if col in _INTEGER_COLUMNS else "TEXT")
        for col in _extracted_column_names()
    )
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agents (
            name TEXT PRIMARY KEY,
            describe_url TEXT,
            %s,
            fetched_at TEXT NOT NULL,
            agent_info TEXT NOT NULL
        )
        """ % extracted_ddl
    )
    conn.commit()
    return conn


def _extracted_columns(agent_info):
    """Pull selected fields out of a DescribeAgent profile for flat columns.

    Returns a dict keyed by column name. Missing nested keys yield None
    rather than raising, so schema changes upstream won't break ingestion.
    """
    prof = agent_info.get("profile", {}) if isinstance(agent_info, dict) else {}
    governance = agent_info.get("governance") if isinstance(agent_info, dict) else None
    prov_and_rep = agent_info.get("provenance_and_reproducibility", {}) if isinstance(agent_info, dict) else {}
    provider = prof.get("provider", {}) or {}
    # New schema uses provider.contacts (array); legacy used provider.contact (object).
    contacts = provider.get("contacts")
    if isinstance(contacts, list) and contacts:
        contact = contacts[0] or {}
    else:
        contact = provider.get("contact", {}) or {}
    skills = agent_info.get("skills") or []
    keywords = agent_info.get("keywords") or []

    # New schema exposes a single execute_task object; legacy exposed an
    # operations array. Normalize both into an operations list so the
    # downstream counts / names work unchanged.
    execute_task = agent_info.get("execute_task")
    if isinstance(execute_task, dict) and execute_task:
        operations = [execute_task]
    else:
        operations = agent_info.get("operations") or []

    extensions = agent_info.get("extensions") or {}
    conformance = agent_info.get("conformance") or {}
    provenance = prov_and_rep.get("provenance") or {}
    reproducibility = prov_and_rep.get("reproducibility") or {}
    validation = prov_and_rep.get("validation") or {}

    return {
        "agent_id": prof.get("agent_id"),
        "description": prof.get("description"),
        "version": prof.get("version"),
        "agent_base_url": prof.get("base_url"),
        "last_updated": prof.get("last_updated"),
        "default_model": prof.get("default_model"),
        "provider_name": provider.get("name"),
        "provider_website": provider.get("website"),
        "contact_name": contact.get("name"),
        "contact_email": contact.get("email"),
        "keywords": ", ".join(k for k in keywords if k) or None,
        "skill_count": len(skills),
        "operation_count": len(operations),
        "governance": json.dumps(governance, ensure_ascii=False) if governance is not None else None,
        "provenance": json.dumps(prov_and_rep.get("provenance"), ensure_ascii=False) if prov_and_rep.get("provenance") is not None else None,

        # Flat, query-able columns (use LIKE / WHERE without json_extract).
        "operation_names": ", ".join(
            op.get("name", "") for op in operations if op.get("name")
        ) or None,
        "skill_names": "; ".join(
            s.get("name", "") for s in skills if s.get("name")
        ) or None,
        "skill_descriptions": " | ".join(
            s.get("description", "") for s in skills if s.get("description")
        ) or None,
        # Full structured skills array as JSON -- query per-skill with
        # SQLite json_each() / json_extract() (handles N skills cleanly).
        "skills": json.dumps(skills, ensure_ascii=False) if skills else None,
        "operations": json.dumps(operations, ensure_ascii=False) if operations else None,
        "extensions_description": extensions.get("description")
        if isinstance(extensions, dict) else None,
        "conformance_gas_version": conformance.get("gas_version")
        if isinstance(conformance, dict) else None,
        "provenance_supported": provenance.get("supported"),
        "reproducibility_supported": reproducibility.get("supported"),
        "validation_supported": validation.get("supported"),
    }


def save_agent_to_db(agent_name, db_path=DEFAULT_DB_PATH, describe_url=None,
                     base_url=DEFAULT_BASE_URL, timeout=30):
    """Fetch one agent's profile and upsert it into the SQLite database.

    Re-running for the same agent replaces the existing row (latest wins).
    Returns the agent name that was stored.
    """
    agent_info = describe_agent(agent_name, base_url=base_url, timeout=timeout)
    fetched_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    cols = _extracted_columns(agent_info)

    # Build the column list / values in the same order as the schema:
    # name, describe_url, <extracted...>, fetched_at, profile.
    extracted_names = list(cols.keys())
    columns = ["name", "describe_url"] + extracted_names + ["fetched_at",
                                                            "agent_info"]
    values = ([agent_name, describe_url]
              + [cols[name] for name in extracted_names]
              + [fetched_at, json.dumps(agent_info, ensure_ascii=False)])

    placeholders = ", ".join(["?"] * len(columns))
    # Update every column except the primary key on conflict.
    updates = ", ".join("%s = excluded.%s" % (c, c)
                        for c in columns if c != "name")
    sql = ("INSERT INTO agents (%s) VALUES (%s) "
           "ON CONFLICT(name) DO UPDATE SET %s"
           % (", ".join(columns), placeholders, updates))

    conn = init_db(db_path)
    try:
        conn.execute(sql, values)
        conn.commit()
    finally:
        conn.close()
    return agent_name


def save_all_agents_to_db(db_path=DEFAULT_DB_PATH, base_url=DEFAULT_BASE_URL,
                          timeout=30):
    """Store every agent listed by GetCapabilities into the database.

    Returns the list of agent names that were stored.
    """
    stored = []
    for agent in get_capabilities(base_url=base_url, timeout=timeout):
        save_agent_to_db(agent["name"], db_path=db_path,
                         describe_url=agent.get("describeUrl"),
                         base_url=base_url, timeout=timeout)
        stored.append(agent["name"])
    return stored


def register_server(capabilities_url, db_path=DEFAULT_DB_PATH, timeout=30):
    """Register every agent published by a GAS server into the database.

    ``capabilities_url`` is a full GetCapabilities URL (e.g. the one a user
    pastes into the Register dialog). The base URL -- everything before the
    query string -- is used to fetch the agent list and each DescribeAgent.
    Existing agents are updated in place (upsert); nothing is deleted.
    Returns the list of registered agent names.
    """
    base_url = (capabilities_url or "").split("?", 1)[0].strip()
    if not base_url:
        base_url = DEFAULT_BASE_URL
    return save_all_agents_to_db(db_path=db_path, base_url=base_url,
                                 timeout=timeout)


def recreate_db(db_path=DEFAULT_DB_PATH, base_url=DEFAULT_BASE_URL,
                timeout=30):
    """Delete any existing database file and rebuild it from scratch.

    ``init_db`` uses CREATE TABLE IF NOT EXISTS, so an existing file keeps
    its old columns even after the schema in code changes. Call this after
    a schema change to drop the stale file and re-fetch every agent into
    the current schema. Returns the list of agent names that were stored.
    """
    if os.path.exists(db_path):
        os.remove(db_path)
    return save_all_agents_to_db(db_path=db_path, base_url=base_url,
                                 timeout=timeout)


def reset_db_with(agent_names, db_path=DEFAULT_DB_PATH,
                  base_url=DEFAULT_BASE_URL, timeout=30):
    """Delete the database and rebuild it containing ONLY the given agents.

    ``agent_names`` may be a single name (str) or an iterable of names.
    Useful for checking that a consumer (e.g. index.html via gas_api.py)
    really reads from the database: shrink it to one agent and confirm
    only that agent shows up. Returns the list of stored agent names.
    """
    if isinstance(agent_names, str):
        agent_names = [agent_names]
    agent_names = list(agent_names)
    if os.path.exists(db_path):
        os.remove(db_path)
    for name in agent_names:
        save_agent_to_db(name, db_path=db_path, base_url=base_url,
                         timeout=timeout)
    return agent_names


def load_agent_from_db(agent_name, db_path=DEFAULT_DB_PATH):
    """Return the stored profile dict for one agent, or None if not present."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT profile FROM agents WHERE name = ?", (agent_name,)
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def show_db(db_path=DEFAULT_DB_PATH):
    """Print a summary of the stored agents (every column except the
    full ``profile`` blob). Returns the number of rows shown.
    """
    conn = sqlite3.connect(db_path)
    try:
        cols = [d[1] for d in conn.execute("PRAGMA table_info(agents)")
                if d[1] != "profile"]
        rows = conn.execute(
            "SELECT %s FROM agents ORDER BY name" % ", ".join(cols)
        ).fetchall()
    finally:
        conn.close()

    print("%s  (%d agents, columns: %s)" % (db_path, len(rows),
                                            ", ".join(cols)))
    for row in rows:
        print("-" * 70)
        for col, val in zip(cols, row):
            print("  %-16s %s" % (col + ":", val))
    return len(rows)


if __name__ == "__main__":
    # Usage:
    #   python gas_store.py                 -> list agents + print one profile
    #   python gas_store.py --recreate      -> rebuild gas_store.db from scratch
    #   python gas_store.py --recreate PATH -> rebuild the DB at PATH
    #   python gas_store.py --show          -> print stored agents
    #   python gas_store.py --show PATH     -> print stored agents from PATH
    #   python gas_store.py --test                  -> ONLY data_retriever_agent
    #   python gas_store.py --test AGENT            -> ONLY AGENT
    #   python gas_store.py --test AGENT1 AGENT2    -> ONLY those agents
    #   python gas_store.py --test A1,A2            -> comma-separated also OK
    #   any argument ending in .db is taken as the database path
    if "--test" in sys.argv:
        idx = sys.argv.index("--test")
        rest = sys.argv[idx + 1:]
        # Any arg ending in .db is the database path; the rest are agents.
        db_args = [a for a in rest if a.endswith(".db")]
        db_path = db_args[0] if db_args else DEFAULT_DB_PATH
        agent_tokens = [a for a in rest if not a.endswith(".db")]
        # Accept both "a1 a2" and "a1,a2" (and "a1, a2").
        agents = [name.strip()
                  for token in agent_tokens
                  for name in token.split(",")
                  if name.strip()]
        if not agents:
            agents = ["data_retriever_agent"]
        reset_db_with(agents, db_path)
        conn = sqlite3.connect(db_path)
        try:
            stored = [r[0] for r in
                      conn.execute("SELECT name FROM agents ORDER BY name")]
        finally:
            conn.close()
        print("Test DB %s now contains %d agent(s): %s"
              % (db_path, len(stored), ", ".join(stored)))
        print("Reload the page (served by gas_api.py) -- only these "
              "card(s) should appear, proving it reads from the database.")
        sys.exit(0)

    if "--recreate" in sys.argv:
        idx = sys.argv.index("--recreate")
        db_path = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else DEFAULT_DB_PATH
        names = recreate_db(db_path)
        print("Recreated %s with %d agents:" % (db_path, len(names)))
        for n in names:
            print("  " + n)
        sys.exit(0)

    if "--show" in sys.argv:
        idx = sys.argv.index("--show")
        db_path = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else DEFAULT_DB_PATH
        show_db(db_path)
        sys.exit(0)

    agents = get_capabilities()
    print("Available agents (%d):" % len(agents))
    for agent in agents:
        print("  %s -> %s" % (agent["name"], agent["describeUrl"]))

    if agents:
        # first = agents[0]["name"]
        first = "data_retriever_agent"
        print("\nFull profile for %s:" % first)
                
        print(json.dumps(describe_agent(first), indent=2))
