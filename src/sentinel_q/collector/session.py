"""浏览器会话：登录保持、验证码等待、节奏控制（能力五）。

## 这里刻意不做什么

参考仓库 `zhihu-scraper` 的 `browser.py` 里有 200 行反检测代码——canvas/WebGL/
AudioContext 指纹加噪、覆写 `navigator.webdriver`、随机化 `hardwareConcurrency`、
随机 UA、随机窗口尺寸。**指纹伪造、随机化那一整套一条都没抄**，
这是架构文档边界条款的直接执行：

> "逆向破解知乎的加密签名参数，或搭建专门用于规避风控的代理池/打码平台，
> 不在本系统的技术方案范围内……用有规避风控痕迹的方式采集的数据，
> 会削弱证据的正当性。"

一个**真实浏览器 + 真实账号 + 放慢节奏**的会话，本身就是最干净的采集方式：
不需要伪装，因为本来就是真的。反而是指纹加噪这种半吊子伪装更容易被识别。

所以这里的反爬策略只有三条：**用持久 profile 保持真实登录态**、**放慢节奏**、
**遇到登录/验证码停下来等人工，绝不绕过**。

## ⚠️ 一处具名例外（2026-09-26）

下面 `_STEALTH_ARGS` 里有一条 `--disable-blink-features=AutomationControlled`，
用来关掉 `navigator.webdriver`。这是**全项目唯一一处**，由项目所有者明确同意，
理由和边界分析写在 `SessionConfig.suppress_webdriver_flag` 的注释里，去看那段。

一句话版本：带着那个标记时，知乎发下来的是**一个加载不出来的验证码**，
人工想点都点不了——等于把"遇到验证码停下来等人工"这条设计废掉了。
关掉它是为了让那条设计重新可用，不是为了让验证码消失。

## ⚠️ 一个必须知道的限制：profile 目录是独占的

Chromium 对 `user-data-dir` 加独占锁。**同一台机器上的两个采集进程不能共用一个
profile 目录**——第二个会直接启动失败。所以：

- 一人一台机器（架构文档设想的 2~4 人并行）→ 各自一个 profile，互不影响
- 一台机器要跑多进程 → 必须给每个进程不同的 `--profile` 名字，
  代价是**每个 profile 都要单独人工登录一次**
"""

from __future__ import annotations

import logging
import random
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sentinel_q.collector import selectors

log = logging.getLogger(__name__)

HOME_URL = "https://www.zhihu.com"
SIGNIN_URL = "https://www.zhihu.com/signin"

SETTLE_MS = 1_200
"""`settle()` 的时长（毫秒）——`load` 最多等这么久，之后**再实打实地停这么久**。

⚠️ **只在没给 `ready` 时是这样**：给了 `ready` 就换成轮询，见 `READY_MS`。

原来是 `networkidle` + 10 秒：实测知乎上 `networkidle` 永远不触发，
于是每次调用都满额烧完 10 秒（见 `settle()`）。
"""

READY_MS = 3_000
"""`settle(ready=...)` 最多等这么久（毫秒）。

**比 `SETTLE_MS` 长是故意的**：那时等的不是"过了一段时间"，而是"某个元素真的
出现了"——没出现的话多等一会儿是有意义的（页面慢 ≠ 页面没有）。多的那点时间
只落在**本来就拿不到东西**的页面上，那些页面本来就是失败的。
"""

READY_POLL_MS = 150
"""轮询间隔。一轮是一次 JS 往返（本机个位数毫秒），150ms 是留给页面重排的余量。"""

_STEALTH_ARGS: tuple[str, ...] = ("--disable-blink-features=AutomationControlled",)
"""⚠️ 全项目仅此一处"反检测"，且**只有这一条**。

关掉 `navigator.webdriver`，让知乎把验证码正常渲染出来，人工才有得点。
理由与范围锁定见 `SessionConfig.suppress_webdriver_flag` 的注释。

**不要往这里加东西。** 加之前先读那段注释——它写明了为什么这一条可以、
以及为什么下一加一条就不行。
"""


