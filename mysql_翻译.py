# -*- coding: utf-8 -*-
"""
mysql_翻译.py — MySQL 语句 → 霸下语句。

这一层**不碰 socket，也不碰闸口**。它干三件事：

    收 MySQL 的一句话  →  翻成霸下的语句（+ 参数）
    收 MySQL 的一句话  →  认出"这句不用碰库"（SET / BEGIN / COMMIT …）
    收霸下给回来的结果  →  翻成 MySQL 结果集的样子

为什么这一层比预想的小得多
==========================

**霸下本来就听得懂英文关键字。** `sql.py:111-310` 的 `KEYWORDS` 里
`CREATE` / `TABLE` / `IF` / `EXISTS` / `SHOW` / `TABLES` / `DESCRIBE` /
`COLUMNS` / `INSERT` / `INTO` / `VALUES` / `DELETE` / `FROM` / `WHERE` /
`IN` / `SELECT` / `LIMIT` / `OFFSET` / `INT` / `TEXT` / `REAL` / `PRIMARY` /
`KEY` / `NOT` / `NULL` / `DEFAULT` **全都在**，而且和中文走的是同一条解析路。

所以爬虫实际发出的八条语句里，`SHOW TABLES`、`DESCRIBE`、`SELECT *`、
`DELETE … IN`、`INSERT … VALUES` **五条一个字都不用改**——它们本来就是
霸下认得的。这一层真正要动的只有两处：

    建表   有 `AUTO_INCREMENT` 和 `ENGINE=InnoDB` 这类霸下没有的壳，得剥
    插入   值已经由客户端转义成一整段文本了，得**扫**回 Python 值再用 `?` 喂

**"扫回来再用参数喂"是这一层最要紧的一条。**
绝不把值拼进霸下语句的文本里——那样注入和"值被当成语法"两类错会一起来，
而且都是安静的。扫出来的 Python 值走 `?`（`sql.py:90` 的 `T_PARAM`），
霸下那边是参数绑定的，值到不了语法的位置上。

认不出来的一律报错，绝不默默回 OK
=================================

**默默回一个 OK，就是让一次写凭空消失**——而这是没人会发现的那种错：
爬虫记了一笔"入库成功"，库里没有那行，等发现的时候已经查不出是哪一次。

所以下面每一条分派最后都落到 `回错(1064, …)`，文案里带原始语句的开头。
"""

from __future__ import annotations

import re

import mysql_协议 as _协议
import schema as _schema

__all__ = [
    '翻译', '会话', '查', '查后做', '写', '直接OK', '直接错',
    '列定义', '列类型表', '引', '扫描器', '翻译错',
]


class 翻译错(Exception):
    """这一层自己发现的错。**带 MySQL 错误码**，由 `翻译` 收口成 `直接错`。"""

    def __init__(self, 码, 文案):
        super().__init__(文案)
        self.码 = 码
        self.文案 = 文案


# ══ 一、类型表：**一处权威** ══════════════════════════════════════════
#
# 这张表同时管两件事，**必须是一份**：
#   ① 发给客户端的 Column Definition 里的 (类型码, charsetnr, 长度, 小数位)
#   ② `DESCRIBE` 回给客户端的那个 `Type` 文本
#
# 分成两份的那天，`DESCRIBE` 说"这是 text"、线路那头按 double 解——而两边
# 都觉得自己是对的，没有一处会报错。
#
# 两条要害，都是对着 pymysql 源码核过的：
#
# ⛔ **`charsetnr` 决定客户端把这一列解成什么**（`connections.py:1425-1433`）：
#
#       charsetnr == 63  →  encoding = None  →  **保持 bytes**
#       charsetnr != 63  →  按连接编码解码    →  **str**
#
#   所以**文本列绝不能填 63**。填了爬虫拿到的是一堆 `bytes`，它那边的
#   清洗代码会当场炸——而报出来的错会指到清洗代码上去，离真因很远。
#
# ⛔ **非文本列要给 ASCII 文本，不能给二进制**：pymysql 对非文本列是
#   `encoding='ascii'` 再走 converter 的，而 `int(b'123')` 在 Python 里
#   根本不成立。所以数值列我们发的是 `'123'` 这五个字节，不是 0x7B。

列类型表 = {
    # 霸下类型   : (MySQL 字段类型,        charsetnr, 长度,   小数位, Type 文本)
    _schema.INT:   (_协议.字段类型.LONG,     63, 11,     0,  'int(11)'),
    _schema.FLOAT: (_协议.字段类型.DOUBLE,   63, 22,    31,  'double'),
    _schema.TEXT:  (_协议.字段类型.STRING,   45, 262140, 0,  'text'),
    _schema.BLOB:  (_协议.字段类型.BLOB,     63, 65535,  0,  'blob'),
    _schema.TIME:  (_协议.字段类型.DATETIME, 63, 19,     0,  'datetime'),
}


def 列定义(名, 霸下类型, 主键=False):
    """
    一列的 Column Definition 那一串（给 `mysql_协议.编列定义` 当参数）。

    认不出的类型号 —— **不发**。发一个没核过的类型码，客户端会拿一个我们
    没验证过的转换器去解，解出来的东西长什么样谁也不知道。
    """
    项 = 列类型表.get(霸下类型)
    if 项 is None:
        raise 翻译错(1105, '类型号 %r 没有对应的 MySQL 类型' % (霸下类型,))
    码, cs, 长, 小, _ = 项
    return (名, 码, cs, 长, 小, 0x0001 if 主键 else 0)


def 类型文本(霸下类型):
    """`DESCRIBE` 那个 `Type` 列。**和上面同一张表**，不另抄一份。"""
    项 = 列类型表.get(霸下类型)
    return '?' if 项 is None else 项[4]


# ══ 二、标识符 ════════════════════════════════════════════════════════

def 引(名):
    """
    把标识符包成霸下的反引号形式，里面的反引号双写。

    霸下词法器和 MySQL 是同一套（`sql.py:392` 的 `_QUOTES`）。**这是
    "标识符不可能注入"的结构性保证**——不靠过滤危险字符，靠**把整个名字
    关进引号里**，引号里出现什么都不再是语法。
    """
    return '`' + str(名).replace('`', '``') + '`'


# ══ 三、字面量扫描器 ══════════════════════════════════════════════════
#
# 客户端发来的 INSERT 值**已经被 pymysql 转义成一整段文本**了，比如
#
#     INSERT INTO `招聘信息` (`标题`,`正文`) VALUES ('中文\'引号','很长的正文')
#
# 所以这里要把那段文本**扫回 Python 值**，再用 `?` 参数喂给霸下。
#
# 认的形态**只有这几种**，别的一律抛：
#
#     NULL / '…' / _binary'…' / X'ABCD' / 0xABCD / 整数 / 浮点 / TRUE / FALSE
#
# ⛔ **不许用 `eval`，也不许用 `ast.literal_eval`。** 后者接受的是 Python
# 语法（`b'..'`、`None`、元组），那是 MySQL 里根本不存在的东西，静默接受
# 等于同一条语句有两种方言——而分岔那天没人查得出是哪一次写入干的。

#: 反斜杠转义表。**只按 pymysql 的 `_escape_table`（`converters.py:62-69`），
#: 实测就是这七条**，多一条都不认：
#:
#:     \0 → NUL    \\ → \    \n → 换行    \r → 回车
#:     \Z → 0x1A   \" → "    \' → '
#:
#: **注意 `\t` 和 `\b` 不在表里**——pymysql 不转义它们（制表符是原样字符，
#: 允许出现在引号里）。所以文本里真出现 `\t` 两个字，按 MySQL 的规矩
#: 就是"反斜杠加 t"，也就是一个 `t`。这由下面那句"别的反斜杠不特殊"兜住。
_转义 = {
    '0': '\x00',
    '\\': '\\',
    'n': '\n',
    'r': '\r',
    'Z': '\x1a',
    '"': '"',
    "'": "'",
}

_数 = re.compile(r'-?(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?'
                 r'|\.\d+(?:[eE][+-]?\d+)?)')
_词 = re.compile(r'[A-Za-z_][A-Za-z_0-9]*')


