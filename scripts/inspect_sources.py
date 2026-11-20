#!/usr/bin/env python3
"""Run live, read-only MCP inspections and preserve each protocol result."""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import traceback
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
MAIN_BIN = Path.home() / "Desktop" / "info_extractor" / ".venv" / "Scripts"
SOURCES = [
    ("neshan-mcp", "neshan-mcp", "NESHAN_MCP_PROXY"),
    ("balad-mcp", "balad-mcp", "BALAD_MCP_PROXY"),
    ("divar-mcp", "divar-mcp", None),
    ("sheypoor-mcp", "sheypoor-mcp", None),
    ("basalam-mcp", "basalam-mcp", "BASALAM_MCP_PROXY"),
    ("emalls-ir-mcp", "emalls-ir-mcp", "EMALLS_MCP_PROXY"),
    ("rasmio-mcp", "rasmio-mcp", "RASMIO_MCP_PROXY"),
    ("iran-jobs-mcp", "iran-jobs-mcp", "IRAN_JOBS_MCP_PROXY"),
    ("torob-mcp", "torob-mcp", "TOROB_MCP_PROXY"),
    ("digikala-mcp", "digikala-mcp", "DIGIKALA_MCP_PROXY"),
]
VALUES = {
    "query": "کافه", "q": "کافه", "text": "کافه", "keyword": "کافه",
    "term": "کافه", "search": "کافه", "search_term": "کافه",
    "search_query": "کافه", "what": "کافه", "name": "کافه",
    "company": "شرکت فناوری", "company_name": "شرکت فناوری",
    "activity": "فناوری اطلاعات", "product": "گوشی موبایل",
    "category": "کافه", "city": "تهران", "province": "تهران",
    "near": "Tehran", "site": "all", "kind": "all",
    "page": 1, "page_number": 1, "page_no": 1, "limit": 5, "size": 5,
    "page_size": 5, "per_page": 5, "lat": 35.6892, "latitude": 35.6892,
    "lng": 51.3890, "lon": 51.3890, "longitude": 51.3890,
    "from_lat": 35.6892, "from_lng": 51.3890, "to_lat": 35.7219,
    "to_lng": 51.3347, "open_now": False,
    "coordinates": "35.6892,51.3890", "location": "35.6892,51.3890",
}
ID_KEYS = {"id", "place_id", "poi_id", "location_id", "product_id", "item_id",
           "seller_id", "shop_id", "vendor_id", "company_id", "national_id",
           "slug", "uuid", "uid", "code", "productid", "sellerid", "shopid"}
ID_KEYS_NORMALIZED = {re.sub(r"[^a-z0-9]", "", key.lower()) for key in ID_KEYS}
WRITE_RE = re.compile(r"\b(create|delete|remove|update|edit|write|send|message|purchase|buy|order|submit|post|login|logout|follow|save|favorite|payment)\b", re.I)
READ_RE = re.compile(r"\b(search|find|list|get|fetch|read|detail|lookup|nearby|route|reviews|categories|product|company|shop|seller|place|photo|geocode|transit)\b", re.I)

def now():
    return datetime.now(timezone.utc).isoformat()

def dump(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=False, by_alias=True)
    return value

def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def executable(command):
    for path in (MAIN_BIN / f"{command}.exe", ROOT / ".venv" / "Scripts" / f"{command}.exe"):
        if path.is_file():
            return path
    return MAIN_BIN / f"{command}.exe"

def tool_safety(tool):
    annotations = tool.get("annotations") or {}
    if annotations.get("destructiveHint") is True or annotations.get("destructive_hint") is True:
        return "unsafe_write"
    if annotations.get("readOnlyHint") is True or annotations.get("read_only_hint") is True:
        return "read_only"
    text = str(tool.get("name", "")) + " " + str(tool.get("description", ""))
    if WRITE_RE.search(text):
        return "unsafe_write"
    if READ_RE.search(text):
        return "read_only_by_name_and_description"
    return "unknown_safety"

def build_args(tool, identifiers=None, page=None):
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    props, required = schema.get("properties") or {}, schema.get("required") or []
    query_fields = {"query", "q", "text", "keyword", "term", "search", "search_query", "search_term", "name", "what", "company", "company_name", "product"}
    args, missing, id_index = {}, [], 0
    identifiers = identifiers or []
    for key, spec in props.items():
        if (key not in required and key.lower() not in query_fields) or not isinstance(spec, dict):
            continue
        value = VALUES.get(key.lower())
        normalized = re.sub(r"[^a-z0-9]", "", key.lower())
        if page is not None and key.lower() in {"page", "page_number", "page_no"}:
            value = page
        if normalized in ID_KEYS_NORMALIZED:
            if identifiers:
                value = identifiers[min(id_index, len(identifiers)-1)]
                id_index += 1
            else:
                missing.append(key)
                continue
        if value is None and spec.get("enum"):
            value = spec["enum"][0]
        types = spec.get("type")
        if isinstance(types, list):
            types = next((t for t in types if t != "null"), None)
        if value is None:
            if types == "boolean":
                value = False
            elif types == "integer":
                value = int(spec.get("minimum", 0))
            elif types == "number":
                value = float(spec.get("minimum", 0))
            else:
                missing.append(key)
                continue
        args[key] = value
    return (args, []) if not missing else (None, missing)

