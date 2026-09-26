"""Pluggable model registry + OpenAIBackend extensions (Azure / vLLM extra_body / GPT-5 reasoning / proxy bypass).
All offline: a fake `openai` module records how clients are built and what create() is called with — no network."""
import sys, types, pathlib, pytest
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
import model_registry as R
import model_backend as MB


def _fake_openai(monkeypatch):
    """Install a fake `openai` module; returns a `calls` dict recording client construction + create() kwargs."""
    calls = {"openai_kw": None, "azure_kw": None, "create_kw": None}

    class _Resp:
        class _Ch:
            class _M:
                content = "ok"; tool_calls = None
            message = _M(); finish_reason = "stop"
        choices = [_Ch()]; usage = None

    class _Client:
        def __init__(self, **kw): pass
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    calls["create_kw"] = kw
                    return _Resp()

    def OpenAI(**kw):
        calls["openai_kw"] = kw
        return _Client()

    def AzureOpenAI(**kw):
        calls["azure_kw"] = kw
        return _Client()

    mod = types.ModuleType("openai")
    mod.OpenAI = OpenAI
    mod.AzureOpenAI = AzureOpenAI
    monkeypatch.setitem(sys.modules, "openai", mod)
    return calls


def test_registry_resolves_azure_geek_local_from_env(monkeypatch):
    monkeypatch.setenv("chem_api_key", "FAKE_AZ"); monkeypatch.setenv("chem_azure_endpoint", "https://x.openai.azure.com/")
    monkeypatch.setenv("geek_api_key", "FAKE_GK"); monkeypatch.setenv("local_gemma_key", "sk-loc")
    az = R.resolve_openai_kwargs(R.MODELS["azure-chem"])
    assert az["api_key"] == "FAKE_AZ" and az["azure_endpoint"].endswith("azure.com/") and az["model_name"] == "gpt-5.4"
    assert az["reasoning_effort"] == "medium"                                # high burns 400k tokens before emit
    assert az["no_proxy"] is True                                            # dev proxy egress is firewalled by Azure
    gk = R.resolve_openai_kwargs(R.MODELS["geek"])
    assert gk["base_url"] == "https://api.gpugeek.com/v1" and gk["api_key"] == "FAKE_GK"
    lg = R.resolve_openai_kwargs(R.MODELS["local-gemma"])
    assert lg["no_proxy"] is True and lg["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_make_backend_geek_builds_openai_with_base_url(monkeypatch):
    calls = _fake_openai(monkeypatch)
    monkeypatch.setenv("geek_api_key", "FAKE_GK")
    b = MB.make_backend("geek", system="S", tool_decls=[], model_name=None, api_key=None)
    assert isinstance(b, MB.OpenAIBackend)
    assert calls["openai_kw"]["base_url"] == "https://api.gpugeek.com/v1"   # geek base_url applied
    assert b.model_name == "Vendor2/Claude-4.5-Sonnet"                       # strong default tool-caller


def test_make_backend_azure_uses_azureopenai(monkeypatch):
    calls = _fake_openai(monkeypatch)
    monkeypatch.setenv("chem_api_key", "FAKE_AZ"); monkeypatch.setenv("chem_azure_endpoint", "https://x.openai.azure.com/")
    MB.make_backend("azure-chem", system="S", tool_decls=[], model_name=None, api_key=None)
    assert calls["azure_kw"] is not None and calls["openai_kw"] is None      # took the Azure path
    assert calls["azure_kw"]["azure_endpoint"].endswith("azure.com/")
    assert calls["azure_kw"]["api_version"] == "2024-12-01-preview"


def test_send_passes_extra_body_reasoning_maxtokens_never_temperature(monkeypatch):
    calls = _fake_openai(monkeypatch)
    b = MB.OpenAIBackend("S", [], "gpt-5.5", client=None, api_key="k",
                         reasoning_effort="high", extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                         max_tokens=4096)
    b.send(["hello"])
    ck = calls["create_kw"]
    assert ck["reasoning_effort"] == "high"
    assert ck["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert ck["max_tokens"] == 4096
    assert "temperature" not in ck                                           # GPT-5 reasoning models reject it


def test_no_proxy_builds_trust_env_false_client(monkeypatch):
    calls = _fake_openai(monkeypatch)
    seen = {}
    fake_httpx = types.ModuleType("httpx")
    class _HC:
        def __init__(self, **kw): seen.update(kw)
    fake_httpx.Client = _HC
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    monkeypatch.setenv("local_gemma_key", "sk-loc")
    MB.make_backend("local-gemma", system="S", tool_decls=[], model_name=None, api_key=None)
    assert seen.get("trust_env") is False                                    # LAN call bypasses env proxies
    assert "http_client" in calls["openai_kw"]


def test_raw_backend_names_still_work(monkeypatch):
    _fake_openai(monkeypatch)
    b = MB.make_backend("openai", system="S", tool_decls=[], model_name="gpt-4o", api_key="sk-x")
    assert isinstance(b, MB.OpenAIBackend) and b.model_name == "gpt-4o"      # legacy/BYO path unchanged


def test_pending_fixes_azure_gpt54_and_no_proxy():
    az = R.MODELS["azure-chem"]
    assert az["default_model"] == "gpt-5.4" and az.get("no_proxy") is True    # only gpt-5.4 deployed; dev proxy firewalled
    assert az["models"][0]["id"] == "gpt-5.4"                                  # confirmed-deployed default leads the list
    assert R.MODELS["geek"].get("no_proxy") is True                           # geek also bypasses the dev proxy


def test_new_byo_providers_prefill_base_url():
    for pid, host in [("byo-openrouter", "openrouter.ai"), ("byo-deepseek", "api.deepseek.com"),
                      ("byo-kimi", "moonshot.cn"), ("byo-qwen", "dashscope"), ("byo-glm", "bigmodel.cn")]:
        spec = R.MODELS[pid]
        assert spec["access"] == "byo" and spec["backend"] == "openai" and host in spec["base_url"]
    # BYO resolve uses the USER's key, keeps the prefilled base_url, honors the chosen sub-model
    kw = R.resolve_openai_kwargs(R.MODELS["byo-openrouter"], api_key="sk-user", model_name="openai/gpt-4o")
    assert kw["api_key"] == "sk-user" and kw["base_url"] == "https://openrouter.ai/api/v1" and kw["model_name"] == "openai/gpt-4o"


def test_public_list_carries_submodels_and_vision_flags():
    by = {m["id"]: m for m in R.public_list()}
    assert by["azure-chem"]["models"][0]["id"] == "gpt-5.4" and by["azure-chem"]["submodel_overridable"] is True
    ds = {x["id"]: x for x in by["byo-deepseek"]["models"]}
    assert ds["deepseek-chat"]["vision"] is False                            # no vision -> UI warns for figure work
    assert by["byo-kimi"]["models"][0]["vision"] is True


def test_load_extra_models_merges_and_rejects_plaintext_key(tmp_path):
    import json as _json
    f = tmp_path / "models.json"
    f.write_text(_json.dumps([
        {"id": "x-good", "label": "Good relay", "backend": "openai", "access": "byo",
         "base_url": "https://relay.example/v1", "default_model": "gpt-4o"},
        {"id": "x-secret", "label": "Bad", "backend": "openai", "access": "byo", "api_key": "sk-PLAINTEXT"},
        {"id": "x-badbackend", "label": "Bad", "backend": "made-up", "access": "byo"},
    ]))
    try:
        accepted = R._load_extra_models(path=str(f))
        assert "x-good" in accepted and "x-good" in R.MODELS                  # legal entry merged in
        assert "x-secret" not in accepted and "x-secret" not in R.MODELS      # plaintext api_key -> whole entry rejected
        assert "x-badbackend" not in accepted                                 # backend off the allow-list -> rejected
    finally:
        R.MODELS.pop("x-good", None)                                          # don't leak into other tests


def test_geek_latest_models_and_open_flag():
    g = R.MODELS["geek"]
    assert g.get("open_models") is True                                      # gateway has ~114 ids; any passes
    ids = {m["id"] for m in g["models"]}
    assert {"Vendor2/Claude-4.6-Sonnet", "Vendor2/Claude-4.8-opus", "Vendor2/GPT-5.5",
            "Vendor2/Gemini-3.1-pro"} <= ids                                 # the 4 latest flagships are quick-picks
