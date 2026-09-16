from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from test_dashboard_core import make_db  # noqa: E402
from test_server import ServerHarness, _request  # noqa: E402

import server  # noqa: E402

ENTRY_KEYS = {"name", "db_path", "size_bytes", "memory_count"}


def _seed_db(path: Path, rows: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE working_memory (id TEXT PRIMARY KEY, content TEXT NOT NULL)")
    for i in range(rows):
        con.execute("INSERT INTO working_memory(id, content) VALUES (?, ?)", (f"w{i}", f"memory {i}"))
    con.commit()
    con.close()
    return path


def _foreign_db(path: Path) -> Path:
    """A valid SQLite file WITHOUT a working_memory table."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE not_mnemosyne (id TEXT PRIMARY KEY)")
    con.execute("INSERT INTO not_mnemosyne(id) VALUES ('x')")
    con.commit()
    con.close()
    return path


def _full_schema_seed(path: Path, rows: int) -> Path:
    """make_db-compatible seed so store endpoints (e.g. /api/stats) work."""
    path.parent.mkdir(parents=True, exist_ok=True)
    make_db(path)
    return path


def _build_tree(root: Path, spec: dict[str, int], seed=_seed_db) -> dict[str, Path]:
    """Build a fixture Hermes tree from layout keys and return {profile_name: db_path}.

    Keys: "default", "bank:<name>", "profile:<name>", "profile-bank:<profile>:<bank>", "shared".
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "profiles").mkdir(parents=True, exist_ok=True)
    dbs: dict[str, Path] = {}
    for key, rows in spec.items():
        if key == "default":
            dbs["default"] = seed(root / "mnemosyne" / "data" / "mnemosyne.db", rows)
        elif key == "shared":
            seed(root / "data" / "shared" / "mnemosyne.db", rows)
        elif key.startswith("bank:"):
            name = key.split(":", 1)[1]
            dbs[name] = seed(root / "mnemosyne" / "data" / "banks" / name / "mnemosyne.db", rows)
        elif key.startswith("profile:"):
            name = key.split(":", 1)[1]
            dbs[name] = seed(root / "profiles" / name / "mnemosyne" / "data" / "mnemosyne.db", rows)
        elif key.startswith("profile-bank:"):
            _, profile, bank = key.split(":", 2)
            dbs[bank] = seed(root / "profiles" / profile / "mnemosyne" / "data" / "banks" / bank / "mnemosyne.db", rows)
        else:
            raise ValueError(f"unknown spec key: {key}")
    return dbs


def _discover(monkeypatch, root: Path) -> list[dict[str, Any]]:
    monkeypatch.setenv("HERMES_HOME", str(root))
    return server.discover_profiles()