class 扫描器(object):
    """在一段 SQL 文本上往前走，读标识符、读字面量、读括号组。"""

    def __init__(self, 文):
        self.文 = 文
        self.位 = 0
        self.长 = len(文)

    # ── 基本 ────────────────────────────────────────────────────────

    def 到尾(self):
        return self.位 >= self.长

    def 看(self):
        return '' if self.到尾() else self.文[self.位]

    def 跳过空白(self):
        while self.位 < self.长 and self.文[self.位] in ' \t\r\n':
            self.位 += 1

    def 要(self, 字):
        self.跳过空白()
        if self.看() != 字:
            raise 翻译错(1064, '这里该是 %r，读到的是 %r' % (字, self.看() or '末尾'))
        self.位 += 1

    def 试吃(self, 字):
        self.跳过空白()
        if self.看() == 字:
            self.位 += 1
            return True
        return False

    def 看到词(self, 词表):
        """下一个词是不是这几个之一（不区分大小写）。**不动位置。**"""
        存 = self.位
        self.跳过空白()
        词 = self.读词()
        self.位 = 存
        return 词 is not None and 词.upper() in 词表

    def 吃词(self, 词表):
        """是这几个之一就吃掉，回**大写**的那个；不是就回 None，位置不动。"""
        存 = self.位
        self.跳过空白()
        词 = self.读词()
        if 词 is not None and 词.upper() in 词表:
            return 词.upper()
        self.位 = 存
        return None

    def 读词(self):
        """读一个裸词（字母数字下划线）。读不到回 None，位置不动。"""
        存 = self.位
        self.跳过空白()
        m = _词.match(self.文, self.位)
        if not m:
            self.位 = 存
            return None
        self.位 = m.end()
        return m.group(0)

    def 读标识符(self):
        """读一个标识符：反引号包的、双引号包的、或者裸的。"""
        self.跳过空白()
        c = self.看()
        if c == '`':
            return self._读包起来的('`')
        if c == '"':
            return self._读包起来的('"')
        名 = self.读词()
        if 名 is None:
            raise 翻译错(1064, '这里该有个名字，读到的是 %r' % (self.看() or '末尾'))
        return 名

    def _读包起来的(self, 引号):
        self.位 += 1                       # 吃掉开引号
        出 = []
        while True:
            if self.位 >= self.长:
                raise 翻译错(1064, '引号没关上')
            c = self.文[self.位]
            if c == 引号:
                if self.位 + 1 < self.长 and self.文[self.位 + 1] == 引号:
                    self.位 += 2           # 引号双写 = 一个引号
                    出.append(引号)
                    continue
                self.位 += 1
                return ''.join(出)
            if c == '\\' and 引号 == '`':
                raise 翻译错(1064, '反引号里不认反斜杠转义')
            出.append(c)
            self.位 += 1

    # ── 值 ──────────────────────────────────────────────────────────

    def 读值(self):
        """
        读一个字面量，回 Python 值。**认不出就抛**，不猜。

        `TRUE`/`FALSE` 回的是 **1 / 0**，不是 `True`/`False`——因为霸下的
        `schema.coerce` **专门把布尔挡在外面**（`schema.py` 那句"整数列不收
        真假值"：Python 里 `True` 就是 1，不挡的话写布尔的人以为在写布尔）。
        这里要是回 `True`，插进整数列会被霸下拒掉，而 MySQL 那边 `TRUE`
        本来就是 1，所以回 1 既合 MySQL 的语义、也合霸下的尺子。
        """
        self.跳过空白()
        if self.到尾():
            raise 翻译错(1064, '这里该有个值，但已经到末尾了')

        c = self.看()

        # 引号串
        if c == "'":
            return self._读引号串()

        # `X'ABCD'` —— 十六进制字面量，给出来是 bytes
        if c in 'xX' and self.位 + 1 < self.长 and self.文[self.位 + 1] == "'":
            self.位 += 1
            文 = self._读引号串()
            长 = len(文)
            if 长 % 2:
                raise 翻译错(1064, "X'…' 里的十六进制位数得是偶数")
            try:
                return bytes.fromhex(文)
            except ValueError:
                raise 翻译错(1064, "X'…' 里不是十六进制：%r" % 文[:20])

        # 裸词：NULL / TRUE / FALSE / _binary / 或者一个没包引号的词（不认）
        存 = self.位
        词 = self.读词()
        if 词 is not None:
            上 = 词.upper()
            if 上 == 'NULL':
                return None
            if 上 == 'TRUE':
                return 1
            if 上 == 'FALSE':
                return 0
            if 上 == '_BINARY':
                self.跳过空白()
                if self.看() != "'":
                    raise 翻译错(1064, "_binary 后面要跟 '…'")
                # 二进制值是**用孤代理穿过 UTF-8** 过来的
                # （`converters.py:86-87`：decode('ascii','surrogateescape')），
                # 所以按同一把尺子编回去，一个字节都不会变。
                return self._读引号串().encode('ascii', 'surrogateescape')
            self.位 = 存
            raise 翻译错(1064, '认不出的值：%r（只认 NULL / 引号串 / 数字 / '
                               '_binary\x27…\x27 / X\x27…\x27 / 0x… / 真 假）' % 词)

        # 数字
        m = _数.match(self.文, self.位)
        if m:
            文 = m.group(0)
            self.位 = m.end()
            if 文[:2].lower() == '0x':
                return bytes.fromhex(文[2:] if len(文) % 2 == 0 else '0' + 文[2:])
            if '.' in 文 or 'e' in 文 or 'E' in 文:
                return float(文)
            return int(文)

        raise 翻译错(1064, '认不出的值：%r' % self.文[self.位:self.位 + 20])

    def _读引号串(self):
        """
        读一个 `'…'`，按 MySQL 的规矩反转义，回 `str`。

        两条规矩并列，**都要**：
          * `''`（两个单引号）就是一个单引号 —— SQL 标准的写法
          * `\\x` 按 `_转义` 那张表；**表里没有的，反斜杠吃掉、留下那个字符**
            （MySQL 对认不出的转义就是这个行为）

        ⛔ 第二条那条"反斜杠吃掉"要留着：`\\t` 在 MySQL 里就是 `t`。
        要是按"原样保留反斜杠"来，爬虫存进去的正文就跟取出来的对不上了——
        而这种错**只在正文里恰好出现反斜杠时才现形**，平时全是绿的。
        """
        self.位 += 1                       # 吃掉开引号
        出 = []
        while True:
            if self.位 >= self.长:
                raise 翻译错(1064, '引号没关上')
            c = self.文[self.位]
            if c == "'":
                if self.位 + 1 < self.长 and self.文[self.位 + 1] == "'":
                    self.位 += 2
                    出.append("'")
                    continue
                self.位 += 1
                return ''.join(出)
            if c == '\\':
                if self.位 + 1 >= self.长:
                    raise 翻译错(1064, '反斜杠到了末尾')
                n = self.文[self.位 + 1]
                出.append(_转义.get(n, n))
                self.位 += 2
                continue
            出.append(c)
            self.位 += 1

    # ── 括号 ────────────────────────────────────────────────────────

    def 读括号组(self):
        """
        读一个 `( … )`，按**深度 0 的逗号**切成若干段，回 `[段, …]`。

        每一段是**原文**（没解析过），由调用方决定怎么解。深度要数，
        因为 `DECIMAL(10,2)` 和 `INT(11)` 这种里面也有括号，按第一个
        `)` 切会把它切碎。

        **切的时候要跳过引号和反引号里的逗号**——`('a,b','c')` 里的那个
        逗号不是分隔符。
        """
        self.要('(')
        段 = []
        当前 = []
        深 = 0
        while True:
            if self.位 >= self.长:
                raise 翻译错(1064, '括号没关上')
            c = self.文[self.位]
            if c in "'\"`":
                引号 = c
                当前.append(c)
                self.位 += 1
                while True:
                    if self.位 >= self.长:
                        raise 翻译错(1064, '引号没关上')
                    d = self.文[self.位]
                    当前.append(d)
                    self.位 += 1
                    if d == '\\' and 引号 != '`' and self.位 < self.长:
                        当前.append(self.文[self.位])
                        self.位 += 1
                        continue
                    if d == 引号:
                        if (引号 != '`' and self.位 < self.长
                                and self.文[self.位] == 引号):
                            当前.append(引号)
                            self.位 += 1
                            continue
                        break
                continue
            if c == '(':
                深 += 1
                当前.append(c)
                self.位 += 1
                continue
            if c == ')':
                if 深 == 0:
                    self.位 += 1
                    段.append(''.join(当前).strip())
                    return [x for x in 段]
                深 -= 1
                当前.append(c)
                self.位 += 1
                continue
            if c == ',' and 深 == 0:
                self.位 += 1
                段.append(''.join(当前).strip())
                当前 = []
                continue
            当前.append(c)
            self.位 += 1

    def 到尾了吗(self):
        """跳掉空白和一个结尾分号之后，还有没有剩。"""
        存 = self.位
        self.跳过空白()
        if self.看() == ';':
            self.位 += 1
            self.跳过空白()
        剩 = self.位 < self.长
        self.位 = 存
        return not 剩


# ══ 四、应答：这一层的返回值 ══════════════════════════════════════════
#
# `翻译` 出来的东西一共五种，`mysql_门` 按类型分派。**每种都很小**，
# 但得分开——因为它们要 `门` 做的事不一样（跑查 / 跑写 / 什么都不跑）。

class 查(object):
    """
    去霸下取一次数，把结果按 `投影` 翻成 MySQL 结果集。

    `投影(result) -> (列, 行) | None`：列是 `mysql_协议.写结果集` 要的那
    一串，行是**已经渲染好的**值（str / bytes / None）。

    ⛔ **回 `None` 是一个正经答案，不是"忘了写"。** 它的意思是"这一步
    不回结果集，只回 OK"——`建表` 走的就是这条：它借一次 `显示 结构` 把
    结构缓存填上，而对客户端要说的是 `affected=0`。

    所以 `门` 那边**必须先接住返回值再拆元组**（见 `mysql_门._翻结果`）：
    直接 `列, 行 = 投影(...)` 的话，这一支会抛
    `TypeError: cannot unpack non-iterable NoneType object`。
    """

    __slots__ = ('语句', '参数', '投影')

    def __init__(self, 语句, 参数, 投影=()):
        self.语句 = 语句
        self.参数 = 参数
        self.投影 = 投影 or 原样投影


