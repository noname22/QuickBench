"""Find out what is actually being served: model name, quantization, inference engine.

The server is first treated as llama.cpp (/props); anything that is missing falls
back to generic endpoints and finally to what the user passed on the command line.
"""

from __future__ import annotations

import re
import urllib.parse

from .clients import ApiError, ConnectionFailed, http_json

QUANT_RE = re.compile(
    r"[-._ ](?P<quant>(?:UD-)?(?:"
    r"I?Q\d_[A-Z0-9]+(?:_[A-Z0-9]+)*"  # Q8_0, Q4_K_M, IQ4_XS, Q4_K_XL
    r"|T?Q\d_\d"
    r"|BF16|FP?16|FP?32|FP8|MXFP4(?:_MOE)?|NVFP4"
    r"|AWQ|GPTQ(?:-Int\d)?|INT[48]|[2-8]bit"
    r"))$",
    re.IGNORECASE,
)

# Canonical spelling of known model families, matched case-insensitively at a token start.
FAMILIES = [
    "Qwen", "QwQ", "Llama", "Gemma", "Mistral", "Mixtral", "Ministral", "Devstral", "Magistral", "Phi", "DeepSeek",
    "GLM", "gpt-oss", "Granite", "OLMo", "SmolLM", "Nemotron", "Command-R", "Yi", "InternLM", "Falcon", "Kimi",
    "MiniMax", "Seed-OSS", "ERNIE", "EXAONE", "Hunyuan", "LFM",
]
FAMILY_RE = re.compile(
    r"(?<![A-Za-z])(?P<family>" + "|".join(re.escape(f) for f in FAMILIES) + r")"
    r"[-_ ]?[vV]?(?P<version>\d+(?:\.\d+)*)?(?![A-Za-z0-9.])",
    re.IGNORECASE,
)
SIZE_RE = re.compile(r"^(?:\d+x)?\d+(?:\.\d+)?[BM](?:-A\d+(?:\.\d+)?B)?$", re.IGNORECASE)
ACTIVE_RE = re.compile(r"^A\d+(?:\.\d+)?B$", re.IGNORECASE)

# Tokens that belong to an official release name rather than to a community fine-tune.
VARIANT_TOKENS = {"instruct", "it", "chat", "base", "thinking", "reasoning", "coder", "code", "vl", "math", "next",
                  "omni", "mini", "small", "medium", "large", "nano", "flash", "air", "distill", "r1", "v3", "qat"}
FAMILY_NAMES = {f.lower() for f in FAMILIES}
# Packaging noise that says nothing about the weights' behaviour.
NOISE_TOKENS = {"gguf", "hf", "meta", "mtp", "imatrix", "i1", "imat"}


def parse_model_name(name: str) -> dict:
    """Heuristically split e.g. 'Swift-Qwen3.8-27B-Uncensored-MTP-Q8_0' into its parts."""
    quantization = None
    m = QUANT_RE.search(name)
    if m:
        quantization = m.group("quant").upper()
        name = name[:m.start()]

    fm = FAMILY_RE.search(name)
    if not fm:
        return {"base_model": name, "fine_tune": None, "quantization": quantization}

    family = next(f for f in FAMILIES if f.lower() == fm.group("family").lower())
    base = [family + (f" {fm.group('version')}" if fm.group("version") else "")]
    before = _tokens(name[:fm.start()])
    after = _tokens(name[fm.end():])

    extra_before = [t for t in before if t.lower() not in NOISE_TOKENS]
    extra_after = []
    for token in after:
        low = token.lower()
        if low in NOISE_TOKENS:
            continue
        if ACTIVE_RE.match(token) and base and SIZE_RE.match(base[-1]):
            base[-1] += f"-{token.upper()}"
        elif SIZE_RE.match(token):
            base.append(token.upper())
        elif extra_after:
            extra_after.append(token)
        elif low in VARIANT_TOKENS or low in FAMILY_NAMES or re.fullmatch(r"\d{4}", token):
            base.append(token)  # e.g. Instruct, 2507, or the Qwen in DeepSeek-R1-Distill-Qwen
        elif re.fullmatch(r"[vV]?\d+(\.\d+)*", token) and not any(SIZE_RE.match(b) for b in base):
            base.append(token)  # version after the variant, e.g. Mistral-Small-3.2
        else:
            extra_after.append(token)

    base_model = " ".join(base)
    fine_tune = None
    if extra_before or extra_after:
        fine_tune = " ".join(extra_before + [base_model] + extra_after)
    return {"base_model": base_model, "fine_tune": fine_tune, "quantization": quantization}


def _tokens(part: str) -> list[str]:
    # Dots separate tokens only when they are not inside a number (keep "3.1", "0.5B").
    return [t for t in re.split(r"[-_ ]+|(?<!\d)\.|\.(?!\d)", part) if t]


def model_name_from_path(path: str) -> str:
    """'/home/x/Models/Foo-Q8_0.gguf' -> 'Foo-Q8_0' (also strips multi-part suffixes)."""
    name = re.split(r"[/\\]", path.strip())[-1]
    name = re.sub(r"\.gguf$", "", name, flags=re.IGNORECASE)
    return re.sub(r"-\d{5}-of-\d{5}$", "", name)


def safe_dir_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]+", "_", name).strip("._") or "unknown-model"


def describe_kv_cache(k: str, v: str) -> str:
    full = {"f16": "full 16-bit", "bf16": "full 16-bit (bf16)", "f32": "full 32-bit"}
    if k == v:
        return full.get(k, k)
    return f"K: {full.get(k, k)}, V: {full.get(v, v)}"


