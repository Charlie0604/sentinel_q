"""模型调用 + 答复解析（架构文档 7.2）。

三个任务（内容 / 问题 / 事件）共用这一个文件——它们只差 prompt 内容和落哪张表，
调模型那一段是完全一样的。所以这里只管两件事：

    complete()      把 system + user 发出去，拿回一段文本
    parse_reply()   把那段文本变成 dict，外加一套字段校验的小工具

## ⚠️ httpx 是延迟 import 的

`import httpx` 写在 `complete()` 里面，和 `storage/migrate.py` 延迟 import psycopg
同一个手法。理由也一样：**没装 `ai` extra 时，这个模块的纯逻辑仍然可用**——
拼 prompt、解析答复、跑离线测试全都照常，只有真的去调模型才报错。

## ⚠️ 一次请求只处理一条内容

架构文档 4.3 明确禁止批量拼接（"每次请求只处理一条内容……避免长上下文中内容被忽略
导致漏判"）。所谓"并发推 8 条"是 8 个**并发请求**，不是把 8 条拼进一个 prompt。
所以这里没有"批"的概念，并发在 `batch.py` 里，解析天然一一对应。

## 解析器的立场：**容错但不猜**

答复的**格式**可以脏（外面裹着 ``` 围栏、前后有寒暄），但**内容**必须合约：
字段缺了、枚举越界了，一律抛 `LLMSchemaError`。

⚠️ **绝不悄悄取默认值。** 把一段坏 JSON 兜成"中风险 / 中立"等于替模型编了一条
判断，而这条判断会被原样写进 `fact_analysis`，事后没人分得清哪条是真的、
哪条是解析器补的。文档 G 里那句"模糊时默认中风险、中立"是给**模型**的判断标准，
不是给**解析器**的兜底——这两件事看着像，差了十万八千里。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from sentinel_q.shared.config import Settings

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120.0
"""单次请求超时。长文不截断（4.3），所以给得比一般的 API 调用宽。"""

DEFAULT_ATTEMPTS = 3
"""1 次 + 2 次重试。失败重试与限流在 4.6 里还是"待讨论"，这是先跑起来的保守值。"""

RETRY_BACKOFF = 1.5
"""退避底数：第 n 次重试前等 `RETRY_BACKOFF ** n` 秒。"""

TEMPERATURE = 0
"""分类任务要的是稳定复现——同一个输入问两遍得到两个答案，追溯就没意义了。"""

JSON_MODE = True
"""发 `response_format={"type": "json_object"}`（OpenAI 兼容协议的标准字段）。

⚠️ 少数自建网关会拒收这个字段。真遇到 400 且报文里提到 response_format，
把这里改成 False 即可——提示词的 E 模块本来就要求"只输出 JSON"，
解析器也容错，关掉它只是少一层保险。
"""

DEFAULT_REASONING_EFFORT = "none"
"""思考强度的兜底值，`.env` 里的 `LLM_REASONING_EFFORT` 优先。

⚠️ **这不是个调优项，是个花钱项。** 以 `deepseek-flash` 为例，`GET /models` 回的
是 `"effort": {"supported_levels": ["low","high","max"], "default_level": "high"}`
——**不传这个字段就是最高档**，每判一条都在烧推理 token，而这只是个分类任务。

⚠️ 还有一层：**思考模式下 `temperature` 被服务端忽略**。所以关掉思考不只是省钱，
它还让 `TEMPERATURE = 0` 那句"同一输入问两遍得到同一个答案"真的成立——
不然决策 47 说的"追溯"就少了一根支柱。