@dataclass
class SessionConfig:
    """会话参数。默认值都是"慢一点、稳一点"。"""

    profile_dir: Path
    channel: str | None = "chrome"
    """用哪个浏览器驱动。默认 `"chrome"` = 你本机装的正版 Google Chrome。

    ⚠️ **这一条是实测出来的，不是洁癖**（2026-09-26）：

    用 Playwright 自带的 Chromium 时，知乎对它的第一反应是
    「系统监测到您的网络环境存在异常」，而那个验证码**加载不出来**
    （一直"正在加载请稍后重试"）——**人工都没法过**，整条路直接堵死。
    同一个网络下换真实 Chrome 就正常，能点验证、能到登录页。

    原因不在 IP，在浏览器本身：自带的 Chromium 是一个独立构建，
    启动时带一堆自动化开关，`navigator.webdriver = true` 直接把身份报了出去。

    ⚠️ **注意我们这里没有加任何伪装。** 文档禁止的是指纹伪造那一类；
    这里做的是相反的事——**用一个真实的浏览器**。这正是架构文档那句话的
    字面意思："一个真实浏览器 + 真实账号的会话，本身就是最干净的采集方式。"

    传 `None` 退回自带的 Chromium。只在没装 Chrome 的机器上用，
    而且大概率会卡在上面那个验证码上。
    """

    headless: bool = False
    """⚠️ 必须为 False。人工登录和处理验证码都需要看到窗口。"""

    suppress_webdriver_flag: bool = True
    """只关掉 `navigator.webdriver` 这一个信号。**边界条款的具名例外，2026-09-26 由项目所有者决定。**

    ## 为什么必须有它

    实测（2026-09-26）：带有 `navigator.webdriver = true` 的浏览器访问知乎，
    得到的不是"一个需要人过的验证码"，而是**一个加载不出来的验证码**
    （一直"正在加载请稍后重试"）。换真实 Chrome 也一样，因为开关还在。
    关掉它之后验证码才能正常渲染，人才能过。

    所以这一条的性质是：**它让"人工处理验证码"这条既定设计重新可用**，
    而不是绕过验证码。没有它，架构文档里"遇到验证码停下来等人工"那一条
    在知乎这里是**失效的**——你想人工过都过不了。

    ## 它和文档禁止的东西差在哪

    文档禁止的是"逆向加密签名、代理池、打码平台"，理由是*用有规避风控痕迹的
    方式采集的数据会削弱证据的正当性*。这一条和它们的区别是：

    | | 本开关 | 文档禁止的 |
    |---|---|---|
    | 身份 | 一个真人 + 一个真实账号 + 一个真实浏览器 | 伪装成别人/别的设备 |
    | 指纹 | 完全没动，就是浏览器自己的值 | canvas/WebGL 加噪、随机 UA |
    | 出口 | 你自己家的网络，且**明确不换 IP** | 代理池轮换 |
    | 验证码 | 人自己过 | 打码平台代过 |

    它不伪造任何东西，只是**不让浏览器主动举手说"我是被程序开的"**。

    ## 范围锁定

    ⚠️ **这就是本次唯一一处例外，清单到此为止。**
    以后任何想往 `_STEALTH_ARGS` 里加东西的想法，都必须像这一条一样
    单独拿出来、说清楚为什么，并由项目所有者明确同意——
    **不允许"反正已经开了一个口子，再多加几个也无所谓"**。
    那种滑坡正是文档那条边界要防的东西。

    想完全回到原状：传 `suppress_webdriver_flag=False`。
    """

    pace_range: tuple[float, float] = (3.0, 8.0)
    """每次页面操作之间的随机停顿秒数。这是**唯一的**反爬手段。"""

    login_timeout: float = 600.0
    """等人工登录的上限（秒），默认 10 分钟。"""

    captcha_timeout: float = 900.0
    """等人工处理验证码的上限（秒），默认 15 分钟。"""

    poll_interval: float = 2.0
    """轮询"人工弄好了没"的间隔。"""

    slow_mo: int = 120
    """Playwright 每个动作自带的额外延迟（毫秒）。"""


class LoginTimeout(RuntimeError):
    """等人登录/过验证码等到超时。"""


class ProfileLocked(RuntimeError):
    """profile 目录被别的进程占着。"""


