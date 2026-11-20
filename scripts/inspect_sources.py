#!/usr/bin/env python3
"""Collect original MCP definitions and complete tool results."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import traceback
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
PROJECT_BIN = Path.home() / "Desktop" / "info_extractor" / ".venv" / "Scripts"
SOURCES = [
    ("neshan-mcp", "neshan-mcp", "neshan-mcp.json"),
    ("balad-mcp", "balad-mcp", "balad-mcp.json"),
    ("divar-mcp", "divar-mcp", "divar-mcp.json"),
    ("sheypoor-mcp", "sheypoor-mcp", "sheypoor-mcp.json"),
    ("basalam-mcp", "basalam-mcp", "basalam-mcp.json"),
    ("emalls-ir-mcp", "emalls-ir-mcp", "emalls.json"),
    ("rasmio-mcp", "rasmio-mcp", "rasmio.json"),
    ("iran-jobs-mcp", "iran-jobs-mcp", "iran-jobs.json"),
    ("torob-mcp", "torob-mcp", "torob.json"),
    ("digikala-mcp", "digikala-mcp", "digikala-mcp.json"),
]
TEST_VALUES = {
    "query": "کافه", "q": "کافه", "text": "کافه", "keyword": "کافه",
    "term": "کافه", "search": "کافه", "search_query": "کافه",
    "search_term": "کافه", "what": "کافه", "name": "کافه",
    "company": "شرکت فناوری", "company_name": "شرکت فناوری",
    "activity": "فناوری اطلاعات", "product": "گوشی موبایل",
    "category": "کافه", "city": "تهران", "province": "تهران",
    "origin": "35.6892,51.3890", "destination": "35.7219,51.3347",
    "destinations": ["35.7219,51.3347"], "address": "میدان ونک",
    "category_code": "mobile-phone", "brand_code": "samsung",
    "role": "python", "career": "python", "specialty": "java",
    "items": ["آیفون 13 128", "هدفون بی‌سیم"],
    "near": "Tehran", "location": "35.6892,51.3890",
    "lat": 35.6892, "latitude": 35.6892, "lng": 51.3890,
    "lon": 51.3890, "longitude": 51.3890, "from_lat": 35.6892,
    "from_lng": 51.3890, "to_lat": 35.7219, "to_lng": 51.3347,
    "site": "all", "kind": "all", "open_now": False,
}
QUERY_ARGUMENTS = {
    "query", "q", "text", "keyword", "term", "search", "search_query",
    "search_term", "what", "name", "company_name", "activity", "product",
    "role", "career", "specialty", "search", "address",
}
REFERENCE_NAMES = {
    "id", "placeid", "poiid", "locationid", "productid", "productids", "itemid",
    "sellerid", "shopid", "vendorid", "companyid", "nationalid",
    "sellercode", "slug", "uuid", "uid", "code", "adid", "jobid",
    "career", "cursor", "nextcursor", "pagetoken",
}

def timestamp():
    return datetime.now(timezone.utc).isoformat()

def serialize(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=False)
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return value
    return str(value)

def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def redact_diagnostics(text):
    text = re.sub(r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret)(\s*[:=]\s*)[^\s,;&]+", r"\1\2[redacted]", text)
    text = re.sub(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@", r"\1[redacted]@", text)
    return re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text)

def source_executable(command):
    choices = (PROJECT_BIN / f"{command}.exe", ROOT / ".venv" / "Scripts" / f"{command}.exe")
    return next((path for path in choices if path.is_file()), choices[0])

def normalized(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())

def schema_types(spec):
    kind = spec.get("type")
    if isinstance(kind, list):
        return {item for item in kind if item != "null"}
    if kind:
        return {kind}
    return {item.get("type") for item in spec.get("anyOf", []) if isinstance(item, dict) and item.get("type") != "null"}

def reference_details(name, spec):
    description = str(spec.get("description", ""))
    item_description = str((spec.get("items") or {}).get("description", ""))
    combined = f"{description} {item_description}"
    normalized_description = normalized(combined)
    allows_free_text = bool(re.search(r"or\s+(?:free\s+text|a\s+job\s+title|name|product\s+names?|specialty|persian\s+name)", combined, re.I))
    identifier = normalized(name) in REFERENCE_NAMES or bool(
        re.search(r"\b(id|identifier|uuid|cursor)\b", combined, re.I)
    ) or any(token in normalized_description for token in ("shopid", "vendorid", "productid", "sellerid"))
    return identifier and not allows_free_text, combined

def tool_policy(definition):
    annotations = definition.get("annotations") or {}
    read_only = annotations.get("readOnlyHint", annotations.get("read_only_hint"))
    destructive = annotations.get("destructiveHint", annotations.get("destructive_hint"))
    if destructive is True:
        return "skip", "MCP marks this operation as destructive."
    if read_only is True:
        return "execute", "MCP marks this operation as read-only."
    if read_only is False or destructive is False:
        return "skip", "The MCP annotations do not establish a read-only operation."
    return "review", "The MCP definition does not establish safe read-only behavior."

def schema_parts(definition):
    schema = definition.get("inputSchema") or definition.get("input_schema") or {}
    return schema, schema.get("properties") or {}, schema.get("required") or []

def argument_values(definition, references=None, page=None):
    _, properties, required = schema_parts(definition)
    references = references or []
    args, missing, reference_index = {}, [], 0
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            continue
        key = normalized(name)
        types = schema_types(spec)
        if name not in required and not (key in QUERY_ARGUMENTS and "string" in types) and not (page is not None and key in {"page", "pagenumber", "pageno"}):
            continue
        if "default" in spec and key not in QUERY_ARGUMENTS and page is None:
            continue
        if page is not None and key in {"page", "pagenumber", "pageno"}:
            args[name] = page
            continue
        value = TEST_VALUES.get(name.lower())
        is_reference, _ = reference_details(name, spec)
        if is_reference:
            if not references:
                missing.append(name)
                continue
            if "array" in types:
                minimum = (spec.get("minItems") or 1)
                if len(references) < minimum:
                    missing.append(name)
                    continue
                maximum = spec.get("maxItems")
                value = list(references[:maximum] if maximum else references)
            else:
                value = references[min(reference_index, len(references) - 1)]
                reference_index += 1
        kind = next(iter(types), None)
        if kind == "array" and isinstance(value, list):
            item_types = schema_types(spec.get("items", {}))
            if "string" in item_types:
                value = [str(item) if isinstance(item, int) and not isinstance(item, bool) else item for item in value]
            elif "integer" in item_types:
                value = [int(item) if isinstance(item, str) and item.isdigit() else item for item in value]
        elif kind == "string" and isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if kind == "integer" and value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            value = int(value) if str(value).isdigit() else None
        if kind == "number" and value is not None and not isinstance(value, (int, float)):
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None
        if spec.get("enum") and value not in spec["enum"]:
            value = spec["enum"][0]
        if value is None and "default" in spec:
            value = spec["default"]
        if value is None:
            if kind == "boolean":
                value = False
            elif kind == "integer":
                value = int(spec.get("minimum", 0))
            elif kind == "number":
                value = float(spec.get("minimum", 0))
            else:
                missing.append(name)
                continue
        args[name] = value
    return (args, []) if not missing else (None, missing)

def reference_chunks(result):
    chunks = []
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        chunks.append(serialize(structured))
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            try:
                chunks.append(json.loads(text))
            except (ValueError, TypeError):
                pass
        else:
            chunks.append(serialize(block))
    return chunks

def reference_argument_names(definition):
    _, properties, required = schema_parts(definition)
    names = []
    for name in required:
        spec = properties.get(name) or {}
        description = str(spec.get("description", ""))
        is_reference, _ = reference_details(name, spec)
        if is_reference:
            names.append(name)
    return names

def references_for(definition, chunks):
    _, properties, required = schema_parts(definition)
    required_properties = []
    for name in required:
        spec = properties.get(name) or {}
        key = normalized(name)
        is_reference, description = reference_details(name, spec)
        if is_reference:
            required_properties.append((key, description, spec))
    if not required_properties:
        return []
    values = []
    for target, description, spec in required_properties:
      described_names = {normalized(name) for name in re.findall(r"\b[a-z][a-z0-9_]{2,}\b", description, re.I)}
      available_names = {normalized(producer) for producer, _ in chunks}
      producer_names = described_names & available_names
      if not producer_names:
          consumer_name = definition.get("name", "")
          pieces = consumer_name.split("_")
          family = "_".join(pieces[:2]) if pieces and pieces[0] == "job" and len(pieces) > 1 else (pieces[0] if pieces else "")
          family_refs = [(producer, chunk) for producer, chunk in chunks if producer.startswith(family + "_")]
          if family_refs:
              chunks_for_target = family_refs
          else:
              chunks_for_target = chunks
      else:
          chunks_for_target = [(producer, chunk) for producer, chunk in chunks if normalized(producer) in producer_names]
      wanted = {target}
      # Match a parameter such as ``vendor`` to a documented ``shop_id``.
      wanted.update(normalized(token) for token in re.findall(r"\b[a-z][a-z0-9_]*(?:id|uuid|slug)\b", description, re.I))
      if target.endswith("ids"):
          wanted.add(target[:-3] + "id")
          if target in {"productids", "products"}:
              wanted.add("id")
      if target.endswith("id"):
          wanted.update({target[:-2] + "id", "id"})
      if target in {"job", "product", "products", "shop", "vendor", "seller", "company", "category", "career"}:
          wanted.update({target + "id", "id", "slug", "uuid", "url", "code"})
      values_for_target = []
      types = schema_types(spec)
      value_types = schema_types(spec.get("items", {})) if "array" in types else types
      pattern = spec.get("pattern") or (spec.get("items") or {}).get("pattern")
      def compatible(value):
          candidate = str(value) if "string" in value_types and isinstance(value, int) else value
          if "string" in value_types and not isinstance(candidate, str):
              return False
          if "integer" in value_types and (isinstance(candidate, bool) or not isinstance(candidate, int)):
              return False
          if "number" in value_types and (isinstance(candidate, bool) or not isinstance(candidate, (int, float))):
              return False
          return not pattern or (isinstance(candidate, str) and re.fullmatch(pattern, candidate) is not None)
      def walk(node):
        if isinstance(node, dict):
            for name, value in node.items():
                key = normalized(name)
                if key in wanted and isinstance(value, (str, int)) and str(value).strip() and compatible(value):
                    values_for_target.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
      for producer, chunk in chunks_for_target:
          walk(chunk)
      values.extend(values_for_target)
    return list(dict.fromkeys(values))

def requires_reference(definition):
    return bool(reference_argument_names(definition))

def is_mcp_error(original):
    if not isinstance(original, dict):
        return False
    if original.get("isError") is True:
        return True
    for block in original.get("content", []) or []:
        text = block.get("text", "") if isinstance(block, dict) else ""
        if isinstance(text, str) and re.match(r"\s*(Error executing tool\b|Tool .+ rejected arguments\b)", text, re.I):
            return True
    return False

def page_argument(definition):
    _, properties, _ = schema_parts(definition)
    return next((name for name in properties if normalized(name) in {"page", "pagenumber", "pageno"}), None)

def new_source_record(source, filename, executable, run_id):
    return {
        "source": source, "run_id": run_id, "started_at": timestamp(),
        "output_file": filename,
        "connection": {
            "status": "not_attempted", "executable": str(executable),
            "executable_available": executable.is_file(), "initialized_at": None,
            "initialization_result": None, "failure": None,
        },
        "tool_discovery": {"status": "not_attempted", "discovered_count": 0},
        "tools": [], "process_output": {"stderr": ""}, "finished_at": None,
    }

async def inspect_source(source, command, filename, run_dir, timeout):
    output = run_dir / filename
    executable = source_executable(command)
    record = new_source_record(source, filename, executable, run_dir.name)
    write_json(output, record)
    if not executable.is_file():
        record["connection"].update(status="unavailable", failure={
            "stage": "startup", "exception_type": "FileNotFoundError",
            "message": f"Configured executable was not found: {executable}",
        })
        record["finished_at"] = timestamp()
        write_json(output, record)
        return record

    params = StdioServerParameters(command=str(executable), args=[], env=os.environ.copy())
    stderr_file = tempfile.TemporaryFile(mode="w+b")
    try:
        async with asyncio.timeout(timeout * 40):
            async with stdio_client(params, errlog=stderr_file) as (read, write):
                record["connection"]["status"] = "started"
                write_json(output, record)
                async with ClientSession(read, write) as session:
                    try:
                        init_result = await asyncio.wait_for(session.initialize(), timeout=timeout)
                        record["connection"].update(
                            status="initialized", initialized_at=timestamp(),
                            initialization_result=serialize(init_result),
                        )
                        write_json(output, record)
                    except Exception as exc:
                        record["connection"]["failure"] = {
                            "stage": "initialization", "exception_type": type(exc).__name__,
                            "message": str(exc), "traceback": traceback.format_exc(),
                        }
                        raise
                    try:
                        listed = await asyncio.wait_for(session.list_tools(), timeout=timeout)
                        definitions = [serialize(tool) for tool in listed.tools]
                        record["tool_discovery"] = {
                            "status": "succeeded", "discovered_count": len(definitions),
                            "retrieved_at": timestamp(),
                        }
                        record["tools"] = [{
                            "original_definition": definition,
                            "execution_policy": None, "execution_status": "pending",
                            "executions": [], "skip_reasons": [],
                        } for definition in definitions]
                        write_json(output, record)
                    except Exception as exc:
                        record["tool_discovery"] = {
                            "status": "failed", "discovered_count": 0,
                            "failure": {"exception_type": type(exc).__name__,
                                        "message": str(exc), "traceback": traceback.format_exc()},
                        }
                        raise

                    async def invoke(tool_record, arguments, stage):
                        definition = tool_record["original_definition"]
                        call = {
                            "started_at": timestamp(), "stage": stage,
                            "arguments_sent": arguments, "status": "started",
                            "completed_at": None, "original_mcp_response": None,
                            "failure": None,
                        }
                        tool_record["executions"].append(call)
                        tool_record["execution_status"] = "attempted"
                        write_json(output, record)
                        try:
                            result = await asyncio.wait_for(
                                session.call_tool(definition["name"], arguments=arguments),
                                timeout=timeout,
                            )
                            call["original_mcp_response"] = serialize(result)
                            call["status"] = "mcp_error" if is_mcp_error(call["original_mcp_response"]) else "success"
                            call["completed_at"] = timestamp()
                            tool_record["execution_status"] = call["status"]
                            write_json(output, record)
                            return result
                        except Exception as exc:
                            call.update(status="failed", completed_at=timestamp(), failure={
                                "exception_type": type(exc).__name__, "message": str(exc),
                                "traceback": traceback.format_exc(),
                            })
                            tool_record["execution_status"] = "failed"
                            write_json(output, record)
                            return None

                    references = []
                    page_calls = []
                    last_map_detail_at = None
                    for item in record["tools"]:
                        definition = item["original_definition"]
                        policy, reason = tool_policy(definition)
                        item["execution_policy"] = policy
                        if policy != "execute":
                            item["execution_status"] = "skipped"
                            item["skip_reasons"].append(reason)
                            write_json(output, record)
                            continue
                        _, _, required = schema_parts(definition)
                        if requires_reference(definition):
                            item["execution_status"] = "deferred"
                            continue
                        arguments, missing = argument_values(definition)
                        if missing:
                            item["execution_status"] = "skipped"
                            item["skip_reasons"].append(
                                "Required schema arguments could not be supplied safely: " + ", ".join(missing)
                            )
                            write_json(output, record)
                            continue
                        result = await invoke(item, arguments, "initial")
                        if result is not None and not is_mcp_error(serialize(result)):
                            references.extend((definition["name"], chunk) for chunk in reference_chunks(result))
                            page_calls.append((item, arguments))

                    for item in record["tools"]:
                        definition = item["original_definition"]
                        if item["execution_policy"] != "execute" or item["execution_status"] != "deferred":
                            continue
                        real_refs = references_for(definition, references)
                        if not real_refs:
                            _, _, required = schema_parts(definition)
                            required_refs = reference_argument_names(definition)
                            item["execution_status"] = "skipped"
                            item["skip_reasons"].append(
                                "No matching real reference was returned for required schema argument(s): "
                                + ", ".join(required_refs)
                            )
                            write_json(output, record)
                            continue
                        arguments, missing = argument_values(definition, references=real_refs)
                        if missing:
                            item["execution_status"] = "skipped"
                            item["skip_reasons"].append(
                                "Required schema arguments could not be supplied safely: " + ", ".join(missing)
                            )
                            write_json(output, record)
                            continue
                        if source in {"neshan-mcp", "balad-mcp"}:
                            now_loop = asyncio.get_running_loop().time()
                            if last_map_detail_at is not None:
                                wait_for_spacing = 6.0 - (now_loop - last_map_detail_at)
                                if wait_for_spacing > 0:
                                    await asyncio.sleep(wait_for_spacing)
                            last_map_detail_at = asyncio.get_running_loop().time()
                        result = await invoke(item, arguments, "referenced_resource")
                        if result is not None and not is_mcp_error(serialize(result)):
                            references.extend((definition["name"], chunk) for chunk in reference_chunks(result))

                    for item, first_arguments in page_calls:
                        key = page_argument(item["original_definition"])
                        if key is None:
                            continue
                        _, page_properties, _ = schema_parts(item["original_definition"])
                        first_page = page_properties.get(key, {}).get("default", 1)
                        if not isinstance(first_page, int):
                            item["skip_reasons"].append("Pagination was not attempted because the schema default is not an integer page value.")
                            continue
                        second_arguments = dict(first_arguments)
                        second_arguments[key] = first_page + 1
                        await invoke(item, second_arguments, "pagination")
    except Exception as exc:
        if record["connection"]["failure"] is None:
            record["connection"]["failure"] = {
                "stage": "transport_or_process", "exception_type": type(exc).__name__,
                "message": str(exc), "traceback": traceback.format_exc(),
            }
        if record["connection"]["status"] != "initialized":
            record["connection"]["status"] = "failed"
    finally:
        for item in record["tools"]:
            if item["execution_status"] in {"pending", "deferred"}:
                item["execution_status"] = "skipped"
                item["skip_reasons"].append("Execution did not reach this tool before the source session ended.")
        stderr_file.seek(0)
        record["process_output"]["stderr"] = redact_diagnostics(stderr_file.read().decode("utf-8", errors="replace"))
        stderr_file.close()
        record["finished_at"] = timestamp()
        write_json(output, record)
    return record

def counts(record):
    tools = record.get("tools", [])
    calls = [call for item in tools for call in item.get("executions", [])]
    return {
        "attempted": len(calls),
        "successful": sum(call.get("status") == "success" for call in calls),
        "failed": sum(call.get("status") in {"failed", "mcp_error"} for call in calls),
        "skipped": sum(item.get("execution_status") == "skipped" for item in tools),
        "saved_responses": sum(call.get("original_mcp_response") is not None for call in calls),
    }

async def async_main(args):
    if args.list:
        for source, command, filename in SOURCES:
            exe = source_executable(command)
            print(f"{source}\t{'available' if exe.is_file() else 'unavailable'}\t{exe}\t{filename}")
        return 0
    selected = [item for item in SOURCES if not args.source or item[0] == args.source]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6]
    run_dir = Path(args.output).resolve() / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    failed_connections = False
    for source, command, filename in selected:
        record = await inspect_source(source, command, filename, run_dir, args.timeout)
        totals = counts(record)
        failed_connections |= record["connection"]["status"] != "initialized"
        print(
            f"{source}: connection={record['connection']['status']}; "
            f"tools={record['tool_discovery']['discovered_count']}; "
            f"calls={totals['attempted']} (ok={totals['successful']}, failed={totals['failed']}, "
            f"skipped={totals['skipped']}); saved_original_responses={totals['saved_responses']}; "
            f"file={run_dir / filename}"
        )
    print(f"Run directory: {run_dir}")
    return 1 if failed_connections else 0

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="show executable availability")
    group.add_argument("--source", choices=[source for source, _, _ in SOURCES])
    parser.add_argument("--all", action="store_true", help="collect all ten sources (default)")
    parser.add_argument("--timeout", type=float, default=25.0, help="timeout per MCP operation")
    parser.add_argument("--output", default=str(ROOT / "source_inspection"))
    args = parser.parse_args()
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130

if __name__ == "__main__":
    raise SystemExit(main())
