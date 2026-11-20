import asyncio
import json
import re
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SEARCH_TERM = "موبایل"

SERVERS = {
    "neshan": {
        "command": "neshan-mcp",
        "search_tool": "ns_search",
        "detail_tool": "ns_place",
    },
    "balad": {
        "command": "balad-mcp",
        "search_tool": "bl_search",
        "detail_tool": "bl_place",
    },
}

OUTPUT_DIR = Path("maps_debug")
OUTPUT_DIR.mkdir(exist_ok=True)


def serialize(obj):
    """Convert MCP/Pydantic objects into JSON-compatible values."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")

    if hasattr(obj, "__dict__"):
        return obj.__dict__

    return str(obj)


def extract_payload(result):
    """Preserve both structured and textual MCP response content."""
    payload = {
        "structuredContent": getattr(result, "structuredContent", None),
        "content": [],
        "isError": getattr(result, "isError", False),
    }

    for item in getattr(result, "content", []) or []:
        if hasattr(item, "text"):
            text = item.text

            try:
                payload["content"].append(json.loads(text))
            except (ValueError, TypeError):
                payload["content"].append(text)
        else:
            payload["content"].append(serialize(item))

    return payload


def get_schema(tool):
    return tool.inputSchema or {}


def build_arguments(tool, kind):
    """
    Build arguments using the actual input schema returned by MCP.
    Unknown required parameters are reported instead of guessed.
    """
    schema = get_schema(tool)
    properties = schema.get("properties", {})
    required = schema.get("required", [])

    args = {}

    aliases = {
        "search": {
            "query": SEARCH_TERM,
            "q": SEARCH_TERM,
            "text": SEARCH_TERM,
            "keyword": SEARCH_TERM,
            "term": SEARCH_TERM,
            "search_query": SEARCH_TERM,
            "search_term": SEARCH_TERM,
        },
        "detail": {
            "place_id": None,
            "id": None,
            "poi_id": None,
            "uid": None,
            "location_id": None,
        },
    }

    for name in properties:
        normalized = name.lower()

        if normalized in aliases[kind]:
            value = aliases[kind][normalized]

            if value is not None:
                args[name] = value

    missing = [name for name in required if name not in args]

    if missing:
        raise ValueError(
            f"Cannot automatically fill required parameters: {missing}\n"
            f"Actual schema: {json.dumps(schema, ensure_ascii=False, indent=2)}"
        )

    return args


def find_place_ids(data):
    """Recursively discover possible place identifiers in search results."""
    found = []

    id_keys = {
        "place_id",
        "poi_id",
        "id",
        "uid",
        "location_id",
    }

    if isinstance(data, dict):
        for key, value in data.items():
            if key.lower() in id_keys and isinstance(value, (str, int)):
                if str(value).strip():
                    found.append(str(value))

            else:
                found.extend(find_place_ids(value))

    elif isinstance(data, list):
        for item in data:
            found.extend(find_place_ids(item))

    return list(dict.fromkeys(found))


async def inspect_server(source, config):
    print("\n" + "=" * 90)
    print(f"STARTING: {source.upper()}")
    print("=" * 90)

    server = StdioServerParameters(
        command=config["command"],
        args=[],
        env=None,
    )

    source_data = {
        "source": source,
        "search_term": SEARCH_TERM,
        "tools": [],
        "search": None,
        "details": [],
        "errors": [],
    }

    try:
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:

                await session.initialize()

                # Discover actual tools and their schemas.
                tools_response = await session.list_tools()

                print("\n[AVAILABLE TOOLS]")

                tool_map = {}

                for tool in tools_response.tools:
                    tool_map[tool.name] = tool

                    tool_info = {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": get_schema(tool),
                    }

                    source_data["tools"].append(tool_info)

                    print(f"\nTool: {tool.name}")
                    print("Description:", tool.description)
                    print(
                        json.dumps(
                            get_schema(tool),
                            ensure_ascii=False,
                            indent=2,
                        )
                    )

                # Search
                search_name = config["search_tool"]

                if search_name not in tool_map:
                    raise RuntimeError(f"Tool {search_name} not found!")

                search_tool = tool_map[search_name]

                search_args = build_arguments(search_tool, "search")

                print("\n[SEARCH]")
                print("Tool:", search_name)
                print("Arguments:", search_args)

                search_result = await session.call_tool(
                    search_name,
                    arguments=search_args,
                )

                search_payload = extract_payload(search_result)
                source_data["search"] = search_payload

                print("\n[RAW SEARCH RESPONSE]")
                print(
                    json.dumps(
                        search_payload,
                        ensure_ascii=False,
                        indent=2,
                        default=serialize,
                    )
                )

                if getattr(search_result, "isError", False):
                    raise RuntimeError("MCP search returned an error.")

                # Find IDs in all search response structures.
                place_ids = find_place_ids(search_payload)

                print("\n[DISCOVERED PLACE IDS]")
                print(place_ids)

                # Fetch details for each result.
                detail_name = config["detail_tool"]

                if detail_name not in tool_map:
                    print(f"Detail tool {detail_name} not available.")
                else:
                    detail_tool = tool_map[detail_name]

                    print("\n[PLACE DETAILS]")

                    for place_id in place_ids:

                        try:
                            schema = get_schema(detail_tool)
                            properties = schema.get("properties", {})
                            required = schema.get("required", [])

                            id_candidates = {
                                "place_id",
                                "id",
                                "poi_id",
                                "uid",
                                "location_id",
                            }

                            id_field = next(
                                (
                                    name
                                    for name in properties
                                    if name.lower() in id_candidates
                                ),
                                None,
                            )

                            if not id_field:
                                raise ValueError(
                                    f"Could not identify ID field: {schema}"
                                )

                            detail_args = {id_field: place_id}

                            missing = [
                                name for name in required if name not in detail_args
                            ]

                            if missing:
                                print(
                                    f"Skipping {place_id}: "
                                    f"additional required arguments {missing}"
                                )
                                continue

                            print(f"\nFetching: {place_id}")

                            detail_result = await session.call_tool(
                                detail_name,
                                arguments=detail_args,
                            )

                            detail_payload = extract_payload(detail_result)

                            source_data["details"].append(
                                {
                                    "place_id": place_id,
                                    "arguments": detail_args,
                                    "response": detail_payload,
                                }
                            )

                            print(
                                json.dumps(
                                    detail_payload,
                                    ensure_ascii=False,
                                    indent=2,
                                    default=serialize,
                                )
                            )

                        except Exception as exc:
                            error = {
                                "place_id": place_id,
                                "error": str(exc),
                            }

                            source_data["errors"].append(error)
                            print("[DETAIL ERROR]", error)

    except Exception as exc:
        source_data["errors"].append({"error": str(exc)})

        print(f"\n[SERVER ERROR] {exc}")

    output_file = OUTPUT_DIR / f"{source}.json"

    output_file.write_text(
        json.dumps(
            source_data,
            ensure_ascii=False,
            indent=2,
            default=serialize,
        ),
        encoding="utf-8",
    )

    print(f"\nSaved: {output_file.resolve()}")

    return source_data


async def main():
    results = {}

    for source, config in SERVERS.items():
        results[source] = await inspect_server(source, config)

    combined_file = OUTPUT_DIR / "all_results.json"

    combined_file.write_text(
        json.dumps(
            results,
            ensure_ascii=False,
            indent=2,
            default=serialize,
        ),
        encoding="utf-8",
    )

    print("\n" + "=" * 90)
    print("ALL TESTS FINISHED")
    print("=" * 90)
    print(f"Results directory: {OUTPUT_DIR.resolve()}")
    print(f"Combined JSON: {combined_file.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