@dataclass
class BrowserSession:
    """一个真实浏览器 + 一个真实登录态。

    用法：

        with BrowserSession(config) as session:
            session.ensure_logged_in()
            page = session.open("https://www.zhihu.com/search?q=...")
    """

    config: SessionConfig
    on_status: Callable[[str], None] | None = None
    """状态变化回调，用来把 `paused_need_captcha` 写进 ops。

    回调而不是直接依赖 OpsStore，是为了让"暂停状态必须落盘"这件事
    在调用方显式可见——它关系到崩溃后断点还在不在。
    """

    _playwright: object = field(default=None, init=False, repr=False)
    _context: object = field(default=None, init=False, repr=False)
    _page: object = field(default=None, init=False, repr=False)

    # ── 生命周期 ────────────────────────────────────────────────────

    def __enter__(self) -> BrowserSession:
        if self.config.headless:
            log.warning(
                "headless=True：人工登录和验证码处理都会变得无法进行。"
                "仅在已确认登录态有效、且不预期触发验证码时使用。"
            )

        from playwright.sync_api import sync_playwright  # 延迟导入：测试不需要它

        self.config.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()

        launch_options: dict[str, object] = {
            "user_data_dir": str(self.config.profile_dir),
            "headless": self.config.headless,
            "slow_mo": self.config.slow_mo,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            # 注意：这里没有 user_agent、没有 viewport 随机化、没有
            # add_init_script 指纹伪装。用浏览器自己的默认值——
            # 一个配置稳定的真实浏览器，比每次随机换皮更像真人。
        }
        if self.config.channel:
            launch_options["channel"] = self.config.channel
        if self.config.suppress_webdriver_flag:
            launch_options["args"] = list(_STEALTH_ARGS)

        try:
            self._context = self._playwright.chromium.launch_persistent_context(  # type: ignore[attr-defined]
                **launch_options
            )
        except Exception as exc:
            self._shutdown()
            if _looks_like_profile_lock(exc):
                raise ProfileLocked(
                    f"profile 目录被占用：{self.config.profile_dir}\n"
                    "   浏览器对 user-data-dir 加独占锁，同机器上的两个采集进程"
                    "不能共用同一个 profile，"
                    "**你手动开的那个 Chrome 窗口也必须先关掉**。\n"
                    "   给这个进程换一个 --profile 名字（代价是要单独登录一次），"
                    "或者等同机器上另一个采集进程结束。"
                ) from exc
            if _looks_like_missing_channel(exc, self.config.channel):
                raise RuntimeError(
                    f"找不到浏览器 channel={self.config.channel!r}。\n"
                    "   装上 Google Chrome 再跑，或者显式传 channel=None 退回自带的 Chromium\n"
                    "   ⚠️ 但自带的 Chromium 大概率会卡在知乎那个加载不出来的验证码上，"
                    "人工都过不去（见 SessionConfig.channel 的注释）。"
                ) from exc
            raise

        self._page = self._context.new_page()  # type: ignore[attr-defined]
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._shutdown()

    def _shutdown(self) -> None:
        for closer in (self._context, self._playwright):
            if closer is None:
                continue
            try:
                closer.close() if hasattr(closer, "close") else closer.stop()  # type: ignore[attr-defined]
            except Exception:
                pass  # 关不掉就算了，别在退出路径上抛异常盖住真正的问题
        self._context = self._page = self._playwright = None

    @property
    def page(self):
        if self._page is None:
            raise RuntimeError("会话还没启动。请用 `with BrowserSession(...) as s:`")
        return self._page

    # ── 节奏 ────────────────────────────────────────────────────────

    def pace(self, factor: float = 1.0) -> None:
        """随机停顿。**这是本系统唯一的反爬手段**，别为了跑快把它去掉。"""
        low, high = self.config.pace_range
        time.sleep(random.uniform(low, high) * factor)

    def settle(self, ready: Callable[[], bool] | None = None) -> None:
        """停一下，等 SPA 把 DOM 渲染出来。

        ⚠️ **不给 `ready` 时等的不是 `networkidle`，是一个固定时长**，这是刻意的：
        知乎有长连接，`networkidle` 实测**永远不触发**，所以那 10 秒的 timeout
        每次都会老老实实烧完——开一页 10 秒、开个弹窗又 10 秒，采一条内容
        光等待就一分多钟（项目所有者实测反馈）。

        **给了 `ready` 就换成轮询**：条件一成立立刻往下走，最多等 `READY_MS`。
        它替掉的是那个固定时长（快的时候省下大半），"什么算渲染好了"由调用方
        定义——只有调用方知道自己在等什么（能力二给的是 `content._body_ready`）。

        ⚠️ **不给 `ready` 时它仍然不保证任何东西。** 需要"某个元素真的出现了"
        才算就绪的地方，要么传 `ready`，要么像 `search._wait_for_panel` 那样自己轮询。
        把这个值调大只会让每一次点击都变慢，不会更可靠。
        """
        # 延迟导入，和 `_start` 里一样：不碰浏览器的路径（--help、参数校验）不该
        # 因为这个 import 就要求装了 playwright。下面也只吞这一个具体异常。
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        try:
            # `load` 在 SPA 上是"资源齐了"，React 渲染在它之后，所以后面还得等。
            self.page.wait_for_load_state("load", timeout=SETTLE_MS)
        except PlaywrightTimeout:
            # 超时**不是错误**：图片视频没下完也得往下走，后面的等待才是真正等的。
            log.debug("settle：load 没在 %d ms 内完成，照常继续", SETTLE_MS)

        if ready is None:
            self.page.wait_for_timeout(SETTLE_MS)
            return

        # 用死线而不是"睡了几轮"：`ready()` 自己也要花时间（一次 JS 往返），
        # 只数睡眠的话实际总时长会比 READY_MS 长出一截。
        deadline = time.monotonic() + READY_MS / 1000
        while not ready():
            if time.monotonic() >= deadline:
                # 不是错误、也不抛：等到的可能性已经很小了，往下走让解析层
                # 用更准的判据失败一次（那种失败是**报出来的**，见 content）。
                log.debug("settle：ready 条件等满 %d ms 仍未成立，照常往下走", READY_MS)
                return
            self.page.wait_for_timeout(READY_POLL_MS)

    # ── 导航 ────────────────────────────────────────────────────────

    def open(
        self,
        url: str,
        *,
        navigate: bool = True,
        ready: Callable[[], bool] | None = None,
    ) -> object:
        """打开一个页面：导航 → 查验证码 → 等渲染。返回 page。

        `navigate=False` 给"页面已经在这个 URL 上"的调用方用（能力二采完正文接着
        采评论就是这种）。**省掉的是导航，不是安全检查**——`guard()` 和 `settle()`
        照旧跑，因为验证码可能在上一段操作之后才冒出来。

        `ready` 透传给 `settle()`：给了就轮询等它成立（见那个方法的说明）。
        """
        if navigate:
            self.page.goto(url, wait_until="domcontentloaded")
        self.guard()
        self.settle(ready)
        return self.page

    # ── 登录 ────────────────────────────────────────────────────────

    def ensure_logged_in(self) -> None:
        """没有有效登录态就停下来等人工登录。

        等待期间进程**不退出**——用户明确要求。登录态靠持久 profile 保存，
        所以正常只需要登录这一次，之后每次跑都是好的。
        """
        self.open(HOME_URL)
        if is_logged_in(self.page):
            log.info("登录态有效（profile：%s）", self.config.profile_dir)
            return

        log.warning("未检测到有效登录态，跳转到登录页")
        self.page.goto(SIGNIN_URL, wait_until="domcontentloaded")
        self._wait_for_human(
            done=lambda: is_logged_in(self.page),
            banner=(
                "需要人工登录。\n"
                "   请在打开的浏览器窗口里完成登录（扫码或账号密码均可）。\n"
                "   登录态会保存在 profile 目录里，之后不需要重复登录。\n"
                "   进程会一直等着，不会退出。"
            ),
            timeout=self.config.login_timeout,
            what="登录",
        )
        log.info("人工登录完成，继续采集")
        self.pace()

    # ── 验证码 ──────────────────────────────────────────────────────

    def guard(self) -> None:
        """每次导航后调用：撞上验证码就停下来等人工处理完，再继续。

        **绝不终止进程，也绝不尝试绕过**——打码平台在架构文档里是明确禁止的，
        而绕过的痕迹会削弱采集数据的证据正当性。人工过一次就够了。
        """
        if not captcha_present(self.page):
            return

        # 先把状态落盘再等：万一等待期间进程被杀，断点和"为什么停的"都还在
        if self.on_status:
            self.on_status("paused_need_captcha")

        self._wait_for_human(
            done=lambda: not captcha_present(self.page),
            banner=(
                "触发验证码。\n"
                "   请在浏览器窗口里手动完成验证。\n"
                "   进程会一直等着，不会退出，处理完自动继续。"
            ),
            timeout=self.config.captcha_timeout,
            what="验证码",
        )

        if self.on_status:
            self.on_status("running")
        log.info("验证码已处理，继续采集")
        self.pace()

    def _wait_for_human(
        self,
        *,
        done: Callable[[], bool],
        banner: str,
        timeout: float,
        what: str,
    ) -> None:
        """响铃提示 + 轮询等待。等到了就返回，超时才抛。"""
        _alert(banner)
        deadline = time.monotonic() + timeout
        warned = False
        while time.monotonic() < deadline:
            time.sleep(self.config.poll_interval)
            try:
                if done():
                    return
            except Exception:
                # 等待期间页面可能正在跳转，查询会临时失败。这不算错误，继续轮询。
                continue

            left = deadline - time.monotonic()
            if left < 60 and not warned:
                warned = True
                log.warning("%s 还剩不到 1 分钟，超时后进程会退出", what)

        raise LoginTimeout(
            f"等了 {timeout:.0f} 秒仍没等到{what}完成。\n"
            "   断点已保存在 runtime/ops/ 下，重新跑会从这里接着走。"
        )


