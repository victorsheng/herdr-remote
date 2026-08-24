# 群名状态色环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把飞书群名的固定前缀 `herdr · ` 换成随 agent 状态实时变化的彩色符号（🔴🟡🟢⚪️），让会话列表兼作状态盘。

**Architecture:** 群名从静态标识符变为动态展示层。三处配套改动：`/spaces` 的群复用改走绑定关系（不再靠群名匹配）、observer 的项目群判定改按符号开头、新增改名节流器（防抖 + blocked 例外 + 最小间隔 + 幂等），因为实测每次改群名都会在群里刷一条 system 消息。

**Tech Stack:** Python 3.10+，标准库 unittest，`uv run` 跑脚本（PEP 723 内联依赖）。测试不依赖 lark-oapi SDK。

**Spec:** `docs/superpowers/specs/2026-08-24-lark-chat-status-glyph-design.md`

---

## 背景：给没有本仓上下文的工程师

**怎么跑测试**（没有 pytest，用标准库 unittest；Makefile 里没有测试目标）：

```bash
cd /Users/victor/code-github/herdr-remote
uv run tests/test_lark.py                    # 536 个测试，约 0.25s
uv run tests/test_lark_observer.py           # 52 个测试，约 0.01s
```

跑单个测试类或方法：

```bash
uv run tests/test_lark.py ChatTitleDisambiguationTests
uv run tests/test_lark.py ChatTitleDisambiguationTests.test_plain_title_without_marker
```

**两个模块的关系**：`relay/herdr_lark.py`（飞书机器人）和
`relay/herdr_lark_observer.py`（质检 observer）是**两个独立进程**，
observer **不 import** herdr_lark，靠注释约定手工同步常量。
这是刻意的隔离，本计划不改变它——符号表会在两处各留一份。

**测试里怎么引用模块**：
- `tests/test_lark.py` 里模块名是 `lk`（`sys.path` 插了 `relay/`）
- `tests/test_lark_observer.py` 里模块名是 `ob`（用 `importlib.util` 按路径加载）

**状态取值**：`blocked` / `working` / `done` / `idle` / `unknown`，
见 `relay/herdr_lark.py:59` 的 `STATUS_ORDER`。

---

## 文件结构

| 文件 | 职责 | 本计划的改动 |
|------|------|------------|
| `relay/herdr_lark.py` | 飞书机器人主体 | 加 `status_glyph` / `is_project_chat` / `ChatRenamer`，改 `chat_title_for`、`find_existing_chat`，删 `CHAT_TITLE_PREFIX` |
| `relay/herdr_lark_observer.py` | 质检 observer（独立进程） | `_chat_covers` 改按符号判定，提为模块级纯函数 |
| `tests/test_lark.py` | 机器人的纯函数与事件测试 | 新增 4 组测试，改 2 处现有断言 |
| `tests/test_lark_observer.py` | observer 测试 | 新增 1 组测试（`_chat_covers` 目前无测试） |

`herdr_lark.py` 已有 3629 行，偏大。但本计划新增的量小（一个类 + 两个纯函数），
且都与既有的群名/状态逻辑同属一处关注点，按仓内既有组织方式就近放置，
不做文件拆分——拆分是独立的重构，不该混在这个改动里。

---

## Task 1: 状态符号映射

**Files:**
- Modify: `relay/herdr_lark.py`（在 `_STATUS_ICONS` 之后，约 1140 行）
- Test: `tests/test_lark.py`

- [ ] **Step 1: 写失败的测试**

加在 `tests/test_lark.py` 的 `ChatTitleDisambiguationTests` 类**之前**
（约 2337 行，`class ChatTitleDisambiguationTests` 那一行上方）：

```python
class StatusGlyphTests(unittest.TestCase):
    """群名用的彩色状态符号。

    卡片里用的是黑白符号（_STATUS_ICONS: ⏸ ▶ ✅ ○），群名要彩色——
    会话列表里灰扑扑的符号扫一眼分不出轻重。两套刻意不同。
    """

    def test_four_states_map_to_colored_glyphs(self):
        self.assertEqual(lk.status_glyph("blocked"), "🔴")
        self.assertEqual(lk.status_glyph("working"), "🟡")
        self.assertEqual(lk.status_glyph("done"), "🟢")
        self.assertEqual(lk.status_glyph("idle"), "⚪️")

    def test_unknown_falls_back_to_idle_glyph(self):
        """unknown 与 idle 同色——都是「没事干」。"""
        self.assertEqual(lk.status_glyph("unknown"), "⚪️")

    def test_unrecognized_status_falls_back(self):
        """没见过的状态不能抛异常，群名比状态准确性重要。"""
        self.assertEqual(lk.status_glyph("wat"), "⚪️")
        self.assertEqual(lk.status_glyph(""), "⚪️")

    def test_glyphs_are_all_distinct_except_idle_unknown(self):
        """四个符号必须互不相同，否则区分不出状态。"""
        glyphs = {lk.status_glyph(s)
                  for s in ("blocked", "working", "done", "idle")}
        self.assertEqual(len(glyphs), 4)

    def test_every_known_status_has_a_glyph(self):
        """STATUS_ORDER 里的状态都得有符号，加状态时别漏。"""
        for status in lk.STATUS_ORDER:
            self.assertNotEqual(lk.status_glyph(status), "",
                                f"{status} 没有符号")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run tests/test_lark.py StatusGlyphTests`
Expected: FAIL — `AttributeError: module 'herdr_lark' has no attribute 'status_glyph'`

- [ ] **Step 3: 写最小实现**

在 `relay/herdr_lark.py` 的 `_STATUS_ICONS` 定义之后（`_CARD_BODY_LIMIT` 那行之前）插入：

```python
# 群名用的彩色符号。卡片里的 _STATUS_ICONS 是黑白的（正文里更克制），
# 群名要彩色：会话列表里灰扑扑的符号扫一眼分不出轻重。
# 颜色语义与 _STATUS_COLORS 一致（red/orange/green/grey）。
_STATUS_GLYPHS = {
    "blocked": "🔴",
    "working": "🟡",
    "done": "🟢",
    "idle": "⚪️",
    "unknown": "⚪️",
}
_IDLE_GLYPH = "⚪️"


def status_glyph(status: str) -> str:
    """状态 → 群名里的彩色符号。没见过的状态回落成「闲着」。"""
    return _STATUS_GLYPHS.get(status or "", _IDLE_GLYPH)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run tests/test_lark.py StatusGlyphTests`
Expected: `Ran 5 tests` … `OK`

- [ ] **Step 5: 跑全量确认无回归**

Run: `uv run tests/test_lark.py`
Expected: `Ran 541 tests` … `OK (skipped=1)`

- [ ] **Step 6: 提交**

```bash
git add relay/herdr_lark.py tests/test_lark.py
git commit -m "feat(lark): 群名的彩色状态符号映射

卡片里的黑白符号在会话列表里分不出轻重，群名要用彩色。
颜色语义沿用 _STATUS_COLORS，未知状态回落成闲着。"
```

---

## Task 2: `chat_title_for` 带上状态符号

**Files:**
- Modify: `relay/herdr_lark.py:3410-3426`（`CHAT_TITLE_PREFIX` 与 `chat_title_for`）
- Modify: `relay/herdr_lark.py:2838`（`/unbind` 的群名重置）
- Modify: `tests/test_lark.py:1147`、`tests/test_lark.py:2342`（断言了 `CHAT_TITLE_PREFIX`）
- Test: `tests/test_lark.py`

- [ ] **Step 1: 写失败的测试**

