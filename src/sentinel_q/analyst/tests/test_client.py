"""模型调用与答复解析（架构文档 7.2）。

三组断言：

  1. **解析容错但不猜**——格式可以脏（围栏、寒暄），内容不合约就抛
  2. **不合约时绝不返回一个"默认中风险/中立"的对象**（这条是整套测试里最要紧的）
  3. **没装 httpx 时报一句能照着做的人话**，且这个模块在没装它时照样能 import

⚠️ 全程**不联网**。`HttpLLMClient` 那一组把 `sys.modules["httpx"]` 换成一个假的，
所以断言的是"我们会发出什么样的请求"，而不是"那家 API 收不收"。
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any, Self

import pytest

from sentinel_q.analyst import client as client_mod
from sentinel_q.analyst.client import (
    HttpLLMClient,
    LLMClient,
    LLMError,
    LLMReplyError,
    LLMSchemaError,
    optional_float,
    optional_str,
    parse_reply,
    require_bool,
    require_choice,
)
from sentinel_q.analyst.fake import FakeLLMClient
from sentinel_q.shared.config import Settings

STANCES = ("有利", "抹黑", "中立", "不相关")


# ── 解析：格式可以脏 ────────────────────────────────────────────────


def test_plain_json_is_parsed() -> None:
    assert parse_reply('{"is_relevant": true}') == {"is_relevant": True}


def test_fenced_json_is_parsed() -> None:
    """围栏该由提示词的 E 模块挡住，但真出现了也不该算失败。"""
    assert parse_reply('```json\n{"is_relevant": false}\n```') == {"is_relevant": False}


def test_bare_fence_without_language_is_parsed() -> None:
    assert parse_reply('```\n{"a": 1}\n```') == {"a": 1}


def test_chatter_around_the_json_is_tolerated() -> None:
    """模型爱在 JSON 前后加一句"好的，判断如下"。那是格式脏，不是内容错。"""
    text = '好的，我的判断如下：\n{"is_relevant": true}\n希望有帮助。'
    assert parse_reply(text) == {"is_relevant": True}


def test_non_json_reports_an_excerpt() -> None:
    """⚠️ 报错必须带原文片段——不带原文的解析错误，事后只能靠猜。"""
    with pytest.raises(LLMReplyError) as excinfo:
        parse_reply("我不知道该怎么回答这个问题。")

    assert "我不知道该怎么回答" in str(excinfo.value)


def test_json_that_is_not_an_object_is_rejected() -> None:
    """后面所有字段校验都按"这是个 dict"写的，放过去会在更远的地方炸。"""
    for text in ("[1, 2]", '"相关"', "3"):
        with pytest.raises(LLMReplyError) as excinfo:
            parse_reply(text)
        assert "不是对象" in str(excinfo.value)


def test_empty_reply_is_rejected() -> None:
    with pytest.raises(LLMReplyError):
        parse_reply("")


# ── 校验：不合约就抛，绝不取默认值 ──────────────────────────────────


def test_require_bool_refuses_an_int() -> None:
    """`isinstance(True, int)` 是 True，所以 1 必须当成"模型没按 schema 输出"。"""
    with pytest.raises(LLMSchemaError):
        require_bool({"is_relevant": 1}, "is_relevant")


def test_require_bool_refuses_a_missing_field() -> None:
    with pytest.raises(LLMSchemaError) as excinfo:
        require_bool({}, "is_relevant")
    assert "is_relevant" in str(excinfo.value)


def test_require_choice_refuses_a_value_outside_the_enum() -> None:
    """⭐ 不挑一个最近的。'支持' 不是 '有利'，替它挑一个等于替模型编判断。"""
    with pytest.raises(LLMSchemaError) as excinfo:
        require_choice({"platform_stance": "支持"}, "platform_stance", STANCES)
    assert "有利" in str(excinfo.value)  # 报错里得列清楚可选值


def test_require_choice_strips_whitespace() -> None:
    assert require_choice({"s": " 中立 "}, "s", STANCES) == "中立"


def test_optional_str_treats_blank_as_absent() -> None:
    """`fact_analysis` 里"空摘要"和"没摘要"是两回事，统一归一成 None。"""
    assert optional_str({"ai_summary": "   "}, "ai_summary") is None
    assert optional_str({}, "ai_summary") is None
    assert optional_str({"ai_summary": " 说了一件事 "}, "ai_summary") == "说了一件事"


def test_optional_str_refuses_a_non_string() -> None:
    with pytest.raises(LLMSchemaError):
        optional_str({"ai_summary": ["不是字符串"]}, "ai_summary")


def test_optional_str_truncates_at_the_limit() -> None:
    assert optional_str({"s": "x" * 50}, "s", limit=10) == "x" * 10


def test_optional_float_refuses_out_of_range_instead_of_clamping() -> None:
    """⭐ 夹到 1.0 比抛出去更糟：那是一个看起来正常的假数字。"""
    with pytest.raises(LLMSchemaError):
        optional_float({"stance_confidence": 1.4}, "stance_confidence")
    with pytest.raises(LLMSchemaError):
        optional_float({"stance_confidence": -0.1}, "stance_confidence")


def test_optional_float_refuses_a_bool() -> None:
    with pytest.raises(LLMSchemaError):
        optional_float({"stance_confidence": True}, "stance_confidence")


def test_optional_float_accepts_an_int() -> None:
    assert optional_float({"stance_confidence": 1}, "stance_confidence") == 1.0
    assert optional_float({}, "stance_confidence") is None


def test_nothing_is_ever_defaulted() -> None:
    """⭐ 这条是整套测试的题眼。

    `{}`（合法 JSON、但不合约）从任何一个校验函数里过一遍，结果只能是抛异常。
    文档 G 那句"模糊时默认中风险、中立"是给**模型**的判断标准，不是给解析器的
    兜底——把坏 JSON 兜成"中风险"，这条判断事后没人分得清是模型给的还是解析器补的。
    """
    with pytest.raises(LLMSchemaError):
        require_bool({}, "is_relevant")
    with pytest.raises(LLMSchemaError):
        require_choice({}, "platform_stance", STANCES)
    with pytest.raises(LLMSchemaError):
        require_choice({}, "risk_level", ("低风险", "中风险", "高风险"))


# ── 协议 ────────────────────────────────────────────────────────────


def test_the_fake_satisfies_the_protocol_the_judges_accept() -> None:
    """三个 judge_*.py 只认这个协议，所以替身必须真的满足它。"""
    assert isinstance(FakeLLMClient(), LLMClient)


# ── HttpLLMClient（假 httpx，不联网） ───────────────────────────────


class _Response:
    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or ("" if payload is None else json.dumps(payload, ensure_ascii=False))

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


def _ok(content: str) -> _Response:
    return _Response(200, {"choices": [{"message": {"content": content}}]})


def _fake_httpx(responses: list[Any]) -> tuple[SimpleNamespace, list[dict[str, Any]]]:
    """一个假 httpx 模块：按顺序发 `responses`，把收到的请求记下来。

    列表里放 `Exception` 实例就当成传输层错误抛出去（连不上、超时那类）。
    """
    sent: list[dict[str, Any]] = []

    class HTTPError(Exception):
        pass

    class _Client:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def post(self, url: str, *, json: Any, headers: dict[str, str]) -> _Response:
            sent.append({"url": url, "body": json, "headers": headers, "timeout": self.timeout})
            reply = responses[min(len(sent) - 1, len(responses) - 1)]
            if isinstance(reply, Exception):
                raise reply
            return reply

    return SimpleNamespace(Client=_Client, HTTPError=HTTPError), sent


def _client(**overrides: Any) -> HttpLLMClient:
    fields = {"base_url": "https://api.example.com", "api_key": "sk-test", "model": "test-model"}
    fields.update(overrides)
    return HttpLLMClient(**fields)


def test_request_body_has_the_openai_compatible_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    module, sent = _fake_httpx([_ok('{"is_relevant": true}')])
    monkeypatch.setitem(sys.modules, "httpx", module)

    reply = _client(base_url="https://api.example.com/").complete(system="SYS", user="USR")

    assert reply == '{"is_relevant": true}'
    assert len(sent) == 1
    assert sent[0]["url"] == "https://api.example.com/chat/completions"  # 尾部斜杠不留双斜杠
    assert sent[0]["headers"]["Authorization"] == "Bearer sk-test"
    assert sent[0]["timeout"] == client_mod.DEFAULT_TIMEOUT
    body = sent[0]["body"]
    assert body["model"] == "test-model"
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == "SYS"
    assert body["messages"][1]["content"] == "USR"
    # 分类任务要的是稳定复现：同一个输入问两遍得到两个答案，追溯就没意义了
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}


def test_the_default_body_asks_for_no_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    """⭐ 不写这一条，默认值就会悄悄漂回服务端的 `high`——那是**最高档**的推理
    token 开销，而这是个分类任务。这个字段的存在理由就在这儿。"""
    module, sent = _fake_httpx([_ok("{}")])
    monkeypatch.setitem(sys.modules, "httpx", module)

    _client().complete(system="S", user="U")

    assert sent[0]["body"]["reasoning_effort"] == "none"


def test_an_empty_effort_omits_the_field_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ 空串是"别管，用服务端默认"，**不是**"发一个空值过去"——后者会被
    当成非法枚举，白挨一个 400。这是个留给将来的逃生口：服务端改了默认档，
    下游不用改代码就能跟着走。"""
    module, sent = _fake_httpx([_ok("{}")])
    monkeypatch.setitem(sys.modules, "httpx", module)

    _client(reasoning_effort="").complete(system="S", user="U")

    assert "reasoning_effort" not in sent[0]["body"]