# ── 页面状态判断（纯函数，方便单独测）────────────────────────────────


def is_logged_in(page: object) -> bool:
    """是否已登录。**以 cookie 为准，不以 DOM 为准。**

    ## 为什么改（2026-09-26，真事）

    原来只看 `LOGIN_INDICATOR` 那个 DOM 选择器。用户手动登录成功了，
    程序却一直判定"未登录"，卡在"等你登录"那一步死等到超时。

    原因很直接：那个选择器是照旧版知乎猜的、从没校准过，匹配不到任何元素——
    而"匹配不到"和"没登录"在代码里长得一模一样，都是 `query_selector` 返回 None。
    **一个未经校准的选择器不会报错，它只是安静地判错。**

    换成 cookie 之后这类错误就没了：cookie 是**依据**，DOM 只是**表现**。
    浏览器带着登录 cookie，服务器就认你；页面长什么样取决于前端今天怎么写。

    ## 方向性

    判不准时**返回 False**（当作没登录）——这个方向的错误是"多等一次人工确认"，
    可见且无害；反方向（明明没登录却当作已登录）会让整轮采集全部抓到登录墙，
    而且看起来像"搜索没有结果"，属于文档 4.1 说的不可接受的失败。
    """
    if _auth_cookies(page):
        return True

    # cookie 没有时再看两个便宜的旁证。**它们只用来加分，不用来减分**：
    # 匹配不到不代表没登录，所以这里返回 False 依然是"判不准"的保守答案。
    try:
        if page.query_selector(selectors.css(selectors.LOGIN_INDICATOR)):  # type: ignore[attr-defined]
            return True
    except Exception:
        pass
    return False


