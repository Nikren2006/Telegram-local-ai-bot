#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
openai_compat.py (v3) — универсальный клиент чата для tgbot.py.

Почему v3: шлюз отвечает успешно (200), но формат ответа отличается от
классического OpenAI. v3 делает так:
  1) сначала пробует SSE-стрим и понимает форматы OpenAI, Anthropic,
     Google Gemini, NDJSON и обёртки {"data": {...}};
  2) если стрим не дал текста — запрашивает цельный ответ и вытаскивает
     текст/tool_calls из любого из этих форматов (или открытого текста);
  3) если распознать не удалось — поднимает APIError с сырым фрагментом
     ответа шлюза (видно в терминале бота — сразу ясна причина).

Интерфейс совместим с тем, что использует tgbot.py:
  OpenAI(base_url, api_key), client.chat.completions.create(..., stream=True),
  client.models.retrieve/list, исключения APIError/BadRequestError/
  AuthenticationError/NotFoundError/InternalServerError/APIConnectionError/
  APITimeoutError. Отладка сырых ответов: AI_DEBUG_RAW=1.
"""

import json
import os
import sys
import types
from urllib.parse import quote

import httpx

__all__ = [
    "OpenAI",
    "APIError", "APIStatusError", "BadRequestError", "AuthenticationError",
    "NotFoundError", "InternalServerError", "APIConnectionError",
    "APITimeoutError",
]

DEFAULT_TIMEOUT = 600.0
_DEBUG_RAW = os.getenv("AI_DEBUG_RAW", "0") == "1"


# ---------------------------------------------------------------------------
# Исключения (те же имена, что в openai SDK)
# ---------------------------------------------------------------------------

class APIError(Exception):
    def __init__(self, message, *, body=None):
        super().__init__(message)
        self.body = body


class APIStatusError(APIError):
    def __init__(self, message, *, status_code, body=None):
        super().__init__(message, body=body)
        self.status_code = status_code


class BadRequestError(APIStatusError):        # 400
    pass


class AuthenticationError(APIStatusError):    # 401/403
    pass


class NotFoundError(APIStatusError):          # 404
    pass


class InternalServerError(APIStatusError):    # 5xx — бот ретраит
    pass


class APIConnectionError(APIError):
    pass


class APITimeoutError(APIConnectionError):
    pass


_STATUS_ERRORS = {400: BadRequestError, 401: AuthenticationError,
                  403: AuthenticationError, 404: NotFoundError}


def _raise_for_status(resp: httpx.Response) -> None:
    code = resp.status_code
    if 200 <= code < 300:
        return
    try:
        body = resp.json()
    except Exception:
        body = None
    detail = (json.dumps(body, ensure_ascii=False)[:300]
              if body is not None else resp.text[:300])
    message = f"AI-шлюз вернул HTTP {code}: {detail}"
    if code >= 500:
        raise InternalServerError(message, status_code=code, body=body)
    cls = _STATUS_ERRORS.get(code, APIStatusError)
    raise cls(message, status_code=code, body=body)


# ---------------------------------------------------------------------------
# Универсальное извлечение текста и tool_calls из ответа любого формата
# ---------------------------------------------------------------------------

def _content_to_text(content):
    """content: str | None | список частей -> текст (str) или None."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                if isinstance(p.get("text"), str):
                    parts.append(p["text"])
                elif isinstance(p.get("content"), str):
                    parts.append(p["content"])
        joined = "".join(parts)
        return joined or None
    return str(content)


def _tc_from_args(index, tc_id, name, arguments):
    if not isinstance(arguments, str):
        try:
            arguments = json.dumps(arguments or {}, ensure_ascii=False)
        except Exception:
            arguments = "{}"
    return {"index": index, "id": tc_id, "name": name, "arguments": arguments}


def _tool_calls_from_openai(raw):
    tcs = []
    if not isinstance(raw, list):
        return tcs
    for i, tc in enumerate(raw):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        tcs.append(_tc_from_args(tc.get("index", i),
                                 tc.get("id") or f"call_{i}",
                                 fn.get("name") or tc.get("name") or "",
                                 fn.get("arguments")))
    return tcs