class 查后做(object):
    """
    先取一次数（结果拿去**填会话里的缓存**），再按 `造(result) -> 应答`
    造出真正要跑的那一步。

    两个地方要用它：
      * 表结构缓存是冷的，先 `显示 结构` 才知道有几列
      * 自增 id 要接着已有最大的那个往后排，得先 `排序 按 id 降序 限制 1`
    """

    __slots__ = ('语句', '参数', '造')

    def __init__(self, 语句, 参数, 造):
        self.语句 = 语句
        self.参数 = 参数
        self.造 = 造


class 写(object):
    """
    去霸下改。`批` 是 `[(语句, 参数), …]`——一条 MySQL 的 INSERT 可能
    对应好几条霸下语句（`VALUES (…),(…)` 那种多行写法）。

    `可攒` 是给 `门` 的：批得起来的一律攒着，到 COMMIT 一次落盘
    （霸下是**每条语句都真写盘**，见 `syntax.py:1331` 那个 `_flush(force=True)`，
    所以逐行插就是逐行落盘，攒批这一件事省的正是这个）。
    """

    __slots__ = ('批', '影响', '自增', '可攒')

    #: `影响` 给 `None` 的意思是"**问引擎要**"——`门` 拿 `Result.affected`
    #: 填。`DELETE` 走这条：到底删掉几行只有扫过才知道，本层数不出来。
    def __init__(self, 批, 影响=1, 自增=0, 可攒=True):
        self.批 = 批
        self.影响 = 影响
        self.自增 = 自增
        self.可攒 = 可攒


class 直接OK(object):
    """
    不用碰库就能回的一个 OK。

    `动作` 是给 `门` 看的那一格，**不是文案**：
        `None`    什么都不用做
        `'落盘'`  把这批攒着的写落下去（`COMMIT`）
        `'丢弃'`  把这批攒着的**还没落盘的**丢掉（`ROLLBACK`）

    ⛔ **不做成"看 `说明` 里有没有 ROLLBACK 这几个字"**：那样 `SET` 一条
    名叫 `ROLLBACK_LOG` 的会话变量就会把客户端的写丢掉，而两边看着
    都"对"。分类要靠一个**能遍历的取值**，不靠一段给人看的字。

    `要记=True` 是"这一条无论快慢都得留个记录"。只有"客户端在设一个
    我们不认的会话变量"用它——那是排错时最关键的一条线索，而它在别处
    一点痕迹都不留。**不加这个标记的话它会掉进"没超时就记"那个筛子里，
    而那些没超时的永远不记。**
    """

    __slots__ = ('影响', '自增', '说明', '动作', '要记')

    def __init__(self, 影响=0, 自增=0, 说明='', 动作=None, 要记=False):
        self.影响 = 影响
        self.自增 = 自增
        self.说明 = 说明
        self.动作 = 动作
        self.要记 = 要记


class 直接错(object):
    """不用碰库就能回的一个 ERR。"""

    __slots__ = ('码', '文案')

    def __init__(self, 码, 文案):
        self.码 = 码
        self.文案 = 文案


# ══ 五、结果反翻译：霸下 Result → MySQL 结果集 ════════════════════════

def 原样投影(result):
    """
    `Result` 原样翻出去：列名照抄，类型走 `列类型表`。

    ⚠️ **取值必须取 `as_lists()` 那份渲染过的**（`syntax.py:112-115`），
    不能自己再渲染一遍。同一格里 `columns` 给类型、`as_lists` 给值，
    **两边各取一处**——这是这类适配器最常见的错位来源（拿 A 处的列配
    B 处的值，然后整行读歪）。
    """
    列 = []
    for 名, 型 in result.columns:
        项 = 列类型表.get(型)
        if 项 is None:
            raise 翻译错(1105, '列 %s 的类型号 %r 本层不认识' % (名, 型))
        码, cs, 长, 小, _ = 项
        列.append((名, 码, cs, 长, 小, 0))
    return 列, result.as_lists()


# ══ 六、会话 ══════════════════════════════════════════════════════════

class 会话(object):
    """
    一条 MySQL 连接自己的状态。**只在它自己那条 socket 线程上用。**

    里面两份东西，**都是"丢得掉"的**：

        `结构`   {表名: [列]}          —— 丢了下次重新 `显示 结构` 就有
        `自增`   {表名: 下一个 id}      —— 丢了从 `max(id)` 重推，结果一样

    **一份都不落盘。** 落盘就得跟库同步，而库那边别人（界面、另一条连接）
    随时能改；同步不上那天，一个缓存文件会以"id 从 1 开始"的形式
    把主键撞掉。重推永远等于盘上真有什么。
    """

    def __init__(self, 连接号, 库名=''):
        self.连接号 = 连接号
        self.库名 = 库名
        self.结构 = {}          # 表名 -> [列]
        self.自增 = {}          # 表名 -> 下一个该发的 id
        self.自增已播种 = set()  # 表名
        self.状态位 = _协议.状态.AUTOCOMMIT

    # ── 结构缓存 ────────────────────────────────────────────────────

    def 表结构(self, 表):
        return self.结构.get(表)

    def 记结构(self, 表, 列):
        self.结构[表] = 列

    def 忘了结构(self, 表=None):
        if 表 is None:
            self.结构.clear()
        else:
            self.结构.pop(表, None)

    def 列(self, 表, 名):
        for c in (self.结构.get(表) or ()):
            if c['名'] == 名:
                return c
        return None

    def 主键列(self, 表):
        return [c for c in (self.结构.get(表) or ()) if c['主键']]

    # ── 自增 ────────────────────────────────────────────────────────

    def 取号(self, 表):
        """拿下一个 id，并把计数器往前推一格。"""
        号 = self.自增.get(表, 1)
        self.自增[表] = 号 + 1
        return 号

    def 看一眼号(self, 表):
        return self.自增.get(表, 1)

    def 定号(self, 表, 下一个):
        """播种：下一个该发的号。**只往前不往后**，免得把已发过的号收回去。"""
        if self.自增.get(表, 1) < 下一个:
            self.自增[表] = 下一个


#: 本层自己发号的那个列长什么样：**主键恰好一列、是整数、名字就是 `id`**。
#:
#: ⛔ **这条判据读不出来，只能推。** 霸下没有"自增"这个概念，`显示 结构`
#: 那七列里也没有这一格（`syntax.py:2058-2131`）。于是：
#:
#:   * 爬虫 DDL 里写的 `AUTO_INCREMENT` 只在**那一次解析**里活着——
#:     `_解列` 收到它，但拼霸下 `建表` 时它就没了（霸下没处放）；
#:   * 建完立刻重读一次 `显示 结构`，缓存里**没有**自增这一位。
#:
#: 所以判据只能是结构性的：`id INT AUTO_INCREMENT PRIMARY KEY` 在 MySQL
#: 那边长什么样，我们就在霸下这边照着认。**恰好这一个形状**才发号——
#: 别的主键形态（复合主键、主键不叫 id、主键不是整数）当场按"没给主键列"
#: 报 1364（见 `_造插入`），**绝不猜**。猜出来的号会撞主键，而撞的时机
#: 取决于表里原来有多少行，那种错看起来像"随机的数据丢失"。
自增主键名 = 'id'


def _发号列(列们):
    """
    这一批列里该由本层发号的是哪一列；没有就回 `None`。

    **判据只有这一处**——`_收结构`（填缓存、给 `DESCRIBE` 报 `Extra`）和
    `_插入有结构`（真发号）都调它。分成两处写迟早会分岔，而分岔的样子是
    "`DESCRIBE` 说这列会自增、插进去却说主键空着"。
    """
    主键 = [c for c in 列们 if c['主键']]
    if len(主键) != 1:
        return None
    c = 主键[0]
    if c['名'] != 自增主键名 or c['类型'] != _schema.INT:
        return None
    return c


#: `显示 结构` 那七列的位置（`syntax.py:2058-2131` 的 `_show`）。
_结构列 = {'列号': 0, '名字': 1, '类型': 2, '可空': 3, '默认': 4, '主键': 5,
           '维度': 6}


def _收结构(会话对象, 表, result):
    """
    把 `显示 结构` 的结果收进会话缓存。

    ⚠️ **收 `as_lists()` 那份渲染过的文本**，和 `界面_接驳._装` 同一套读法
    ——那边也读 `行[3] == '1'`。两处读法不一样的话，同一个库在界面上和在
    爬虫眼里会是两张不同的表。
    """
    if not result.ok:
        raise 翻译错(_表不存在的码(result.error, 表), result.error)
    列 = []
    for 行 in result.as_lists():
        名 = 行[_结构列['名字']]
        型名 = 行[_结构列['类型']]
        列.append({
            '名': 名,
            '类型': _schema.TYPE_ALIASES.get(型名, _schema.TEXT),
            '类型名': 型名,
            '可空': 行[_结构列['可空']] == '1',
            '默认': 行[_结构列['默认']],
            '主键': 行[_结构列['主键']] == '1',
            '维度': 行[_结构列['维度']] == '1',
        })
    发号 = _发号列(列)
    for c in 列:
        # **`自增` 这一位是推出来的，不是读出来的**（理由见 `_发号列`）。
        # `DESCRIBE` 的 `Extra` 那一格照它报，`_插入` 照它发号——
        # 两边共用这一个判据。
        c['自增'] = (c is 发号)
    会话对象.记结构(表, 列)
    return 列