加在 Task 1 新增的 `StatusGlyphTests` 类之后：

```python
class ChatTitleStatusTests(unittest.TestCase):
    """群名带状态符号，不再带 herdr · 前缀。

    前缀每个群都一样，在会话列表这种窄地方纯属浪费；换成符号后
    同样的宽度能表达「该先处理谁」。
    """

    def test_status_glyph_leads_the_title(self):
        title = lk.chat_title_for("datapilot", status="blocked")
        self.assertTrue(title.startswith("🔴 "), title)
        self.assertIn("datapilot", title)

    def test_no_herdr_prefix_anymore(self):
        """前缀省下来的宽度是这次改动的全部意义。"""
        self.assertNotIn("herdr", lk.chat_title_for("x", status="working"))

    def test_status_omitted_means_no_glyph(self):
        """还不知道状态的调用点不该被塞一个假符号。"""
        self.assertEqual(lk.chat_title_for("tailcale"), "tailcale")

    def test_marker_sits_between_glyph_and_project(self):
        """标记仍在项目名前面——会话列表尾部会被截掉。"""
        title = lk.chat_title_for("same", " [w22]", status="done")
        self.assertTrue(title.startswith("🟢 "), title)
        self.assertLess(title.index("w22"), title.index("same"))

    def test_long_project_truncated_but_glyph_survives(self):
        """项目名太长时宁可截项目名，也要保住符号和标记。"""
        title = lk.chat_title_for("p" * 200, " [w22]", status="blocked")
        self.assertLessEqual(len(title), 60)
        self.assertTrue(title.startswith("🔴 "), title)
        self.assertIn("w22", title)

    def test_empty_project_still_produces_a_title(self):
        title = lk.chat_title_for("", status="idle")
        self.assertTrue(title.startswith("⚪️ "), title)
        self.assertIn("?", title)

    def test_same_project_different_status_differs(self):
        """状态变了群名就得变，否则符号不起作用。"""
        a = lk.chat_title_for("x", status="working")
        b = lk.chat_title_for("x", status="done")
        self.assertNotEqual(a, b)


class UnboundChatNameTests(unittest.TestCase):
    """/unbind 之后群名重置成什么。"""

    def test_unbound_name_has_no_glyph(self):
        """没绑 agent 就没有状态可显示。"""
        self.assertEqual(lk.UNBOUND_CHAT_NAME, "herdr")

    def test_old_prefix_constant_is_gone(self):
        """CHAT_TITLE_PREFIX 的两个用途都被接管了，别留着误用。"""
        self.assertFalse(hasattr(lk, "CHAT_TITLE_PREFIX"))
```

- [ ] **Step 2: 改掉两处现有断言**

这两处断言了即将删除的 `CHAT_TITLE_PREFIX`，不改会失败。

`tests/test_lark.py:1145-1147`，把：

```python
    def test_chat_title_has_stable_prefix(self):
        """统一前缀，一眼看出哪些群是 herdr 的。"""
        self.assertTrue(lk.chat_title_for("x").startswith(lk.CHAT_TITLE_PREFIX))
```

替换为：

```python
    def test_chat_title_leads_with_status_glyph(self):
        """项目群靠状态符号识别，不再靠统一前缀。"""
        self.assertTrue(
            lk.chat_title_for("x", status="working").startswith("🟡 "))
```

`tests/test_lark.py:2341-2342`，把：

```python
    def test_plain_title_without_marker(self):
        self.assertEqual(lk.chat_title_for("tailcale"), lk.CHAT_TITLE_PREFIX + "tailcale")
```

替换为：

```python
    def test_plain_title_without_marker(self):
        self.assertEqual(lk.chat_title_for("tailcale", status="idle"),
                         "⚪️ tailcale")
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run tests/test_lark.py ChatTitleStatusTests UnboundChatNameTests`
Expected: FAIL — `TypeError: chat_title_for() got an unexpected keyword argument 'status'`

- [ ] **Step 4: 写实现**

在 `relay/herdr_lark.py`，把 3408-3426 行这一段：

```python
CHAT_TITLE_PREFIX = "herdr · "
_CHAT_TITLE_LIMIT = 60


def chat_title_for(project: str, marker: str = "") -> str:
    """把群名改成「herdr · [标记] <项目>」，一眼看出这个群管的是谁。

    重名 agent 必须带上标记：两个群都叫「herdr · yqg-dw-datapilot」的话，
    会话列表里根本切不对。

    标记放在项目名**前面**：会话列表宽度有限，尾部会被截掉，放后面等于
    看不见。项目名太长时宁可截项目名，也要保住标记。
    """
    marker = (marker or "").strip()
    head = f"{marker} " if marker else ""
    room = _CHAT_TITLE_LIMIT - len(CHAT_TITLE_PREFIX) - len(head)
    return CHAT_TITLE_PREFIX + head + (project or "?")[:max(1, room)]
```

替换为：

```python
# 没绑 agent 的群叫这个：没有状态可显示，也不该顶着别人的符号。
UNBOUND_CHAT_NAME = "herdr"
_CHAT_TITLE_LIMIT = 60


def chat_title_for(project: str, marker: str = "", status: str = "") -> str:
    """把群名改成「<状态符号> [标记] <项目>」，一眼看出这个群管的是谁、忙不忙。

    曾经用统一的「herdr · 」前缀，但每个群都一样，在会话列表这种窄地方
    纯属浪费。换成状态符号：同样的宽度，还能看出该先处理谁。

    重名 agent 必须带上标记：两个群都叫「🟡 yqg-dw-datapilot」的话，
    会话列表里根本切不对。

    标记放在项目名**前面**：会话列表宽度有限，尾部会被截掉，放后面等于
    看不见。项目名太长时宁可截项目名，也要保住符号和标记。

    status 为空表示调用方还不知道状态，此时不带符号——塞一个假符号
    比不带更糟。
    """
    marker = (marker or "").strip()
    head = f"{marker} " if marker else ""
    glyph = f"{status_glyph(status)} " if status else ""
    room = _CHAT_TITLE_LIMIT - len(glyph) - len(head)
    return glyph + head + (project or "?")[:max(1, room)]
```

再改 `relay/herdr_lark.py:2838`（`/unbind` 的群名重置），把：

```python
                self.api.set_chat_name(chat_id, CHAT_TITLE_PREFIX.rstrip(" ·· "))
```

替换为：

```python
                self.api.set_chat_name(chat_id, UNBOUND_CHAT_NAME)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run tests/test_lark.py ChatTitleStatusTests UnboundChatNameTests`
Expected: `Ran 9 tests` … `OK`

- [ ] **Step 6: 跑全量，抓出所有还在用旧常量的地方**

Run: `uv run tests/test_lark.py`
Expected: `OK`。若报 `AttributeError: ... CHAT_TITLE_PREFIX`，
用 `grep -rn CHAT_TITLE_PREFIX relay/ tests/` 找出残留并按上面的方式改掉
（observer 里的那处属于 Task 4，此刻只需确认它是注释而非代码）。

- [ ] **Step 7: 提交**

```bash
git add relay/herdr_lark.py tests/test_lark.py
git commit -m "feat(lark): 群名前缀换成状态符号

herdr · 每个群都一样，把会话列表里真正要看的项目名挤掉了。
换成状态符号：同样宽度还能看出该先处理谁。

status 缺省时不带符号——还不知道状态的调用点塞个假符号比不带更糟。
/unbind 的群名重置改用 UNBOUND_CHAT_NAME，不再靠 rstrip 拼字面量。"
```

---

## Task 3: 项目群判定 `is_project_chat`