def _normalize_response(data):
    """-> (content|None, [tool_calls], finish_reason|None) или None."""
    if not isinstance(data, dict):
        return None

    # 1) OpenAI chat.completions (и legacy completions)
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        ch = choices[0]
        msg = ch.get("message")
        if isinstance(msg, dict):
            return (_content_to_text(msg.get("content")),
                    _tool_calls_from_openai(msg.get("tool_calls")),
                    ch.get("finish_reason"))
        delta = ch.get("delta")
        if isinstance(delta, dict):
            return (_content_to_text(delta.get("content")),
                    _tool_calls_from_openai(delta.get("tool_calls")),
                    ch.get("finish_reason"))
        if isinstance(ch.get("text"), str):
            return ch["text"], [], ch.get("finish_reason")

    # 2) OpenAI Responses API
    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        return data["output_text"], [], None
    output = data.get("output")
    if isinstance(output, list):
        texts, tcs = [], []
        for item in output:
            if not isinstance(item, dict):
                continue
            c = _content_to_text(item.get("content"))
            if c:
                texts.append(c)
            if item.get("type") == "function_call" or (
                    item.get("name") and "arguments" in item):
                tcs.append(_tc_from_args(len(tcs),
                                         item.get("call_id") or item.get("id") or f"call_{len(tcs)}",
                                         item.get("name", ""),
                                         item.get("arguments")))
        if texts or tcs:
            return ("".join(texts) or None), tcs, None

    # 3) Anthropic messages API
    blocks = data.get("content")
    if isinstance(blocks, list) or "stop_reason" in data or data.get("role") == "assistant":
        if isinstance(blocks, list):
            texts, tcs = [], []
            for i, b in enumerate(blocks):
                if isinstance(b, str):
                    texts.append(b)
                elif isinstance(b, dict):
                    if b.get("type") == "tool_use":
                        tcs.append(_tc_from_args(i, b.get("id") or f"call_{i}",
                                                 b.get("name", ""), b.get("input")))
                    elif isinstance(b.get("text"), str) and b["text"]:
                        texts.append(b["text"])
            if texts or tcs:
                return ("".join(texts) or None), tcs, data.get("stop_reason")
        elif isinstance(blocks, str):
            return blocks, [], data.get("stop_reason")

    # 4) Google Gemini
    candidates = data.get("candidates")
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        texts, tcs = [], []
        for p in parts:
            if not isinstance(p, dict):
                continue
            if isinstance(p.get("text"), str):
                texts.append(p["text"])
            fc = p.get("functionCall")
            if isinstance(fc, dict):
                tcs.append(_tc_from_args(len(tcs), f"call_{len(tcs)}",
                                         fc.get("name", ""), fc.get("args")))
        if texts or tcs:
            return ("".join(texts) or None), tcs, candidates[0].get("finishReason")

    # 5) последний шанс: любые строковые text/content в глубине объекта
    found = []

    def _walk(node):
        if len(found) >= 10:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("text", "content", "output_text") and isinstance(v, str) and v.strip():
                    found.append(v)
                else:
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    if found:
        return "\n".join(found)[:20000], [], None
    return None


# ---------------------------------------------------------------------------
# Разбор строк стрима (SSE / NDJSON / открытый текст)
# ---------------------------------------------------------------------------

