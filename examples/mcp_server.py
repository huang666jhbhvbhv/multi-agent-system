"""A real, dependency-free MCP stdio server with deterministic math/text tools.

Run by mcp.json via subprocess. Stdout is reserved exclusively for JSON-RPC.
"""
import json
import sys

TOOLS = [
    {"name": "add", "description": "Add two numbers exactly using Python arithmetic.",
     "inputSchema": {"type": "object", "properties": {
         "a": {"type": "number"}, "b": {"type": "number"}}, "required": ["a", "b"]}},
    {"name": "text_stats", "description": "Count characters and lines in supplied text.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
]


def handle(message):
    method = message.get("method")
    if method == "initialize":
        return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "deepseek-code-example", "version": "1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        params = message["params"]
        args = params.get("arguments", {})
        try:
            if params["name"] == "add":
                if any(not isinstance(args[k], (int, float)) or isinstance(args[k], bool) for k in ("a", "b")):
                    raise ValueError("a and b must be numbers")
                result = args["a"] + args["b"]
            elif params["name"] == "text_stats":
                text = args["text"]
                if not isinstance(text, str):
                    raise ValueError("text must be a string")
                result = {"characters": len(text), "lines": len(text.splitlines())}
            else:
                raise ValueError("Unknown tool")
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
        except Exception as exc:
            return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
    raise ValueError("Unknown method")


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if "id" not in message:
                continue
            try:
                reply = {"jsonrpc": "2.0", "id": message["id"], "result": handle(message)}
            except Exception as exc:
                reply = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": str(exc)}}
            print(json.dumps(reply, ensure_ascii=False), flush=True)
        except ValueError:
            pass


if __name__ == "__main__":
    main()
