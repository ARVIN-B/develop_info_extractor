import asyncio
import json
import traceback

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_server(name, executable):
    print("\n" + "=" * 70)
    print(f"TESTING: {name}")
    print("=" * 70)

    params = StdioServerParameters(
        command=executable,
        args=[],
        env=None,
    )

    try:
        async with stdio_client(params) as (read, write):
            print("[OK] STDIO connection established")

            async with ClientSession(read, write) as session:

                print("[OK] ClientSession created")

                result = await session.initialize()

                print("[OK] MCP initialized")

                print("\nSERVER INFORMATION:")
                print(
                    json.dumps(
                        result.server_info.model_dump(mode="json"),
                        ensure_ascii=False,
                        indent=2,
                    )
                )

                # Discover all available tools
                tools_response = await session.list_tools()

                print("\n" + "-" * 70)
                print(f"AVAILABLE TOOLS: {name}")
                print("-" * 70)

                all_tools = []

                for tool in tools_response.tools:

                    tool_info = {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": tool.input_schema,
                    }

                    all_tools.append(tool_info)

                    print(f"\nTOOL NAME: {tool.name}")
                    print(f"DESCRIPTION: {tool.description}")

                    print("\nINPUT SCHEMA:")
                    print(
                        json.dumps(
                            tool.input_schema,
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    
                    
                    
                    
                    
                    tool_info = tool.model_dump(mode="json")

                    all_tools.append(tool_info)

                    print(f"\nTOOL NAME: {tool.name}")
                    print(f"DESCRIPTION: {tool.description}")
                    print("\nFULL TOOL DATA:")
                    print(json.dumps(
                        tool_info,
                        ensure_ascii=False,
                        indent=2,
                    ))

                # Save discovered tools
                filename = f"{name.lower()}_tools.json"

                with open(
                    filename,
                    "w",
                    encoding="utf-8",
                ) as f:
                    json.dump(
                        {
                            "server": name,
                            "server_info": result.server_info.model_dump(mode="json"),
                            "tools": all_tools,
                        },
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )

                print(f"\n[SAVED] {filename}")
                print(f"TOTAL TOOLS: {len(all_tools)}")

    except Exception as exc:
        print(f"\n[ERROR] {name}")
        traceback.print_exc()

        if isinstance(exc, BaseExceptionGroup):
            for sub_exc in exc.exceptions:
                traceback.print_exception(sub_exc)


async def main():
    await test_server(
        "NESHAN",
        r".\.venv\Scripts\neshan-mcp.exe",
    )

    await test_server(
        "BALAD",
        r".\.venv\Scripts\balad-mcp.exe",
    )


if __name__ == "__main__":
    asyncio.run(main())
