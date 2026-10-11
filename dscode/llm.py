"""DeepSeek Chat Completions: tool_calls / role=tool, not Anthropic blocks."""
import json
import threading
import time
import urllib.error
import urllib.request


class APIError(RuntimeError):
    pass


class DeepSeek:
    def __init__(self, config):
        self.config = config
        self.tokens = 0
        self.calls = 0
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(4)

    def chat(self, messages, tools=None, max_tokens=None):
        c = self.config
        if not c.key or c.key == "YOUR_DEEPSEEK_API_KEY":
            raise APIError("请在应用目录 .env 中填写 DEEPSEEK_API_KEY。")
        body = {"model": c.model, "messages": messages, "stream": False,
                "max_tokens": max_tokens or c.max_tokens}
        if c.thinking in ("enabled", "disabled"):
            body["thinking"] = {"type": c.thinking}
        if tools:
            body["tools"] = tools
        with self.slots:
            with self.lock:
                if c.token_budget and self.tokens >= c.token_budget:
                    raise APIError("本进程 Token 预算已达到；请检查 /status 后再启动新进程。")
            request = urllib.request.Request(
                c.base_url + "/chat/completions",
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"Authorization": "Bearer " + c.key,
                         "Content-Type": "application/json"}, method="POST")
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(request, timeout=c.timeout) as response:
                        data = json.load(response)
                    break
                except urllib.error.HTTPError as exc:
                    detail = exc.read().decode("utf-8", errors="replace")[:1500]
                    detail = detail.replace(c.key, "[REDACTED]")
                    if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    raise APIError(f"DeepSeek HTTP {exc.code}: {detail}") from None
                except (urllib.error.URLError, TimeoutError) as exc:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    raise APIError(f"连接 DeepSeek 失败：{exc}") from None
            try:
                choice = data["choices"][0]
                raw = choice["message"]
                # Preserve reasoning_content verbatim for thinking + tool calls.
                message = {k: raw[k] for k in
                           ("role", "content", "reasoning_content", "tool_calls") if k in raw}
                message["role"] = "assistant"
                message.setdefault("content", None)
                with self.lock:
                    self.tokens += data.get("usage", {}).get("total_tokens", 0)
                    self.calls += 1
                return message, choice.get("finish_reason", "stop")
            except (KeyError, IndexError, TypeError):
                raise APIError("API 响应不包含有效的 choices/message。") from None


def json_answer(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return value