⚠️ 但**关掉思考是有代价的**：模型不再"想一遍再答"。真发现它在事件任务那种
"同一家企业、另一桩事"上判错，第一个该动的旋钮就是它——改成 `low` 是一行 `.env`
的事，不用动代码。空串 = 整个字段不传，完全交给服务端默认。
"""

_REPLY_EXCERPT = 200
"""报错时附上答复的前多少个字符。不带原文的解析错误没法查。"""


# ── 异常 ────────────────────────────────────────────────────────────


class LLMError(RuntimeError):
    """调模型这一层的统一父类。

    调用方只想区分"这条判不出来"和"代码写错了"，所以底下分两个子类就够了。
    """


class LLMReplyError(LLMError):
    """答复不是合法 JSON，或者不是一个 JSON 对象。"""


class LLMSchemaError(LLMError):
    """JSON 合法，但字段不合约：缺字段、枚举越界、类型不对、数值出界。"""


# ── 客户端 ──────────────────────────────────────────────────────────


@runtime_checkable
class LLMClient(Protocol):
    """调模型的最小接口。

    三个 `judge_*.py` 只依赖这个协议，所以测试可以喂一个假的进来——
    **一次真实 API 都不用调**（架构文档 7.5 的"独立测试"就是这个意思）。
    """

    model: str

    def complete(self, *, system: str, user: str) -> str:
        """把 system + user 发出去，拿回模型答复的**原文**。

        解析不在这里做：这里返回的是字符串，脏格式由 `parse_reply` 兜。
        """
        ...


def _httpx() -> Any:
    """延迟取 httpx。**没装时说一句能照着做的人话。**"""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - 装了 ai extra 就不会走到
        raise LLMError(
            "❌ 没装 httpx，发不出请求，调不了模型。\n"
            '   装一下：pip install -e ".[ai]"\n'
            "   （拼 prompt、解析答复这些纯逻辑不需要它，只有真发请求才需要。）"
        ) from exc
    return httpx


def _content_of(payload: Any) -> str:
    """从 OpenAI 兼容的响应体里取答复正文。

    形状是 `{"choices": [{"message": {"content": "..."}}]}`。取不到就抛——
    这里**不返回空串**：空串会让解析器报"不是 JSON"，而真正的原因是
    接口返回了一个我们没见过的形状，两件事得分开说。
    """
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMReplyError(
            f"答复里没有 choices[0].message.content——接口返回的形状不对："
            f"{_excerpt(json.dumps(payload, ensure_ascii=False))}"
        ) from exc
    if not isinstance(content, str):
        raise LLMReplyError(f"choices[0].message.content 不是字符串：{content!r}")
    return content


def _worth_retrying(status: int) -> bool:
    """哪些状态码值得重试。

    ⚠️ **4xx 一律不重试。** 400（报文写错了）、401（密钥不对）、403（没权限）
    重试三次只是把配额白烧三遍，还把真正的错因埋在三行 warning 底下。
    429 和 5xx 是另一回事——那是"现在不行"，等一会儿大概率就行了。
    """
    return status == 429 or status >= 500


@dataclass(frozen=True)
class HttpLLMClient:
    """走 OpenAI 兼容协议的 HTTP 客户端。

    ⚠️ **每次调用新建一个 `httpx.Client`**，用完就关。看着浪费，但换来两件事：
    没有跨线程共享的连接状态（`batch.py` 要并发 8 条），也不用管谁负责关闭它。
    真到了瓶颈再换成共享连接池。
    """

    base_url: str
    api_key: str
    model: str
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    """思考强度；**空串 = 报文里不带这个字段**，用服务端默认。见模块级那条注释。"""

    timeout: float = DEFAULT_TIMEOUT
    attempts: int = DEFAULT_ATTEMPTS

    @classmethod
    def from_settings(cls, settings: Settings) -> HttpLLMClient:
        """从 `.env` 来的配置拼一个客户端。**缺配置就报错，不猜默认值。**

        和 `storage/__main__.py::_dsn()` 同一条规矩：宁可当场停下说清楚该填哪两个
        变量，也不要拿一个猜的地址去发请求——那样拿到的是 404 或 401，
        而人会先怀疑自己的 key 是不是过期了。
        """
        missing = [
            name
            for name, value in (
                ("LLM_BASE_URL", settings.llm_base_url),
                ("LLM_API_KEY", settings.llm_api_key),
            )
            if not value
        ]
        if missing:
            raise LLMError(
                f"❌ 没配 {'、'.join(missing)}，调不了模型。\n"
                "   复制 .env.example 成 .env（已在 .gitignore 里），填上：\n"
                "     LLM_BASE_URL=   OpenAI 兼容接口的根地址，不带 /chat/completions\n"
                "     LLM_API_KEY=    那家的密钥\n"
                "     LLM_MODEL=      要和上面那个地址对得上的模型名\n"
                "   （可选）LLM_REASONING_EFFORT=  思考强度，缺省 none=关掉思考"
            )
        effort = settings.llm_reasoning_effort.strip()
        # ⚠️ 这一条专门拦"行尾写注释"。`shared/config.py::load_dotenv` 是刻意
        #    **不把 `#` 当注释**的（DSN 的密码里可能有 `#`），所以
        #    `LLM_REASONING_EFFORT=low  # none/low/high/max` 读出来是整串
        #    "low  # none/low/high/max"，发出去就是 400。宁可这里说清楚，
        #    也不要让人对着一条 "invalid reasoning_effort" 去猜自己哪里写错了。
        #    **不替它截断**——把值切成第一个词就是在猜，和本模块"绝不悄悄取默认值"
        #    那条是同一条规矩。
        if any(ch.isspace() for ch in effort):
            raise LLMError(
                f"❌ LLM_REASONING_EFFORT 的值里有空白：{effort!r}\n"
                "   最常见的原因是**在行尾写了注释**——本项目的 `.env` 解析器"
                "把 `#` 当普通字符（DSN 密码里可能有 `#`），\n"
                "   所以 `LLM_REASONING_EFFORT=low  # 说明` 读出来就是这一整串。\n"
                "   把注释挪到单独一行，只留取值本身：none / low / high / max"
            )

        return cls(
            base_url=settings.llm_base_url or "",
            api_key=settings.llm_api_key or "",
            model=settings.llm_model,
            reasoning_effort=effort,
        )

    def complete(self, *, system: str, user: str) -> str:
        httpx = _httpx()
        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": TEMPERATURE,
        }
        if JSON_MODE:
            body["response_format"] = {"type": "json_object"}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort

        last: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                with httpx.Client(timeout=self.timeout) as http:
                    response = http.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                # 传输层：连不上、超时、TLS 出错——都值得再试一次
                last = exc
                log.warning("调模型失败（第 %d/%d 次，传输层）：%s", attempt, self.attempts, exc)
            else:
                if response.status_code == 200:
                    return _content_of(_json_body(response))
                if not _worth_retrying(response.status_code):
                    raise LLMError(
                        f"❌ 接口返回 {response.status_code}，重试也没用，当场停下。\n"
                        f"   {_excerpt(response.text)}"
                    )
                last = LLMError(f"接口返回 {response.status_code}")
                log.warning(
                    "调模型失败（第 %d/%d 次，HTTP %d）：%s",
                    attempt,
                    self.attempts,
                    response.status_code,
                    _excerpt(response.text),
                )

            if attempt < self.attempts:
                time.sleep(RETRY_BACKOFF**attempt)

        raise LLMError(f"❌ 试了 {self.attempts} 次都没成功，最后一条：{last}")


def _json_body(response: Any) -> Any:
    """把响应体解析成 JSON。**不是 JSON 就抛，不要往下走。**"""
    try:
        return response.json()
    except Exception as exc:
        raise LLMReplyError(
            f"接口返回的正文不是 JSON（HTTP {response.status_code}）："
            f"{_excerpt(response.text)}"
        ) from exc


# ── 答复解析 ────────────────────────────────────────────────────────


def _excerpt(text: str, limit: int = _REPLY_EXCERPT) -> str:
    """截一段原文。带原文的报错才查得动，但整篇贴进日志就没人看了。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _strip_fence(text: str) -> str:
    """剥掉 ```` ```json ```` 围栏。

    围栏本来就该由提示词挡住（E 模块写着"只输出 JSON，不要输出任何额外说明文字"），
    但真出现了也不该算失败——**格式脏**和**内容不合约**是两件事。
    """
    if not text.startswith("```"):
        return text
    lines = text.splitlines()[1:]  # 去掉 ``` / ```json 那一行
    if lines and lines[-1].strip() == "```":
        lines.pop()
    return "\n".join(lines).strip()


