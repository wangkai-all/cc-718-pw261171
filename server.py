"""Minimal stdlib HTTP server for the unit-aware warehouse UI."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import sample  # noqa: E402
from app.analyzer import analyze  # noqa: E402
from app.errors import EngineError  # noqa: E402
from app.store import Store  # noqa: E402
from app.units import CATEGORY_LABELS, UNITS, unit_catalog  # noqa: E402

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT_DIR, "web")
DB_PATH = os.environ.get("WAREHOUSE_DB", os.path.join(ROOT_DIR, "data", "warehouse.db"))

STORE = None


def get_store():
    global STORE
    if STORE is None:
        STORE = Store(DB_PATH)
        seed()
    return STORE


def seed():
    st = STORE
    if st.list_datasets():
        return
    ds = st.create_dataset(sample.SAMPLE_NAME, sample.SAMPLE_COLUMNS,
                           sample.SAMPLE_ROWS)
    st.save_draft(ds["id"], sample.DEFAULT_FIELDS, sample.DEFAULT_OUTPUT,
                  sample.DEFAULT_FX)
    st.publish(ds["id"], sample.DEFAULT_FIELDS, sample.DEFAULT_FX,
               sample.DEFAULT_OUTPUT)


class ApiError(Exception):
    def __init__(self, status, message, details=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


def parse_csv(text):
    """Tiny CSV/TSV parser (quoted fields supported)."""
    lines = [ln for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    if not lines:
        raise ApiError(400, "CSV 内容为空")
    delimiter = "\t" if lines[0].count("\t") > lines[0].count(",") else ","

    def split(line):
        out, buf, quoted = [], [], False
        i = 0
        while i < len(line):
            ch = line[i]
            if quoted:
                if ch == '"':
                    if i + 1 < len(line) and line[i + 1] == '"':
                        buf.append('"')
                        i += 2
                        continue
                    quoted = False
                else:
                    buf.append(ch)
            else:
                if ch == '"':
                    quoted = True
                elif ch == delimiter:
                    out.append("".join(buf))
                    buf = []
                else:
                    buf.append(ch)
            i += 1
        out.append("".join(buf))
        return out

    header = [h.strip() for h in split(lines[0])]
    if not header or any(not h for h in header):
        raise ApiError(400, "表头包含空列名")
    rows = []
    for lineno, line in enumerate(lines[1:], start=2):
        cells = split(line)
        if len(cells) > len(header):
            raise ApiError(400, f"第 {lineno} 行列数多于表头")
        cells += [""] * (len(header) - len(cells))
        rows.append(cells)
    return header, rows


class Handler(BaseHTTPRequestHandler):
    server_version = "UnitWarehouse/1.0"

    def log_message(self, fmt, *args):  # quieter logs
        if os.environ.get("WAREHOUSE_LOG"):
            super().log_message(fmt, *args)

    # -- helpers -------------------------------------------------------
    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message, details=None):
        self.send_json({"error": message, "details": details}, status)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise ApiError(400, "请求体不是合法 JSON")

    def route(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if path.startswith("/api/"):
            return self.handle_api(path, query)
        if path == "/" or path == "/index.html":
            return self.serve_file("index.html")
        if path.startswith("/static/"):
            return self.serve_file(path[len("/static/"):])
        return self.send_error_json(404, "Not found")

    def do_GET(self):
        try:
            self.route()
        except ApiError as err:
            self.send_error_json(err.status, err.message, err.details)
        except EngineError as err:
            self.send_error_json(400, err.message, err.to_dict())
        except Exception as exc:  # pragma: no cover - last resort
            import traceback
            traceback.print_exc()
            self.send_error_json(500, f"服务器错误：{exc}")

    def do_POST(self):
        self.do_GET()

    def do_PUT(self):
        self.do_GET()

    def do_DELETE(self):
        self.do_GET()

    def serve_file(self, name):
        safe = os.path.normpath(name).lstrip("/\\")
        if safe.startswith(".."):
            return self.send_error_json(403, "Forbidden")
        path = os.path.join(WEB_DIR, safe)
        if not os.path.isfile(path):
            return self.send_error_json(404, "Not found")
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype in ("application/javascript", "application/json") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # -- api -----------------------------------------------------------
    def handle_api(self, path, query):
        st = get_store()
        parts = [p for p in path.split("/") if p][1:]  # drop 'api'

        if parts == ["health"]:
            return self.send_json({"ok": True})
        if parts == ["units"]:
            return self.send_json({
                "units": unit_catalog(),
                "categories": CATEGORY_LABELS,
                "dimensions": {
                    "length": "长度", "mass": "质量", "time": "时间",
                    "current": "电流", "temperature": "温度",
                    "amount": "物质的量", "luminosity": "发光强度",
                    "count": "数量", "data": "数据量",
                },
            })
        if parts == ["datasets"]:
            if self.command == "GET":
                return self.send_json({"datasets": st.list_datasets()})
            if self.command == "POST":
                body = self.read_body()
                name = (body.get("name") or "").strip() or "未命名数据集"
                csv_text = body.get("csv")
                if not csv_text:
                    raise ApiError(400, "请粘贴 CSV 内容或上传文件")
                columns, rows = parse_csv(csv_text)
                ds = st.create_dataset(name, columns, rows, source="csv")
                fields = [{"name": c, "kind": "raw", "unit": "", "label": c}
                          for c in columns]
                st.save_draft(ds["id"], fields, None, {})
                st.log_event(ds["id"], "dataset_created",
                             f"导入数据集 {name}（{len(rows)} 行）",
                             {"columns": columns})
                return self.send_json({"dataset": ds}, 201)
            raise ApiError(405, "Method not allowed")

        m = re.match(r"^datasets/([^/]+)(?:/(.*))?$", "/".join(parts))
        if m:
            did, rest = m.group(1), (m.group(2) or "")
            dataset = st.get_dataset(did)
            if dataset is None:
                raise ApiError(404, "数据集不存在")
            sub = [p for p in rest.split("/") if p]
            if not sub:
                if self.command == "GET":
                    return self.send_json({
                        "dataset": {
                            "id": dataset["id"], "name": dataset["name"],
                            "columns": dataset["columns"],
                            "row_count": len(dataset["rows"]),
                            "source": dataset["source"],
                        },
                        "draft": st.get_draft(did),
                    })
                if self.command == "DELETE":
                    st.delete_dataset(did)
                    return self.send_json({"ok": True})
            if sub == ["preview"] and self.command == "POST":
                body = self.read_body()
                return self.send_json(st.preview(
                    did, body.get("fields") or [],
                    body.get("fx") or {},
                    body.get("output"),
                    body.get("base_version")))
            if sub == ["publish"] and self.command == "POST":
                body = self.read_body()
                fields = body.get("fields") or []
                fx = body.get("fx") or {}
                output = body.get("output")
                st.save_draft(did, fields, output, fx)
                version = st.publish(did, fields, fx, output)
                status = 200 if version["status"] == "success" else 422
                return self.send_json({"version": version}, status)
            if sub == ["analyze"] and self.command == "POST":
                body = self.read_body()
                analyzer = analyze({"fields": body.get("fields") or []},
                                   body.get("fx") or {})
                return self.send_json({
                    "errors": [e.to_dict() for e in analyzer.errors],
                    "analysis": [dict(v, name=k) for k, v in
                                 analyzer.analysis.items()],
                })
            if sub == ["draft"]:
                if self.command == "GET":
                    return self.send_json({"draft": st.get_draft(did)})
                if self.command == "PUT" or self.command == "POST":
                    body = self.read_body()
                    draft = st.save_draft(
                        did, body.get("fields") or [],
                        body.get("output"), body.get("fx") or {})
                    return self.send_json({"draft": draft})
            if sub == ["versions"]:
                if self.command == "GET":
                    return self.send_json({"versions": st.list_versions(did)})
            if len(sub) == 2 and sub[0] == "versions" and self.command == "GET":
                try:
                    num = int(sub[1])
                except ValueError:
                    raise ApiError(400, "版本号必须是整数")
                version = st.get_version(did, num)
                if version is None:
                    raise ApiError(404, "版本不存在")
                return self.send_json({"version": version})
            if sub == ["latest"] and self.command == "GET":
                latest = st.latest_version(did)
                if latest is None:
                    return self.send_json({"version": None})
                return self.send_json({"version": latest})
            if sub == ["events"] and self.command == "GET":
                return self.send_json({"events": st.list_events(did)})
        raise ApiError(404, "Not found")


def create_server(host="127.0.0.1", port=0, db_path=None):
    global DB_PATH, STORE
    if db_path:
        DB_PATH = db_path
        STORE = None
    get_store()
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd


def main(argv=None):
    parser = argparse.ArgumentParser(description="量纲检查分析仓")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=None)
    args = parser.parse_args(argv)
    if args.db:
        global DB_PATH, STORE
        DB_PATH = args.db
        STORE = None
    get_store()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"量纲检查分析仓已启动： http://{args.host}:{httpd.server_port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
