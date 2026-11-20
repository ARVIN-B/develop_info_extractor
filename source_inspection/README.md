# Source inspection

The runner captures live MCP tool definitions and read-only results. Each invocation writes to a unique runs/<timestamp>_<id>/ directory; prior raw responses are retained.

From the project root:

```powershell
.\.venv\Scripts\python.exe scripts\inspect_sources.py --list
.\.venv\Scripts\python.exe scripts\inspect_sources.py --all
.\.venv\Scripts\python.exe scripts\inspect_sources.py --source neshan-mcp
.\.venv\Scripts\python.exe scripts\inspect_sources.py --source neshan-mcp --operation search
```

The current runner discovers tools from each server's live MCP session. It only executes tools identified as read-only by MCP annotations or by their operation names and descriptions. Identifier-based detail calls use identifiers found in that run's real search responses. Unclear or unsafe operations are saved as skipped records. Errors are stored separately from raw successful results.

The runner uses executables from the adjacent info_extractor/.venv/Scripts directory when present, then falls back to this project's .venv/Scripts. It inherits the current process environment, never writes environment variable values, and reports proxy variable presence only. API credential requirements remain unknown unless confirmed by local configuration or live authentication evidence.

summary.json and credentials_required.json at this directory root select the newest evidence per source. Earlier records remain under runs/. sources.json describes the requested scope and local integration evidence, not successful live behavior.

The original maps_debug/*.json and *_tools.json files predate this runner. They are not counted as live responses by the runner because the saved debug records show empty tool/response collections and generic startup errors.