def _表不存在的码(错文, 表):
    """霸下说"没有这张表"的时候，翻成 MySQL 的 1146；别的算 1105。"""
    文 = 错文 or ''
    if ('没有' in 文 and '表' in 文) or '不存在' in 文:
        return 1146
    return 1105


# ══ 七、分派 ══════════════════════════════════════════════════════════

def 翻译(语句, 会话对象):
    """
    MySQL 语句 → 应答（`查` / `查后做` / `写` / `直接OK` / `直接错`）。

    **不认识的一律 `直接错`，绝不默默 OK。** 理由写在文件头。

    ⛔ **契约：返回 `查` / `查后做` / `写` 时，这一层还没干完。**
    `门` 跑完那一步之后，还要回调 `投影` 或 `造`——**而那两样也会抛
    `翻译错`**（最典型的是 `DESCRIBE` 一张不存在的表：`_收结构` 在那时
    才发现，报 1146）。

    所以 `门` 那边**必须把回调也包在同一个 `try/except 翻译错` 里**
    ——不能只包 `翻译()` 这一下。只包这一下的话，那些错会以未捕获异常的
    形式把 socket 线程打死，客户端看到的是"连接断了"，而不是
    "表不存在"。**同一个错，报成两个差得很远的东西。**
    """
    if 语句 is None:
        return 直接错(1064, '空语句')
    文 = 语句.strip()
    while 文.endswith(';'):
        文 = 文[:-1].rstrip()
    if not 文:
        return 直接错(1064, '空语句')

    头 = _首词(文)
    try:
        if 头 == 'CREATE':
            return _建表(文, 会话对象)
        if 头 in ('SHOW', 'DESCRIBE', 'DESC'):
            return _显示(文, 会话对象)
        if 头 == 'EXPLAIN':
            # **不接**。MySQL 的 `EXPLAIN` 要的是执行计划，翻成 `显示 结构`
            # 是拿一个别的东西冒名顶替——看着像成功，内容不是问的那件事。
            return 直接错(1235, '本层不做 EXPLAIN')
        if 头 == 'INSERT' or 头 == 'REPLACE':
            return _插入(文, 会话对象)
        if 头 == 'DELETE':
            return _删除(文, 会话对象)
        if 头 == 'SELECT':
            return _查询(文, 会话对象)
        if 头 in ('SET', 'BEGIN', 'START', 'COMMIT', 'ROLLBACK', 'USE',
                  'SETNAMES', 'UNLOCK', 'LOCK', 'FLUSH'):
            return _会话语句(文, 头, 会话对象)
    except 翻译错 as exc:
        return 直接错(exc.码, exc.文案)
    return 直接错(1064, '认不出这条语句：%s' % 文[:80])


def _首词(文):
    m = re.match(r'[A-Za-z_]+', 文)
    return m.group(0).upper() if m else ''


# ── 7.1 建表 ────────────────────────────────────────────────────────
#
# **唯一一条需要真解析的。** 爬虫发出的原文长这样（`config.py:4906-4911`）：
#
#     CREATE TABLE IF NOT EXISTS `招聘信息` (
#         id INT AUTO_INCREMENT PRIMARY KEY,
#         `标题` TEXT, `公司` TEXT, ...
#     ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
#
# ⛔ **绝不把这段话原样喂给霸下解析器。** 霸下不认 `AUTO_INCREMENT`、不认
# `ENGINE=InnoDB`，喂过去会得到一个语法错；而"顺手把不认识的词跳过"更坏
# ——那样一个没见过的列属性会被静默丢掉，语义变了没人知道。

#: MySQL 类型词 → 霸下类型。**认不出的类型一律报错**，不退回文本。
_建表类型 = {}
for _族, _型 in (
        (('INT', 'INTEGER', 'BIGINT', 'SMALLINT', 'TINYINT', 'MEDIUMINT'), _schema.INT),
        (('TEXT', 'TINYTEXT', 'MEDIUMTEXT', 'LONGTEXT', 'VARCHAR', 'CHAR',
          'ENUM', 'SET', 'JSON'), _schema.TEXT),
        (('REAL', 'FLOAT', 'DOUBLE', 'DECIMAL', 'NUMERIC'), _schema.FLOAT),
        (('BLOB', 'TINYBLOB', 'MEDIUMBLOB', 'LONGBLOB', 'BINARY', 'VARBINARY'), _schema.BLOB),
        (('DATETIME', 'TIMESTAMP', 'DATE', 'TIME', 'YEAR'), _schema.TIME),
):
    for _t in _族:
        _建表类型[_t] = _型
del _族, _型, _t

# ⚠️ 列属性那张"可丢白名单"**故意不单列成一张表**——它就写在 `_解列` 的
# 分支里，一个分支一种属性、各自带着自己的理由。列成一张常量表再在别处
# 解释它，就是**两份地方说同一件事**，而分岔那天没人知道该信哪份。

#: 建表尾巴上**认得出、直接丢掉**的选项（`)` 后面那一截）。
_可丢尾巴 = re.compile(
    r'\s*(?:ENGINE|DEFAULT\s+CHARSET|CHARSET|DEFAULT\s+COLLATE|COLLATE|'
    r'AUTO_INCREMENT|ROW_FORMAT|COMMENT|KEY_BLOCK_SIZE|STATS_PERSISTENT|'
    r'PACK_KEYS|CHECKSUM|DELAY_KEY_WRITE|MAX_ROWS|MIN_ROWS|AVG_ROW_LENGTH|'
    r'COMPRESSION|ENCRYPTION|CONNECTION|DATA\s+DIRECTORY|INDEX\s+DIRECTORY|'
    r'TABLESPACE|STORAGE|UNION|INSERT_METHOD|PASSWORD|PAGE_COMPRESSED)\s*'
    r'(?:=\s*(?:\'[^\']*\'|"[^"]*"|\S+)|'
    r'(?:\'[^\']*\'|"[^"]*"|[A-Za-z_][A-Za-z_0-9]*))\s*',
    re.I)


def _建表(文, 会话对象):
    m = re.match(r'\s*CREATE\s+(?:TEMPORARY\s+)?TABLE\s+', 文, re.I)
    if not m:
        raise 翻译错(1064, '这条 CREATE 不是建表：%s' % 文[:60])
    s = 扫描器(文)
    s.位 = m.end()

    如果不存在 = False
    if s.吃词({'IF'}):
        if not s.吃词({'NOT'}):
            raise 翻译错(1064, 'IF 后面该是 NOT')
        if not s.吃词({'EXISTS'}):
            raise 翻译错(1064, 'IF NOT 后面该是 EXISTS')
        如果不存在 = True

    表 = s.读标识符()
    段们 = s.读括号组()

    # 尾巴：逐项吃掉认得的，**认不出就报**
    余 = 文[s.位:].strip()
    while 余:
        m2 = _可丢尾巴.match(余)
        if not m2:
            raise 翻译错(1064, '建表后面这段认不出：%s' % 余[:60])
        余 = 余[m2.end():].strip()

    # 逐项解列
    列们 = []
    表级主键 = []
    for 段 in 段们:
        if not 段:
            continue
        上 = 段.upper()
        if re.match(r'^(PRIMARY\s+KEY|UNIQUE\s+KEY|UNIQUE\s+INDEX|UNIQUE|KEY|INDEX|'
                    r'FULLTEXT|SPATIAL|CONSTRAINT|FOREIGN\s+KEY|CHECK)\b', 上):
            if 上.startswith('PRIMARY'):
                表级主键 = _表级主键里那几个名字(段)
            elif 上.startswith('UNIQUE'):
                raise 翻译错(1064, '本库没有唯一约束——霸下那边会直接拒。'
                                   '要唯一就把它并进主键')
            else:
                # 索引 / 外键 / 检查约束：霸下不做这几样。
                # **报错，不静默丢**：丢了的话"我建了索引"这句话就成了假的。
                raise 翻译错(1064, '本库不支持建表语句里的这种约束：%s' % 段[:40])
            continue
        列们.append(_解列(段))

    # 主键：列上标的优先，其次表级的，其次叫 id 的，最后补一列
    主键 = [c['名'] for c in 列们 if c['主键']] or 表级主键
    if not 主键:
        if any(c['名'] == 'id' for c in 列们):
            主键 = ['id']
        else:
            # 霸下**要求有主键**（`sql.py:1063`：没有主键就没有 O(1) 点查，
            # 它直接拒）。所以替爬虫补一列，并且**明说补了**。
            列们.insert(0, {'名': 'id', '类型': _schema.INT, '可空': False,
                            '默认': None, '主键': True, '自增': True})
            主键 = ['id']
    for c in 列们:
        c['主键'] = c['名'] in 主键
    # 主键列一律非空（霸下的点查要求）
    for c in 列们:
        if c['主键']:
            c['可空'] = False

    if 如果不存在:
        语句 = '建表 如果不存在 %s (%s)' % (引(表), _拼列们(列们))
    else:
        语句 = '建表 %s (%s)' % (引(表), _拼列们(列们))

    # 这只是一条**去改库**的写。真正让它回 OK 的是下面那步"重读结构"——
    # 建表这一步本身的结果没有人要，`查后做` 只把它当"先决的一步"跑掉。
    return 查后做(语句, (), lambda r: _建表第二步(表, 会话对象, r))