def _events_from_event(ev):
    """dict события -> список пар (текст|None, tool_call|None)."""
    out = []
    if not isinstance(ev, dict):
        return out
    inner = ev.get("data")
    if isinstance(inner, dict) and "choices" not in ev:
        return _events_from_event(inner)  # обёртка {"data": {...}}

    choices = ev.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        ch = choices[0]
        node = (ch.get("delta") if isinstance(ch.get("delta"), dict)
                else ch.get("message") if isinstance(ch.get("message"), dict)
                else ch)
        text = _content_to_text(node.get("content"))
        if text:
            out.append((text, None))
        if not text and isinstance(node.get("text"), str) and node["text"]:
            out.append((node["text"], None))
        for i, tc in enumerate(node.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            out.append((None, {
                "index": tc.get("index", i), "id": tc.get("id"),
                "name": fn.get("name") or tc.get("name"),
                "arguments": fn.get("arguments")
                             if isinstance(fn.get("arguments"), str) else None,
            }))
        return out

    etype = ev.get("type")  # Anthropic SSE
    if etype == "content_block_delta":
        d = ev.get("delta") or {}
        if isinstance(d.get("text"), str) and d["text"]:
            out.append((d["text"], None))
        elif isinstance(d.get("partial_json"), str) and d["partial_json"]:
            out.append((None, {"index": ev.get("index", 0), "id": None,
                               "name": None, "arguments": d["partial_json"]}))
        return out
    if etype == "content_block_start":
        b = ev.get("content_block") or {}
        if isinstance(b, dict) and b.get("type") == "tool_use":
            out.append((None, {"index": ev.get("index", 0), "id": b.get("id"),
                               "name": b.get("name"), "arguments": ""}))
        elif isinstance(b, dict) and isinstance(b.get("text"), str) and b["text"]:
            out.append((b["text"], None))
        return out

    candidates = ev.get("candidates")  # Gemini SSE
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        for p in parts:
            if not isinstance(p, dict):
                continue
            if isinstance(p.get("text"), str) and p["text"]:
                out.append((p["text"], None))
            fc = p.get("functionCall")
            if isinstance(fc, dict):
                out.append((None, {"index": len(out), "id": None,
                                   "name": fc.get("name", ""),
                                   "arguments": json.dumps(fc.get("args") or {},
                                                           ensure_ascii=False)}))
        return out

    if isinstance(ev.get("text"), str) and ev["text"]:  # простой {"text": "..."}
        out.append((ev["text"], None))
    return out


def _events_from_line(line):
    out = []
    s = (line or "").strip()
    if not s:
        return out
    if s.startswith("data:"):
        s = s[5:].strip()
        if not s or s == "[DONE]":
            return out
    if not s.startswith("{"):
        return out
    try:
        ev = json.loads(s)
    except ValueError:
        return out
    return _events_from_event(ev)


def _chunk(content=None, tool_calls=None, finish_reason=None):
    tcs = [
        types.SimpleNamespace(
            index=tc.get("index", i), id=tc.get("id"), type="function",
            function=types.SimpleNamespace(name=tc.get("name"),
                                           arguments=tc.get("arguments")),
        )
        for i, tc in enumerate(tool_calls or [])
    ]
    return types.SimpleNamespace(choices=[types.SimpleNamespace(
        index=0,
        delta=types.SimpleNamespace(role="assistant", content=content,
                                    tool_calls=tcs or None),
        finish_reason=finish_reason)])


# ---------------------------------------------------------------------------
# Клиент
# ---------------------------------------------------------------------------

class _Completions:
    def __init__(self, client: "OpenAI"):
        self._client = client

    def create(self, model, messages, *, temperature=None, stream=False,
               tools=None, timeout=None, **extra):
        payload = {"model": model, "messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools
        payload.update(extra)
        return self._stream_events(payload, timeout)

    def _stream_events(self, payload, timeout):
        client = self._client
        body = {k: v for k, v in payload.items() if k != "stream"}
        stream_body = dict(body)
        stream_body["stream"] = True
        headers = client._headers()
        headers["Accept"] = "text/event-stream"

        # --- Попытка 1: стрим (шлюз с maxWaitMs рвёт долгие цельные запросы)
        got_any = False
        raw_sample = []
        try:
            with httpx.stream("POST", client._url("/chat/completions"),
                              json=stream_body, headers=headers,
                              timeout=timeout or client.timeout) as resp:
                _raise_for_status(resp)
                for line in resp.iter_lines():
                    if not got_any and len(raw_sample) < 12 and line.strip():
                        raw_sample.append(line.strip()[:300])
                    for text, tc in _events_from_line(line):
                        got_any = True
                        yield _chunk(text, [tc] if tc else None)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if got_any:
                return  # часть текста уже отдали — не превращаем в ошибку
            if isinstance(e, httpx.TimeoutException):
                raise APITimeoutError(str(e)) from e
            raise APIConnectionError(str(e)) from e
        if got_any:
            return

        # --- Попытка 2: цельный ответ (шлюз мог проигнорировать stream:true)
        try:
            data = client._request("POST", "/chat/completions", body,
                                   timeout, allow_text=True)
        except httpx.TimeoutException as e:
            raise APITimeoutError(str(e)) from e
        except httpx.TransportError as e:
            raise APIConnectionError(str(e)) from e

        if isinstance(data, dict) and data.get("__raw_text__", "").strip():
            if _DEBUG_RAW:
                print("[openai_compat] сырой ответ:\n" + data["__raw_text__"][:2000],
                      file=sys.stderr)
            yield _chunk(data["__raw_text__"])
            return

        parsed = _normalize_response(data)
        if parsed and (parsed[0] or parsed[1]):
            if _DEBUG_RAW and isinstance(data, dict):
                print("[openai_compat] сырой ответ:\n" +
                      json.dumps(data, ensure_ascii=False)[:2000], file=sys.stderr)
            yield _chunk(parsed[0], parsed[1], parsed[2])
            return

        try:
            ns_snippet = json.dumps(data, ensure_ascii=False)[:600]
        except Exception:
            ns_snippet = str(data)[:600]
        raise APIError(
            "Не удалось распознать ответ шлюза ни в одном формате.\n"
            f"Стрим, первые строки: {raw_sample or ['(пусто)']}\n"
            f"Цельный ответ: {ns_snippet}")


class _Models:
    def __init__(self, client: "OpenAI"):
        self._client = client

    def retrieve(self, model):
        data = self._client._request("GET", f"/models/{quote(str(model), safe='')}")
        inner = data.get("data") if isinstance(data, dict) else data
        return _Obj(inner if isinstance(inner, dict) else {})

    def list(self):
        data = self._client._request("GET", "/models")
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            items = []
        return types.SimpleNamespace(data=[_Obj(m) for m in items
                                           if isinstance(m, dict)])


class _Obj:
    def __init__(self, data):
        object.__setattr__(self, "_data", data if isinstance(data, dict) else {})

    def __getattr__(self, key):
        data = object.__getattribute__(self, "_data")
        if key in data:
            return data[key]
        raise AttributeError(key)

    def __repr__(self):
        return f"_Obj({object.__getattribute__(self, '_data')!r})"

    def model_dump(self):
        return dict(object.__getattribute__(self, "_data"))


class OpenAI:
    def __init__(self, *, base_url="https://api.openai.com/v1", api_key=None,
                 timeout=DEFAULT_TIMEOUT, **_ignored):
        self.base_url = str(base_url).rstrip("/")
        self.api_key = api_key or ""
        self.timeout = float(timeout)
        self.chat = types.SimpleNamespace(completions=_Completions(self))
        self.models = _Models(self)

    def _url(self, path: str) -> str:
        return self.base_url + path

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(self, method, path, payload=None, timeout=None, allow_text=False):
        try:
            resp = httpx.request(method, self._url(path), json=payload,
                                 headers=self._headers(),
                                 timeout=timeout or self.timeout)
        except httpx.TimeoutException as e:
            raise APITimeoutError(str(e)) from e
        except httpx.TransportError as e:
            raise APIConnectionError(str(e)) from e
        _raise_for_status(resp)
        try:
            return resp.json()
        except ValueError:
            if allow_text and resp.text.strip():
                return {"__raw_text__": resp.text}
            raise APIError(f"Некорректный JSON в ответе шлюза: {resp.text[:200]}") from None


if __name__ == "__main__":
    c = OpenAI(base_url=os.getenv("AI_BASE_URL", "http://localhost:8080/v1"),
               api_key=os.getenv("AI_API_KEY", "test"))
    try:
        ms = c.models.list()
        print(f"Шлюз отвечает, моделей: {len(ms.data)}")
    except APIError as e:
        print("Шлюз недоступен:", e)
        raise SystemExit(1)
    try:
        s = c.chat.completions.create(
            model=os.getenv("AI_MODEL", "deepseek-v4-flash-vision-exp"),
            messages=[{"role": "user", "content": "Ответь одним словом: тест"}],
            stream=True)
        text = "".join(ev.choices[0].delta.content or "" for ev in s if ev.choices)
        print("Ответ модели:", repr(text))
    except APIError as e:
        print("ОШИБКА:", e)