**Files:**
- Modify: `relay/herdr_lark.py`（紧跟 `chat_title_for` 之后）
- Test: `tests/test_lark.py`

- [ ] **Step 1: 写失败的测试**

加在 Task 2 新增的 `UnboundChatNameTests` 之后：

```python
class ProjectChatDetectionTests(unittest.TestCase):
    """群名是不是 herdr 项目群。

    observer 靠这个判定过滤对账范围（它自己有一份副本）。原来靠
    startswith("herdr")，前缀删掉后必须换判据，否则所有项目群都被
    判成无关群、对账全废。
    """

    def test_recognizes_all_four_status_glyphs(self):
        for status in ("blocked", "working", "done", "idle"):
            name = lk.chat_title_for("proj", status=status)
            self.assertTrue(lk.is_project_chat(name), name)

    def test_recognizes_unbound_chat(self):
        """/unbind 之后的空闲群仍属项目群。"""
        self.assertTrue(lk.is_project_chat(lk.UNBOUND_CHAT_NAME))

    def test_recognizes_unbound_with_trailing_text(self):
        """历史上的「herdr · xxx」旧群名也得认，改造期间新旧并存。"""
        self.assertTrue(lk.is_project_chat("herdr · datapilot"))

    def test_rejects_unrelated_chat(self):
        """闲聊群不参与对账。"""
        self.assertFalse(lk.is_project_chat("盛大宝123"))
        self.assertFalse(lk.is_project_chat("项目讨论"))

    def test_rejects_empty_name(self):
        self.assertFalse(lk.is_project_chat(""))
        self.assertFalse(lk.is_project_chat(None))

    def test_rejects_glyph_in_the_middle(self):
        """符号必须在开头，正文里出现不算。"""
        self.assertFalse(lk.is_project_chat("讨论 🔴 的问题"))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run tests/test_lark.py ProjectChatDetectionTests`
Expected: FAIL — `AttributeError: module 'herdr_lark' has no attribute 'is_project_chat'`

- [ ] **Step 3: 写实现**

在 `relay/herdr_lark.py` 的 `chat_title_for` 之后插入：

```python
def is_project_chat(name: str) -> bool:
    """这个群名是不是 herdr 的项目群。

    observer 靠这个过滤对账范围。原来的判据是 startswith("herdr")，
    前缀删掉后改看状态符号；「herdr」开头仍然认，一是覆盖 /unbind 之后
    的空闲群，二是改造期间新旧群名并存。
    """
    name = (name or "").lstrip()
    if not name:
        return False
    if name.startswith(UNBOUND_CHAT_NAME):
        return True
    return any(name.startswith(g) for g in set(_STATUS_GLYPHS.values()))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run tests/test_lark.py ProjectChatDetectionTests`
Expected: `Ran 6 tests` … `OK`

- [ ] **Step 5: 跑全量**

Run: `uv run tests/test_lark.py`
Expected: `OK`

- [ ] **Step 6: 提交**

```bash
git add relay/herdr_lark.py tests/test_lark.py
git commit -m "feat(lark): 按状态符号判定项目群

前缀删掉后 startswith(\"herdr\") 就废了。改看状态符号开头，
herdr 开头仍然认——覆盖 /unbind 后的空闲群和改造期的旧群名。"
```

---

## Task 4: observer 跟上新判据

**Files:**
- Modify: `relay/herdr_lark_observer.py:579-585`（`_chat_covers`）
- Test: `tests/test_lark_observer.py`

observer 是独立进程、不 import herdr_lark（刻意的隔离），
所以符号表在这里复制一份。同时把 `_chat_covers` 从实例方法提为模块级
纯函数——逻辑只看群名，纯函数好测，与 observer 里 `card_is_output_only`
等既有纯函数的风格一致。

- [ ] **Step 1: 写失败的测试**

加在 `tests/test_lark_observer.py` 末尾（`if __name__` 之前）：

```python
class ProjectChatDetectionTests(unittest.TestCase):
    """哪些群参与对账。

    群名前缀改成状态符号后，原来的 startswith("herdr") 会把所有项目群
    判成无关群——漏发检测直接失效，而且是静默失效（不报错，只是什么都
    不检查了），所以必须有测试兜着。
    """

    def test_recognizes_status_glyph_chats(self):
        for name in ("🔴 datapilot", "🟡 datapilot",
                     "🟢 datapilot", "⚪️ datapilot"):
            self.assertTrue(ob.is_project_chat(name), name)

    def test_recognizes_unbound_and_legacy_names(self):
        self.assertTrue(ob.is_project_chat("herdr"))
        self.assertTrue(ob.is_project_chat("herdr · datapilot"))

    def test_rejects_unrelated_chat(self):
        """有人把机器人拉进闲聊群，那儿的消息不该参与对账。"""
        self.assertFalse(ob.is_project_chat("盛大宝123"))
        self.assertFalse(ob.is_project_chat(""))

    def test_glyph_table_matches_herdr_lark(self):
        """符号表两处各一份，必须一致——不一致会静默漏掉整类群。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "lk_for_glyph_check",
            os.path.join(os.path.dirname(__file__), "..",
                         "relay", "herdr_lark.py"))
        lk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lk)
        self.assertEqual(set(ob._PROJECT_CHAT_GLYPHS),
                         set(lk._STATUS_GLYPHS.values()))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run tests/test_lark_observer.py ProjectChatDetectionTests`
Expected: FAIL — `AttributeError: module 'ob' has no attribute 'is_project_chat'`

- [ ] **Step 3: 写实现**

在 `relay/herdr_lark_observer.py` 的 `_PANE_CARD_LABELS` 那一段附近
（模块级常量区，约 183 行之后）插入：

```python
# herdr_lark.py 的 _STATUS_GLYPHS 的值 + UNBOUND_CHAT_NAME。
# observer 是独立进程、不 import herdr_lark，所以这里留一份副本。
# 与 herdr_lark.py 保持同步：那边改符号就往这里改。
_PROJECT_CHAT_GLYPHS = ("🔴", "🟡", "🟢", "⚪️")
_UNBOUND_CHAT_NAME = "herdr"


def is_project_chat(name: str) -> bool:
    """这个群是不是 herdr 的项目群。

    群名以状态符号开头（herdr_lark 的 chat_title_for 生成），或者是
    没绑 agent 的「herdr」。别的群（比如有人把机器人拉进了闲聊群）
    里的消息不参与对账。

    判错的后果是静默的：把项目群判成无关群，漏发检测什么都不查了
    但也不报错。所以 tests 里有一条断言符号表与 herdr_lark 一致。
    """
    name = (name or "").lstrip()
    if not name:
        return False
    if name.startswith(_UNBOUND_CHAT_NAME):
        return True
    return any(name.startswith(g) for g in _PROJECT_CHAT_GLYPHS)
```

再把 579-585 行的实例方法：

```python
    def _chat_covers(self, chat_name: str, chat_id: str) -> bool:
        """这个群是不是 herdr 的项目群。

        群名前缀是 herdr_lark.py 里的 CHAT_TITLE_PREFIX。别的群（比如
        有人把机器人拉进了闲聊群）里的消息不参与对账。
        """
        return (chat_name or "").startswith("herdr")
```

替换为：

```python
    def _chat_covers(self, chat_name: str, chat_id: str) -> bool:
        """这个群是不是 herdr 的项目群。判据见模块级 is_project_chat。"""
        return is_project_chat(chat_name)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run tests/test_lark_observer.py ProjectChatDetectionTests`
Expected: `Ran 4 tests` … `OK`