def _建表第二步(表, 会话对象, 建表结果):
    """
    建表跑完了 → 立刻重读一次结构，**一边填缓存一边回 OK**。

    ⛔ **为什么必须重读，不能拿我们自己刚拼的那份列清单当数**
    （`syntax.py:1501-1503`）：**表已经存在时 `建表 如果不存在` 原样回
    `affected=0`，一个列都不改**。这时候盘上的列集跟"我们这次想建的"
    可以完全不同——表是上一次爬虫用别的模板建的，或者中间有人
    `加列` 过。只有 `显示 结构` 读回来的那份才是真的。

    ⚠️ 爬虫每次 `_connect` 都发这条建表（`config.py:4899`），所以"表已存在"
    才是**常态**，不是意外。按"我刚建的所以我说了算"来写，第二天就会开始
    往不存在的列里插值。
    """
    if not 建表结果.ok:
        raise 翻译错(1105, 建表结果.error)

    def 投影(result):
        _收结构(会话对象, 表, result)
        return None            # None = 这一步不回结果集，只回 OK

    return 查('显示 结构 %s' % 引(表), (), 投影)


def _表级主键里那几个名字(段):
    """`PRIMARY KEY (a, b)` → `['a', 'b']`（反引号去壳）。"""
    左 = 段.find('(')
    右 = 段.rfind(')')
    if 左 < 0 or 右 < 左:
        raise 翻译错(1064, 'PRIMARY KEY 后面该跟一对括号：%s' % 段[:40])
    return [_去壳(x.strip()) for x in 段[左 + 1:右].split(',') if x.strip()]


def _解列(段):
    """
    解一个列定义：`名字 类型 [属性…]`。

    **属性走白名单**：认得出、霸下没有的丢掉；认得出的收下；**其余报错**。
    报错而不是跳过，是因为"静默丢掉一个没见过的属性"= 语义悄悄变了。
    """
    s = 扫描器(段)
    名 = s.读标识符()
    词 = s.读词()
    if 词 is None:
        raise 翻译错(1064, '列 %s 后面没有类型' % 名)
    上 = 词.upper()
    # 类型可能带括号：`VARCHAR(255)` / `DECIMAL(10,2)` / `INT(11)`
    # （`读词` 读到头就停，不会顺手把后面的空白吃掉，所以这里自己跳一下
    #  ——`VARCHAR (255)` 这种写法在导出的 DDL 里是有的。）
    s.跳过空白()
    if s.看() == '(':
        s.读括号组()
    # `DOUBLE PRECISION` / `CHARACTER VARYING` 这种两段式
    if 上 in ('DOUBLE', 'CHARACTER', 'NATIONAL'):
        s.跳过空白()
        二 = s.读词()
        if 二 and 二.upper() == 'PRECISION':
            上 = 'DOUBLE'
    if 上 not in _建表类型:
        raise 翻译错(1064, '认不出的列类型：%s（列 %s）' % (上, 名))
    型 = _建表类型[上]

    可空 = True
    默认 = None
    是主键 = False
    自增 = False
    while True:
        s.跳过空白()
        if s.到尾() or s.看() == '':
            break
        存 = s.位
        词 = s.读词()
        if 词 is None:
            # 剩下的是符号：`DEFAULT 'x'` 里的引号之类已经在上面吃掉了；
            # 走到这儿说明有个我们不认的东西
            raise 翻译错(1064, '列 %s 的定义里认不出这一段：%s' % (名, 段[存:][:40]))
        上 = 词.upper()
        if 上 == 'NOT':
            if not (s.吃词({'NULL'}) or s.吃词({'NULLABLE'})):
                raise 翻译错(1064, 'NOT 后面该是 NULL')
            可空 = False
        elif 上 in ('NULL', 'NULLABLE'):
            可空 = True
        elif 上 == 'PRIMARY':
            s.吃词({'KEY'})
            是主键 = True
        elif 上 == 'AUTO_INCREMENT':
            自增 = True
        elif 上 == 'DEFAULT':
            s.跳过空白()
            if s.吃词({'NULL'}):
                默认 = None
            else:
                默认 = s.读值()
        elif 上 == 'COMMENT':
            s.跳过空白()
            if s.看() == "'":
                s._读引号串()
        elif 上 == 'COLLATE':
            s.跳过空白()
            if s.看() == '=':
                s.位 += 1
            s.读标识符()
        elif 上 in ('CHARACTER', 'CHARSET'):
            if 上 == 'CHARACTER':
                if not (s.吃词({'SET'}) or s.吃词({'VARYING'})):
                    raise 翻译错(1064, 'CHARACTER 后面该是 SET')
            s.跳过空白()
            if s.看() == '=':
                s.位 += 1
            s.读标识符()
        elif 上 == 'ON':
            # `ON UPDATE CURRENT_TIMESTAMP`
            if not s.吃词({'UPDATE'}):
                raise 翻译错(1064, '列 %s 上的 ON 后面只认 UPDATE' % 名)
            s.跳过空白()
            函 = s.读词()
            if 函 is None:
                raise 翻译错(1064, 'ON UPDATE 后面该是个值')
            if s.看() == '(':
                s.读括号组()
            raise 翻译错(1064, '列 %s 上的 ON UPDATE 本库给不了——'
                               '霸下的时间列是自己存的，没有"更新时自动改"' % 名)
        elif 上 in ('UNSIGNED', 'SIGNED', 'ZEROFILL', 'VISIBLE', 'INVISIBLE',
                    'STORAGE', 'COLUMN_FORMAT', 'SRID', 'CHECK'):
            if 上 == 'CHECK':
                if s.看() == '(':
                    s.读括号组()
            elif 上 in ('STORAGE', 'COLUMN_FORMAT'):
                s.跳过空白()
                s.读词()
        elif 上 in ('GENERATED', 'AS'):
            raise 翻译错(1064, '列 %s 是生成列，本库不做' % 名)
        elif 上 in ('REFERENCES',):
            raise 翻译错(1064, '本库不做外键')
        elif 上 in ('UNIQUE',):
            raise 翻译错(1064, '本库没有唯一约束（列 %s）' % 名)
        else:
            raise 翻译错(1064, '列 %s 上认不出的属性：%s' % (名, 词))

    return {'名': 名, '类型': 型, '可空': 可空, '默认': 默认,
            '主键': 是主键, '自增': 自增}


_霸下类型词 = {_schema.INT: '整数', _schema.FLOAT: '实数', _schema.TEXT: '文本',
               _schema.BLOB: '二进制', _schema.TIME: '时间'}


def _拼列们(列们):
    """
    拼 `建表` 的列清单。**标识符一律反引号**（`sql.py:392`，霸下词法器和
    MySQL 同一套），所以列名里有什么都不再是语法。
    """
    出 = []
    for c in 列们:
        段 = '%s %s' % (引(c['名']), _霸下类型词[c['类型']])
        if c.get('主键'):
            段 += ' 主键'
        if not c.get('可空', True):
            段 += ' 非空'
        if c.get('默认') is not None:
            段 += ' 默认 ' + _字面(c['默认'], c['类型'])
        出.append(段)
    return ', '.join(出)


def _字面(值, 型):
    """把一个 Python 值写成霸下的字面量（只用在 `默认` 上）。"""
    if isinstance(值, str):
        return "'" + 值.replace("'", "''") + "'"
    if isinstance(值, bytes):
        return "'" + 值.decode('ascii', 'surrogateescape').replace("'", "''") + "'"
    if isinstance(值, bool):
        return '真' if 值 else '假'
    return str(值)


# ── 7.2 显示（SHOW / DESCRIBE）──────────────────────────────────────