def ids_in(data):
    found = []
    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                norm = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if norm in ID_KEYS_NORMALIZED and isinstance(value, (str, int)) and str(value).strip():
                    found.append(str(value))
                else:
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)
    walk(data)
    return list(dict.fromkeys(found))

def identifiers_for_detail(tool, source, search_results):
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    required_ids = [key for key in (schema.get("required") or []) if re.sub(r"[^a-z0-9]", "", str(key).lower()) in ID_KEYS_NORMALIZED]
    if not required_ids:
        return []
    target = re.sub(r"[^a-z0-9]", "", required_ids[0].lower())
    aliases = {target}
    if target == "placeid" and source in {"neshan-mcp", "balad-mcp"}:
        aliases.update({"id", "poiid", "locationid"})
    elif target == "productid" and source in {"digikala-mcp", "snappshop-mcp"}:
        aliases.update({"id", "productid", "itemid"})
    elif target in {"shopid", "sellerid", "vendorid"} and source in {"torob-mcp", "basalam-mcp", "digikala-mcp"}:
        aliases.update({"shopid", "sellerid", "vendorid", "merchantid"})
    elif target == "companyid" and source in {"rasmio-mcp", "iran-jobs-mcp"}:
        aliases.update({"nationalid", "registrationid"})
    found = []
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized in aliases and isinstance(value, (str, int)) and str(value).strip():
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    for _, data in search_results.values():
        for chunk in parsed_chunks(data):
            walk(chunk)
    return list(dict.fromkeys(found))

def payload(result):
    return {
        "structuredContent": dump(getattr(result, "structuredContent", None)),
        "content": [dump(item) for item in (getattr(result, "content", None) or [])],
        "isError": bool(getattr(result, "isError", False)),
        "_meta": dump(getattr(result, "_meta", None)),
    }

def is_tool_error(data):
    if data.get("isError"):
        return True
    for item in data.get("content", []):
        text = item.get("text", "") if isinstance(item, dict) else ""
        if isinstance(text, str) and re.match(r"\s*(Error executing tool\b|Tool .+ (?:failed|rejected arguments)\b|Error:)", text, re.I):
            return True
    structured = data.get("structuredContent")
    return isinstance(structured, dict) and bool(structured.get("error"))

def parsed_chunks(data):
    chunks = [data.get("structuredContent")]
    for item in data.get("content", []):
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            try:
                chunks.append(json.loads(item["text"]))
            except (ValueError, TypeError):
                pass
    return chunks

def inventory(paths, run_root):
    found = defaultdict(lambda: {"observed_values_count": 0, "null_count": 0, "empty_count": 0, "present_in_files": [], "observed_types": []})
    def type_name(value):
        if value is None: return "null"
        if isinstance(value, bool): return "boolean"
        if isinstance(value, int): return "integer"
        if isinstance(value, float): return "number"
        if isinstance(value, str): return "string"
        if isinstance(value, dict): return "object"
        if isinstance(value, list): return "array"
        return type(value).__name__
    def observe_type(entry, value):
        name = type_name(value)
        if name not in entry["observed_types"]:
            entry["observed_types"].append(name)
    def walk(node, prefix, filename):
        if isinstance(node, dict):
            for key, value in node.items():
                field = f"{prefix}.{key}" if prefix else str(key)
                rec = found[field]
                rec["observed_values_count"] += 1
                observe_type(rec, value)
                if filename not in rec["present_in_files"]:
                    rec["present_in_files"].append(filename)
                if value is None:
                    rec["null_count"] += 1
                elif value == "" or value == [] or value == {}:
                    rec["empty_count"] += 1
                walk(value, field, filename)
        elif isinstance(node, list):
            for value in node:
                field = prefix + "[]"
                rec = found[field]
                rec["observed_values_count"] += 1
                observe_type(rec, value)
                if filename not in rec["present_in_files"]:
                    rec["present_in_files"].append(filename)
                if value is None:
                    rec["null_count"] += 1
                elif value == "" or value == [] or value == {}:
                    rec["empty_count"] += 1
                walk(value, field, filename)
    for path in paths:
        rel = path.relative_to(run_root).as_posix()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for chunk in parsed_chunks(data):
            walk(chunk, "", rel)
    for value in found.values():
        value["missing_from_file_count"] = max(0, len(paths) - len(value["present_in_files"]))
    fields = {}
    for key, value in sorted(found.items()):
        types = value["observed_types"]
        fields[key] = {**value, "observed_type": types[0] if len(types) == 1 else "multiple", "evidence_status": "verified_live_response"}
    return {"evidence_status": "verified_live_response" if paths else "not_tested",
            "response_file_count": len(paths),
            "observed_fields": fields,
            "note": "Only captured successful response payloads are inventoried; absent fields are not asserted unavailable."}