def login_diagnostics(page: object) -> str:
    """把判断登录态用到的信号全打出来——用来**校准** `is_logged_in`。

    和 selectors.py 的离线校准是同一个套路：看不到真实情况就别猜，
    把原始信号摆出来，照着事实改。这里尤其要紧，因为登录判断错了会让
    整个采集卡死在"等你登录"，而不是报一个显眼的错。
    """
    lines = ["登录诊断："]

    try:
        cookies = _cookie_jar(page)
        lines.append(f"  cookie 共 {len(cookies)} 个（zhihu.com 域下）")
        for name in sorted(cookies):
            mark = "← 认作登录态" if name in _auth_names() else ""
            value = cookies[name]
            shown = f"{value[:8]}…({len(value)} 字符)" if value else "(空)"
            lines.append(f"    {name} = {shown} {mark}".rstrip())
        if not cookies:
            lines.append("    ⚠️ 一个 cookie 都没有——profile 可能没存住")
    except Exception as exc:
        lines.append(f"  cookie 读取失败：{exc}")

    try:
        url = page.url  # type: ignore[attr-defined]
        on_signin = selectors.SIGNIN_PATH in url
        lines.append(f"  当前 URL：{url}{'  ← 停在登录页' if on_signin else ''}")
    except Exception:
        pass

    try:
        matched = len(page.query_selector_all(selectors.css(selectors.LOGIN_INDICATOR)))  # type: ignore[attr-defined]
        lines.append(f"  LOGIN_INDICATOR 命中 {matched} 个（未校准，0 是正常的）")
    except Exception:
        pass

    lines.append(f"  判定结果：{'已登录' if is_logged_in(page) else '未登录'}")
    return "\n".join(lines)