def _显示(文, 会话对象):
    上 = 文.strip().upper()
    m = re.match(r'^SHOW\s+(?:FULL\s+)?TABLES\s*(.*)$', 上, re.S)
    if m:
        余 = m.group(1).strip().rstrip(';').strip()
        if 余:
            # `SHOW TABLES LIKE '…'` / `FROM db`。**不装作支持**：真做的话
            # 得实现 LIKE，而"把 LIKE 忽略掉、原样回全表"是个**看着成功、
            # 内容不对**的答案——正是这一层最要躲开的那类错。
            raise 翻译错(1235, '本层不做 `SHOW TABLES %s`；'
                               '不带条件的 `SHOW TABLES` 可以' % 余[:40])

        # 霸下 `显示 表列表` 给五列（表名/表号/列数/主键/维度列），
        # MySQL 只给一列。**只投影第 0 列**，多的四列直接丢。
        def 投影(result):
            if not result.ok:
                raise 翻译错(1105, result.error)
            名 = 'Tables_in_%s' % (会话对象.库名 or '')
            行 = [[r[0]] for r in result.as_lists()]
            return [(名, _协议.字段类型.STRING, 45, 262140, 0, 0)], 行
        return 查('显示 表列表', (), 投影)

    m = re.match(r'^(?:DESCRIBE|DESC|SHOW\s+(?:FULL\s+)?COLUMNS\s+(?:FROM|IN))\s+'
                 r'([^\s;]+)', 文.strip(), re.I)
    if m:
        表 = _去壳(m.group(1))
        return _结构应答(会话对象, 表)

    if re.match(r'^SHOW\s+(FULL\s+)?(COLUMNS|FIELDS)\b', 上):
        # `SHOW COLUMNS FROM t` 之类上面那条正则已经兜住了；走到这儿说明
        # 形状没见过（比如没写 FROM）
        raise 翻译错(1064, '这条 SHOW 认不出：%s' % 文[:60])

    if re.match(r'^SHOW\s+(DATABASES|SCHEMAS)\b', 上):
        raise 翻译错(1235, '本层没有"列出库"这个概念：一次只开一个库，'
                           '库名就是界面上开着的那个')
    if re.match(r'^SHOW\s+(VARIABLES|STATUS|ENGINES|CHARSET|COLLATION|'
                r'WARNINGS|ERRORS|PROCESSLIST|GRANTS|CREATE|INDEX|KEYS|'
                r'TABLE\s+STATUS|TRIGGERS|EVENTS)\b', 上):
        raise 翻译错(1235, '这条 SHOW 本层不做：%s' % 文[:60])
    raise 翻译错(1064, '认不出的 SHOW：%s' % 文[:60])


def _结构应答(会话对象, 表):
    """`DESCRIBE t` → `显示 结构`，翻成 MySQL 那六列。"""
    def 投影(result):
        列 = _收结构(会话对象, 表, result)
        出 = []
        for c in 列:
            出.append([
                c['名'],
                类型文本(c['类型']),
                'YES' if c['可空'] else 'NO',
                'PRI' if c['主键'] else '',
                None if c['默认'] is None else c['默认'],
                # `Extra`。**这里必须和 `_插入` 那边说同一件事**：报了
                # `auto_increment` 就一定真发号，否则客户端（爬虫）会
                # 以为不写 id 也成，然后插进去一行主键空的。
                'auto_increment' if c['自增'] else '',
            ])
        # **全报 253/45 ⇒ 客户端全解成 str。** 爬虫那边读的是
        # `[r[0] for r in cur.fetchall() if r[0] != "id"]`（`config.py:4924`）
        # ——列名要是 bytes，`!=` 恒真，id 列会被当成业务列塞进 INSERT。
        列定义们 = [(n, _协议.字段类型.STRING, 45, 262140, 0, 0)
                  for n in ('Field', 'Type', 'Null', 'Key', 'Default', 'Extra')]
        return 列定义们, 出
    return 查('显示 结构 %s' % 引(表), (), 投影)


def _去壳(名):
    if len(名) >= 2 and 名[0] == '`' and 名[-1] == '`':
        return 名[1:-1].replace('``', '`')
    return 名


# ── 7.3 插入 ────────────────────────────────────────────────────────

def _插入(文, 会话对象):
    """
    `INSERT INTO …` 和 `REPLACE INTO …` 走同一条路，只有表头那个词不同。

    **`替换` 是真对应，不是将就**：霸下的 `替换` 就是"主键已有行就覆盖"
    （`syntax.py:2350-2391`，那里还有一句原话"主键已经有行了。要覆盖请把
    插入 换成 替换"）——正是 MySQL `REPLACE INTO` 的语义。所以照搬。

    ⚠️ 但权限上不等价：`替换` 要**同时有 `插` 和 `改`**（`syntax.py:2352`
    那句"不然一个只有 `插` 的人就能拿 `替换` 把整张表逐行改写一遍"）。
    所以爬虫那个霸下账号要是只给了 `插`，`REPLACE` 会被霸下拒掉——
    这是**对的**，不该在这层抹平。
    """
    s = 扫描器(文)
    头 = (s.读词() or '').upper()
    覆盖 = (头 == 'REPLACE')
    s.吃词({'LOW_PRIORITY', 'DELAYED', 'HIGH_PRIORITY', 'IGNORE'})
    s.吃词({'INTO'})
    表 = s.读标识符()

    # ⛔ **`看()` 不跳空白，所以这里必须自己先跳。** 爬虫发出来的正是
    # ``INSERT INTO `t` (`a`, `b`) VALUES …`` ——表名反引号后面**有一个
    # 空格**。不跳的话这一支进不来，`列名们` 留在 `None`，接着下面那句
    # "找不到 VALUES" 会在一个明明有 VALUES 的语句上抛出来。
    列名们 = None
    s.跳过空白()
    if s.看() == '(':
        段们 = s.读括号组()
        列名们 = [_去壳(x.strip()) for x in 段们 if x.strip()]

    # `INSERT INTO t SET a=1, b=2` 是 MySQL 的另一种写法，霸下没有。
    # **明着拒**：不拒的话下面那句"找不到 VALUES"会报一个指错地方的话。
    if s.看到词({'SET'}):
        raise 翻译错(1235, '本层不做 `INSERT … SET` 这种写法，'
                           '请用 `INSERT … (列) VALUES (值)`')
    if not s.看到词({'VALUES', 'VALUE'}):
        raise 翻译错(1064, 'INSERT 里找不到 VALUES：%s' % 文[:60])
    s.吃词({'VALUES', 'VALUE'})

    值组 = []
    while True:
        s.跳过空白()                    # 同样：`看()` 不跳空白
        if s.看() != '(':
            break
        段们 = s.读括号组()
        行 = []
        for 段 in 段们:
            t = 扫描器(段)
            值 = t.读值()
            if not t.到尾了吗():
                raise 翻译错(1064, '值后面还有东西认不出：%s' % 段[:40])
            行.append(值)
        值组.append(行)
        if not s.试吃(','):
            break

    if not 值组:
        raise 翻译错(1064, 'INSERT 里一个值组都没有：%s' % 文[:60])

    if 会话对象.表结构(表) is None:
        # 缓存是冷的：先 `显示 结构`，再拿它做上面这些
        return 查后做('显示 结构 %s' % 引(表), (),
                      lambda r: _插入有结构(表, 列名们, 值组, 会话对象, r,
                                          覆盖, 文))
    return _插入有结构(表, 列名们, 值组, 会话对象, None, 覆盖, 文)


def _插入有结构(表, 列名们, 值组, 会话对象, 结构结果, 覆盖, 原文):
    if 结构结果 is not None:
        _收结构(会话对象, 表, 结构结果)
    列们 = 会话对象.表结构(表) or []
    有 = {c['名']: c for c in 列们}

    if 列名们 is not None:
        缺 = [n for n in 列名们 if n not in 有]
        if 缺:
            raise 翻译错(1054, '没有这些列：%s（表 %s）' % (', '.join(缺), 表))
        目标 = [有[n] for n in 列名们]
    else:
        目标 = list(列们)

    # 自增那一列：看看要不要我们发号。**判据在 `_发号列` 一处**——
    # 它只认"单列整数主键、名字叫 id"。复合主键、主键不叫 id、主键不是
    # 整数，一律回 `None`，于是下面按"主键列没给值"报 1364，不猜。
    自增列 = _发号列(列们)

    要点名 = (自增列 is not None and 自增列['名'] not in (列名们 or []))

    if 要点名 and 表 not in 会话对象.自增已播种:
        # **先看看表里最大到哪了。**
        # ⛔ 不能从 1 开始：重启之后从 1 开始就会**撞主键**，而撞的时机
        # 取决于表里原来有多少行——这种错看起来像"随机的数据丢失"。
        # 也不能用 `计数(*)`：删过行的表 count 比 max(id) 小，同样会撞。
        return 查后做('查询 %s 从 %s 排序 按 %s 降序 限制 1'
                      % (引(自增列['名']), 引(表), 引(自增列['名'])), (),
                      lambda r: _播种之后造(表, 自增列, 列名们, 值组, 列们,
                                          会话对象, r, 覆盖, 原文))

    return _造插入(表, 自增列, 列名们, 值组, 列们, 会话对象, 覆盖)


def _播种之后造(表, 自增列, 列名们, 值组, 列们, 会话对象, 结果, 覆盖, 原文):
    """`排序 按 id 降序 限制 1` 回来了，把号接上，再造插入。"""
    最大 = 0
    if 结果.ok:
        行 = 结果.as_lists()
        if 行 and 行[0] and 行[0][0] is not None:
            try:
                最大 = int(行[0][0])
            except (TypeError, ValueError):
                最大 = 0
    elif '没有' in (结果.error or ''):
        # 表刚建、还没行——查询空表在霸下回的是"没有"还是空结果集取决于是
        # 哪条路；两种都按"没有行"处理，从 1 开始是对的。
        最大 = 0
    else:
        raise 翻译错(1105, '数不出 %s 的自增号：%s' % (表, 结果.error))
    会话对象.定号(表, 最大 + 1)
    会话对象.自增已播种.add(表)
    return _造插入(表, 自增列, 列名们, 值组, 列们, 会话对象, 覆盖)