def response_field_paths(path):
    try:
        payload_data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    fields = set()
    def walk(node, prefix=""):
        if isinstance(node, dict):
            for key, value in node.items():
                current = f"{prefix}.{key}" if prefix else str(key)
                fields.add(current)
                walk(value, current)
        elif isinstance(node, list):
            current = prefix + "[]"
            fields.add(current)
            for value in node:
                walk(value, current)
    for chunk in parsed_chunks(payload_data):
        walk(chunk)
    return fields

def write_analyses(source_dir, run_root):
    response_dir = source_dir / "responses"
    request_dir = source_dir / "requests"
    response_files = sorted(response_dir.glob("*.json"))
    requests = {}
    for path in request_dir.glob("*.json"):
        try:
            requests[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    file_fields = {p.name: response_field_paths(p) for p in response_files}
    search_files, detail_files, page_files = [], [], []
    for path in response_files:
        req = requests.get(path.stem, {})
        stage = req.get("stage")
        if stage == "detail":
            detail_files.append(path.name)
        elif stage == "pagination":
            page_files.append(path.name)
        elif re.search(r"search|find|nearby|discover|shops|companies", req.get("tool", ""), re.I):
            search_files.append(path.name)
    search_fields = set().union(*(file_fields.get(name, set()) for name in search_files)) if search_files else set()
    detail_fields = set().union(*(file_fields.get(name, set()) for name in detail_files)) if detail_files else set()
    inventory_data = inventory(response_files, run_root)
    observed_fields = inventory_data.get("observed_fields", {})
    contact = {key: value for key, value in observed_fields.items() if re.search(r"phone|mobile|telephone|email|website|address|social|contact|seller", key, re.I)}
    identifiers = {key: value for key, value in observed_fields.items() if re.search(r"(^|[.\[\]])(id|.*_id|.*id|slug|uuid|code)(\[\]|$)", key, re.I)}
    analysis_dir = source_dir / "analysis"
    write_json(analysis_dir / "contact_fields.json", {"evidence_status": inventory_data["evidence_status"], "observed_contact_fields": contact, "phone_value_observed": any(("phone" in key.lower() or "mobile" in key.lower()) and value["observed_values_count"] > value["null_count"] + value["empty_count"] for key, value in contact.items())})
    write_json(analysis_dir / "potential_identifiers.json", {"evidence_status": inventory_data["evidence_status"], "observed_identifier_fields": identifiers, "note": "These are fields present in captured payloads, not a complete source schema."})
    write_json(analysis_dir / "search_detail_comparison.json", {"evidence_status": "verified_live_response" if search_files or detail_files else "not_tested", "search_response_files": search_files, "detail_response_files": detail_files, "fields_observed_in_search": sorted(search_fields), "fields_observed_in_detail": sorted(detail_fields), "common_fields": sorted(search_fields & detail_fields), "search_only_fields": sorted(search_fields - detail_fields), "detail_only_fields": sorted(detail_fields - search_fields)})
    write_json(analysis_dir / "pagination.json", {"evidence_status": "verified_live_response" if page_files else "not_tested", "page_two_response_files": page_files, "page_two_tested": bool(page_files), "pagination_request_files": [f"requests/{Path(p).stem}.json" for p in page_files]})

def has_next_page(data):
    found = False
    def walk(node):
        nonlocal found
        if isinstance(node, dict):
            for key, value in node.items():
                norm = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if norm in {"pages", "totalpages", "totalpage"} and isinstance(value, (int, float)) and value > 1:
                    found = True
                elif norm in {"hasmore", "hasnext", "hasnextpage"} and value is True:
                    found = True
                elif norm in {"nextpage", "nextcursor", "nexttoken", "nextpagetoken"} and value not in (None, "", False):
                    found = True
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    for chunk in parsed_chunks(data):
        walk(chunk)
    return found

async def inspect(source, command, proxy, run_root, timeout, operation):
    base = run_root / source
    for folder in ("requests", "responses", "errors", "analysis"):
        (base / folder).mkdir(parents=True, exist_ok=True)
    exe = executable(command)
    status = {"source": source, "integration_type": "MCP stdio", "timestamp": now(),
              "evidence_status": "not_tested", "startup_status": "not_attempted",
              "authentication_status": "unknown", "tool_discovery_status": "not_attempted",
              "real_request_executed": False, "real_response_received": False,
              "failure_stage": None, "executable_configured": str(exe),
              "executable_exists": exe.is_file(), "required_credentials_or_configuration": [],
              "notes": []}
    if proxy:
        status["optional_proxy_variable"] = proxy
        status["optional_proxy_configured"] = bool(os.environ.get(proxy))
    if not exe.is_file():
        status.update(evidence_status="unavailable", startup_status="unavailable", failure_stage="startup")
        status["notes"].append("No executable was found in the project virtual environments; no source request was made.")
        write_json(base / "status.json", status)
        write_json(base / "server_info.json", {"evidence_status": "unavailable", "executable": str(exe)})
        write_json(base / "tools.json", {"evidence_status": "unavailable", "tools": []})
        write_json(base / "analysis" / "field_inventory.json", {"evidence_status": "not_tested", "observed_fields": {}})
        return status
    env = os.environ.copy()
    params = StdioServerParameters(command=str(exe), args=[], env=env)
    tool_defs, responses, execution_records = [], [], []
    n_requests = n_success = n_fail = 0
    try:
        # Keep individual operations bounded while allowing a source to expose
        # and execute its full (potentially multi-tool) read-only surface.
        async with asyncio.timeout(timeout * 40):
            async with stdio_client(params) as (read, write):
                status["startup_status"] = "started"
                async with ClientSession(read, write) as session:
                    initialized = await asyncio.wait_for(session.initialize(), timeout=timeout)
                    status["startup_status"] = "initialized"
                    info = {"evidence_status": "verified_tool_schema", "timestamp": now(),
                            "server_info": dump(getattr(initialized, "serverInfo", None) or getattr(initialized, "server_info", None)),
                            "protocol_version": getattr(initialized, "protocolVersion", None) or getattr(initialized, "protocol_version", None),
                            "capabilities": dump(getattr(initialized, "capabilities", None)),
                            "instructions": getattr(initialized, "instructions", None)}
                    tools = await asyncio.wait_for(session.list_tools(), timeout=timeout)
                    tool_defs = [dump(tool) for tool in tools.tools]
                    status["tool_discovery_status"] = "succeeded"
                    write_json(base / "server_info.json", info)
                    write_json(base / "tools.json", {"evidence_status": "verified_tool_schema", "tools": tool_defs})
                    discovered_ids = []
                    successful_searches = {}
                    # Exercise every tool whose read-only nature is established.
                    for idx, tool in enumerate(tool_defs):
                        name = str(tool.get("name", f"tool_{idx}"))
                        if operation and operation.lower() not in name.lower():
                            continue
                        safety = tool_safety(tool)
                        if safety not in {"read_only", "read_only_by_name_and_description"}:
                            write_json(base / "errors" / f"tool_{idx:03d}_skipped.json", {
                                "timestamp": now(), "tool": name, "stage": "safety_classification",
                                "status": safety, "reason": "Read-only behavior was not established; tool was not executed."})
                            continue
                        args, missing = build_args(tool)
                        if missing:
                            required = (tool.get("inputSchema") or tool.get("input_schema") or {}).get("required", [])
                            is_id_tool = any(re.sub(r"[^a-z0-9]", "", str(k).lower()) in ID_KEYS_NORMALIZED for k in required)
                            if is_id_tool:
                                status["notes"].append(f"{name}: deferred until a real response supplies a required identifier.")
                                continue
                            write_json(base / "errors" / f"tool_{idx:03d}_skipped.json", {
                                "timestamp": now(), "tool": name, "stage": "argument_construction",
                                "status": "requires_manual_configuration", "missing_required_fields": missing,
                                "reason": "No safe argument could be derived from the actual schema."})
                            continue
                        req_id = f"{idx:03d}_{uuid.uuid4().hex[:8]}"
                        req_file = f"requests/{req_id}.json"
                        write_json(base / req_file, {"timestamp": now(), "source": source, "tool": name, "arguments": args, "evidence_status": "request_sent"})
                        n_requests += 1
                        status["real_request_executed"] = True
                        try:
                            result = await asyncio.wait_for(session.call_tool(name, arguments=args), timeout=timeout)
                            data = payload(result)
                            if is_tool_error(data):
                                n_fail += 1
                                write_json(base / "errors" / f"{req_id}.json", {"timestamp": now(), "source": source, "tool": name, "stage": "execution", "evidence_status": "verified_live_error", "exception_type": "MCPToolError", "message": "The live tool response contained an error.", "response": data, "request_file": req_file})
                            else:
                                out = base / "responses" / f"{req_id}.json"
                                write_json(out, data)
                                responses.append(out)
                                n_success += 1
                                status["real_response_received"] = True
                                ids = ids_in(parsed_chunks(data))
                                if ids and re.search(r"search|find|nearby|company|product|shop", name, re.I):
                                    discovered_ids.extend(ids)
                                if re.search(r"search|find|nearby|company|product|shop", name, re.I):
                                    successful_searches[name] = (args, data)
                                execution_records.append({"tool": name, "request_file": req_file, "response_file": out.relative_to(base).as_posix(), "arguments": args, "ids_observed": ids})
                        except Exception as exc:
                            n_fail += 1
                            write_json(base / "errors" / f"{req_id}.json", {"timestamp": now(), "source": source, "tool": name, "stage": "execution", "exception_type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc(), "request_file": req_file})
                    # Try identifier-based detail calls only with IDs from a live response.
                    last_neshan_detail = 0.0
                    for idx, tool in enumerate(tool_defs):
                        name = str(tool.get("name", f"tool_{idx}"))
                        required = (tool.get("inputSchema") or tool.get("input_schema") or {}).get("required", [])
                        if operation and operation.lower() not in name.lower():
                            continue
                        if not discovered_ids or tool_safety(tool) not in {"read_only", "read_only_by_name_and_description"}:
                            continue
                        if not any(re.sub(r"[^a-z0-9]", "", str(k).lower()) in ID_KEYS_NORMALIZED for k in required):
                            continue
                        matching_ids = identifiers_for_detail(tool, source, successful_searches)
                        if not matching_ids:
                            status["notes"].append(f"{name}: skipped because no matching identifier field was present in a real search response.")
                            continue
                        args, missing = build_args(tool, matching_ids[:3])
                        if missing:
                            continue
                        if source == "neshan-mcp" and name == "ns_place":
                            delay = 6.0 - (asyncio.get_running_loop().time() - last_neshan_detail)
                            if last_neshan_detail and delay > 0:
                                await asyncio.sleep(delay)
                            last_neshan_detail = asyncio.get_running_loop().time()
                        req_id = f"detail_{idx:03d}_{uuid.uuid4().hex[:8]}"
                        req_file = f"requests/{req_id}.json"
                        write_json(base / req_file, {"timestamp": now(), "source": source, "tool": name, "arguments": args, "stage": "detail", "evidence_status": "request_sent"})
                        n_requests += 1
                        status["real_request_executed"] = True
                        try:
                            result = await asyncio.wait_for(session.call_tool(name, arguments=args), timeout=timeout)
                            data = payload(result)
                            if is_tool_error(data):
                                n_fail += 1
                                write_json(base / "errors" / f"{req_id}.json", {"timestamp": now(), "source": source, "tool": name, "stage": "detail_execution", "evidence_status": "verified_live_error", "exception_type": "MCPToolError", "message": "The live tool response contained an error.", "response": data, "request_file": req_file})
                            else:
                                out = base / "responses" / f"{req_id}.json"
                                write_json(out, data)
                                responses.append(out)
                                n_success += 1
                                status["real_response_received"] = True
                                execution_records.append({"tool": name, "request_file": req_file, "response_file": out.relative_to(base).as_posix(), "stage": "detail", "arguments": args})
                        except Exception as exc:
                            n_fail += 1
                            write_json(base / "errors" / f"{req_id}.json", {"timestamp": now(), "source": source, "tool": name, "stage": "detail_execution", "exception_type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc(), "request_file": req_file})
                    # Follow an advertised next page only when page-one data
                    # proves another page exists and the live schema accepts a page field.
                    for idx, tool in enumerate(tool_defs):
                        name = str(tool.get("name", f"tool_{idx}"))
                        if operation and operation.lower() not in name.lower():
                            continue
                        if name not in successful_searches:
                            continue
                        first_args, first_data = successful_searches[name]
                        if not has_next_page(first_data):
                            continue
                        schema = tool.get("inputSchema") or tool.get("input_schema") or {}
                        props = schema.get("properties") or {}
                        page_key = next((k for k in props if k.lower() in {"page", "page_number", "page_no"}), None)
                        if not page_key:
                            continue
                        args = dict(first_args)
                        args[page_key] = 2
                        req_id = f"pagination_{idx:03d}_{uuid.uuid4().hex[:8]}"
                        req_file = f"requests/{req_id}.json"
                        write_json(base / req_file, {"timestamp": now(), "source": source, "tool": name, "arguments": args, "stage": "pagination", "evidence_status": "request_sent"})
                        n_requests += 1
                        status["real_request_executed"] = True
                        try:
                            result = await asyncio.wait_for(session.call_tool(name, arguments=args), timeout=timeout)
                            data = payload(result)
                            if is_tool_error(data):
                                n_fail += 1
                                write_json(base / "errors" / f"{req_id}.json", {"timestamp": now(), "source": source, "tool": name, "stage": "pagination", "evidence_status": "verified_live_error", "exception_type": "MCPToolError", "message": "The live tool response contained an error.", "response": data, "request_file": req_file})
                            else:
                                out = base / "responses" / f"{req_id}.json"
                                write_json(out, data)
                                responses.append(out)
                                n_success += 1
                                status["real_response_received"] = True
                                execution_records.append({"tool": name, "request_file": req_file, "response_file": out.relative_to(base).as_posix(), "stage": "pagination", "arguments": args})
                        except Exception as exc:
                            n_fail += 1
                            write_json(base / "errors" / f"{req_id}.json", {"timestamp": now(), "source": source, "tool": name, "stage": "pagination", "exception_type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc(), "request_file": req_file})
    except Exception as exc:
        stage = "initialization" if status["startup_status"] != "initialized" else "tool_discovery" if status["tool_discovery_status"] != "succeeded" else "process_or_transport"
        status["failure_stage"] = stage
        status["notes"].append(f"{type(exc).__name__}: {exc}")
        n_fail += 1
        write_json(base / "errors" / "source_failure.json", {"timestamp": now(), "source": source, "stage": stage, "exception_type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()})
        if not (base / "server_info.json").exists():
            write_json(base / "server_info.json", {"evidence_status": "unavailable", "failure_stage": stage})
        if not (base / "tools.json").exists():
            write_json(base / "tools.json", {"evidence_status": "unavailable", "tools": []})
    status.update({"discovered_tool_count": len(tool_defs), "executed_request_count": n_requests,
                   "successful_request_count": n_success, "failed_request_count": n_fail,
                   "actual_response_file_count": len(responses),
                   "evidence_status": "verified_live_response" if responses else "verified_tool_schema" if tool_defs else "unavailable"})
    write_json(base / "status.json", status)
    write_json(base / "analysis" / "field_inventory.json", inventory(responses, run_root))
    write_json(base / "analysis" / "executions.json", {"evidence_status": "execution_records", "records": execution_records})
    write_analyses(base, run_root)
    return status

def create_credentials_report(run_root):
    rows = []
    for source, command, proxy in SOURCES:
        exe = executable(command)
        rows.append({"source": source, "required_variables": [],
                     "optional_variables": ([{"name": proxy, "configured": bool(os.environ.get(proxy)), "purpose": "proxy variable named in adjacent info_extractor source registry"}] if proxy else []),
                     "authentication_tested": False, "credential_requirement_status": "unknown_from_local_configuration" if exe.is_file() else "not_assessed_source_unavailable",
                     "evidence_status": "inferred_from_local_configuration",
                     "evidence_sources": ["../info_extractor/leads/mcp_client.py", "../info_extractor/.env.example"],
                     "executable_available": exe.is_file(),
                     "note": "Credential values are never read or written. No credential requirement is inferred from package naming."})
    write_json(run_root / "credentials_required.json", {"timestamp": now(), "sources": rows})

def reclassify_tool_errors(run_root):
    """MCP servers sometimes return errors as text while leaving isError false."""
    for source_dir in run_root.iterdir():
        if not source_dir.is_dir() or not (source_dir / "status.json").exists():
            continue
        response_dir = source_dir / "responses"
        error_dir = source_dir / "errors"
        moved = set()
        for response_file in list(response_dir.glob("*.json")):
            try:
                data = json.loads(response_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not is_tool_error(data):
                continue
            request_file = source_dir / "requests" / (response_file.stem + ".json")
            request_data = json.loads(request_file.read_text(encoding="utf-8")) if request_file.exists() else {}
            error_file = error_dir / response_file.name
            write_json(error_file, {"timestamp": now(), "source": source_dir.name,
                                    "tool": request_data.get("tool"), "stage": request_data.get("stage", "execution"),
                                    "evidence_status": "verified_live_error", "exception_type": "MCPToolError",
                                    "message": "The live tool response contained an error.",
                                    "response": data, "request_file": f"requests/{response_file.name}"})
            response_file.unlink()
            moved.add(response_file.stem)
        if moved:
            exec_file = source_dir / "analysis" / "executions.json"
            if exec_file.exists():
                records = json.loads(exec_file.read_text(encoding="utf-8"))
                records["records"] = [r for r in records.get("records", []) if Path(r.get("response_file", "")).stem not in moved]
                write_json(exec_file, records)
            status_file = source_dir / "status.json"
            status = json.loads(status_file.read_text(encoding="utf-8"))
            status["successful_request_count"] = max(0, status.get("successful_request_count", 0) - len(moved))
            status["failed_request_count"] = status.get("failed_request_count", 0) + len(moved)
            status["actual_response_file_count"] = len(list(response_dir.glob("*.json")))
            status["evidence_status"] = "verified_live_response" if status["actual_response_file_count"] else "verified_live_error"
            write_json(status_file, status)
            paths = list(response_dir.glob("*.json"))
            write_json(source_dir / "analysis" / "field_inventory.json", inventory(paths, run_root))
        write_analyses(source_dir, run_root)

def create_summary(run_root, statuses):
    output = []
    for status in statuses:
        source = status["source"]
        base = run_root / source
        request_files = list((base / "requests").glob("*.json"))
        response_files = list((base / "responses").glob("*.json"))
        error_files = list((base / "errors").glob("*.json"))
        phone = {"search": False, "detail": False}
        execution_file = base / "analysis" / "executions.json"
        execution_rows = json.loads(execution_file.read_text(encoding="utf-8")).get("records", []) if execution_file.exists() else []
        tool_by_response = {row.get("response_file"): row.get("tool", "") for row in execution_rows}
        request_data_by_stem = {}
        for req_path in (base / "requests").glob("*.json"):
            try:
                request_data_by_stem[req_path.stem] = json.loads(req_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        search_tested = any(re.search(r"search|find|nearby|discover|shops|companies", req.get("tool", ""), re.I) for req in request_data_by_stem.values())
        detail_tested = any(req.get("stage") == "detail" for req in request_data_by_stem.values())
        pagination_tested = any(req.get("stage") == "pagination" for req in request_data_by_stem.values())
        for path in response_files:
            data = json.loads(path.read_text(encoding="utf-8"))
            def has_contact_field(node, require_value=False):
                if isinstance(node, dict):
                    for field, value in node.items():
                        is_phone = any(k in str(field).lower() for k in ("phone", "mobile", "telephone"))
                        if is_phone and (not require_value or value not in (None, "", [], {})):
                            return True
                        if has_contact_field(value, require_value):
                            return True
                elif isinstance(node, list):
                    return any(has_contact_field(item, require_value) for item in node)
                return False
            tool_name = tool_by_response.get(path.relative_to(base).as_posix(), "")
            req_info = request_data_by_stem.get(path.stem, {})
            is_detail = req_info.get("stage") == "detail"
            key = "detail" if is_detail else "search"
            phone[key] |= has_contact_field(parsed_chunks(data))
            phone[key + "_value"] = phone.get(key + "_value", False) or has_contact_field(parsed_chunks(data), require_value=True)
        output.append({"source": source, "integration_type": status.get("integration_type", "MCP stdio"),
                       "startup_status": status.get("startup_status"), "authentication_status": status.get("authentication_status"),
                       "tool_operation_discovery_status": status.get("tool_discovery_status"),
                       "discovered_tool_count": status.get("discovered_tool_count", 0),
                       "executed_request_count": len(request_files), "successful_request_count": status.get("successful_request_count", 0),
                       "failed_request_count": status.get("failed_request_count", len(error_files)),
                       "actual_response_file_count": len(response_files),
                       "search_tested": search_tested,
                       "detail_retrieval_tested": detail_tested,
                       "pagination_tested": pagination_tested,
                       "phone_numbers_observed": phone.get("search_value", False) or phone.get("detail_value", False),
                       "phone_fields_in_search_results": phone["search"], "phone_fields_in_detail_results": phone["detail"],
                       "contact_information_requires_additional_access": "unknown",
                       "missing_configuration": status.get("required_credentials_or_configuration", []),
                       "overall_evidence_status": status.get("evidence_status")})
    write_json(run_root / "summary.json", {"timestamp": now(), "evidence_policy": "All counts derive from this run's records.", "sources": output})

def validate_run(run_root):
    problems = []
    for path in run_root.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"Invalid JSON: {path.relative_to(run_root)} ({exc})")
            continue
        if path.parent.name == "requests" and path.parent.parent.name not in {"", "runs"}:
            if not isinstance(data, dict) or not data.get("timestamp") or not data.get("source"):
                problems.append(f"Request missing timestamp/source: {path.relative_to(run_root)}")
        if path.parent.name == "responses":
            req = path.parent.parent / "requests" / (path.stem + ".json")
            if not req.is_file():
                problems.append(f"Response has no corresponding request: {path.relative_to(run_root)}")
            try:
                req_data = json.loads(req.read_text(encoding="utf-8"))
                if req_data.get("source") != path.parent.parent.name:
                    problems.append(f"Response/request source mismatch: {path.relative_to(run_root)}")
            except (OSError, ValueError):
                pass
        def check_keys(node, loc=""):
            if isinstance(node, dict):
                for key, value in node.items():
                    if re.search(r"(^|[_-])(api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|password|cookie|secret)([_-]|$)", str(key), re.I):
                        if value not in (None, "", "[redacted]"):
                            problems.append(f"Potential secret value in {path.relative_to(run_root)} at {loc}{key}")
                    check_keys(value, f"{loc}{key}.")
            elif isinstance(node, list):
                for value in node:
                    check_keys(value, loc)
        check_keys(data)
    summary_path = run_root / "summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        for source in summary.get("sources", []):
            base = run_root / source["source"]
            actual_requests = len(list((base / "requests").glob("*.json")))
            actual_responses = len(list((base / "responses").glob("*.json")))
            if source.get("executed_request_count") != actual_requests:
                problems.append(f"Summary request count mismatch: {source['source']}")
            if source.get("actual_response_file_count") != actual_responses:
                problems.append(f"Summary response count mismatch: {source['source']}")
            status_path = base / "status.json"
            if not status_path.is_file():
                problems.append(f"Missing status.json: {source['source']}")
            else:
                status = json.loads(status_path.read_text(encoding="utf-8"))
                if status.get("evidence_status") == "verified_live_response" and not list((base / "responses").glob("*.json")):
                    problems.append(f"Live response status has no response artifact: {source['source']}")
    return problems

def refresh_latest_indexes(output_root):
    """Keep root-level reports global while selecting the newest evidence per source."""
    latest_summary, latest_credentials, latest_runs = {}, {}, {}
    run_dirs = sorted((output_root / "runs").glob("*"), key=lambda p: p.name, reverse=True)
    for run_dir in run_dirs:
        if not run_dir.is_dir():
            continue
        reclassify_tool_errors(run_dir)
        statuses = [json.loads(p.read_text(encoding="utf-8")) for p in run_dir.glob("*/status.json")]
        if statuses:
            create_summary(run_dir, statuses)
            manifest_path = run_dir / "run.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["source_results"] = statuses
                write_json(manifest_path, manifest)
            problems = validate_run(run_dir)
            write_json(run_dir / "validation.json", {"timestamp": now(), "passed": not problems, "problems": problems})
        try:
            summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            summary = {}
        try:
            credentials = json.loads((run_dir / "credentials_required.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            credentials = {}
        for row in summary.get("sources", []):
            source = row.get("source")
            if source and source not in latest_summary:
                latest_summary[source] = {**row, "run_id": run_dir.name}
                latest_runs[source] = run_dir.name
        for row in credentials.get("sources", []):
            source = row.get("source")
            if source and source not in latest_credentials:
                latest_credentials[source] = {**row, "run_id": run_dir.name}
    ordered = [latest_summary[s] for s, _, _ in SOURCES if s in latest_summary]
    write_json(output_root / "summary.json", {"timestamp": now(), "evidence_policy": "Each row points to the newest run that inspected that source; counts derive from its execution files.", "latest_run_by_source": latest_runs, "sources": ordered})
    write_json(output_root / "credentials_required.json", {"timestamp": now(), "sources": [latest_credentials[s] for s, _, _ in SOURCES if s in latest_credentials]})

async def async_main(args):
    if args.list:
        for source, command, _ in SOURCES:
            exe = executable(command)
            print(f"{source}\t{exe}\t{'available' if exe.is_file() else 'unavailable'}")
        return 0
    selected = [(s, e, p) for s, e, p in SOURCES if not args.source or args.source == s]
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "Z_" + uuid.uuid4().hex[:6]
    run_root = Path(args.output).resolve() / "runs" / name
    run_root.mkdir(parents=True, exist_ok=False)
    statuses = []
    for source, command, proxy in selected:
        print(f"[{source}] starting")
        result = await inspect(source, command, proxy, run_root, args.timeout, args.operation)
        statuses.append(result)
        print(f"[{source}] startup={result['startup_status']} tools={result.get('discovered_tool_count', 0)} requests={result.get('executed_request_count', 0)} responses={result.get('actual_response_file_count', 0)} evidence={result.get('evidence_status')}")
    create_credentials_report(run_root)
    create_summary(run_root, statuses)
    problems = validate_run(run_root)
    write_json(run_root / "validation.json", {"timestamp": now(), "passed": not problems, "problems": problems})
    refresh_latest_indexes(run_root.parent.parent)
    write_json(run_root / "run.json", {"run_id": name, "timestamp": now(), "sources_requested": [s for s, _, _ in selected], "source_results": statuses})
    print(f"Output: {run_root}")
    return 1 if problems or any(s.get("evidence_status") == "unavailable" for s in statuses) else 0

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true")
    group.add_argument("--source", choices=[s for s, _, _ in SOURCES])
    parser.add_argument("--all", action="store_true", help="inspect all ten sources (default)")
    parser.add_argument("--operation", help="restrict execution to discovered tool names containing this text")
    parser.add_argument("--timeout", type=float, default=25.0, help="timeout in seconds per source/session")
    parser.add_argument("--output", default=str(ROOT / "source_inspection"))
    args = parser.parse_args()
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130

if __name__ == "__main__":
    raise SystemExit(main())