- [ ] **Step 5: 跑两边全量**

Run: `uv run tests/test_lark_observer.py && uv run tests/test_lark.py`
Expected: 两个都 `OK`

- [ ] **Step 6: 提交**

```bash
git add relay/herdr_lark_observer.py tests/test_lark_observer.py
git commit -m "fix(observer): 项目群判定跟上群名新格式

startswith(\"herdr\") 在前缀删掉后会把所有项目群判成无关群，
漏发检测静默失效——不报错，只是什么都不查了。

改按状态符号判定。observer 不 import herdr_lark（进程隔离），
符号表复制一份，加一条断言两边一致的测试兜着。
顺手把 _chat_covers 提成模块级纯函数，之前它没有测试。"
```

---

## Task 5: 改名节流器 — 防抖与幂等

**Files:**
- Modify: `relay/herdr_lark.py`（`is_project_chat` 之后）
- Test: `tests/test_lark.py`

实测过：每次改群名都会在群里刷一条 system 消息
（`{from_user} updated the group name from ...`）。relay 每 2 秒推一次状态
（`relay/herdr_relay.py:47`），不节流会刷屏。

本任务只做防抖 + 幂等；`blocked` 例外和最小间隔在 Task 6。

- [ ] **Step 1: 写失败的测试**

加在 Task 3 新增的 `ProjectChatDetectionTests` 之后：

```python
class ChatRenamerDebounceTests(unittest.TestCase):
    """改名节流：状态稳住了才改，别刷屏。

    实测每次改群名都在群里留一条「XXX 修改群名为…」的系统消息，
    而 relay 每 2 秒推一次状态。不节流的话 working⇄idle 抖几下
    就刷一屏系统消息，比原来浪费群名宽度的问题严重得多。

    decide() 传入 now，不读时钟——测试不需要 sleep 或 mock。
    """

    def test_first_sight_waits_for_debounce(self):
        """刚看到一个状态先不动，可能只是抖动。"""
        r = lk.ChatRenamer()
        self.assertIsNone(r.decide("oc_1", "🟡 x", "working", now=0))

    def test_stable_past_debounce_renames(self):
        r = lk.ChatRenamer()
        r.decide("oc_1", "🟡 x", "working", now=0)
        self.assertEqual(
            r.decide("oc_1", "🟡 x", "working", now=31), "🟡 x")

    def test_just_under_debounce_holds(self):
        r = lk.ChatRenamer()
        r.decide("oc_1", "🟡 x", "working", now=0)
        self.assertIsNone(r.decide("oc_1", "🟡 x", "working", now=29))

    def test_flapping_never_renames(self):
        """working→idle→working 在防抖窗口内反复，全程不改名。"""
        r = lk.ChatRenamer()
        self.assertIsNone(r.decide("oc_1", "🟡 x", "working", now=0))
        self.assertIsNone(r.decide("oc_1", "⚪️ x", "idle", now=10))
        self.assertIsNone(r.decide("oc_1", "🟡 x", "working", now=20))
        self.assertIsNone(r.decide("oc_1", "⚪️ x", "idle", now=25))

    def test_flapping_then_settling_renames(self):
        """抖完了稳住 30s，还是要改的。"""
        r = lk.ChatRenamer()
        r.decide("oc_1", "🟡 x", "working", now=0)
        r.decide("oc_1", "⚪️ x", "idle", now=10)
        self.assertEqual(
            r.decide("oc_1", "⚪️ x", "idle", now=45), "⚪️ x")

    def test_idempotent_when_name_already_correct(self):
        """群名已经对了就不要再调 API——那会白刷一条系统消息。"""
        r = lk.ChatRenamer(known_names={"oc_1": "🟡 x"})
        r.decide("oc_1", "🟡 x", "working", now=0)
        self.assertIsNone(r.decide("oc_1", "🟡 x", "working", now=100))

    def test_renaming_updates_known_name(self):
        """改过一次之后，同样的目标名不该再改第二次。"""
        r = lk.ChatRenamer()
        r.decide("oc_1", "🟡 x", "working", now=0)
        self.assertEqual(r.decide("oc_1", "🟡 x", "working", now=31), "🟡 x")
        self.assertIsNone(r.decide("oc_1", "🟡 x", "working", now=200))

    def test_chats_are_independent(self):
        """一个群的防抖不该影响另一个群。"""
        r = lk.ChatRenamer()
        r.decide("oc_1", "🟡 x", "working", now=0)
        self.assertIsNone(r.decide("oc_2", "🟡 y", "working", now=31))
        self.assertEqual(r.decide("oc_1", "🟡 x", "working", now=31), "🟡 x")

    def test_baseline_from_startup_avoids_pointless_rename(self):
        """启动时拉一次群名当基线，省掉每次重启的整轮无谓改名。

        开发期重启频繁（日志里 47 次 Bot ready），一 agent 一群之后
        群数会到十几个，不做基线就是每次重启刷十几条系统消息。
        """
        r = lk.ChatRenamer(known_names={"oc_1": "🟢 x"})
        r.decide("oc_1", "🟢 x", "done", now=0)
        self.assertIsNone(r.decide("oc_1", "🟢 x", "done", now=999))

    def test_empty_baseline_degrades_gracefully(self):
        """拉群名失败就空基线：每群多改一次名，不阻断启动。"""
        r = lk.ChatRenamer(known_names={})
        r.decide("oc_1", "🟢 x", "done", now=0)
        self.assertEqual(r.decide("oc_1", "🟢 x", "done", now=31), "🟢 x")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run tests/test_lark.py ChatRenamerDebounceTests`
Expected: FAIL — `AttributeError: module 'herdr_lark' has no attribute 'ChatRenamer'`

- [ ] **Step 3: 写实现**

在 `relay/herdr_lark.py` 的 `is_project_chat` 之后插入：

```python
# 改名节流。实测每次改群名都会在群里留一条「XXX 修改群名为…」的系统
# 消息，而 relay 每 2 秒推一次状态（herdr_relay.POLL_INTERVAL）——
# 不节流的话状态抖几下就刷一屏，比原来浪费群名宽度的问题严重得多。
RENAME_DEBOUNCE_S = 30


class ChatRenamer:
    """决定「现在该不该改这个群的名字」。

    决策与执行分离：decide() 是纯函数式的（now 从外面传进来，不读时钟），
    时间和 IO 都在外层，测试不需要 mock 时钟或网络。

    状态纯内存不落盘：落盘会多一份可能与飞书实际群名不一致的副本。
    启动时用 chat_inventory() 拉一次群名当基线即可。
    """

    def __init__(self, known_names: dict[str, str] | None = None):
        # 飞书上当前的群名。启动时拉一次当基线，之后跟着改名更新。
        self._known: dict[str, str] = dict(known_names or {})
        # chat_id -> (待定的目标名, 该目标名第一次出现的时刻)
        self._pending: dict[str, tuple[str, float]] = {}

    def decide(self, chat_id: str, target_name: str,
               status: str, now: float) -> str | None:
        """该改成什么名字，或 None 表示按住不动。

        不改的三种情况：群名已经对了（幂等）、目标名刚变（防抖未满）、
        或者上次改名太近（最小间隔，见子类逻辑）。
        """
        chat_id = str(chat_id)
        if self._known.get(chat_id) == target_name:
            self._pending.pop(chat_id, None)
            return None

        pending = self._pending.get(chat_id)
        if pending is None or pending[0] != target_name:
            # 目标名变了，防抖重新计时
            self._pending[chat_id] = (target_name, now)
            return None

        if now - pending[1] < RENAME_DEBOUNCE_S:
            return None

        return self._commit(chat_id, target_name, now)

    def _commit(self, chat_id: str, target_name: str,
                now: float) -> str:
        """记下「已经改成这个名字了」，返回该改的名字。"""
        self._known[chat_id] = target_name
        self._pending.pop(chat_id, None)
        return target_name

    def forget(self, chat_id: str) -> None:
        """群解散或解绑后清掉它的状态，别占着内存。"""
        chat_id = str(chat_id)
        self._known.pop(chat_id, None)
        self._pending.pop(chat_id, None)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run tests/test_lark.py ChatRenamerDebounceTests`