def _造插入(表, 自增列, 列名们, 值组, 列们, 会话对象, 覆盖):
    """
    把扫出来的 Python 值**用 `?` 绑**进霸下语句。

    ⛔ **值一个都不许拼进文本。** 这一条同时解决注入和"值被当成语法"
    两类错。标识符走 `引()`（反引号双写），那是结构性保证。
    """
    有 = {c['名']: c for c in 列们}
    批 = []
    第一个号 = 0

    for i, 值组一 in enumerate(值组):
        if 列名们 is not None:
            if len(列名们) != len(值组一):
                raise 翻译错(1136, '列数(%d)和值数(%d)对不上'
                                   % (len(列名们), len(值组一)))
            对 = list(zip(列名们, 值组一))
        else:
            if len(列们) != len(值组一) and 自增列 is None:
                raise 翻译错(1136, '表有 %d 列，值给了 %d 个'
                                   % (len(列们), len(值组一)))
            # 没写列名：按表序对上；自增那列跳过
            名们 = [c['名'] for c in 列们 if not (c.get('自增') and c['主键'])]
            if len(名们) != len(值组一):
                raise 翻译错(1136, '表有 %d 个可写列，值给了 %d 个'
                                   % (len(名们), len(值组一)))
            对 = list(zip(名们, 值组一))

        发号 = None
        if 自增列 is not None and 自增列['名'] not in [n for n, _ in 对]:
            发号 = 会话对象.取号(表)
            if i == 0:
                第一个号 = 发号

        名们, 参数 = [], []
        for 名, 值 in 对:
            c = 有[名]
            名们.append(名)
            参数.append(_按列收(c, 值, 表))
        if 发号 is not None:
            名们.insert(0, 自增列['名'])
            参数.insert(0, 发号)

        列文 = ', '.join(引(n) for n in 名们)
        占位 = ', '.join('?' for _ in 名们)
        头 = '替换' if 覆盖 else '插入'
        批.append(('%s %s (%s) 值 (%s)' % (头, 引(表), 列文, 占位),
                   tuple(参数)))

    return 写(批, 影响=len(值组), 自增=第一个号, 可攒=not 覆盖)


def _按列收(c, 值, 表):
    """
    按目标列的**类型**把值收一收。**用 `schema.coerce`，和霸下同一把尺子。**

    这里先收一遍是为了**把错挡在 COMMIT 之前**：霸下那边一条语句一条语句
    落盘，等到 COMMIT 才发现第 300 行类型不对，前面 299 行已经写下去了
    （见 `写` 和攒批那段）。先收，就能在攒批之前把整条语句拒掉。

    收不了就是 1366（MySQL 的"Incorrect value"）。
    """
    类型 = c['类型']
    if 值 is None:
        if not c['可空']:
            raise 翻译错(1048, '列 %s 不能是空' % c['名'])
        return None
    try:
        return _schema.coerce(类型, 值)
    except _schema.SchemaError as exc:
        raise 翻译错(1366, '列 %s 的值不合适：%s' % (c['名'], exc))


# ── 7.4 删除 ────────────────────────────────────────────────────────

def _删除(文, 会话对象):
    s = 扫描器(文)
    s.读词()
    s.吃词({'LOW_PRIORITY', 'QUICK', 'IGNORE'})
    if not s.吃词({'FROM'}):
        raise 翻译错(1064, 'DELETE 后面该是 FROM：%s' % 文[:60])
    表 = s.读标识符()

    if s.看到词({'ORDER', 'LIMIT'}):
        raise 翻译错(1235, 'DELETE 的 排序/限制 本层不做')

    if not s.吃词({'WHERE'}):
        # 没有 WHERE 的 DELETE 是**清空整表**。MySQL 允许，但在这条路上
        # 几乎只可能是写错了——而后果是整张表没了。所以明着拒。
        raise 翻译错(1064, '本层不接没有条件的 DELETE（那会清空整张表）。'
                           '要清空表请自己写明条件')

    条件 = s.文[s.位:].strip()

    if 会话对象.表结构(表) is None:
        return 查后做('显示 结构 %s' % 引(表), (),
                      lambda r: _删除有结构(表, 条件, 会话对象, r))
    return _删除有结构(表, 条件, 会话对象, None)


def _删除有结构(表, 条件, 会话对象, 结构结果):
    if 结构结果 is not None:
        _收结构(会话对象, 表, 结构结果)
    条, 参数 = _解条件(表, 条件, 会话对象, 表)
    语句 = '删除 从 %s 其中 %s' % (引(表), ' 且 '.join(条))
    # `影响=None` ⇒ 删掉几行问引擎要（`syntax.py:2468` 的 `Result.affected`），
    # 和 MySQL 报的数一致。
    #
    # `可攒=False`：删除**不攒批**。攒批是为了把 N 次落盘并成一次，而删除
    # 本来就只有一条语句、本来就要立刻落盘；把它攒着只会让"删了没生效"
    # 变成一个能持续几秒的状态。
    return 写([(语句, tuple(参数))], 影响=None, 可攒=False)


# ── 7.5 查询 ────────────────────────────────────────────────────────

def _查询(文, 会话对象):
    s = 扫描器(文)
    s.读词()
    s.吃词({'ALL'})
    去重 = s.吃词({'DISTINCT', 'DISTINCTROW'}) is not None

    列文 = []
    while True:
        s.跳过空白()
        if s.看() == '*':
            s.位 += 1
            列文.append('*')
        else:
            列文.append(引(s.读标识符()))
        if not s.试吃(','):
            break

    if not s.吃词({'FROM'}):
        raise 翻译错(1064, 'SELECT 后面该是 FROM：%s' % 文[:60])
    表 = s.读标识符()
    s.跳过空白()
    # 别名 `FROM t a` / `FROM t AS a`
    if not s.到尾() and not s.看到词({'WHERE', 'ORDER', 'LIMIT', 'GROUP',
                                     'HAVING', 'OFFSET', 'FOR'}):
        if s.吃词({'AS'}):
            s.读标识符()
        elif (s.看() or '').isalpha():
            s.读标识符()

    if s.看到词({'JOIN', 'INNER', 'LEFT', 'RIGHT', 'CROSS', 'NATURAL'}):
        raise 翻译错(1235, '本层不做 JOIN')
    if s.看到词({'GROUP', 'HAVING'}):
        raise 翻译错(1235, '本层不做 GROUP BY / HAVING')
    if s.看到词({'UNION', 'INTERSECT', 'EXCEPT'}):
        raise 翻译错(1235, '本层不做集合运算')

    尾 = []
    if s.吃词({'WHERE'}):
        尾.append(('其中', s.文[s.位:]))
        # 把 WHERE 那一段切出来：切到下一个子句为止
        条件 = _切到子句(s.文[s.位:])
        s.位 += len(条件)
        尾[-1] = ('其中', 条件)
    if 去重:
        raise 翻译错(1235, '本层不做 SELECT DISTINCT')

    排序文 = None
    if s.吃词({'ORDER'}):
        s.吃词({'BY'})
        段 = []
        while True:
            c = s.读标识符()
            下 = '升序'
            if s.吃词({'DESC'}):
                下 = '降序'
            else:
                s.吃词({'ASC'})
            段.append('%s %s' % (引(c), 下))
            if not s.试吃(','):
                break
        排序文 = ', '.join(段)

    限制 = 偏移 = None
    if s.吃词({'LIMIT'}):
        限制 = s._读数(文)
        if s.吃词({'OFFSET'}):
            偏移 = s._读数(文)
        elif s.试吃(','):
            偏移 = 限制
            限制 = s._读数(文)

    if not s.到尾了吗():
        raise 翻译错(1235, '这条 SELECT 尾巴上还有认不出的东西：%s'
                           % s.文[s.位:s.位 + 40])

    # 现在就差 WHERE 里那些参数了：它要按列类型收，所以得先有结构
    if 尾:
        if 会话对象.表结构(表) is None:
            return 查后做('显示 结构 %s' % 引(表), (),
                          lambda r: _查询有结构(表, 列文, 尾[0][1], 排序文,
                                               限制, 偏移, 会话对象, r))
        return _查询有结构(表, 列文, 尾[0][1], 排序文, 限制, 偏移, 会话对象, None)
    return _查询有结构(表, 列文, None, 排序文, 限制, 偏移, 会话对象, None)


_子句词 = ('ORDER', 'LIMIT', 'OFFSET', 'GROUP', 'HAVING', 'FOR', 'UNION',
           'INTERSECT', 'EXCEPT', 'LOCK', 'INTO', 'PROCEDURE')


def _切到子句(文):
    """从一段文里切出"到下一个子句之前"那部分。**跳过引号里的关键字。**"""
    s = 扫描器(文)
    深 = 0
    切 = len(文)
    while s.位 < s.长:
        c = s.文[s.位]
        if c in '"\'':
            引号 = c
            s.位 += 1
            while s.位 < s.长:
                d = s.文[s.位]
                s.位 += 1
                if d == '\\' and 引号 != '`' and s.位 < s.长:
                    s.位 += 1
                    continue
                if d == 引号:
                    if 引号 != '`' and s.位 < s.长 and s.文[s.位] == 引号:
                        s.位 += 1
                        continue
                    break
            continue
        if c == '(':
            深 += 1
            s.位 += 1
            continue
        if c == ')':
            深 -= 1
            s.位 += 1
            continue
        if 深 == 0 and (c.isalpha() or c == '_'):
            存 = s.位
            词 = s.读词()
            if 词 and 词.upper() in _子句词:
                切 = 存
                break
            continue
        s.位 += 1
    return 文[:切].strip()


