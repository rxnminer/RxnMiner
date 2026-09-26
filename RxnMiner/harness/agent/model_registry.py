"""Config-driven model registry for the agent entry — one place to plug in a new model.

Each entry maps a selectable MODEL ID to a spec: which ModelBackend to build, its access tier, and the
connection config (base_url / azure endpoint+version / extra_body / key source / default model). make_backend()
(in model_backend.py) consults this so adding a model = adding one entry here, and the server reads `access` to
gate it.

Access tiers:
  - "public"     : anyone (incl. guests). Our own free inference — the LAN vLLM models.
  - "privileged" : needs the chemagent access key / an admin account. Our PAID keys (managed Gemini, Azure, geek).
  - "byo"        : the user brings their own key + base_url. We run the agent, they pay the API.

SECURITY: keys are NEVER hard-coded here. `key_env` names a variable read from REPO/.env at RUNTIME via _env()
(never logged, never returned to a client). Only non-secret base_urls (LAN addresses) live in this file.
"""
from __future__ import annotations
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_ENV_CACHE = None


def _env(name: str):
    """Read a value from REPO/.env at runtime (case-insensitive key), or None. Cached after first read.
    Mirrors scripts/ccbench.py::_geek_key — the value is a secret: never log it, never echo it to a client.
    An already-exported process env var of the same name (any case) wins, so deploys can use real env too."""
    global _ENV_CACHE
    if name is None:
        return None
    # process env first (exact, then case-insensitive)
    if os.environ.get(name):
        return os.environ[name]
    for k, v in os.environ.items():
        if k.lower() == name.lower() and v:
            return v
    if _ENV_CACHE is None:
        _ENV_CACHE = {}
        envf = REPO / ".env"
        if envf.exists():
            try:
                for line in envf.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, val = line.split("=", 1)
                    _ENV_CACHE[k.strip().lower()] = val.strip().strip('"').strip("'")
            except Exception:
                _ENV_CACHE = {}
    return _ENV_CACHE.get(name.lower()) or None


def _vis(*ids):
    """Shorthand: turn ('gpt-4o', 'gpt-4.1') into [{id,label,vision:True}, ...] (all vision-capable)."""
    return [{"id": i, "label": i, "vision": True} for i in ids]


def _models_from_env_or(env_name: str, default: list) -> list:
    """A provider's sub-model list, overridable from .env (comma-separated ids → all assumed vision-capable).
    Lets a deploy widen/narrow the offered sub-models (e.g. which Azure deployments actually exist) without code."""
    raw = _env(env_name)
    if raw:
        ids = [s.strip() for s in raw.split(",") if s.strip()]
        if ids:
            return [{"id": i, "label": i, "vision": True} for i in ids]
    return default