def test_discovery_default_only(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    _build_tree(root, {"default": 4})
    result = _discover(monkeypatch, root)
    assert [p["name"] for p in result] == ["default"]
    entry = result[0]
    assert set(entry) == ENTRY_KEYS
    assert entry["db_path"] == str(root / "mnemosyne" / "data" / "mnemosyne.db")
    assert entry["memory_count"] == 4
    assert entry["size_bytes"] > 0


def test_discovery_top_level_bank_l4(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    _build_tree(root, {"bank:zeus": 3})
    result = _discover(monkeypatch, root)
    assert [p["name"] for p in result] == ["zeus"]
    assert result[0]["db_path"] == str(root / "mnemosyne" / "data" / "banks" / "zeus" / "mnemosyne.db")
    assert result[0]["memory_count"] == 3


def test_discovery_profile_direct_l3(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    _build_tree(root, {"profile:atlas": 2})
    result = _discover(monkeypatch, root)
    assert [p["name"] for p in result] == ["atlas"]
    assert result[0]["db_path"] == str(root / "profiles" / "atlas" / "mnemosyne" / "data" / "mnemosyne.db")
    assert result[0]["memory_count"] == 2


def test_discovery_profile_banks_l2_matching_name(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    _build_tree(root, {"profile-bank:bricks:bricks": 5})
    result = _discover(monkeypatch, root)
    assert [p["name"] for p in result] == ["bricks"]
    assert result[0]["db_path"] == str(root / "profiles" / "bricks" / "mnemosyne" / "data" / "banks" / "bricks" / "mnemosyne.db")
    assert result[0]["memory_count"] == 5


def test_discovery_profile_banks_l2_bank_name_differs_from_profile(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    _build_tree(root, {"profile-bank:coder:coder-bank": 1})
    result = _discover(monkeypatch, root)
    assert [p["name"] for p in result] == ["coder-bank"]
    assert result[0]["db_path"] == str(root / "profiles" / "coder" / "mnemosyne" / "data" / "banks" / "coder-bank" / "mnemosyne.db")
    assert result[0]["memory_count"] == 1


def test_discovery_excludes_db_without_working_memory_table(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    _build_tree(root, {"default": 2, "bank:zeus": 1})
    _foreign_db(root / "mnemosyne" / "data" / "banks" / "fake" / "mnemosyne.db")
    _foreign_db(root / "profiles" / "ghost" / "mnemosyne" / "data" / "mnemosyne.db")
    result = _discover(monkeypatch, root)
    assert [p["name"] for p in result] == ["default", "zeus"]


def test_discovery_skips_non_directory_entries_under_profiles(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    _build_tree(root, {"default": 1})
    (root / "profiles" / "stray-file.txt").write_text("not a profile dir")
    (root / "profiles" / "broken-link").symlink_to(root / "profiles" / "does-not-exist")
    result = _discover(monkeypatch, root)
    assert [p["name"] for p in result] == ["default"]


def test_discovery_dedups_db_reachable_via_two_layouts(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    _build_tree(root, {"default": 2})
    # Symlink the profile's data dir at the default data dir so the same file
    # is reachable via both L1 (default) and L3 (profile direct).
    link = root / "profiles" / "zeus" / "mnemosyne" / "data"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(root / "mnemosyne" / "data")
    result = _discover(monkeypatch, root)
    assert [p["name"] for p in result] == ["default"]
    assert all(p["db_path"] == str(root / "mnemosyne" / "data" / "mnemosyne.db") for p in result)


def test_discovery_excludes_shared_db(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    _build_tree(root, {"default": 2, "shared": 7})
    result = _discover(monkeypatch, root)
    assert [p["name"] for p in result] == ["default"]
    assert all("data/shared" not in p["db_path"] for p in result)


def test_discovery_combined_tree_yields_each_profile_once_with_counts(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    _build_tree(root, {"default": 4, "bank:zeus": 3, "profile:atlas": 2, "profile-bank:bricks:bricks": 5})
    result = _discover(monkeypatch, root)
    assert [p["name"] for p in result] == ["atlas", "bricks", "default", "zeus"]
    by_name = {p["name"]: p for p in result}
    assert by_name["default"]["memory_count"] == 4
    assert by_name["zeus"]["memory_count"] == 3
    assert by_name["atlas"]["memory_count"] == 2
    assert by_name["bricks"]["memory_count"] == 5
    assert by_name["zeus"]["db_path"] == str(root / "mnemosyne" / "data" / "banks" / "zeus" / "mnemosyne.db")
    assert by_name["atlas"]["db_path"] == str(root / "profiles" / "atlas" / "mnemosyne" / "data" / "mnemosyne.db")
    assert by_name["bricks"]["db_path"] == str(root / "profiles" / "bricks" / "mnemosyne" / "data" / "banks" / "bricks" / "mnemosyne.db")
    for entry in result:
        assert set(entry) == ENTRY_KEYS


def test_hermes_root_resolves_profile_subdirectory(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    (root / "profiles" / "zeus").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "zeus"))
    assert server._hermes_root() == root


def test_hermes_root_accepts_root_directly(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    (root / "profiles").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    assert server._hermes_root() == root


def test_profiles_endpoint_returns_discovered_profiles(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    dbs = _build_tree(root, {"default": 2, "bank:zeus": 3})
    harness = ServerHarness(tmp_path, monkeypatch, db=dbs["default"], hermes_home=root)
    try:
        status, _headers, body = _request(f"{harness.base}/api/profiles")
        payload = json.loads(body)
        assert status == 200
        assert set(payload) == {"profiles"}
        assert [p["name"] for p in payload["profiles"]] == ["default", "zeus"]
        for p in payload["profiles"]:
            assert set(p) == ENTRY_KEYS
    finally:
        harness.close()


def test_active_endpoint_reports_name_of_active_db(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    (root / "profiles").mkdir(parents=True, exist_ok=True)
    default_db = root / "mnemosyne" / "data" / "mnemosyne.db"
    default_db.parent.mkdir(parents=True, exist_ok=True)
    make_db(default_db)
    zeus_db = root / "mnemosyne" / "data" / "banks" / "zeus" / "mnemosyne.db"
    zeus_db.parent.mkdir(parents=True, exist_ok=True)
    make_db(zeus_db)
    harness = ServerHarness(tmp_path, monkeypatch, db=zeus_db, hermes_home=root)
    try:
        status, _headers, body = _request(f"{harness.base}/api/profile/active")
        payload = json.loads(body)
        assert status == 200
        assert payload["name"] == "zeus"
        assert payload["db_path"] == str(zeus_db)
    finally:
        harness.close()


def test_active_endpoint_reports_unknown_when_active_db_not_discovered(tmp_path, monkeypatch):
    # Bank-pinned-instance failure mode: the running DB sits outside the
    # HERMES_HOME tree, so discovery cannot name it.
    harness = ServerHarness(tmp_path, monkeypatch)
    try:
        status, _headers, body = _request(f"{harness.base}/api/profile/active")
        payload = json.loads(body)
        assert status == 200
        assert payload["name"] == "unknown"
        assert payload["db_path"] == str(harness.db)
    finally:
        harness.close()


def _enable_admin(harness: ServerHarness) -> None:
    status, _headers, _body = _request(
        f"{harness.base}/api/config",
        method="POST",
        body={"host": "127.0.0.1", "memory_admin_enabled": True},
    )
    assert status == 200


def test_switch_endpoint_repoints_server_and_stats_follow(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    # Full schema so the store endpoints (e.g. /api/stats) work after the switch.
    dbs = _build_tree(root, {"default": 4, "bank:zeus": 4}, seed=_full_schema_seed)
    harness = ServerHarness(tmp_path, monkeypatch, db=dbs["default"], hermes_home=root)
    try:
        _enable_admin(harness)
        status, _headers, body = _request(f"{harness.base}/api/profile/switch", method="POST", body={"name": "zeus"})
        payload = json.loads(body)
        assert status == 200
        assert payload["ok"] is True
        assert payload["profile"]["name"] == "zeus"
        assert harness.httpd.db_path == dbs["zeus"]
        status, _headers, body = _request(f"{harness.base}/api/stats")
        assert status == 200
        assert json.loads(body)["db_path"] == str(dbs["zeus"])
    finally:
        harness.close()


def test_switch_endpoint_requires_admin_mode(tmp_path, monkeypatch):
    harness = ServerHarness(tmp_path, monkeypatch)
    try:
        status, _headers, body = _request(f"{harness.base}/api/profile/switch", method="POST", body={"name": "default"})
        assert status == 403
        assert b"admin mode is disabled" in body
        # Read-only profile endpoints stay behind plain auth, not admin.
        status, _headers, body = _request(f"{harness.base}/api/profiles")
        assert status == 200
    finally:
        harness.close()


def test_switch_endpoint_rejects_empty_name(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    dbs = _build_tree(root, {"default": 2})
    harness = ServerHarness(tmp_path, monkeypatch, db=dbs["default"], hermes_home=root)
    try:
        _enable_admin(harness)
        status, _headers, body = _request(f"{harness.base}/api/profile/switch", method="POST", body={"name": "   "})
        assert status == 400
        assert b"profile name required" in body
        status, _headers, body = _request(f"{harness.base}/api/profile/switch", method="POST", body={})
        assert status == 400
    finally:
        harness.close()


def test_switch_endpoint_unknown_profile_returns_404(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    dbs = _build_tree(root, {"default": 2, "bank:zeus": 3})
    harness = ServerHarness(tmp_path, monkeypatch, db=dbs["default"], hermes_home=root)
    try:
        _enable_admin(harness)
        status, _headers, body = _request(f"{harness.base}/api/profile/switch", method="POST", body={"name": "phantom"})
        assert status == 404
        assert b"not found" in body
        assert harness.httpd.db_path == dbs["default"]
    finally:
        harness.close()


class _ConnectionLedger:
    def __init__(self):
        self.opened = 0
        self.closed = 0


class _TrackedConnection:
    def __init__(self, inner: sqlite3.Connection, ledger: _ConnectionLedger):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_ledger", ledger)
        ledger.opened += 1

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)

    def close(self):
        self._ledger.closed += 1
        self._inner.close()


def test_probe_connections_are_closed_on_success_and_error_paths(tmp_path, monkeypatch):
    # #18 leak regression: every sqlite3.connect issued by the discovery probes
    # must be closed on BOTH the success path and the error paths.
    ledger = _ConnectionLedger()
    real_connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        return _TrackedConnection(real_connect(*args, **kwargs), ledger)

    monkeypatch.setattr(server.sqlite3, "connect", tracked_connect)

    good_db = _seed_db(tmp_path / "good" / "mnemosyne.db", 3)
    no_table_db = _foreign_db(tmp_path / "no-table" / "mnemosyne.db")
    corrupt_db = tmp_path / "corrupt" / "mnemosyne.db"
    corrupt_db.parent.mkdir(parents=True, exist_ok=True)
    corrupt_db.write_bytes(b"this is not a sqlite database at all")
    missing_db = tmp_path / "missing" / "mnemosyne.db"

    # Success paths.
    ledger.opened = ledger.closed = 0
    assert server._is_valid_mnemosyne_db(good_db) is True
    assert (ledger.opened, ledger.closed) == (1, 1)
    ledger.opened = ledger.closed = 0
    assert server._count_working_memory(good_db) == 3
    assert (ledger.opened, ledger.closed) == (1, 1)

    # Error path: valid SQLite file but no working_memory table.
    ledger.opened = ledger.closed = 0
    assert server._is_valid_mnemosyne_db(no_table_db) is False
    assert (ledger.opened, ledger.closed) == (1, 1)
    ledger.opened = ledger.closed = 0
    assert server._count_working_memory(no_table_db) == 0
    assert (ledger.opened, ledger.closed) == (1, 1)

    # Error path: corrupt file (probe fails after connect).
    ledger.opened = ledger.closed = 0
    assert server._is_valid_mnemosyne_db(corrupt_db) is False
    assert ledger.closed == ledger.opened
    ledger.opened = ledger.closed = 0
    assert server._count_working_memory(corrupt_db) == 0
    assert ledger.closed == ledger.opened

    # Error path: non-existent file.
    ledger.opened = ledger.closed = 0
    assert server._is_valid_mnemosyne_db(missing_db) is False
    assert (ledger.opened, ledger.closed) == (0, 0)
    ledger.opened = ledger.closed = 0
    assert server._count_working_memory(missing_db) == 0
    assert ledger.closed == ledger.opened