def test_a_malformed_response_body_is_reported_as_such(monkeypatch: pytest.MonkeyPatch) -> None:
    """取不到 choices[0].message.content 时，说的必须是"形状不对"，不是"不是 JSON"。"""
    module, _ = _fake_httpx([_Response(200, {"result": "换了一家的返回格式"})])
    monkeypatch.setitem(sys.modules, "httpx", module)

    with pytest.raises(LLMReplyError) as excinfo:
        _client().complete(system="S", user="U")
    assert "形状不对" in str(excinfo.value)


def test_5xx_is_retried_with_exponential_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    module, sent = _fake_httpx([_Response(503, text="busy"), _Response(500), _ok('{"a": 1}')])
    monkeypatch.setitem(sys.modules, "httpx", module)
    slept: list[float] = []
    monkeypatch.setattr(client_mod.time, "sleep", slept.append)

    assert _client().complete(system="S", user="U") == '{"a": 1}'
    assert len(sent) == 3
    assert slept == [client_mod.RETRY_BACKOFF, client_mod.RETRY_BACKOFF**2]


def test_429_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    module, sent = _fake_httpx([_Response(429), _ok('{"a": 1}')])
    monkeypatch.setitem(sys.modules, "httpx", module)
    monkeypatch.setattr(client_mod.time, "sleep", lambda _seconds: None)

    assert _client().complete(system="S", user="U") == '{"a": 1}'
    assert len(sent) == 2