Expected: `Ran 10 tests` … `OK`

- [ ] **Step 5: 跑全量**

Run: `uv run tests/test_lark.py`
Expected: `OK`

- [ ] **Step 6: 提交**

```bash
git add relay/herdr_lark.py tests/test_lark.py
git commit -m "feat(lark): 改名节流器的防抖与幂等

实测每次改群名都在群里留一条系统消息，而 relay 每 2 秒推一次状态。
不节流的话 working⇄idle 抖几下就刷一屏。

decide() 把 now 从外面传进来，不读时钟——测试不用 sleep 也不用
mock 时钟。状态纯内存，启动时拉一次群名当基线。"
```

---

## Task 6: 节流器的 blocked 例外与最小间隔

**Files:**
- Modify: `relay/herdr_lark.py`（Task 5 建的 `ChatRenamer`）
- Test: `tests/test_lark.py`

- [ ] **Step 1: 写失败的测试**

加在 `ChatRenamerDebounceTests` 之后：

```python
class ChatRenamerBlockedAndIntervalTests(unittest.TestCase):
    """blocked 立即改名，其余守最小间隔。

    blocked 是唯一「要人立刻动手」的状态，等 30s 防抖没有意义——
    等的这半分钟正是最该看见它的时候。
    """

    def test_blocked_renames_immediately(self):
        r = lk.ChatRenamer()
        self.assertEqual(
            r.decide("oc_1", "🔴 x", "blocked", now=0), "🔴 x")

    def test_blocked_beats_min_interval(self):
        """刚改过名也照样立即改——例外优先于最小间隔。"""
        r = lk.ChatRenamer()
        r.decide("oc_1", "🟡 x", "working", now=0)
        self.assertEqual(r.decide("oc_1", "🟡 x", "working", now=31), "🟡 x")
        self.assertEqual(
            r.decide("oc_1", "🔴 x", "blocked", now=32), "🔴 x")

    def test_blocked_still_idempotent(self):
        """已经是 blocked 名字了就别再改。"""
        r = lk.ChatRenamer(known_names={"oc_1": "🔴 x"})
        self.assertIsNone(r.decide("oc_1", "🔴 x", "blocked", now=0))

    def test_leaving_blocked_uses_debounce(self):
        """离开 blocked 走正常防抖，不必抢时间。"""
        r = lk.ChatRenamer()
        r.decide("oc_1", "🔴 x", "blocked", now=0)
        self.assertIsNone(r.decide("oc_1", "🟡 x", "working", now=1))
        self.assertIsNone(r.decide("oc_1", "🟡 x", "working", now=20))

    def test_min_interval_holds_non_blocked(self):
        """两次改名间隔不足 60s，非 blocked 的一律按住。"""
        r = lk.ChatRenamer()
        r.decide("oc_1", "🟡 x", "working", now=0)
        self.assertEqual(r.decide("oc_1", "🟡 x", "working", now=31), "🟡 x")
        # 目标名变了、防抖也满了，但距上次改名只有 40s
        r.decide("oc_1", "🟢 x", "done", now=40)
        self.assertIsNone(r.decide("oc_1", "🟢 x", "done", now=71))

    def test_rename_allowed_after_min_interval(self):
        r = lk.ChatRenamer()
        r.decide("oc_1", "🟡 x", "working", now=0)
        self.assertEqual(r.decide("oc_1", "🟡 x", "working", now=31), "🟡 x")
        r.decide("oc_1", "🟢 x", "done", now=40)
        self.assertEqual(r.decide("oc_1", "🟢 x", "done", now=95), "🟢 x")

    def test_min_interval_is_per_chat(self):
        r = lk.ChatRenamer()
        r.decide("oc_1", "🟡 x", "working", now=0)
        r.decide("oc_1", "🟡 x", "working", now=31)
        r.decide("oc_2", "🟡 y", "working", now=32)
        self.assertEqual(r.decide("oc_2", "🟡 y", "working", now=63), "🟡 y")

    def test_forget_clears_interval_state(self):
        """群解散后再出现的同 id，不该被旧的间隔按住。"""
        r = lk.ChatRenamer()
        r.decide("oc_1", "🔴 x", "blocked", now=0)
        r.forget("oc_1")
        self.assertEqual(
            r.decide("oc_1", "🔴 x", "blocked", now=1), "🔴 x")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run tests/test_lark.py ChatRenamerBlockedAndIntervalTests`
Expected: FAIL — `test_blocked_renames_immediately` 得到 `None`，期望 `🔴 x`

- [ ] **Step 3: 写实现**

在 `relay/herdr_lark.py` 把 `RENAME_DEBOUNCE_S = 30` 那行改成：

```python
RENAME_DEBOUNCE_S = 30
# 同一群两次改名的最小间隔。兜底，防御没预料到的抖动模式。
RENAME_MIN_INTERVAL_S = 60
```

把 `ChatRenamer.__init__` 里补一个字段：

```python
    def __init__(self, known_names: dict[str, str] | None = None):
        # 飞书上当前的群名。启动时拉一次当基线，之后跟着改名更新。
        self._known: dict[str, str] = dict(known_names or {})
        # chat_id -> (待定的目标名, 该目标名第一次出现的时刻)
        self._pending: dict[str, tuple[str, float]] = {}
        # chat_id -> 上次真正改名的时刻，用于最小间隔
        self._last_rename: dict[str, float] = {}
```

把 `decide` 整个替换为：

```python
    def decide(self, chat_id: str, target_name: str,
               status: str, now: float) -> str | None:
        """该改成什么名字，或 None 表示按住不动。

        blocked 是唯一「要人立刻动手」的状态，不等防抖也不守最小间隔——
        等的那半分钟正是最该看见它的时候。其余状态走防抖 + 最小间隔。
        """
        chat_id = str(chat_id)
        if self._known.get(chat_id) == target_name:
            self._pending.pop(chat_id, None)
            return None

        if status == "blocked":
            return self._commit(chat_id, target_name, now)

        pending = self._pending.get(chat_id)
        if pending is None or pending[0] != target_name:
            # 目标名变了，防抖重新计时
            self._pending[chat_id] = (target_name, now)
            return None

        if now - pending[1] < RENAME_DEBOUNCE_S:
            return None

        last = self._last_rename.get(chat_id)
        if last is not None and now - last < RENAME_MIN_INTERVAL_S:
            return None

        return self._commit(chat_id, target_name, now)
```

把 `_commit` 替换为（多记一个改名时刻）：

```python
    def _commit(self, chat_id: str, target_name: str,
                now: float) -> str:
        """记下「已经改成这个名字了」，返回该改的名字。"""
        self._known[chat_id] = target_name
        self._last_rename[chat_id] = now
        self._pending.pop(chat_id, None)
        return target_name
```

把 `forget` 替换为：

```python
    def forget(self, chat_id: str) -> None:
        """群解散或解绑后清掉它的状态，别占着内存。"""
        chat_id = str(chat_id)
        self._known.pop(chat_id, None)
        self._pending.pop(chat_id, None)
        self._last_rename.pop(chat_id, None)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run tests/test_lark.py ChatRenamerBlockedAndIntervalTests`