# The selectable models. `id` is what the UI/run() pass as `provider`. Add a model = add an entry.
# `models` (optional) = a sub-model list [{id,label,vision}] shown as a second dropdown; the chosen id is passed
# as model_name. Vision is a HARD requirement for this figure-reading agent — non-vision sub-models are flagged.
MODELS = {
    # ── public: our LAN vLLM (OpenAI-compatible; reachable from the server but only with the proxy OFF) ──
    "local-gemma": {
        "label": "Gemma-4-31B (local)", "backend": "openai", "access": "public",
        "base_url": _env("LOCAL_GEMMA_BASE_URL") or "http://127.0.0.1:8010/v1", "key_env": "local_gemma_key",
        "default_model": "gemma-4-31B-it", "no_proxy": True, "max_tokens": 81920, "timeout": 3600,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}, "skip_special_tokens": False},
    },
    "local-qwen": {
        "label": "Qwen3.5-397B (local)", "backend": "openai", "access": "public",
        "base_url": _env("LOCAL_QWEN_BASE_URL") or "http://127.0.0.1:8009/v1", "key_env": "local_qwen_key",
        "default_model": "Qwen3.5-397B-A17B-FP8", "no_proxy": True, "max_tokens": 81920, "timeout": 3600,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    },
    # ── privileged: our PAID keys, gated behind the chemagent access key / admin ──
    "gemini-vertex": {                                   # keep id == backend so run()'s existing Vertex path serves it
        "label": "Managed Gemini", "backend": "gemini-vertex", "access": "privileged",
    },
    "azure-chem": {
        # Azure: model == DEPLOYMENT name; only deployed ones resolve (others 404). Confirmed deployed: gpt-5.4.
        # The offered sub-models can be narrowed/widened via the `azure_models` env var on the deploy.
        "label": "Azure GPT-5", "backend": "openai", "access": "privileged",
        "azure_endpoint_env": "chem_azure_endpoint", "api_version": "2024-12-01-preview",
        # reasoning_effort "medium" (not "high"): in a 13+ turn tool loop, high reasoning burns the whole 400k
        # token budget before emit_full and the job ends "incomplete" — medium finishes within budget.
        "key_env": "chem_api_key", "default_model": "gpt-5.4", "reasoning_effort": "medium", "no_proxy": True,
        "models": _models_from_env_or("azure_models", _vis(
            "gpt-5.4", "gpt-5.4-mini", "gpt-5.1", "gpt-5.1-codex", "gpt-5", "gpt-5-mini")),
    },
    "geek": {
        "label": "GPUGeek", "backend": "openai", "access": "privileged",
        "base_url": "https://api.gpugeek.com/v1", "key_env": "geek_api_key", "no_proxy": True,
        # default = proven stable tool-caller (Claude-4.5-Sonnet ran 49-2-small done/valid). The gateway carries
        # ~114 models; `models` below are the latest-flagship QUICK PICKS — the UI also fetches the FULL live list
        # (GET /api/provider_models/geek) into a searchable box, and `open_models` lets ANY gateway id through.
        "model_env": "geek_model", "default_model": "Vendor2/Claude-4.5-Sonnet", "open_models": True,
        "models": _models_from_env_or("geek_models", _vis(
            "Vendor2/Claude-4.6-Sonnet", "Vendor2/Claude-4.8-opus", "Vendor2/Claude-4.5-Sonnet",
            "Vendor2/GPT-5.5", "Vendor2/GPT-5.1", "Vendor2/Gemini-3.1-pro", "Vendor2/Gemini-2.5-Pro")),
    },
    # ── byo: the user supplies key (+ base_url / azure endpoint). base_url prefilled where the gateway is fixed. ──
    "byo-gemini": {"label": "BYO Gemini official (AIza key)", "backend": "gemini-api", "access": "byo",
                   "default_model": "gemini-2.5-pro",
                   "models": _vis("gemini-2.5-pro", "gemini-2.5-flash", "gemini-3-pro-preview")},
    "byo-anthropic": {"label": "BYO Anthropic official (sk-ant-)", "backend": "anthropic", "access": "byo",
                      "default_model": "claude-sonnet-4-5",
                      "models": _vis("claude-sonnet-4-5", "claude-opus-4-1", "claude-3-5-sonnet-20241022")},
    "byo-openai": {"label": "BYO OpenAI official (sk-)", "backend": "openai", "access": "byo",
                   "base_url": "https://api.openai.com/v1", "default_model": "gpt-4o",
                   "models": _vis("gpt-4o", "gpt-4.1", "gpt-5", "o4-mini")},
    "byo-openai-compat": {"label": "BYO OpenAI-compatible (relay / 中转站 / geek / local)", "backend": "openai",
                          "access": "byo", "default_model": "gpt-4o"},   # base_url comes from the user
    "byo-azure": {"label": "BYO Azure OpenAI", "backend": "openai", "access": "byo",
                  "api_version": "2024-12-01-preview", "default_model": "gpt-4o"},  # endpoint+key from the user
    "byo-openrouter": {"label": "BYO OpenRouter (1 key → 300+ models)", "backend": "openai", "access": "byo",
                       "base_url": "https://openrouter.ai/api/v1", "default_model": "anthropic/claude-3.7-sonnet",
                       "models": _vis("anthropic/claude-3.7-sonnet", "openai/gpt-4o", "google/gemini-2.5-pro",
                                      "qwen/qwen2.5-vl-72b-instruct")},
    "byo-deepseek": {"label": "BYO DeepSeek", "backend": "openai", "access": "byo",
                     "base_url": "https://api.deepseek.com/v1", "default_model": "deepseek-chat",
                     # DeepSeek has no general vision model yet → flagged; figure extraction will not work.
                     "models": [{"id": "deepseek-chat", "label": "deepseek-chat", "vision": False},
                                {"id": "deepseek-reasoner", "label": "deepseek-reasoner", "vision": False}]},
    "byo-kimi": {"label": "BYO Kimi / Moonshot", "backend": "openai", "access": "byo",
                 "base_url": "https://api.moonshot.cn/v1", "default_model": "moonshot-v1-128k-vision-preview",
                 "models": _vis("moonshot-v1-128k-vision-preview", "moonshot-v1-32k-vision-preview")},
    "byo-qwen": {"label": "BYO Qwen / DashScope", "backend": "openai", "access": "byo",
                 "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "default_model": "qwen-vl-max",
                 "models": _vis("qwen-vl-max", "qwen-vl-plus", "qwen2.5-vl-72b-instruct")},
    "byo-glm": {"label": "BYO Zhipu GLM", "backend": "openai", "access": "byo",
                "base_url": "https://open.bigmodel.cn/api/paas/v4", "default_model": "glm-4v-plus",
                "models": _vis("glm-4v-plus", "glm-4v")},
}