def test_4xx_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ 400/401/403 重试三次只是把配额白烧三遍，还把真正的错因埋在 warning 底下。"""
    module, sent = _fake_httpx([_Response(401, text='{"error": "invalid api key"}')])
    monkeypatch.setitem(sys.modules, "httpx", module)
    slept: list[float] = []
    monkeypatch.setattr(client_mod.time, "sleep", slept.append)

    with pytest.raises(LLMError) as excinfo:
        _client().complete(system="S", user="U")

    assert len(sent) == 1, "401 不该重试"
    assert slept == []
    assert "401" in str(excinfo.value)
    assert "invalid api key" in str(excinfo.value)  # 原文带上，不然查不动


def test_a_transport_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """连不上、超时是"现在不行"，等一会儿大概率就行了。"""
    responses: list[Any] = []  # 先建模块再往里塞，因为 HTTPError 得从模块里取
    module, sent = _fake_httpx(responses)
    responses.append(module.HTTPError("connection reset"))
    responses.append(_ok('{"a": 1}'))
    monkeypatch.setitem(sys.modules, "httpx", module)
    monkeypatch.setattr(client_mod.time, "sleep", lambda _seconds: None)

    assert _client().complete(system="S", user="U") == '{"a": 1}'
    assert len(sent) == 2


def test_giving_up_reports_the_last_error(monkeypatch: pytest.MonkeyPatch) -> None:
    module, sent = _fake_httpx([_Response(500, text="boom")])
    monkeypatch.setitem(sys.modules, "httpx", module)
    monkeypatch.setattr(client_mod.time, "sleep", lambda _seconds: None)

    with pytest.raises(LLMError) as excinfo:
        _client(attempts=3).complete(system="S", user="U")

    assert len(sent) == 3
    assert "3 次" in str(excinfo.value)


def test_missing_httpx_says_how_to_install_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """⭐ 没装 ai extra 时，报错得让人知道该敲哪一行。"""
    # sys.modules 里放 None == 让 `import httpx` 当场失败，不管本机装没装
    monkeypatch.setitem(sys.modules, "httpx", None)

    with pytest.raises(LLMError) as excinfo:
        _client().complete(system="S", user="U")

    message = str(excinfo.value)
    assert 'pip install -e ".[ai]"' in message


def test_the_module_imports_without_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    """延迟 import 的意义就在这里：没装也能拼 prompt、跑离线测试。"""
    monkeypatch.setitem(sys.modules, "httpx", None)
    assert callable(client_mod.parse_reply)


# ── from_settings：缺配置当场停下 ──────────────────────────────────


def _settings(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "supabase_dsn": None,
        "llm_api_key": "sk-test",
        "llm_base_url": "https://api.example.com",
        "llm_model": "test-model",
        "llm_reasoning_effort": "none",
        "batch_size": 8,
    }
    fields.update(overrides)
    return Settings(**fields)


def test_from_settings_names_every_missing_variable() -> None:
    """和 `storage/__main__.py::_dsn()` 同一条规矩：不猜默认值，说清该填哪个。"""
    with pytest.raises(LLMError) as excinfo:
        HttpLLMClient.from_settings(_settings(llm_api_key=None, llm_base_url=None))

    message = str(excinfo.value)
    assert "LLM_BASE_URL" in message
    assert "LLM_API_KEY" in message
    assert ".env.example" in message


def test_from_settings_names_only_the_missing_one() -> None:
    """⚠️ 只看第一行。下面那段"填上：…"把三个变量都列出来是**提示**，
    不是"你缺这三个"——所以断言得落在"没配"那一句上。"""
    with pytest.raises(LLMError) as excinfo:
        HttpLLMClient.from_settings(_settings(llm_api_key=None))

    complaint = str(excinfo.value).splitlines()[0]
    assert "LLM_API_KEY" in complaint
    assert "LLM_BASE_URL" not in complaint


def test_from_settings_builds_a_client() -> None:
    built = HttpLLMClient.from_settings(_settings())

    assert (built.base_url, built.api_key, built.model) == (
        "https://api.example.com",
        "sk-test",
        "test-model",
    )


def test_from_settings_carries_the_reasoning_effort_through() -> None:
    """⭐ 这个字段是**花钱开关**，不是装饰。丢在 from_settings 里的话，
    `.env` 写了 `none` 而报文里还是服务端默认的 `high`——账单上看得出来，
    代码里看不出来。"""
    built = HttpLLMClient.from_settings(_settings(llm_reasoning_effort="low"))

    assert built.reasoning_effort == "low"


def test_an_inline_comment_in_the_effort_is_caught_before_spending() -> None:
    """⭐ `load_dotenv` **刻意**不把 `#` 当注释（DSN 密码里可能有 `#`），
    所以行尾注释会整串吃掉。这个错必须在这里说清楚——放过去的话，
    用户看到的是接口回的 "invalid reasoning_effort"，然后去怀疑模型名写错了。"""
    with pytest.raises(LLMError) as excinfo:
        HttpLLMClient.from_settings(
            _settings(llm_reasoning_effort="low      # none / low / high / max")
        )

    message = str(excinfo.value)
    assert "LLM_REASONING_EFFORT" in message
    assert "注释" in message


def test_the_effort_is_not_silently_truncated() -> None:
    """⚠️ 拦下来是为了让人改 `.env`，**不是**为了替他把值切成第一个词——
    那叫猜。和本模块"绝不悄悄取默认值"是同一条规矩。"""
    for bad in ("low high", "none\t# 说明"):
        with pytest.raises(LLMError):
            HttpLLMClient.from_settings(_settings(llm_reasoning_effort=bad))


def test_a_blank_effort_is_still_allowed_through() -> None:
    """空串是"不传这个字段"的意思，不是错——它是留给将来的逃生口。"""
    assert HttpLLMClient.from_settings(_settings(llm_reasoning_effort="  ")).reasoning_effort == ""