Expected: `Ran 8 tests` … `OK`

- [ ] **Step 5: 跑防抖那组确认没被破坏**

Run: `uv run tests/test_lark.py ChatRenamerDebounceTests ChatRenamerBlockedAndIntervalTests`
Expected: `Ran 18 tests` … `OK`

- [ ] **Step 6: 跑全量**

Run: `uv run tests/test_lark.py`
Expected: `OK`

- [ ] **Step 7: 提交**

```bash
git add relay/herdr_lark.py tests/test_lark.py
git commit -m "feat(lark): 节流器的 blocked 例外与最小间隔

blocked 立即改名：它是唯一要人立刻动手的状态，等 30s 防抖没意义——
等的那半分钟正是最该看见它的时候。例外优先于最小间隔。

其余状态加 60s 最小间隔兜底，防没预料到的抖动模式。"
```

---

## Task 7: `/spaces` 的群复用改走绑定关系

**Files:**
- Modify: `relay/herdr_lark.py:1480-1505`（`find_existing_chat`、`plan_chat_provisioning`）
- Modify: `relay/herdr_lark.py:2873`（`/spaces` 的调用处）
- Test: `tests/test_lark.py`

群名变动态后，按群名精确匹配必然失配 → 重复建群。
`lark_bindings.json` 的 `{chat_id: pane_id}` 是事实源。

- [ ] **Step 1: 先看清现有调用与测试**

```bash
grep -n "find_existing_chat\|plan_chat_provisioning" relay/herdr_lark.py tests/test_lark.py
```

记下所有调用点，Step 4 要一起改。

- [ ] **Step 2: 写失败的测试**

加在 Task 6 新增的类之后：

```python
class SpacesReuseByBindingTests(unittest.TestCase):
    """/spaces 靠绑定关系复用群，不靠群名。

    群名带上会变的状态符号后，按名字精确匹配必然失配——agent 从
    working 变 done、群名从 🟡 x 变 🟢 x，/spaces 就认不出这个群，
    会给同一个 agent 重复建群。

    绑定表（lark_bindings.json 的 {chat_id: pane_id}）才是事实源。
    """

    def _agent(self, pane_id, project):
        return {"pane_id": pane_id, "project": project,
                "agent": "claude", "status": "working"}

    def test_reuses_chat_bound_to_pane(self):
        plan = lk.plan_chat_provisioning(
            [self._agent("w1:p1", "datapilot")],
            bindings={"oc_a": "w1:p1"},
            authorized={"oc_a"})
        self.assertEqual(plan[0]["chat_id"], "oc_a")

    def test_reuse_survives_renamed_chat(self):
        """人手改过群名也认得出——这正是不靠群名的意义。"""
        plan = lk.plan_chat_provisioning(
            [self._agent("w1:p1", "datapilot")],
            bindings={"oc_a": "w1:p1"},
            authorized={"oc_a"})
        self.assertEqual(plan[0]["chat_id"], "oc_a")

    def test_creates_when_no_binding(self):
        plan = lk.plan_chat_provisioning(
            [self._agent("w1:p1", "datapilot")],
            bindings={}, authorized=set())
        self.assertEqual(plan[0]["chat_id"], "")

    def test_creates_when_bound_chat_left_authorized_set(self):
        """群被解散了，绑定还在——得当成没群，重新建。"""
        plan = lk.plan_chat_provisioning(
            [self._agent("w1:p1", "datapilot")],
            bindings={"oc_gone": "w1:p1"},
            authorized={"oc_other"})
        self.assertEqual(plan[0]["chat_id"], "")

    def test_each_agent_gets_its_own_chat(self):
        plan = lk.plan_chat_provisioning(
            [self._agent("w1:p1", "a"), self._agent("w2:p1", "b")],
            bindings={"oc_a": "w1:p1", "oc_b": "w2:p1"},
            authorized={"oc_a", "oc_b"})
        got = {p["pane_id"]: p["chat_id"] for p in plan}
        self.assertEqual(got, {"w1:p1": "oc_a", "w2:p1": "oc_b"})

    def test_plan_titles_carry_status_glyph(self):
        """建群时就带上当时的状态符号。"""
        plan = lk.plan_chat_provisioning(
            [self._agent("w1:p1", "datapilot")],
            bindings={}, authorized=set())
        self.assertTrue(plan[0]["title"].startswith("🟡 "), plan[0]["title"])

    def test_duplicate_projects_still_disambiguated(self):
        """同名 agent 的标记逻辑没被破坏。"""
        plan = lk.plan_chat_provisioning(
            [self._agent("w1:p1", "same"), self._agent("w2:p1", "same")],
            bindings={}, authorized=set())
        self.assertNotEqual(plan[0]["title"], plan[1]["title"])
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run tests/test_lark.py SpacesReuseByBindingTests`
Expected: FAIL — `TypeError: plan_chat_provisioning() got an unexpected keyword argument 'bindings'`

- [ ] **Step 4: 写实现**

在 `relay/herdr_lark.py` 把 1480-1505 这一段：

```python
def find_existing_chat(existing: dict[str, str], project: str, marker: str) -> str | None:
    """按群名找已有的群，找到就复用，不重复建。"""
    return existing.get(chat_title_for(project, marker))


def plan_chat_provisioning(agents: list[dict],
                           existing: dict[str, str]) -> list[dict]:
    """算出每个 agent 该用哪个群：已有的复用，缺的才建。

    返回 [{pane_id, project, title, chat_id}]，chat_id 为空表示要新建。
    """
    ordered = index_agents(agents)
    markers = disambiguate_suffixes(ordered)
    plan = []
    for agent in ordered:
        pane_id = str(agent.get("pane_id") or "")
        project = agent.get("project") or agent.get("agent") or "agent"
        marker = markers.get(pane_id, "")
        title = chat_title_for(project, marker)
        plan.append({
            "pane_id": pane_id,
            "project": project,
            "title": title,
            "chat_id": find_existing_chat(existing, project, marker) or "",
        })
    return plan
```

替换为：

```python
def find_bound_chat(bindings: dict[str, str], pane_id: str,
                    authorized) -> str | None:
    """这个 agent 已经有群了吗。

    曾经按群名精确匹配（find_existing_chat），但群名带上状态符号后会变：
    agent 从 working 变 done、群名从「🟡 x」变「🟢 x」，就认不出来了，
    于是给同一个 agent 重复建群。绑定表才是事实源。

    绑定还在但群已不在授权列表（群被解散了）时返回 None——当成没群。
    """
    for chat_id, bound_pane in bindings.items():
        if bound_pane != pane_id:
            continue
        if authorized and chat_id not in authorized:
            continue
        return chat_id
    return None


def plan_chat_provisioning(agents: list[dict],
                           bindings: dict[str, str],
                           authorized=None) -> list[dict]:
    """算出每个 agent 该用哪个群：已绑的复用，缺的才建。

    返回 [{pane_id, project, title, chat_id}]，chat_id 为空表示要新建。
    """
    ordered = index_agents(agents)
    markers = disambiguate_suffixes(ordered)
    plan = []
    for agent in ordered:
        pane_id = str(agent.get("pane_id") or "")
        project = agent.get("project") or agent.get("agent") or "agent"
        marker = markers.get(pane_id, "")
        title = chat_title_for(project, marker,
                              status=agent.get("status", ""))
        plan.append({
            "pane_id": pane_id,
            "project": project,
            "title": title,
            "chat_id": find_bound_chat(bindings, pane_id, authorized) or "",
        })
    return plan
```