def _auth_names() -> tuple[str, ...]:
    return tuple(
        name.strip()
        for name in selectors.css(selectors.AUTH_COOKIE_NAMES).split(",")
        if name.strip()
    )


def _cookie_jar(page: object) -> dict[str, str]:
    """当前浏览器在 zhihu.com 域下的所有 cookie：`{名字: 值}`。"""
    context = page.context  # type: ignore[attr-defined]
    cookies = context.cookies([HOME_URL, SIGNIN_URL])
    return {str(c.get("name", "")): str(c.get("value", "")) for c in cookies}


def _auth_cookies(page: object) -> dict[str, str]:
    """登录 cookie 里**有值**的那些。空值不算——知乎会发一个同名空 cookie 占位。"""
    try:
        jar = _cookie_jar(page)
    except Exception:
        return {}
    return {name: jar[name] for name in _auth_names() if jar.get(name)}


def captcha_present(page: object) -> bool:
    """是否撞上了验证码。三个层面一起判——知乎的验证方式不止一种。"""
    try:
        url = page.url  # type: ignore[attr-defined]
        for pattern in _split(selectors.CAPTCHA_URL_PATTERN):
            if pattern in url:
                return True
        if page.query_selector(selectors.CAPTCHA_CONTAINER):  # type: ignore[attr-defined]
            return True
        for text in _texts(selectors.CAPTCHA_TEXT, selectors.RISK_INTERSTITIAL_TEXTS):
            if page.query_selector(f"text={text}"):  # type: ignore[attr-defined]
                return True
    except Exception:
        return False
    return False


def _split(css_group: str) -> list[str]:
    """把 CSS 分组拆成候选列表。"""
    return [part.strip() for part in css_group.split(",") if part.strip()]


def _looks_like_profile_lock(exc: Exception) -> bool:
    """判断启动失败是不是因为 profile 被占用。"""
    message = str(exc).lower()
    return any(
        marker in message
        for marker in ("processsingleton", "singletonlock", "already in use", "user data dir")
    )


def _texts(*groups: str | tuple[str, ...]) -> list[str]:
    """把逗号分隔的选择器串和文案元组合并成一个去重的列表。"""
    out: list[str] = []
    for group in groups:
        parts = group if isinstance(group, tuple) else group.split(",")
        for part in parts:
            text = part.strip()
            if text and text not in out:
                out.append(text)
    return out


def _looks_like_missing_channel(exc: Exception, channel: str | None) -> bool:
    """判断启动失败是不是因为找不到指定的浏览器（没装 Chrome）。"""
    if not channel:
        return False
    message = str(exc).lower()
    return any(
        marker in message
        for marker in ("channel", "executable doesn't exist", "not found", "no such file")
    )


def _alert(banner: str) -> None:
    """需要人工介入时的提示：响铃 + 醒目横幅。

    终端响铃可能被关掉，所以 macOS 上额外放一声系统音——
    需要人工介入的场景，人往往是离开了电脑的。
    """
    print("\a", end="", flush=True)
    print("\n" + "=" * 68, flush=True)
    for line in banner.splitlines():
        print(f"⚠️  {line}" if line is banner.splitlines()[0] else f"   {line}", flush=True)
    print("=" * 68 + "\n", flush=True)
    sys.stdout.flush()

    try:
        subprocess.Popen(
            ["afplay", "/System/Library/Sounds/Glass.aiff"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass  # 非 macOS 或没这个文件，响铃已经尽力了