# ── data-driven: admin can add OpenAI-compatible providers via REPO/models.json (no source edit). ──
_ALLOWED_SPEC_KEYS = {"id", "label", "backend", "access", "base_url", "key_env", "azure_endpoint_env",
                      "api_version", "default_model", "model_env", "models", "no_proxy", "max_tokens",
                      "timeout", "extra_body", "reasoning_effort"}
_ALLOWED_BACKENDS = {"openai", "gemini-api", "anthropic"}
_ALLOWED_ACCESS = {"public", "privileged", "byo"}


def _load_extra_models(path=None) -> dict:
    """Merge REPO/models.json (a JSON array of provider specs) into MODELS. SECURITY: only whitelisted fields;
    a plaintext `api_key` (or any non-whitelisted key) makes the entry REJECTED — secrets must go through
    `key_env`→.env, never the JSON. Returns the dict of accepted {id: spec} (also merged into MODELS in place)."""
    import json as _json
    p = Path(path) if path else (REPO / "models.json")
    accepted = {}
    if not p.exists():
        return accepted
    try:
        entries = _json.loads(p.read_text())
    except Exception:
        return accepted
    if not isinstance(entries, list):
        return accepted
    for e in entries:
        if not isinstance(e, dict):
            continue
        mid = e.get("id")
        extra = set(e) - _ALLOWED_SPEC_KEYS
        if not mid or extra:                              # unknown/secret field (e.g. api_key) → reject whole entry
            continue
        if e.get("backend") not in _ALLOWED_BACKENDS or e.get("access", "byo") not in _ALLOWED_ACCESS:
            continue
        spec = {k: v for k, v in e.items() if k != "id"}
        spec.setdefault("access", "byo")
        accepted[mid] = spec
        MODELS[mid] = spec
    return accepted


_load_extra_models()


def get_spec(model_id: str):
    return MODELS.get(model_id)


def access_of(model_id: str) -> str:
    spec = MODELS.get(model_id)
    return spec.get("access") if spec else "byo"


def public_list():
    """Non-secret picker metadata (never keys). Each entry carries its `models` sub-list so the UI can render a
    second 'sub-model' dropdown; vision flags let the UI warn when a non-vision model is picked for figure work."""
    out = []
    for k, v in MODELS.items():
        models = v.get("models") or []
        out.append({
            "id": k, "label": v.get("label", k), "access": v.get("access", "byo"), "backend": v.get("backend"),
            "default_model": v.get("default_model"), "open_models": bool(v.get("open_models")),
            "needs_base_url": (v.get("access") == "byo" and v.get("backend") == "openai" and not v.get("base_url")),
            "is_azure": bool(v.get("azure_endpoint_env") or (v.get("access") == "byo" and k == "byo-azure")),
            "model_overridable": (v.get("access") == "byo" or bool(v.get("model_env"))),
            "models": [{"id": m.get("id"), "label": m.get("label", m.get("id")), "vision": bool(m.get("vision", True))}
                       for m in models if m.get("id")],
            "submodel_overridable": bool(models or v.get("model_env") or v.get("access") == "byo"),
        })
    return out


def resolve_openai_kwargs(spec: dict, *, api_key=None, base_url=None, azure_endpoint=None,
                          api_version=None, model_name=None) -> dict:
    """Build the OpenAIBackend connection kwargs from a spec + any user-supplied overrides. Server-keyed specs
    read their key/endpoint from .env; BYO specs take them from the caller. Returns a dict to splat into the
    backend (api_key/base_url/azure_endpoint/api_version/extra_body/reasoning_effort/max_tokens/timeout/no_proxy
    + model_name)."""
    out = {}
    out["api_key"] = api_key or _env(spec.get("key_env"))
    out["base_url"] = base_url or spec.get("base_url")
    out["azure_endpoint"] = azure_endpoint or _env(spec.get("azure_endpoint_env"))
    out["api_version"] = api_version or spec.get("api_version")
    out["extra_body"] = spec.get("extra_body")
    out["reasoning_effort"] = spec.get("reasoning_effort")
    out["max_tokens"] = spec.get("max_tokens")
    out["timeout"] = spec.get("timeout")
    out["no_proxy"] = bool(spec.get("no_proxy"))
    out["model_name"] = model_name or _env(spec.get("model_env")) or spec.get("default_model")
    return out