再改 `/spaces` 的调用处。原来是（约 2866-2873 行）：

```python
        try:
            existing = self.api.list_chats()
        except Exception as exc:
            self.reply_text(ctx.chat_id, f"列群失败: {scrub(exc)}")
            return

        plan = plan_chat_provisioning(self.agents, existing)
```

替换为：

```python
        plan = plan_chat_provisioning(
            self.agents, self.bindings.as_dict(), self.chat_ids)
```

注意两点：

1. 不再需要 `list_chats()`，那个 API 调用连同它的错误处理一起去掉。
2. 取绑定表用 `BindingStore.as_dict()`（已存在的方法）。
   别跟 `ChatIdStore.all()` 搞混——那是另一个类的方法，
   `BindingStore` 上没有 `all()`。

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run tests/test_lark.py SpacesReuseByBindingTests`
Expected: `Ran 7 tests` … `OK`

- [ ] **Step 6: 跑全量，修掉旧签名的调用**

Run: `uv run tests/test_lark.py`
Expected: 可能有旧测试用 `plan_chat_provisioning(agents, existing)` 的两参形式
或引用 `find_existing_chat`。逐个改成新签名：
`plan_chat_provisioning(agents, bindings={...}, authorized={...})`。
`find_existing_chat` 的测试直接删掉——那个函数已不存在，它测的行为
（按群名匹配）正是本任务要废除的。

- [ ] **Step 7: 提交**

```bash
git add relay/herdr_lark.py tests/test_lark.py
git commit -m "fix(lark): /spaces 靠绑定关系复用群，不靠群名

群名带上状态符号后会变：agent 从 working 变 done、群名从 🟡 x 变
🟢 x，按名字精确匹配就认不出来了，于是给同一个 agent 重复建群。

改查 lark_bindings.json 的 {chat_id: pane_id}——那才是事实源，
人手改过群名也认得出。顺带省掉一次 list_chats API 调用。"
```

---

## Task 8: 接进状态更新流

**Files:**
- Modify: `relay/herdr_lark.py:3470`（`_track_updates`）
- Modify: `relay/herdr_lark.py:2478-2495`（`LarkBot.__init__`）
- Modify: `relay/herdr_lark.py`（启动处，`main()` 附近）
- Test: `tests/test_lark.py`

前面 7 个任务把零件都做好了，这一步接线：状态变化 → 节流器 → 改名。

- [ ] **Step 1: 看清接线位置**

```bash
sed -n '3470,3500p' relay/herdr_lark.py     # _track_updates
grep -n "bot = LarkBot" relay/herdr_lark.py  # 启动处
grep -n "def make_bot" -A 15 tests/test_lark.py | head -20
```

- [ ] **Step 2: 写失败的测试**

加在 Task 7 新增的类之后：

```python
class RenameOnStatusChangeTests(unittest.TestCase):
    """状态变了就（按节流规则）改群名。"""

    def _bot_with_binding(self):
        bot = make_bot()
        bot.chat_ids = {"oc_1"}
        bot._active = {"oc_1": "w1:p1"}
        bot.api.set_chat_name = unittest.mock.Mock()
        return bot

    def test_blocked_triggers_immediate_rename(self):
        bot = self._bot_with_binding()
        lk._sync_chat_names(
            bot,
            [{"pane_id": "w1:p1", "project": "datapilot",
              "status": "blocked"}],
            now=0)
        bot.api.set_chat_name.assert_called_once()
        args = bot.api.set_chat_name.call_args[0]
        self.assertEqual(args[0], "oc_1")
        self.assertTrue(args[1].startswith("🔴 "), args[1])

    def test_working_waits_for_debounce(self):
        bot = self._bot_with_binding()
        agents = [{"pane_id": "w1:p1", "project": "datapilot",
                   "status": "working"}]
        lk._sync_chat_names(bot, agents, now=0)
        bot.api.set_chat_name.assert_not_called()
        lk._sync_chat_names(bot, agents, now=31)
        bot.api.set_chat_name.assert_called_once()

    def test_unbound_chat_not_renamed(self):
        """没绑 agent 的群不该被改名。"""
        bot = self._bot_with_binding()
        bot._active = {}
        lk._sync_chat_names(
            bot,
            [{"pane_id": "w1:p1", "project": "x", "status": "blocked"}],
            now=0)
        bot.api.set_chat_name.assert_not_called()

    def test_rename_failure_does_not_raise(self):
        """改名失败不能影响正事——它只是展示。"""
        bot = self._bot_with_binding()
        bot.api.set_chat_name.side_effect = RuntimeError("改群名失败: boom")
        lk._sync_chat_names(
            bot,
            [{"pane_id": "w1:p1", "project": "x", "status": "blocked"}],
            now=0)  # 不抛异常即通过

    def test_agent_without_matching_chat_is_skipped(self):
        bot = self._bot_with_binding()
        lk._sync_chat_names(
            bot,
            [{"pane_id": "w9:p9", "project": "other",
              "status": "blocked"}],
            now=0)
        bot.api.set_chat_name.assert_not_called()
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run tests/test_lark.py RenameOnStatusChangeTests`
Expected: FAIL — `AttributeError: module 'herdr_lark' has no attribute '_sync_chat_names'`

- [ ] **Step 4: 写实现**

在 `relay/herdr_lark.py` 的 `_track_updates` 之前插入：

```python
def _sync_chat_names(bot: "LarkBot", agents: list[dict],
                     now: float | None = None) -> None:
    """按当前状态刷各群群名，节流器说不改就不改。

    两份 agent 数据各有用处，别混：

    - `agents` 是这一帧刚到的，status 最新，但 agent_update 分支里只有一个。
    - `bot.agents` 是上一帧的全集（调用点在 bot.agents 赋值**之前**），
      status 旧，但重名消歧需要看全集才算得出标记。

    消歧只依赖 project/agent/cwd/workspace_id，不依赖 status，所以用旧全集
    算标记是安全的；agent 集合真变了下一帧就收敛。

    改名失败只记日志：群名是展示，不是功能前提（与 set_active 一致）。
    """
    if now is None:
        now = time.time()
    markers = disambiguate_suffixes(index_agents(bot.agents or agents))
    by_pane = {str(a.get("pane_id")): a for a in agents if a.get("pane_id")}
    for chat_id, pane_id in list(bot._active.items()):
        agent = by_pane.get(str(pane_id))
        if not agent:
            continue  # 这一帧没带这个 pane 的消息，下一帧再说
        project = agent.get("project") or agent.get("agent") or "agent"
        status = agent.get("status", "")
        target = chat_title_for(project, markers.get(str(pane_id), ""),
                                status=status)
        name = bot.renamer.decide(chat_id, target, status, now)
        if not name:
            continue
        try:
            bot.api.set_chat_name(chat_id, name)
        except Exception as exc:
            log.warning("rename chat failed: %s", scrub(exc))
```

注意 `bot.agents or agents` 这个写法：`bot.agents` 非空时取它（全集），
首帧 `bot.agents` 为空时退回 `agents`。首帧的消歧可能不准（只看到部分
agent），下一帧就对了——群名不值得为此多存一份状态。

在 `_track_updates` 的 `for` 循环**之后**加一行调用。改完整个函数尾部长这样
（注意缩进 4 格——是函数体内、`for` 体外；`now` 复用函数开头已取好的那个，
同一帧内两次取时钟没意义）：

```python
        # 只有群绑着它才推：没绑就没有该收这条通知的地方，不发。
        if is_finish_transition(old_status, new_status) and chats_watching(bot, pane_id):
            # 读 pane 要走 relay 往返，不能卡住监听循环——丢到后台跑。
            asyncio.create_task(_notify_finished(bot, dict(agent)))
        bot.prev_statuses[pane_id] = new_status

    # 状态可能变了，群名跟上（节流器决定这次要不要真改）
    _sync_chat_names(bot, updated, now)