def parse_reply(text: str) -> dict[str, Any]:
    """把模型答复解析成 dict。**容错但不猜**。

    三步，一步比一步凶：

      1. 原样 `json.loads`（正常情况）
      2. 剥掉 ``` 围栏再来一次
      3. 取第一个 `{` 到最后一个 `}` 再来一次（前后有寒暄时）

    三步都失败就抛 `LLMReplyError`，并且**附上答复的前 200 个字符**——
    不带原文的解析错误，事后只能靠猜。

    ⚠️ 解析出来不是对象（比如模型返回了 `[1, 2]` 或 `"相关"`）同样算失败：
    后面所有字段校验都是按"这是个 dict"写的，放过去会在更远的地方炸。
    """
    stripped = text.strip()
    for candidate in (stripped, _strip_fence(stripped), _braces(stripped)):
        if candidate is None:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
        raise LLMReplyError(
            f"答复是合法 JSON，但不是对象（是 {type(data).__name__}）：{_excerpt(text)}"
        )

    raise LLMReplyError(f"答复不是合法 JSON：{_excerpt(text)}")


def _braces(text: str) -> str | None:
    """取第一个 `{` 到最后一个 `}`。够用就行——不写"括号配平"那种解析器。"""
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else None


# ── 字段校验 ────────────────────────────────────────────────────────
#
# 三个 judge_*.py 共用这几个。**不合约一律抛 `LLMSchemaError`**，
# 没有一个"算了给个默认值"的分支——理由见模块开头。


