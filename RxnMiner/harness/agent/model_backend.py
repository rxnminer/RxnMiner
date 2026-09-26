"""Provider-abstraction for the agent's model call (feature A, step 1 — additive, run() not yet rewired).

The agent loop in chem_harness.run() is a tool-use conversation. Today it is hardwired to Vertex Gemini
(GenerativeModel + start_chat + send_message, parse function_call parts, Part.from_data images). This module
extracts that into a narrow `ModelBackend` interface + adapters so the SAME loop can later target a user's own
key across Gemini / OpenAI / Anthropic. Tool *implementations* and the SYSTEM skill stay provider-agnostic.

Design (see local_work/MULTIPROVIDER_DESIGN.md):
- A backend owns the conversation state. `send(parts)` appends `parts` as a user turn, calls the model, and
  returns a neutral `Turn`. run() builds `parts` with backend.text/image/tool_result so it never touches a
  provider type.
- `Turn` = {text, tool_calls:[{id,name,args}], usage:{in,out,cached}, finish_reason}.
- v1 ships GeminiVertexBackend (must be byte-for-byte the current behavior) + GeminiAPIBackend (BYO Gemini key).
  OpenAI/Anthropic adapters slot in later behind the same interface.

This module imports vertexai LAZILY (only when a Gemini backend is constructed) so importing it offline (tests)
costs nothing and never needs creds.
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Turn:
    """One assistant response, provider-neutral."""
    text: str = ""
    tool_calls: list = field(default_factory=list)   # [{"id": str|None, "name": str, "args": dict}]
    usage: dict = field(default_factory=lambda: {"in": 0, "out": 0, "cached": 0})
    finish_reason: str = ""


def neutral_tool_decls(table: dict) -> list:
    """The chem_harness TOOLS table {name: (fn, desc, params)} -> a neutral JSON-Schema decl list that every
    adapter can translate to its own format. `params` is {argname: type} where type is a JSON-schema scalar
    or 'array' (an array of numbers, matching the existing _decls behavior)."""
    decls = []
    for name, (_fn, desc, params) in table.items():
        props = {}
        for k, t in params.items():
            props[k] = {"type": "array", "items": {"type": "number"}} if t == "array" else {"type": t}
        decls.append({"name": name, "description": desc,
                      "parameters": {"type": "object", "properties": props}})
    return decls


class ModelBackend(ABC):
    """Owns one agent conversation. run() calls send() each turn and builds parts via the *_part helpers."""
    supports_vision: bool = True

    @abstractmethod
    def send(self, parts: list) -> Turn:
        """Append `parts` (built via text_part/image_part/tool_result_part) as a user turn, call the model,
        return the assistant Turn. Implementations keep conversation state internally."""

    @abstractmethod
    def text_part(self, s: str): ...
    @abstractmethod
    def image_part(self, data: bytes, mime: str = "image/png"): ...
    @abstractmethod
    def tool_result_part(self, name: str, response: dict, call_id=None): ...

    def prune_images(self, keep_first: int = 1) -> None:
        """Drop image parts from the conversation history beyond the first `keep_first` (keeps the base figure),
        replacing them with a short text stub. Used by run()'s progressive-focus mode to keep the context lean
        after a first draft. Default: no-op (only the stateful Vertex chat needs/implements it)."""
        return None


# ───────────────────────────── Gemini (Vertex — current behavior) ─────────────────────────────
class GeminiVertexBackend(ModelBackend):
    """Wraps the EXACT Vertex path chem_harness.run() uses today. `model` may be injected (the offline
    _MockModel) to keep the self-test working; otherwise a GenerativeModel is built from the service account."""
    supports_vision = True

    def __init__(self, system: str, tool_decls: list, model_name: str, creds_path: str = None,
                 project: str = None, location: str = None, model=None):
        self._Part = None
        if model is None:
            import vertexai
            from google.oauth2 import service_account
            from vertexai.preview.generative_models import GenerativeModel, Tool, FunctionDeclaration
            creds = service_account.Credentials.from_service_account_file(str(creds_path))
            vertexai.init(project=project, location=location, credentials=creds)
            fds = [FunctionDeclaration(name=d["name"], description=d["description"], parameters=d["parameters"])
                   for d in tool_decls]
            model = GenerativeModel(model_name, system_instruction=system,
                                    tools=[Tool(function_declarations=fds)])
        self._model = model                           # kept so prune_images() can rebuild the chat with pruned history
        try:
            self.chat = model.start_chat(response_validation=False)
        except TypeError:
            self.chat = model.start_chat()            # the mock model takes no kwarg

    def _part(self):
        if self._Part is None:
            from vertexai.preview.generative_models import Part
            self._Part = Part
        return self._Part

    def text_part(self, s: str):
        return s                                       # Vertex accepts bare strings in the parts list

    def image_part(self, data: bytes, mime: str = "image/png"):
        return self._part().from_data(data, mime)

    def tool_result_part(self, name: str, response: dict, call_id=None):
        return self._part().from_function_response(name=name, response=response)

    def prune_images(self, keep_first: int = 1) -> None:
        """Rebuild the stateful chat dropping image parts beyond the first `keep_first` (the base figure), so the
        resent history stays lean after a first draft. Best-effort: any SDK-shape change -> leave history intact."""
        try:
            from vertexai.preview.generative_models import Content
            seen, newh = 0, []
            for c in self.chat.history:
                kept = []
                for p in c.parts:
                    try: is_img = "inline_data" in p.to_dict()
                    except Exception: is_img = False
                    if is_img:
                        seen += 1
                        if seen > keep_first:
                            kept.append(self._part().from_text("[image pruned to keep context lean]")); continue
                    kept.append(p)
                newh.append(Content(role=c.role, parts=kept))
            self.chat = self._model.start_chat(history=newh, response_validation=False)
        except Exception:
            pass

    def send(self, parts: list) -> Turn:
        resp = self.chat.send_message(parts)
        um = getattr(resp, "usage_metadata", None)
        usage = {"in": getattr(um, "prompt_token_count", 0) or 0,
                 "out": getattr(um, "candidates_token_count", 0) or 0,
                 "cached": getattr(um, "cached_content_token_count", 0) or 0} if um else {"in": 0, "out": 0, "cached": 0}
        text, calls, fr = "", [], ""
        try:
            cand = resp.candidates[0]
            fr = str(getattr(cand, "finish_reason", "") or "")
            for p in cand.content.parts:
                fc = getattr(p, "function_call", None)
                if fc:
                    calls.append({"id": None, "name": fc.name, "args": {k: v for k, v in (fc.args or {}).items()}})
        except Exception as e:
            fr = fr or f"parse_error: {e}"
        try:
            text = resp.text or ""
        except Exception:
            text = ""
        return Turn(text=text, tool_calls=calls, usage=usage, finish_reason=fr)


# ───────────────────────────── Gemini (BYO API key — google-genai) ─────────────────────────────
def _parse_gemini_turn(resp) -> Turn:
    """Both Gemini backends share the response surface: candidates[0].content.parts[*].function_call +
    usage_metadata + .text. Parse it into a neutral Turn (never raises)."""
    um = getattr(resp, "usage_metadata", None)
    usage = {"in": getattr(um, "prompt_token_count", 0) or 0,
             "out": getattr(um, "candidates_token_count", 0) or 0,
             "cached": getattr(um, "cached_content_token_count", 0) or 0} if um else {"in": 0, "out": 0, "cached": 0}
    text, calls, fr = "", [], ""
    try:
        cand = resp.candidates[0]
        fr = str(getattr(cand, "finish_reason", "") or "")
        for p in cand.content.parts:
            fc = getattr(p, "function_call", None)
            if fc:
                calls.append({"id": getattr(fc, "id", None), "name": fc.name,
                              "args": {k: v for k, v in (fc.args or {}).items()}})
    except Exception as e:
        fr = fr or f"parse_error: {e}"
    try:
        text = resp.text or ""
    except Exception:
        text = ""
    return Turn(text=text, tool_calls=calls, usage=usage, finish_reason=fr)


class GeminiAPIBackend(ModelBackend):
    """BYO Gemini: the user's own AIza... API key via the google-genai SDK (`from google import genai`), NOT
    Vertex/our creds. Uses a stateful genai chat (manages history) — same response surface as Vertex, so the parse
    is shared. ISOLATED from GeminiVertexBackend (the deployed path): wiring into run() is step 3b."""
    supports_vision = True

    def __init__(self, system: str, tool_decls: list, model_name: str, api_key: str = None, chat=None):
        self._types = None
        if chat is None:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=api_key)
            fds = [types.FunctionDeclaration(name=d["name"], description=d["description"], parameters=d["parameters"])
                   for d in tool_decls]
            cfg = types.GenerateContentConfig(system_instruction=system,
                                              tools=[types.Tool(function_declarations=fds)])
            chat = client.chats.create(model=model_name, config=cfg)
        self.chat = chat

    def _t(self):
        if self._types is None:
            from google.genai import types
            self._types = types
        return self._types

    def text_part(self, s: str):
        return self._t().Part(text=s)

    def image_part(self, data: bytes, mime: str = "image/png"):
        return self._t().Part.from_bytes(data=data, mime_type=mime)

    def tool_result_part(self, name: str, response: dict, call_id=None):
        return self._t().Part.from_function_response(name=name, response=response)

    def send(self, parts: list) -> Turn:
        # tolerate bare strings (run() has text literals) by wrapping them as text parts
        norm = [self._t().Part(text=p) if isinstance(p, str) else p for p in parts]
        return _parse_gemini_turn(self.chat.send_message(norm))


# ───────────────────────────── OpenAI (BYO API key — openai SDK) ─────────────────────────────
class OpenAIBackend(ModelBackend):
    """BYO OpenAI (or any OpenAI-compatible endpoint via base_url): the user's own sk-... key through the
    `openai` SDK. The Chat Completions API is STATELESS, so this backend owns the running `messages` list and
    replays it each call. The agent loop is identical to the Gemini path; only the wire format differs --
    content blocks for user turns, role='tool' results keyed by tool_call_id, and JSON-STRING tool arguments.
    `client` may be injected (a mock) for offline tests, exactly like the Gemini backends' `model`/`chat`."""
    supports_vision = True

    def __init__(self, system: str, tool_decls: list, model_name: str, api_key: str = None, client=None,
                 base_url: str = None, azure_endpoint: str = None, api_version: str = None,
                 extra_body: dict = None, reasoning_effort: str = None, max_tokens: int = None,
                 timeout: float = None, no_proxy: bool = False):
        self.model_name = model_name
        self.messages = [{"role": "system", "content": system}]
        self.tools = [{"type": "function",
                       "function": {"name": d["name"], "description": d["description"],
                                    "parameters": d["parameters"]}} for d in tool_decls]
        self._pending = []                              # tool_calls from the last assistant turn awaiting results
        # per-call knobs: vLLM enable_thinking (extra_body), GPT-5 reasoning_effort (NEVER temperature), token cap.
        self.extra_body, self.reasoning_effort, self.max_tokens = extra_body, reasoning_effort, max_tokens
        if client is None:
            # no_proxy: build an httpx client that ignores env proxies, so a LAN base_url (our vLLM) is reached
            # directly even though the server exports http_proxy/https_proxy for Vertex egress (same idea as
            # tools.py stripping PROXY_* for the MolParser CN-IP). httpx is an openai SDK dependency.
            http_client = None
            if no_proxy:
                import httpx
                http_client = httpx.Client(trust_env=False, timeout=timeout or 600.0)
            if azure_endpoint:                          # Azure OpenAI (api-key + api-version + endpoint)
                from openai import AzureOpenAI
                kw = {"api_key": api_key, "api_version": api_version or "2024-12-01-preview",
                      "azure_endpoint": azure_endpoint}
                if http_client is not None:
                    kw["http_client"] = http_client
                if timeout:
                    kw["timeout"] = timeout
                client = AzureOpenAI(**kw)
            else:
                from openai import OpenAI
                kw = {"api_key": api_key}
                if base_url:
                    kw["base_url"] = base_url
                if http_client is not None:
                    kw["http_client"] = http_client
                if timeout:
                    kw["timeout"] = timeout
                client = OpenAI(**kw)
        self.client = client

    def text_part(self, s: str):
        return {"type": "text", "text": s}

    def image_part(self, data: bytes, mime: str = "image/png"):
        import base64
        b64 = base64.b64encode(data).decode()
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}

    def tool_result_part(self, name: str, response: dict, call_id=None):
        return {"__tool_result__": True, "name": name, "response": response, "call_id": call_id}

    def send(self, parts: list) -> Turn:
        content, answered = [], set()
        for p in parts:
            if isinstance(p, str):
                content.append({"type": "text", "text": p})
            elif isinstance(p, dict) and p.get("__tool_result__"):
                cid = p.get("call_id")
                if not cid:                             # run() doesn't track ids -> match by name to a pending call
                    for q in self._pending:
                        if q["name"] == p["name"] and q["id"] not in answered:
                            cid = q["id"]; break
                if cid:
                    answered.add(cid)
                    self.messages.append({"role": "tool", "tool_call_id": cid,
                                          "content": json.dumps(p["response"])})
            elif isinstance(p, dict):
                content.append(p)                       # already an image/text content block
        # OpenAI rejects a turn unless EVERY prior tool_call got a tool response -> stub any we didn't answer
        for q in self._pending:
            if q["id"] not in answered:
                self.messages.append({"role": "tool", "tool_call_id": q["id"], "content": "(no result)"})
        self._pending = []
        if content:
            self.messages.append({"role": "user", "content": content})
        # NEVER pass temperature: GPT-5 reasoning models reject it. extra_body carries vLLM's enable_thinking;
        # reasoning_effort/max_tokens are passed only when configured. Unknown kwargs to a vanilla model would
        # error, so we add each conditionally.
        kw = {"model": self.model_name, "messages": self.messages, "tools": self.tools or None}
        if self.max_tokens:
            kw["max_tokens"] = self.max_tokens
        if self.reasoning_effort:
            kw["reasoning_effort"] = self.reasoning_effort
        if self.extra_body:
            kw["extra_body"] = self.extra_body
        resp = self.client.chat.completions.create(**kw)
        return self._consume(resp)

    def _consume(self, resp) -> Turn:
        choice = resp.choices[0]
        m = choice.message
        raw = list(getattr(m, "tool_calls", None) or [])
        amsg = {"role": "assistant", "content": getattr(m, "content", None) or ""}
        if raw:                                          # keep tool_calls in history or the next tool msgs are invalid
            amsg["tool_calls"] = [{"id": tc.id, "type": "function",
                                   "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                                  for tc in raw]
        self.messages.append(amsg)
        calls, fr = [], str(getattr(choice, "finish_reason", "") or "")
        for tc in raw:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args, fr = {}, "MALFORMED_FUNCTION_CALL"   # reuse run()'s malformed-retry recovery
            calls.append({"id": tc.id, "name": tc.function.name, "args": args})
        self._pending = [{"id": c["id"], "name": c["name"]} for c in calls]
        um = getattr(resp, "usage", None)
        cached = 0
        if um is not None:
            ptd = getattr(um, "prompt_tokens_details", None)
            cached = (getattr(ptd, "cached_tokens", 0) or 0) if ptd else 0
        usage = ({"in": getattr(um, "prompt_tokens", 0) or 0, "out": getattr(um, "completion_tokens", 0) or 0,
                  "cached": cached} if um else {"in": 0, "out": 0, "cached": 0})
        return Turn(text=(getattr(m, "content", None) or ""), tool_calls=calls, usage=usage, finish_reason=fr)


# ───────────────────────────── Anthropic (BYO API key — anthropic SDK) ─────────────────────────────
class AnthropicBackend(ModelBackend):
    """BYO Anthropic: the user's own sk-ant-... key via the `anthropic` SDK (Messages API). Stateless, so this
    backend owns the `messages` list. Differences from the others: `system` is a TOP-LEVEL create() param (not a
    message); tool args come back ALREADY PARSED (block.input is a dict, no JSON string); tool results live in a
    user message as tool_result blocks keyed by tool_use_id; max_tokens is REQUIRED. `client` is injectable."""
    supports_vision = True

    def __init__(self, system: str, tool_decls: list, model_name: str, api_key: str = None, client=None,
                 max_tokens: int = 16384):
        self.system, self.model_name, self.max_tokens = system, model_name, max_tokens
        self.messages = []
        self.tools = [{"name": d["name"], "description": d["description"], "input_schema": d["parameters"]}
                      for d in tool_decls]
        self._pending = []
        if client is None:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
        self.client = client

    def text_part(self, s: str):
        return {"type": "text", "text": s}

    def image_part(self, data: bytes, mime: str = "image/png"):
        import base64
        return {"type": "image", "source": {"type": "base64", "media_type": mime,
                                            "data": base64.b64encode(data).decode()}}

    def tool_result_part(self, name: str, response: dict, call_id=None):
        return {"__tool_result__": True, "name": name, "response": response, "call_id": call_id}

    def send(self, parts: list) -> Turn:
        tool_blocks, content, answered = [], [], set()
        for p in parts:
            if isinstance(p, str):
                content.append({"type": "text", "text": p})
            elif isinstance(p, dict) and p.get("__tool_result__"):
                cid = p.get("call_id")
                if not cid:
                    for q in self._pending:
                        if q["name"] == p["name"] and q["id"] not in answered:
                            cid = q["id"]; break
                if cid:
                    answered.add(cid)
                    tool_blocks.append({"type": "tool_result", "tool_use_id": cid,
                                        "content": json.dumps(p["response"])})
            elif isinstance(p, dict):
                content.append(p)
        self._pending = []
        msg_content = tool_blocks + content                  # tool_result blocks first, then any new text/image
        if msg_content:
            self.messages.append({"role": "user", "content": msg_content})
        resp = self.client.messages.create(model=self.model_name, max_tokens=self.max_tokens,
                                            system=self.system, messages=self.messages, tools=self.tools or None)
        return self._consume(resp)

    def _consume(self, resp) -> Turn:
        blocks = list(getattr(resp, "content", None) or [])
        # replay-safe assistant turn (convert SDK blocks -> dicts)
        amsg = []
        calls, texts = [], []
        for b in blocks:
            bt = getattr(b, "type", None)
            if bt == "text":
                texts.append(getattr(b, "text", "") or "")
                amsg.append({"type": "text", "text": getattr(b, "text", "") or ""})
            elif bt == "tool_use":
                inp = getattr(b, "input", None) or {}
                calls.append({"id": b.id, "name": b.name, "args": dict(inp)})
                amsg.append({"type": "tool_use", "id": b.id, "name": b.name, "input": inp})
        self.messages.append({"role": "assistant", "content": amsg})
        self._pending = [{"id": c["id"], "name": c["name"]} for c in calls]
        um = getattr(resp, "usage", None)
        usage = ({"in": getattr(um, "input_tokens", 0) or 0, "out": getattr(um, "output_tokens", 0) or 0,
                  "cached": getattr(um, "cache_read_input_tokens", 0) or 0} if um else {"in": 0, "out": 0, "cached": 0})
        return Turn(text="".join(texts), tool_calls=calls, usage=usage,
                    finish_reason=str(getattr(resp, "stop_reason", "") or ""))


def _build(backend: str, kw: dict) -> ModelBackend:
    if backend == "gemini-vertex":
        return GeminiVertexBackend(**kw)
    if backend == "gemini-api":
        return GeminiAPIBackend(**kw)
    if backend == "openai":
        return OpenAIBackend(**kw)
    if backend == "anthropic":
        return AnthropicBackend(**kw)
    raise ValueError(f"unknown backend {backend!r}")


def make_backend(provider: str, **kw) -> ModelBackend:
    """Factory. `provider` is either a registry MODEL ID (model_registry.MODELS — local-gemma/azure-chem/geek/
    byo-*) or a raw backend name ('gemini-vertex'/'gemini-api'/'openai'/'anthropic', the legacy/BYO path).

    Registry ids resolve their connection config (base_url / azure endpoint+version / extra_body / reasoning /
    key-from-.env / default model) from the spec; caller kwargs (api_key, base_url, azure_endpoint, model_name)
    override for BYO. The Vertex managed path keeps taking creds straight from the caller (run())."""
    try:
        from model_registry import get_spec, resolve_openai_kwargs
    except Exception:
        get_spec = resolve_openai_kwargs = None
    spec = get_spec(provider) if get_spec else None
    if spec:
        backend = spec["backend"]
        if backend == "openai":
            sys_kw = {"system": kw.get("system"), "tool_decls": kw.get("tool_decls")}
            conn = resolve_openai_kwargs(
                spec, api_key=kw.get("api_key"), base_url=kw.get("base_url"),
                azure_endpoint=kw.get("azure_endpoint"), api_version=kw.get("api_version"),
                model_name=kw.get("model_name"))
            return OpenAIBackend(**sys_kw, **conn)
        if backend in ("gemini-api", "anthropic"):
            # BYO key: prefer the caller kwarg; else read the standard env var so CLIs/tests that don't thread
            # api_key through still work (gemini-api -> GEMINI_API_KEY/GOOGLE_API_KEY; anthropic -> ANTHROPIC_API_KEY).
            _key = kw.get("api_key")
            if not _key:
                if backend == "gemini-api":
                    _key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
                else:
                    _key = os.environ.get("ANTHROPIC_API_KEY")
            mk = {"system": kw.get("system"), "tool_decls": kw.get("tool_decls"),
                  "model_name": kw.get("model_name") or spec.get("default_model"), "api_key": _key}
            return _build(backend, mk)
        # gemini-vertex (managed): caller supplies creds/project/location/model -> pass kw straight through
        return _build(backend, kw)
    # raw backend name (legacy / explicit BYO)
    return _build(provider, kw)


def detect_provider(api_key: str) -> str:
    """Best-effort provider from a pasted key prefix (the UI can override)."""
    k = (api_key or "").strip()
    if k.startswith("sk-ant-"):
        return "anthropic"
    if k.startswith("sk-"):
        return "openai"
    if k.startswith("AIza"):
        return "gemini-api"
    return "gemini-api"
