"""SQL 常量与调用点传的参数必须严丝合缝。

**这类错只在真库上炸，而且是在一次跑了几十分钟的采集跑到一半时炸**：
加了一列忘了加参数、改了占位符名忘了改调用点——psycopg 到执行那一刻才报
"missing parameter"，前面已经采完的东西全白跑。

做法是静态的：AST 扫 `supabase.py`，对每个 `cur.execute(_常量, 参数)` 取出
参数那一侧的形状（元组几个元素 / 字典哪些键），再和常量里的 `%s` 数量或
`%(name)s` 名字集合比对。不需要数据库，也不需要装 psycopg。

⚠️ 它**不检查 SQL 的语义对不对**（那是 `@pytest.mark.integration` 的活），
只检查"参数供得上"。这两件事必须分开——后者能离线做，前者不能。
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sentinel_q.shared.models import AnalysisResult, ContentRecord
from sentinel_q.storage import supabase
from sentinel_q.storage.repo import AnalysisPatch, ContentFilter, EventStancePatch

SUPABASE_PY = Path(supabase.__file__)
SOURCE = SUPABASE_PY.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

_NAMED = re.compile(r"%\((\w+)\)s")
_POSITIONAL = re.compile(r"%s")


def _local_param_keys(func: ast.FunctionDef, name: str) -> set[str] | None:
    """把函数体里对局部变量 `name` 的**全部**写入累加起来。

    三种写法都要认，少认一种就会把"参数供得上"误判成"供不上"：

        params: dict[str, Any] = {"content_id": ...}   # AnnAssign + 字典字面量
        params = {"since": since}                      # Assign + 字典字面量
        params.update(vars(patch))                     # ★ 补丁字段是这里进来的

    ⚠️ 只认第一条就返回的话，`_APPLY_HUMAN_ANALYSIS` 会被判成"缺 ai_summary
    等六个键"——那是**漏报的反面**（误报），同样会让人不再信这个测试。
    """
    keys: set[str] = set()
    found = False
    for node in ast.walk(func):
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target

        # `params = {...}` / `params: dict[...] = {...}`
        if isinstance(target, ast.Name) and target.id == name:
            if isinstance(node.value, ast.Dict):
                keys |= _dict_keys(node.value, func)
                found = True
                continue
            # `params = dict(vars(record))` —— insert_content 用的写法
            if (
                isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "dict"
                and node.value.args
            ):
                inner = node.value.args[0]
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "vars"
                    and inner.args
                    and isinstance(inner.args[0], ast.Name)
                ):
                    fields = _dataclass_fields_for_param(func, inner.args[0].id)
                    if fields is not None:
                        keys |= fields
                        found = True
                continue

        # `params["parent_id"] = ...`
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id == name
        ):
            sl = node.targets[0].slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                keys.add(sl.value)
                found = True

        # `params.update(vars(patch))` / `params.update({...})`
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == name
            and node.args
        ):
            arg = node.args[0]
            if isinstance(arg, ast.Dict):
                keys |= _dict_keys(arg, func)
            elif (
                isinstance(arg, ast.Call)
                and isinstance(arg.func, ast.Name)
                and arg.func.id == "vars"
                and arg.args
                and isinstance(arg.args[0], ast.Name)
            ):
                fields = _dataclass_fields_for_param(func, arg.args[0].id)
                if fields is not None:
                    keys |= fields
            found = True
    return keys if found else None


def _dataclass_fields_for_param(func: ast.FunctionDef, param: str) -> set[str] | None:
    """形参 `param` 的注解若是本文件 import 进来的 dataclass，返回它的字段名。"""
    for a in func.args.args + func.args.kwonlyargs:
        if a.arg != param:
            continue
        node = a.annotation
        if isinstance(node, ast.Name):
            cls = _DATACLASSES.get(node.id)
            if cls is not None:
                return set(cls.__dataclass_fields__)
        if isinstance(node, ast.BinOp):  # `X | None`
            for side in (node.left, node.right):
                if isinstance(side, ast.Name) and side.id in _DATACLASSES:
                    return set(_DATACLASSES[side.id].__dataclass_fields__)
    return None


def _dict_keys(node: ast.Dict, func: ast.FunctionDef) -> set[str]:
    keys: set[str] = set()
    for key, value in zip(node.keys, node.values):
        if key is None:
            # `**vars(patch)` / `**vars(result)`
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "vars"
                and value.args
                and isinstance(value.args[0], ast.Name)
            ):
                fields = _dataclass_fields_for_param(func, value.args[0].id)
                if fields is not None:
                    keys |= fields
            continue
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.add(key.value)
    return keys


def _resolve(func: ast.FunctionDef, arg: ast.expr) -> tuple[str, object]:
    """把一个 execute 的第二个实参解析成 ('positional', n) / ('mapping', keys)。

    ⚠️ 键集合用 `frozenset` 而不是 `set`：形状本身要塞进一个 set 去重，
    而 set 不可哈希。
    """
    if isinstance(arg, ast.Tuple):
        return ("positional", len(arg.elts))
    if isinstance(arg, ast.Dict):
        return ("mapping", frozenset(_dict_keys(arg, func)))
    if isinstance(arg, ast.Call):
        # `vars(result)`：从形参注解反推 dataclass
        if (
            isinstance(arg.func, ast.Name)
            and arg.func.id == "vars"
            and arg.args
            and isinstance(arg.args[0], ast.Name)
        ):
            fields = _dataclass_fields_for_param(func, arg.args[0].id)
            if fields is not None:
                return ("mapping", frozenset(fields))
        return ("unknown", ast.dump(arg)[:40])
    if isinstance(arg, ast.Name):
        keys = _local_param_keys(func, arg.id)
        if keys is not None:
            return ("mapping", frozenset(keys))
        return ("unknown-name", arg.id)
    return ("unknown", ast.dump(arg)[:40])


def _call_sites() -> dict[str, set[tuple[str, object]]]:
    """常量名 -> 该常量在各个调用点上的参数形状（可能有多个）。"""
    sites: dict[str, set[tuple[str, object]]] = {}
    for func in [n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute) and node.func.attr == "execute"):
                continue
            if not node.args or not isinstance(node.args[0], ast.Name):
                continue
            const = node.args[0].id
            if not const.startswith("_") or const.startswith("__"):
                continue  # `__doc__` 之类不是 SQL
            shape = ("none", 0) if len(node.args) < 2 else _resolve(func, node.args[1])
            sites.setdefault(const, set()).add(shape)
    return sites


_DATACLASSES = {
    "AnalysisResult": AnalysisResult,
    "ContentRecord": ContentRecord,
    "AnalysisPatch": AnalysisPatch,
    "EventStancePatch": EventStancePatch,
}

# 由 `_search_from_where` / `_PRESCREEN_*` 组装起来、**不直接出现在 execute 里**
# 的片段。⚠️ 显式登记是刻意的：任何"没有调用点"的常量都必须在这里出现，
# 否则 `test_no_constant_hides_from_the_checker` 会红——
# 不让"漏了一个调用点"躲进 skip 里。
_FRAGMENTS = frozenset(
    {
        "_PRESCREEN_INNER",
        "_WHERE_AUTHOR",
        "_WHERE_PLATFORM_STANCE",
        "_WHERE_RISK",
        "_WHERE_CONTENT_TYPE",
        "_WHERE_PUBLISHED_FROM",
        "_WHERE_PUBLISHED_TO",
        "_WHERE_COLLECTED_FROM",
        "_WHERE_EVENT_EXISTS",
    }
)


def _sql_constants(pattern: str) -> dict[str, object]:
    return {
        name: value
        for name, value in vars(supabase).items()
        if name.startswith("_")
        and not name.startswith("__")
        and isinstance(value, str)
        and pattern in value
    }


_NAMED_SQL = {name: set(_NAMED.findall(v)) for name, v in _sql_constants("%(").items()}
_POSITIONAL_SQL = {name: len(_POSITIONAL.findall(v)) for name, v in _sql_constants("%s").items()}


def test_we_actually_found_sql_constants() -> None:
    """探针自检：扫不到东西的话，下面全是空集上的假绿。"""
    assert len(_NAMED_SQL) >= 8
    assert len(_POSITIONAL_SQL) >= 8
    assert len(_call_sites()) >= 20


def test_no_constant_hides_from_the_checker() -> None:
    """每个带占位符的常量，要么有调用点，要么在 `_FRAGMENTS` 里登记过。

    ⚠️ 这条比下面两条更重要：没有它，"解析不出来"就会变成 skip，
    而 skip 是绿色的。这条把沉默的漏检变成响亮的失败。
    """
    known = set(_NAMED_SQL) | set(_POSITIONAL_SQL)
    called = set(_call_sites())
    unexplained = known - called - _FRAGMENTS
    assert not unexplained, (
        f"这些 SQL 常量既没有直接的 execute 调用点，也没登记成片段："
        f"{sorted(unexplained)}。是忘了在代码里用它，还是这里该更新了？"
    )


# `insert_content` 的参数字典是 `dict(vars(record))` 展开的，所以它比
# `fact_content` 的列**多出**这几个：它们是"只用来翻译主键、本身不是列"的
# 知乎侧标识（决策 52 的翻译表：author_* → author_id，question_zhihu_id →
# question_id，parent_zhihu_id → parent_id）。
#
# ⚠️ 显式登记成一份清单，而不是把断言放宽成"包含"：放宽之后，
#    `ContentRecord` 将来多一个字段（比如 `edit_count`）时测试不会红，
#    于是没人需要回答"这一列到底要不要进 `fact_content`"。
#    现在它会红，逼着人当场做那个决定。
_TRANSLATION_ONLY = frozenset(
    {
        "author_zhihu_id",
        "author_name",
        "author_url",
        "question_zhihu_id",
        "parent_zhihu_id",
    }
)

_ALLOWED_EXTRA: dict[str, frozenset[str]] = {"_INSERT_CONTENT": _TRANSLATION_ONLY}


def test_translation_only_fields_are_real() -> None:
    """那几个字段必须真的在 `ContentRecord` 里，且真的不在 `_INSERT_CONTENT` 里。"""
    for name in _TRANSLATION_ONLY:
        assert name in ContentRecord.__dataclass_fields__, f"{name} 不在 ContentRecord 里"
        assert name not in _NAMED_SQL["_INSERT_CONTENT"], f"{name} 不该是 fact_content 的列"


@pytest.mark.parametrize("const", sorted(_NAMED_SQL))
def test_named_placeholders_are_all_supplied(const: str) -> None:
    """`%(name)s` 的集合必须与调用点给的键集合严丝合缝。

    ⚠️ 严格相等，而且**方向不对称**：

    - **SQL 要而参数没给** → psycopg 抛 `ProgrammingError: query parameter missing`
      （见 `psycopg/_queries.py` 的 `validate_and_reorder_params`）。这是一定要挡的。
    - **参数给了而 SQL 没用** → 对**映射**参数 psycopg **静默忽略**
      （它只做 `vars[item] for item in order`，不检查多余的键）。
      所以这条不是"会炸"，而是纪律：参数字典应当**就是**这条查询的完整描述。
      ⚠️ 但**序列**参数不一样，多给一个值它会抛
      "the query has N placeholders but M parameters were passed"——
      所以 `test_positional_parameter_counts_match` 那条是硬性的。
    """
    if const in _FRAGMENTS:
        pytest.skip("片段，由 _search_from_where 组装——见 test_search_fragment_params")
    shapes = _call_sites().get(const)
    assert shapes, f"{const} 没有调用点（test_no_constant_hides_from_the_checker 应该先红）"
    expected = _NAMED_SQL[const] | _ALLOWED_EXTRA.get(const, frozenset())
    for kind, supplied in shapes:
        assert kind == "mapping", f"{const} 的参数解析成了 {kind}，静态检查看不见它"
        assert expected == supplied, (
            f"{const} 的占位符与调用点传的键对不上\n"
            f"  SQL 要：{sorted(expected)}\n"
            f"  实际给：{sorted(supplied)}"  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("const", sorted(_POSITIONAL_SQL))
def test_positional_parameter_counts_match(const: str) -> None:
    """`%s` 的个数必须等于调用点那个元组的长度。"""
    if const in _FRAGMENTS:
        pytest.skip("片段，由 _search_from_where 组装")
    shapes = _call_sites().get(const)
    assert shapes, f"{const} 没有调用点"
    for kind, supplied in shapes:
        assert kind == "positional", f"{const} 的参数解析成了 {kind}，静态检查看不见它"
        assert _POSITIONAL_SQL[const] == supplied, (
            f"{const} 里 {_POSITIONAL_SQL[const]} 个 %s，调用点给了 {supplied} 个值"
        )


# ── 一条专门的回归防线（决策 47）────────────────────────────────────


def test_human_analysis_sql_never_touches_versions() -> None:
    """⭐ 人工复核**不许**覆盖 `model_version` / `prompt_version`。

    这是本轮最容易写错的地方：`_APPLY_HUMAN_ANALYSIS` 与 `_SAVE_ANALYSIS`
    长得几乎一样，而后者是整行 upsert，会把这两列抹成 NULL。
    "这条判断当时用的哪版提示词"只有这两列答得出来，而 5.4 明说不保留修改
    历史——抹了就永远查不回来。

    ⚠️ 这条**必须**是静态断言：`FakeRepo` 那边的"保留"是天然成立的，
    拿 fake 测等于什么都没测。真正的 SQL 只有真库跑得出来，所以这里退一步，
    把最容易犯的那个错（照抄 save_analysis 的 set 子句）钉死在文本上。
    """
    sql = supabase._APPLY_HUMAN_ANALYSIS
    set_clause = sql.split("do update set", 1)[1]
    for column in ("model_version", "prompt_version"):
        assert column not in set_clause, (
            f"_APPLY_HUMAN_ANALYSIS 的 set 子句里出现了 {column}——"
            "人工复核会把 AI 当时用的模型/提示词版本抹掉（决策 47）"
        )
    assert "analyzed_by       = 'human'" in set_clause, "analyzed_by 必须被强制写成 human"


def test_human_analysis_forces_human_authorship() -> None:
    """`analyzed_by` 在 insert 和 update 两侧都必须被强制成 'human'。"""
    sql = supabase._APPLY_HUMAN_ANALYSIS
    # insert 那一侧：字面量 'human' 必须出现在 values 里
    values_clause = sql.split("values", 1)[1].split("on conflict", 1)[0]
    assert "'human'" in values_clause


def test_analysis_patch_has_no_analyzed_by() -> None:
    """类型级的防线：`AnalysisPatch` 里不该有 `analyzed_by`。

    有的话，调用方就能写 `analyzed_by='ai'`——那是一条把"人工改过"伪装成
    "AI 判的"的路。它必须在类型上就走不通，而不是靠实现层记得别覆盖。
    """
    assert "analyzed_by" not in AnalysisPatch.__dataclass_fields__


def test_refresh_metrics_sql_never_touches_body() -> None:
    """`refresh_content_metrics` 只动度量，不碰正文（决策 51）。

    刷掉正文之后，这一行就成了"正文没判过、判断对着旧正文"——
    而 `fact_analysis` 是针对**当时那段正文**做的判断。
    """
    sql = supabase._REFRESH_METRICS
    for column in ("content_text", "storage_path", "raw_content_hash"):
        assert column not in sql, f"_REFRESH_METRICS 不该碰 {column}"


# ── 片段侧：`_search_from_where` 拼出来的东西 ────────────────────────
#
# 这是本文件里**唯一**不靠 AST、直接调函数的一段。理由是基础检索的 SQL 是
# 按筛选项拼的，静态扫不出来；而它恰恰是将来直接对前端暴露的那个接口。

_FILTER_CASES = {
    "全空": ContentFilter(),
    "按风险": ContentFilter(risk_level="高风险"),
    "按平台立场": ContentFilter(platform_stance="抹黑"),
    "按作者": ContentFilter(author_zhihu_id="u1"),
    "按议题": ContentFilter(event_id=3),
    "按议题立场（不带事件 id）": ContentFilter(event_stance="反向"),
    "按类型": ContentFilter(content_type="answer"),
    "按时间": ContentFilter(
        published_from=datetime(2026, 1, 1, tzinfo=UTC),
        published_to=datetime(2026, 2, 1, tzinfo=UTC),
    ),
    "按采集时间": ContentFilter(collected_from=datetime(2026, 3, 1, tzinfo=UTC)),
    "全都给": ContentFilter(
        event_id=3,
        event_stance="反向",
        platform_stance="抹黑",
        risk_level="高风险",
        content_type="answer",
        author_zhihu_id="u1",
        published_from=datetime(2026, 1, 1, tzinfo=UTC),
        published_to=datetime(2026, 2, 1, tzinfo=UTC),
        collected_from=datetime(2026, 3, 1, tzinfo=UTC),
    ),
}


def _placeholders(text: str) -> set[str]:
    return set(_NAMED.findall(text))


@pytest.mark.parametrize("label", sorted(_FILTER_CASES))
def test_search_fragment_params(label: str) -> None:
    """拼出来的 SQL 要哪些参数，就一定只给哪些参数。"""
    from_sql, where_sql, params = supabase._search_from_where(_FILTER_CASES[label])
    wanted = _placeholders(from_sql) | _placeholders(where_sql)
    assert wanted == set(params), (
        f"[{label}] SQL 要 {sorted(wanted)}，参数给了 {sorted(params)}"
    )


@pytest.mark.parametrize("label", sorted(_FILTER_CASES))
def test_search_fragment_values_never_interpolated(label: str) -> None:
    """⭐ 值一律走参数，**绝不出现在 SQL 文本里**。

    这个接口将来直接对前端暴露，任何一处把值拼进字符串就是注入点。
    拿几个不可能出现在列名/关键字里的字符串试：它们若出现在 SQL 里，
    就说明有人把值拼了进去。
    """
    canary = "zz-canary-9f3a"
    flt = ContentFilter(
        content_type=canary,
        author_zhihu_id=canary,
        event_stance=canary,
        platform_stance=canary,
        risk_level=canary,
    )
    from_sql, where_sql, params = supabase._search_from_where(flt)
    assert canary not in from_sql + where_sql, "有值被拼进了 SQL 文本"
    # 每个参数都还带着值本身（`event_id` 没给，所以是 None）
    assert set(params.values()) == {canary, None}


def test_search_inner_join_only_when_filtering() -> None:
    """join 是条件性的，而且是 inner —— 这决定了"哪些行会消失"。

    全空筛选时必须是 left join：基础检索默认要能看到**所有**内容，
    包括没有作者、没有判断的那些。写成 inner 会让它们静默消失。
    """
    from_sql, _, _ = supabase._search_from_where(ContentFilter())
    assert supabase._SEARCH_LEFT_JOIN_AUTHOR in from_sql
    assert supabase._SEARCH_LEFT_JOIN_ANALYSIS in from_sql

    from_sql, _, _ = supabase._search_from_where(ContentFilter(risk_level="高风险"))
    assert supabase._SEARCH_JOIN_ANALYSIS in from_sql.replace(
        supabase._SEARCH_LEFT_JOIN_ANALYSIS, ""
    ), "按风险筛必须 inner join fact_analysis，否则没有判断的行会混进来"


def test_event_filter_uses_exists_not_join() -> None:
    """⭐ 议题筛选走 exists 子查询，**不产生 join**。

    这不是风格问题：`fact_content_event` 的主键是 (content_id, event_id)，
    一条内容可以关联多个议题。用 join 的话，只给 event_stance 不给 event_id 时
    同一条内容会出现**多次**（每个匹配的议题一行），而 count_contents 也得
    跟着去重——两处口径一旦不一致，就是"翻页翻出重复行"这种只有用户
    看得见的错。exists 天然不可能重复。
    """
    from_sql, where_sql, _ = supabase._search_from_where(ContentFilter(event_stance="反向"))
    assert "fact_content_event" not in from_sql, "议题过滤不该出现在 join 里"
    assert "exists" in where_sql
