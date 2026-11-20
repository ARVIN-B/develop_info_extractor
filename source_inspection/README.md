# Raw MCP output collector

Run the collector from the project root:

```powershell
.\.venv\Scripts\python.exe scripts\inspect_sources.py --list
.\.venv\Scripts\python.exe scripts\inspect_sources.py --all
.\.venv\Scripts\python.exe scripts\inspect_sources.py --source neshan-mcp
```

Each invocation creates a unique directory under runs/. An all-source run contains exactly one primary JSON file for each configured MCP. Each file holds connection and initialization results, complete MCP tool definitions, per-tool execution records, sent arguments, original MCP result objects, and any failures or skip reasons.

The collector does not summarize or classify response contents. Text content from the MCP result is retained as returned. It may parse returned content in memory only to route a real identifier to a dependent tool. Such parsed data is never substituted for or written instead of the original result.

The executable lookup checks the adjacent info_extractor/.venv/Scripts directory and then this project's .venv/Scripts directory. A missing executable is recorded in that source's JSON file and does not stop the other sources.

Older run folders are retained as historical evidence from the previous collector. New runs use the one-file-per-source format above.