def _查询有结构(表, 列文, 条件, 排序文, 限制, 偏移, 会话对象, 结构结果):
    if 结构结果 is not None:
        _收结构(会话对象, 表, 结构结果)

    语句 = '查询 %s 从 %s' % (', '.join(列文), 引(表))
    参数 = []
    if 条件:
        条, 参数 = _解条件(表, 条件, 会话对象, 表)
        语句 += ' 其中 ' + ' 且 '.join(条)
    if 排序文:
        语句 += ' 排序 按 ' + 排序文
    if 限制 is not None:
        语句 += ' 限制 %d' % 限制
    if 偏移 is not None:
        语句 += ' 偏移 %d' % 偏移
    return 查(语句, tuple(参数))


# ── 7.6 条件：`列 = 值` / `列 IN (…)` ───────────────────────────────

def _解条件(表, 条件, 会话对象, 当前表):
    """
    解 `WHERE`。**只认这几种形状**，别的一律报。

    支持：`c = v` / `c IN (v,…)` / `c <> v` / `c != v` / `c IS NULL` /
    `c IS NOT NULL`，以及用 `AND` 连起来的若干个。

    ⛔ **不做"尽力而为"的翻译。** 翻错的条件给的是**错答案**——比如把
    `c > 5` 当成 `c = 5`，删掉的是另一批行，而且报的是"成功"。这比报错
    坏得多：报错人会去看，错答案没人看。

    ⚠️ 值是**按目标列的类型收过的**。`DELETE … WHERE id IN ('1','2')` 里
    那两个值是**字符串**（`qt_dialogs.py:1227` 是 `item.text()`，从表格控件
    取出来的 ID 天生是 str），不收的话霸下那边会拿字符串去比整数列。
    """
    条 = []
    参数 = []
    s = 扫描器(条件)
    while True:
        s.跳过空白()
        if s.到尾():
            break
        名 = s.读标识符()
        c = 会话对象.列(当前表, 名)
        if c is None:
            raise 翻译错(1054, '没有这一列：%s（表 %s）' % (名, 当前表))
        s.跳过空白()

        if s.吃词({'IS'}):
            否定 = s.吃词({'NOT'}) is not None
            if not s.吃词({'NULL'}):
                raise 翻译错(1235, '只认 IS NULL / IS NOT NULL')
            # 用**英文**的 `IS NULL`：`sql.py:215` 里 `IS`/`是`/`为` 是同一个
            # 记号，两种拼法都行；挑英文是因为 `不是` 不在那个词表里，
            # 而 `IS NOT NULL` 是三个各自独立的记号，拼起来一定对得上。
            条.append('%s IS %sNULL' % (引(名), 'NOT ' if 否定 else ''))
        elif s.吃词({'IN'}):
            段们 = s.读括号组()
            占位 = []
            for 段 in 段们:
                t = 扫描器(段)
                值 = t.读值()
                if not t.到尾了吗():
                    raise 翻译错(1064, 'IN 里的值后面还有东西：%s' % 段[:30])
                参数.append(_按列收(c, 值, 当前表))
                占位.append('?')
            if not 占位:
                raise 翻译错(1064, 'IN 的括号里一个值都没有')
            条.append('%s 属于 (%s)' % (引(名), ', '.join(占位)))
        elif s.吃词({'NOT'}):
            if not s.吃词({'IN'}):
                raise 翻译错(1235, '只认 NOT IN')
            段们 = s.读括号组()
            占位 = []
            for 段 in 段们:
                t = 扫描器(段)
                值 = t.读值()
                if not t.到尾了吗():
                    raise 翻译错(1064, 'NOT IN 里的值后面还有东西')
                参数.append(_按列收(c, 值, 当前表))
                占位.append('?')
            条.append('%s 不属于 (%s)' % (引(名), ', '.join(占位)))
        else:
            # **每个 `试吃` 自己会先跳空白**，所以 `a != 1` 和 `a!=1` 两种
            # 写法都吃得到。⛔ 不能写成 `s.看() in '<>!'`——`看()` 不跳空白，
            # 于是"运算符前面隔了一个空格"会掉进"什么都不认"那一支，报一句
            # "WHERE 里只认 = / != / …"，而它明明就是 `!=`。
            #
            # **长的先试**：`<=` 不排在 `<` 前面的话，`<=` 会被读成 `<`，
            # 剩下的 `=` 变成下一个值。
            符 = None
            if s.试吃('='):
                符 = '='
            elif s.试吃('!=') or s.试吃('<>'):
                符 = '!='
            elif s.试吃('<='):
                符 = '<='
            elif s.试吃('>='):
                符 = '>='
            elif s.试吃('<'):
                符 = '<'
            elif s.试吃('>'):
                符 = '>'
            if 符 is None:
                raise 翻译错(1235, 'WHERE 里只认 = / != / <> / < / <= / > / >= / '
                                   'IN / NOT IN / IS NULL 这几种')
            if 符 not in ('=', '!='):
                raise 翻译错(1235, '本层不做 %s 这种比较（霸下那边代价形状不同，'
                                   '翻过去会变成一个不保证的扫描）' % 符)
            值 = s.读值()
            参数.append(_按列收(c, 值, 当前表))
            条.append('%s %s ?' % (引(名), 符))

        s.跳过空白()
        if s.吃词({'AND'}):
            continue
        if s.吃词({'OR'}):
            raise 翻译错(1235, '本层不做 WHERE 里的 OR')
        break

    if not 条:
        raise 翻译错(1064, 'WHERE 是空的')
    return 条, 参数


# `读一个非负整数`——挂在扫描器上，因为 LIMIT 用得到
def _读数(self, 原文):
    self.跳过空白()
    m = re.match(r'\d+', self.文[self.位:])
    if not m:
        raise 翻译错(1064, '这里要给一个非负整数')
    self.位 += m.end()
    return int(m.group(0))


扫描器._读数 = _读数


# ── 7.7 会话语句：不用碰库的那些 ────────────────────────────────────
#
# ⛔ **不假装有事务。** 霸下没有 BEGIN/ROLLBACK，也没有撤销。
# 这里把 BEGIN/COMMIT/ROLLBACK 当成**攒批的边界**，不是事务：
#
#     COMMIT    把攒着的写**落盘**（这才是真的有副作用的那一步）
#     ROLLBACK  把**还没落盘**的那批**丢掉**
#
# **已经落盘的丢不掉**——真回滚一个霸下没有的东西，会在"回滚没生效"
# 那天以静默丢数据的形式还回来。这一条要写进报告。

def _会话语句(文, 头, 会话对象):
    上 = 文.strip().upper()

    if re.match(r'^SET\s+NAMES\b', 上):
        # **一律按 UTF-8 处理**，收下就完事。客户端那边已经按 utf8mb4 编了。
        return 直接OK(说明='SET NAMES')
    if re.match(r'^SET\s+(SESSION\s+)?AUTOCOMMIT\s*=\s*[01]', 上):
        开 = 上.rstrip().endswith('1')
        会话对象.状态位 = (_协议.状态.AUTOCOMMIT if 开 else 0)
        return 直接OK(说明='SET AUTOCOMMIT=%d' % (1 if 开 else 0))
    if re.match(r'^SET\s+', 上):
        # 别的会话变量：**收下、回 OK、记一条**。
        # "客户端在设一个我们不认的会话变量"是排错时最关键的一条线索
        # ——它说明客户端的某个行为我们没满足，而不是它写错了。
        return 直接OK(说明='SET（本层不认，收下了）：%s' % 文[:80], 要记=True)
    if 上.startswith('BEGIN') or 上.startswith('START TRANSACTION'):
        return 直接OK(说明='BEGIN（霸下没有事务，这里只当攒批的边界）')
    if 上.startswith('COMMIT'):
        return 直接OK(说明='COMMIT（落盘）', 动作='落盘')
    if 上.startswith('ROLLBACK'):
        # ⛔ **已经落盘的丢不掉。** 霸下没有撤销，也没有事务日志。
        # 这里丢的只是"还攒在内存里、没交给引擎"的那一批。爬虫两处
        # `rollback` 都在 `except` 分支里（`config_gui.py:372/409`），
        # 那时候本来也还没 commit，所以丢掉正是它要的——但这是
        # **碰巧对上**，不是"我们实现了回滚"。这条要写进报告。
        return 直接OK(说明='ROLLBACK（只丢还没落盘的那批）', 动作='丢弃')
    if 上.startswith('USE'):
        raise 翻译错(1235, '本层不认 USE：库名在**握手**就定了，'
                           '而且必须等于界面上开着的那个库')
    if 上.startswith('LOCK') or 上.startswith('UNLOCK'):
        return 直接OK(说明='LOCK/UNLOCK（霸下是单写者，收下）')
    if 上.startswith('FLUSH'):
        return 直接OK(说明='FLUSH（霸下每条语句都直接落盘，没有要刷的）')
    raise 翻译错(1064, '认不出的会话语句：%s' % 文[:60])