def require_bool(data: dict[str, Any], key: str) -> bool:
    """必填的布尔字段。

    ⚠️ 先判 `isinstance(value, bool)` 而不是先判类型再转：Python 里
    `isinstance(True, int)` 是 True，把 `1` 当布尔收下等于默默接受了一个
    模型没按 schema 输出的信号。
    """
    if key not in data:
        raise LLMSchemaError(f"答复里缺 {key!r} 这个字段：{_excerpt(json.dumps(data, ensure_ascii=False))}")
    value = data[key]
    if not isinstance(value, bool):
        raise LLMSchemaError(f"{key!r} 应该是 true/false，拿到的是 {value!r}")
    return value


def require_choice(data: dict[str, Any], key: str, allowed: tuple[str, ...]) -> str:
    """必填的枚举字段。**不在枚举里就抛，不挑一个最近的。**"""
    if key not in data:
        raise LLMSchemaError(f"答复里缺 {key!r} 这个字段：{_excerpt(json.dumps(data, ensure_ascii=False))}")
    value = data[key]
    if not isinstance(value, str) or value.strip() not in allowed:
        raise LLMSchemaError(
            f"{key!r} 只能是 {'/'.join(allowed)} 之一，拿到的是 {value!r}"
        )
    return value.strip()


def optional_str(data: dict[str, Any], key: str, *, limit: int = 500) -> str | None:
    """选填的文本字段。空串归一成 None——`fact_analysis` 里空摘要和没摘要是两回事。"""
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise LLMSchemaError(f"{key!r} 应该是字符串，拿到的是 {value!r}")
    return value.strip()[:limit] or None


def optional_float(
    data: dict[str, Any], key: str, *, low: float = 0.0, high: float = 1.0
) -> float | None:
    """选填的数值字段，默认按置信度收在 0~1。出界就抛，不夹到边界上。"""
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LLMSchemaError(f"{key!r} 应该是数字，拿到的是 {value!r}")
    number = float(value)
    if not low <= number <= high:
        raise LLMSchemaError(f"{key!r} 应该落在 {low}~{high}，拿到的是 {number}")
    return number
