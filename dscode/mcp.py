"""Actual MCP stdio JSON-RPC transport (no fake server objects)."""
import json
import os
import queue
import subprocess
import sys
import threading

from .state import read_json, identifier


class StdioMCP:
    def __init__(self, spec, workspace, app):
        command = spec["command"]
        if command == "{python}":
            command = sys.executable
        args = [s.replace("{workspace}", str(workspace)).replace("{app}", str(app)) for s in spec.get("args", [])]
        env = dict(os.environ)
        # Credentials can be supplied as env var names, not committed literal values.
        env.update({k: os.environ[v] for k, v in spec.get("envFrom", {}).items()})
        self.process = subprocess.Popen([command, *args], cwd=workspace,
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}))
        self.pending = {}
        self.lock = threading.RLock()
        self.counter = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "deepseek-code", "version": "1.0.0"}})
            self.notify("notifications/initialized", {})
            self.tools = []
            cursor = None
            for _ in range(50):
                page = self.request("tools/list", {"cursor": cursor} if cursor else {})
                self.tools.extend(page.get("tools", []))
                cursor = page.get("nextCursor")
                if not cursor:
                    break
        except Exception:
            self.close()
            raise

    def _write(self, data):
        with self.lock:
            self.process.stdin.write(json.dumps(data, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if "method" in message:
                    if "id" in message:
                        if message["method"] == "ping":
                            self._write({"jsonrpc": "2.0", "id": message["id"], "result": {}})
                        else:
                            self._write({"jsonrpc": "2.0", "id": message["id"],
                                         "error": {"code": -32601, "message": "Unsupported client method"}})
                    continue
                with self.lock:
                    waiter = self.pending.get(message.get("id"))
                if waiter:
                    waiter.put(message)
        finally:
            with self.lock:
                for waiter in self.pending.values():
                    waiter.put({"error": {"message": "MCP process closed"}})

    def request(self, method, params):
        with self.lock:
            self.counter += 1
            request_id = self.counter
            waiter = self.pending[request_id] = queue.Queue()
        try:
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            message = waiter.get(timeout=60)
            if "error" in message:
                raise RuntimeError(str(message["error"]))
            return message["result"]
        except queue.Empty:
            raise TimeoutError("MCP request timeout: " + method) from None
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def notify(self, method, params):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def call(self, name, **arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        text = "\n".join(b.get("text", "[non-text content]") for b in result.get("content", []))
        if result.get("structuredContent") is not None:
            text += "\n" + json.dumps(result["structuredContent"], ensure_ascii=False)
        return ("MCP error: " if result.get("isError") else "") + text

    def close(self):
        from .tools import Shell
        Shell.terminate(self.process)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()


class MCPManager:
    def __init__(self, config):
        self.config = config
        self.clients = {}
        self.lock = threading.RLock()

    def specs(self):
        return read_json(self.config.mcp_file, {"mcpServers": {}}).get("mcpServers", {})

    def connect(self, name):
        from .config import APP
        identifier(name)
        with self.lock:
            if name not in self.clients:
                spec = self.specs().get(name)
                if not spec:
                    raise ValueError("Unknown MCP server. Configure mcp.json first.")
                if spec.get("transport", "stdio") != "stdio":
                    raise ValueError("This version supports stdio MCP only")
                self.clients[name] = StdioMCP(spec, self.config.workspace, APP)
            return [t["name"] for t in self.clients[name].tools]

    def register(self, reg):
        with self.lock:
            for server, client in self.clients.items():
                for tool in client.tools:
                    raw = tool["name"]
                    identifier(raw)
                    name = "mcp__" + server + "__" + raw
                    if len(name) > 64:
                        raise ValueError("MCP tool name exceeds 64 chars")
                    schema = tool.get("inputSchema", {"type": "object", "properties": {}})
                    def call(_client=client, _name=raw, **args):
                        return _client.call(_name, **args)
                    # Server hints do not grant permission. All external calls require approval.
                    reg.add(name, tool.get("description", "MCP tool"), call,
                            schema.get("properties", {}), schema.get("required", []), "mcp")
                    reg.entries[name][0]["function"]["parameters"] = schema

    def close(self):
        for client in list(self.clients.values()):
            client.close()