def result_dir_name(model_name: str, cache_k: str, cache_v: str, effort: str | None = None) -> str:
    name = safe_dir_name(model_name)
    if (cache_k, cache_v) != ("f16", "f16"):
        name += f"-k{cache_k}-v{cache_v}"
    if effort:
        name += f"-effort-{safe_dir_name(effort)}"
    return name


def _get(url: str, headers: dict, timeout: float = 15):
    try:
        return http_json(url, headers=headers, timeout=timeout)
    except ApiError:
        return None


def kv_cache_from_args(args) -> tuple[str | None, str | None]:
    """(--cache-type-k, --cache-type-v) from a llama-server command line, None where not given."""
    found: dict[str, str] = {}
    args = [str(a) for a in args or []]
    for i, arg in enumerate(args):
        for flag, key in (("--cache-type-k", "k"), ("-ctk", "k"), ("--cache-type-v", "v"), ("-ctv", "v")):
            if arg == flag and i + 1 < len(args):
                found[key] = args[i + 1]
            elif arg.startswith(flag + "="):
                found[key] = arg.split("=", 1)[1]
    return found.get("k"), found.get("v")


def probe(root: str, api: str, api_key: str | None, model: str | None = None) -> dict:
    """Ask the server about itself. Raises ConnectionFailed when it cannot be reached at all.

    A llama.cpp router serves several models; everything is then asked about `model`.
    """
    headers = {}
    if api_key:
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"} if api == "anthropic" \
            else {"Authorization": f"Bearer {api_key}"}
    if api == "anthropic":
        headers.setdefault("anthropic-version", "2023-06-01")

    info: dict = {"reported_model": None, "quantization": None, "engine": None, "n_params": None, "n_ctx": None,
                  "server_sampling_defaults": None, "tokenize": False, "router": False, "kv_cache": (None, None),
                  "llamacpp": False, "reasoning": {"supports_effort": None, "default_effort": None}}

    props = _get(root + "/props", headers)
    if isinstance(props, dict) and props.get("role") == "router":
        # The router answers for itself; ask again for the model under test (this also loads it).
        info["router"] = True
        build = props.get("build_info")
        # Loading a large model from disk can take minutes.
        props = _get(root + "/props?model=" + urllib.parse.quote(model or "", safe=""), headers, timeout=900)
        if not isinstance(props, dict) or not props.get("model_path"):
            raise ApiError(f"the router at {root} does not serve a model called {model!r}")
        props.setdefault("build_info", build)
    if isinstance(props, dict) and ("build_info" in props or "model_path" in props):
        path = props.get("model_path") or props.get("model_alias")
        if path:
            info["reported_model"] = model_name_from_path(path)
        info["quantization"] = props.get("model_ftype") or None
        info["engine"] = format_llamacpp_build(props.get("build_info"))
        settings = props.get("default_generation_settings") or {}
        info["n_ctx"] = settings.get("n_ctx")
        params = settings.get("params") or {}
        keep = ("temperature", "top_k", "top_p", "min_p", "repeat_penalty", "presence_penalty", "frequency_penalty",
                "seed")
        info["server_sampling_defaults"] = {k: params[k] for k in keep if k in params} or None
        info["tokenize"] = True
        info["llamacpp"] = True
        from .forcing import template_reasoning

        info["reasoning"] = template_reasoning(props)

    models = _get(root + "/v1/models", headers)
    entries = models.get("data") if isinstance(models, dict) else None
    if info["router"] and isinstance(entries, list):
        entry = next((e for e in entries if e.get("id") == model), {})
        info["n_params"] = (entry.get("meta") or {}).get("n_params")
        info["kv_cache"] = kv_cache_from_args((entry.get("status") or {}).get("args"))
    elif isinstance(entries, list) and entries:
        first = entries[0]
        # Only trust the list for the name when it is unambiguous (single-model servers).
        if not info["reported_model"] and len(entries) == 1 and first.get("id"):
            info["reported_model"] = model_name_from_path(first["id"])
        meta = first.get("meta") or {}
        if len(entries) == 1:
            info["n_params"] = meta.get("n_params")
            info["quantization"] = info["quantization"] or meta.get("ftype")

    if not info["engine"]:
        version = _get(root + "/version", headers)  # vLLM
        if isinstance(version, dict) and version.get("version"):
            info["engine"] = f"vLLM {version['version']}"
    if not info["engine"]:
        version = _get(root + "/api/version", headers)  # Ollama
        if isinstance(version, dict) and version.get("version"):
            info["engine"] = f"Ollama {version['version']}"
    return info


def format_llamacpp_build(build_info: str | None) -> str:
    """'b11023-4ff829ec2' -> 'llama.cpp 11023 (4ff829ec2)'."""
    if not build_info:
        return "llama.cpp"
    m = re.fullmatch(r"b?(\d+)-([0-9a-fA-F]+)", build_info.strip())
    return f"llama.cpp {m.group(1)} ({m.group(2)})" if m else f"llama.cpp {build_info}"


class TokenCounter:
    """Counts reasoning tokens. APIs rarely report them, so use llama.cpp's /tokenize when available."""

    def __init__(self, root: str, headers: dict, enabled: bool, model: str | None = None):
        self.url = root + "/tokenize"
        self.headers = headers
        self.enabled = enabled
        self.extra = {"model": model} if model else {}  # a router picks the model from the request body

    def count(self, text: str) -> tuple[int, bool]:
        """Returns (tokens, estimated)."""
        if not text:
            return 0, False
        if self.enabled:
            try:
                body = {"content": text, **self.extra}
                tokens = http_json(self.url, body, self.headers, timeout=60, retries=1).get("tokens")
                if isinstance(tokens, list):
                    return len(tokens), False
            except (ApiError, ConnectionFailed):
                pass
            self.enabled = False
        return max(1, len(text) // 4), True