```

在 `LarkBot.__init__` 里，`self._solo_groups: dict[str, bool] = {}` 那行附近加：

```python
        # 群名改名节流器。启动时的群名基线在 main() 里填（要打 API）。
        self.renamer = ChatRenamer()
```

在启动处（`bot = LarkBot(api, CHAT_ID, loop)` 之后）加基线拉取：

```python
    # 拉一次群名当节流器基线：不拉的话每次重启都要把所有群名重改一遍，
    # 每群刷一条系统消息。开发期重启频繁，群数又会长到十几个。
    try:
        bot.renamer = ChatRenamer(
            {item["chat_id"]: item.get("name", "")
             for item in api.chat_inventory()})
    except Exception as exc:
        log.warning("拉取群名基线失败，降级为空基线: %s", scrub(exc))
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run tests/test_lark.py RenameOnStatusChangeTests`
Expected: `Ran 5 tests` … `OK`

（`time` 已在 `herdr_lark.py` 顶部导入，无需额外处理。）

- [ ] **Step 6: 跑全量**

Run: `uv run tests/test_lark.py && uv run tests/test_lark_observer.py`
Expected: 两个都 `OK`

- [ ] **Step 7: 提交**

```bash
git add relay/herdr_lark.py tests/test_lark.py
git commit -m "feat(lark): 状态变化时刷群名

_track_updates 已经在跟 old_status→new_status 了，接上节流器即可，
不新增轮询。改名失败只记日志——群名是展示不是功能前提。

启动时拉一次群名当基线：不拉的话每次重启都要把所有群名重改一遍，
每群刷一条系统消息，而开发期重启很频繁。"
```

---

## Task 9: 手工验证与文档

**Files:**
- Modify: `docs/lark-client-manual.md`

自动化测试覆盖不到「飞书上真的改了名、真的没刷屏」，这步手工确认。

- [ ] **Step 1: 重启服务**

```bash
./relay/install-lark-service.sh restart
```

- [ ] **Step 2: 确认启动无异常**

```bash
LOG=~/Library/Logs/herdr-remote/lark-stderr.log
START=$(grep -n "Bot ready" "$LOG" | tail -1 | cut -d: -f1)
tail -n +$START "$LOG" | grep -iE "error|traceback|exception|warning|failed" || echo "无异常"
```

Expected: 无异常，或只有已知的无害 warning。
特别确认没有 `拉取群名基线失败`。

- [ ] **Step 3: 看群名是否已带符号**

```bash
set -a; . ~/.config/herdr-remote/secrets.env; set +a
TOKEN=$(curl -s -X POST 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal' \
  -H 'Content-Type: application/json' \
  -d "{\"app_id\":\"$HERDR_LARK_APP_ID\",\"app_secret\":\"$HERDR_LARK_APP_SECRET\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["tenant_access_token"])')
curl -s "https://open.feishu.cn/open-apis/im/v1/chats?page_size=50" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c 'import sys,json;[print(i["chat_id"], i.get("name")) for i in json.load(sys.stdin)["data"]["items"]]'
```

Expected: 绑了 agent 的群名以 🔴/🟡/🟢/⚪️ 开头，且不含 `herdr · `。

- [ ] **Step 4: 确认没刷系统消息**

等 5 分钟，然后数某个项目群里近期的 system 消息（把 `<chat_id>` 换成
上一步里一个绑了 agent 的群）：

```bash
curl -s "https://open.feishu.cn/open-apis/im/v1/messages?container_id_type=chat&container_id=<chat_id>&page_size=20&sort_type=ByCreateTimeDesc" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c '
import sys,json
items=json.load(sys.stdin)["data"]["items"]
sys=[i for i in items if i.get("msg_type")=="system"]
print(f"近 20 条里 system 消息 {len(sys)} 条")
'
```

Expected: 5 分钟内新增的 system 消息为 0-1 条。若明显在涨，
说明节流没生效——回到 Task 5/6 检查 `decide` 的接线。

- [ ] **Step 5: 验证 `/spaces` 不重复建群**

在已授权的群里发 `/spaces dry`，确认输出里「复用」数等于已有项目群数、
「新建」数为 0（前提是每个 agent 都已有群）。

- [ ] **Step 6: 确认 observer 仍在对账**

```bash
tail -30 ~/Library/Logs/herdr-remote/lark-observer-stderr.log | grep -c "期望已满足" || true
```

Expected: 有 `期望已满足` 的记录，说明 `is_project_chat` 认得出新群名。
若一条都没有且质检群里开始刷 `missing`，说明 Task 4 的判定有问题。

- [ ] **Step 7: 更新手册**

`docs/lark-client-manual.md` 里搜 `herdr ·`，把描述群名格式的段落改成
状态符号的说明，并加上符号含义表：

```markdown
| 符号 | 状态 | 含义 |
|------|------|------|
| 🔴 | blocked | 等你批准，要动手 |
| 🟡 | working | 正在跑 |
| 🟢 | done | 完工了 |
| ⚪️ | idle | 没事干 |
```

同时说明：群名改动会节流（状态稳定 30 秒才改，blocked 立即改），
所以符号可能比实际状态滞后半分钟。

- [ ] **Step 8: 提交**

```bash
git add docs/lark-client-manual.md
git commit -m "docs(lark): 手册补上群名状态符号

顺带说明改名有节流，符号可能比实际状态滞后半分钟——
不写清楚的话会被当成 bug 报。"
```

---

## Self-Review 记录

**Spec 覆盖检查**（逐节对照 spec）：

| Spec 章节 | 对应任务 |
|-----------|---------|
| 二、符号映射 | Task 1 |
| 二、群名格式 + `CHAT_TITLE_PREFIX` 删除 | Task 2 |
| 3.1 `/spaces` 群复用 | Task 7 |
| 3.2 observer 判定 | Task 3（函数）+ Task 4（observer 侧） |
| 3.3 改名节流 | Task 5（防抖/幂等）+ Task 6（blocked/间隔） |
| 4.1 纯函数层 | Task 1、2、3 |
| 4.2 节流器 + 启动基线 | Task 5、6 + Task 8（基线拉取） |
| 五、错误处理 | Task 8（改名失败、基线失败）、Task 7（绑定指向消失的群） |
| 六、测试 | 各任务的 Step 1；observer 侧在 Task 4 |
| 七、不做的事 | 计划中未涉及 `_STATUS_ICONS`、无重试队列、无落盘 ✓ |

**命名一致性**：`status_glyph`、`is_project_chat`、`ChatRenamer.decide/forget`、
`find_bound_chat`、`plan_chat_provisioning(agents, bindings, authorized)`、
`_sync_chat_names`、`UNBOUND_CHAT_NAME`、`_STATUS_GLYPHS`、
`_PROJECT_CHAT_GLYPHS`（observer 侧）——各任务间引用一致。

**已知的跨任务依赖**：Task 2 删掉 `CHAT_TITLE_PREFIX` 后，Task 3 的
`is_project_chat` 依赖 `UNBOUND_CHAT_NAME`、Task 4 的 observer 测试依赖
`_STATUS_GLYPHS`。按 1→9 顺序执行即可，不要跳序。
