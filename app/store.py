"""SQLite persistence: drafts, immutable published versions, run snapshots.

Every publish appends a new immutable ``rule_versions`` row whose output data
is written in the same transaction.  Readers always resolve the latest
*successful* version, so a failed publish never disturbs existing reads.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid

from .analyzer import analyze
from .errors import EngineError
from .runtime import evaluate
from .units import UNITS

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source TEXT NOT NULL,           -- 'sample' | 'csv'
    columns TEXT NOT NULL,
    rows TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rulesets (
    dataset_id TEXT PRIMARY KEY,
    fields TEXT NOT NULL,
    output TEXT NOT NULL,           -- selected output field
    fx TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_versions (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,           -- 'success' | 'failed'
    fields TEXT NOT NULL,
    fx TEXT NOT NULL,
    outputs TEXT,                   -- JSON payload of computed data
    summary TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    duration_ms REAL,
    UNIQUE(dataset_id, version)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT,
    created_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- datasets ------------------------------------------------------
    def create_dataset(self, name, columns, rows, source="sample"):
        did = uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO datasets(id,name,source,columns,rows,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (did, name, source, json.dumps(columns, ensure_ascii=False),
                 json.dumps(rows, ensure_ascii=False), now),
            )
            self._conn.commit()
        return self.get_dataset(did)

    def get_dataset(self, did):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM datasets WHERE id=?", (did,)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "source": row["source"],
            "columns": json.loads(row["columns"]),
            "rows": json.loads(row["rows"]),
            "created_at": row["created_at"],
        }

    def list_datasets(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.id,d.name,d.source,d.columns,d.rows,d.created_at,"
                "(SELECT MAX(version) FROM rule_versions v WHERE v.dataset_id=d.id "
                " AND v.status='success') AS latest "
                "FROM datasets d ORDER BY d.created_at DESC").fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["id"], "name": r["name"], "source": r["source"],
                "columns": json.loads(r["columns"]),
                "row_count": len(json.loads(r["rows"])),
                "created_at": r["created_at"],
                "latest_version": r["latest"],
            })
        return out

    def delete_dataset(self, did):
        with self._lock:
            self._conn.execute("DELETE FROM datasets WHERE id=?", (did,))
            self._conn.execute("DELETE FROM rulesets WHERE dataset_id=?", (did,))
            self._conn.execute("DELETE FROM rule_versions WHERE dataset_id=?", (did,))
            self._conn.execute("DELETE FROM events WHERE dataset_id=?", (did,))
            self._conn.commit()

    # -- drafts --------------------------------------------------------
    def get_draft(self, did):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM rulesets WHERE dataset_id=?", (did,)).fetchone()
        if row is None:
            return None
        return {
            "dataset_id": did,
            "fields": json.loads(row["fields"]),
            "output": json.loads(row["output"]),
            "fx": json.loads(row["fx"]),
            "updated_at": row["updated_at"],
        }

    def save_draft(self, did, fields, output, fx):
        with self._lock:
            self._conn.execute(
                "INSERT INTO rulesets(dataset_id,fields,output,fx,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(dataset_id) DO UPDATE SET "
                "fields=excluded.fields, output=excluded.output, "
                "fx=excluded.fx, updated_at=excluded.updated_at",
                (did, json.dumps(fields, ensure_ascii=False),
                 json.dumps(output, ensure_ascii=False),
                 json.dumps(fx, ensure_ascii=False), time.time()),
            )
            self._conn.commit()
        return self.get_draft(did)

    # -- versions ------------------------------------------------------
    def list_versions(self, did):
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,dataset_id,version,status,summary,error,created_at,"
                "duration_ms FROM rule_versions WHERE dataset_id=? "
                "ORDER BY version DESC", (did,)).fetchall()
        return [dict(r) for r in rows]

    def get_version(self, did, version):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM rule_versions WHERE dataset_id=? AND version=?",
                (did, version)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["fields"] = json.loads(data["fields"])
        data["fx"] = json.loads(data["fx"])
        data["outputs"] = json.loads(data["outputs"]) if data["outputs"] else None
        data["summary"] = json.loads(data["summary"]) if data["summary"] else None
        data["error"] = json.loads(data["error"]) if data["error"] else None
        return data

    def latest_version(self, did, success_only=True):
        sql = "SELECT MAX(version) AS v FROM rule_versions WHERE dataset_id=?"
        if success_only:
            sql += " AND status='success'"
        with self._lock:
            row = self._conn.execute(sql, (did,)).fetchone()
        if row is None or row["v"] is None:
            return None
        return self.get_version(did, row["v"])

    def next_version_number(self, did):
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM rule_versions WHERE dataset_id=?",
                (did,)).fetchone()
        return (row["v"] or 0) + 1

    # -- publish -------------------------------------------------------
    def publish(self, did, fields, fx, output, actor="web"):
        """Validate, execute and (if successful) persist a new version.

        Returns the stored version.  A failed run is recorded as a failed
        version but never becomes the latest successful read.
        """
        dataset = self.get_dataset(did)
        if dataset is None:
            raise EngineError("DATASET_NOT_FOUND", "数据集不存在")
        ruleset = {"fields": fields, "output": output, "fx": fx}
        analyzer = analyze(ruleset, fx)
        static_errors = [e.to_dict() for e in analyzer.errors]
        started = time.time()
        if static_errors:
            payload = {
                "analysis": self._analysis_payload(analyzer),
                "errors": static_errors,
                "rows": len(dataset["rows"]),
                "output": output,
            }
            return self._record_version(
                did, "failed", fields, fx, None,
                summary={"errors": len(static_errors),
                         "analysis": payload["analysis"]},
                error={"stage": "analysis", "items": static_errors},
                duration_ms=(time.time() - started) * 1000,
            )
        try:
            evaluator, _ = evaluate(ruleset, dataset, fx)
        except EngineError as err:
            return self._record_version(
                did, "failed", fields, fx, None,
                summary={"errors": 1},
                error={"stage": "runtime", "items": [err.to_dict()]},
                duration_ms=(time.time() - started) * 1000,
            )
        errors = [e.to_dict() for e in evaluator.errors]
        field_reports = list(evaluator.field_reports.values())
        if errors:
            return self._record_version(
                did, "failed", fields, fx, None,
                summary={"errors": len(errors),
                         "fields": field_reports},
                error={"stage": "runtime", "items": errors},
                duration_ms=(time.time() - started) * 1000,
            )
        outputs = self._build_outputs(dataset, evaluator, output)
        summary = {
            "errors": 0,
            "fields": field_reports,
            "analysis": self._analysis_payload(analyzer),
            "row_count": len(dataset["rows"]),
        }
        return self._record_version(
            did, "success", fields, fx, outputs, summary, None,
            duration_ms=(time.time() - started) * 1000,
        )

    def _analysis_payload(self, analyzer):
        out = []
        for name, info in analyzer.analysis.items():
            item = dict(info)
            itype = item.get("type")
            if itype:
                item["type"] = itype
            out.append({"name": name, "kind": analyzer.fields.get(name, {}).get("kind"),
                        **{k: v for k, v in item.items() if k != "name"}})
        return out

    def _build_outputs(self, dataset, evaluator, selected):
        columns = []
        for spec in evaluator.ruleset.get("fields", []):
            name = spec["name"]
            if name in evaluator.row_outputs:
                columns.append({"name": name, "kind": spec.get("kind"),
                                "unit": (evaluator.analyzer.types.get(name).symbol
                                         if evaluator.analyzer.types.get(name) else None)})
            elif name in evaluator.scalar_outputs:
                columns.append({"name": name, "kind": "aggregate",
                                "unit": evaluator.scalar_outputs[name]["unit"]})
        rows = []
        for i in range(len(dataset["rows"])):
            row = {}
            for col in columns:
                name = col["name"]
                if name in evaluator.row_outputs:
                    entry = evaluator.row_outputs[name][i]
                    row[name] = None if entry is None else entry["value"]
                else:
                    row[name] = None
            rows.append(row)
        aggregates = {name: value["value"]
                      for name, value in evaluator.scalar_outputs.items()}
        steps = {}
        for name, info in evaluator.field_reports.items():
            if info.get("status") != "ok":
                continue
            if info.get("kind") == "row":
                steps[name] = {
                    "kind": "row",
                    "unit": info.get("output_unit"),
                    "rows": evaluator._row_steps.get(name, {}),
                }
            elif info.get("kind") == "aggregate":
                steps[name] = {"kind": "aggregate",
                               "unit": info.get("output_unit"),
                               "steps": info.get("steps", [])}
        return {
            "columns": columns,
            "rows": rows,
            "aggregates": aggregates,
            "steps": steps,
            "selected": selected,
        }

    def _record_version(self, did, status, fields, fx, outputs, summary, error,
                        duration_ms):
        with self._lock:
            version = self.next_version_number(did)
            vid = uuid.uuid4().hex[:12]
            self._conn.execute(
                "INSERT INTO rule_versions(id,dataset_id,version,status,fields,"
                "fx,outputs,summary,error,created_at,duration_ms) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (vid, did, version, status,
                 json.dumps(fields, ensure_ascii=False),
                 json.dumps(fx, ensure_ascii=False),
                 json.dumps(outputs, ensure_ascii=False) if outputs else None,
                 json.dumps(summary, ensure_ascii=False) if summary else None,
                 json.dumps(error, ensure_ascii=False) if error else None,
                 time.time(), duration_ms),
            )
            self._conn.execute(
                "INSERT INTO events(dataset_id,kind,message,data,created_at) "
                "VALUES (?,?,?,?,?)",
                (did, "publish_success" if status == "success" else "publish_failed",
                 f"v{version} " + ("发布成功" if status == "success" else "发布失败"),
                 json.dumps({"version": version, "error": error},
                            ensure_ascii=False), time.time()),
            )
            self._conn.commit()
        return self.get_version(did, version)

    # -- preview -------------------------------------------------------
    def preview(self, did, fields, fx, output, base_version=None):
        """Compute a candidate ruleset without persisting anything."""
        dataset = self.get_dataset(did)
        if dataset is None:
            raise EngineError("DATASET_NOT_FOUND", "数据集不存在")
        base = None
        if base_version is not None:
            base = self.get_version(did, base_version)
        else:
            base = self.latest_version(did)
        ruleset = {"fields": fields, "output": output, "fx": fx}
        analyzer = analyze(ruleset, fx)
        result = {
            "dataset_id": did,
            "errors": [e.to_dict() for e in analyzer.errors],
            "analysis": self._analysis_payload(analyzer),
            "fields": fields,
            "fx": fx,
            "valid": not analyzer.errors,
        }
        if analyzer.errors:
            result["diff"] = self._diff_fields(
                base["fields"] if base else [], fields)
            return result
        evaluator, _ = evaluate(ruleset, dataset, fx)
        errors = [e.to_dict() for e in evaluator.errors]
        result["errors"] = errors
        result["valid"] = not errors
        result["outputs"] = self._build_outputs(dataset, evaluator, output)
        result["diff"] = self._diff_fields(base["fields"] if base else [], fields)
        result["base_version"] = base["version"] if base else None
        result["data_diff"] = self._diff_values(base, result["outputs"])
        return result

    def _diff_fields(self, old_fields, new_fields):
        old = {f["name"]: f for f in old_fields or []}
        new = {f["name"]: f for f in new_fields or []}
        added, removed, changed = [], [], []
        for name, spec in new.items():
            if name not in old:
                added.append(name)
            elif _norm(old[name]) != _norm(spec):
                changed.append({
                    "name": name,
                    "unit_from": old[name].get("unit"),
                    "unit_to": spec.get("unit"),
                    "expression_from": old[name].get("expression"),
                    "expression_to": spec.get("expression"),
                    "output_unit_from": old[name].get("output_unit"),
                    "output_unit_to": spec.get("output_unit"),
                })
        for name in old:
            if name not in new:
                removed.append(name)
        return {"added": sorted(added), "removed": sorted(removed),
                "changed": changed}

    def _diff_values(self, base, outputs):
        if base is None or not base.get("outputs") or outputs is None:
            return {"available": False}
        old = base["outputs"]
        old_cols = {c["name"] for c in old.get("columns", [])}
        new_cols = {c["name"] for c in outputs.get("columns", [])}
        changes = []
        for col in outputs.get("columns", []):
            name = col["name"]
            if name not in old_cols:
                changes.append({"field": name, "kind": "added",
                                "affected_rows": len(outputs.get("rows", []))})
                continue
            old_values = [r.get(name) for r in old.get("rows", [])]
            new_values = [r.get(name) for r in outputs.get("rows", [])]
            affected = sum(1 for a, b in zip(old_values, new_values)
                           if not _close(a, b))
            if affected:
                sample = []
                for i, (a, b) in enumerate(zip(old_values, new_values)):
                    if not _close(a, b):
                        sample.append({"row": i + 1, "from": a, "to": b})
                    if len(sample) >= 3:
                        break
                changes.append({"field": name, "kind": "changed",
                                "affected_rows": affected, "samples": sample,
                                "unit_from": _col_unit(old, name),
                                "unit_to": col.get("unit")})
        for name in sorted(old_cols - new_cols):
            changes.append({"field": name, "kind": "removed",
                            "affected_rows": len(old.get("rows", []))})
        return {"available": True, "changes": changes,
                "base_version": base["version"]}

    # -- events --------------------------------------------------------
    def log_event(self, did, kind, message, data=None):
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(dataset_id,kind,message,data,created_at) "
                "VALUES (?,?,?,?,?)",
                (did, kind, message,
                 json.dumps(data, ensure_ascii=False) if data else None,
                 time.time()),
            )
            self._conn.commit()

    def list_events(self, did, limit=30):
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,kind,message,data,created_at FROM events "
                "WHERE dataset_id=? ORDER BY id DESC LIMIT ?",
                (did, limit)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["data"] = json.loads(item["data"]) if item["data"] else None
            out.append(item)
        return out


def _norm(spec):
    return (
        spec.get("kind"), spec.get("unit"), spec.get("expression"),
        spec.get("output_unit"), spec.get("label"),
    )


def _close(a, b):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)))
    except (TypeError, ValueError):
        return a == b


def _col_unit(outputs, name):
    for col in outputs.get("columns", []):
        if col["name"] == name:
            return col.get("unit")
    return None


def validate_unit_exists(symbol):
    return symbol in UNITS
