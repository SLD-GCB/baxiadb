# -*- coding: utf-8 -*-
"""
霸下语法层 — 目录、执行器、维度映射
====================================

这一层把「语句文本 -> 存储动作」接起来。**它不认得帧**——帧在它上下两头
各有一扇门，两扇门都不经过它：

    内核（龙龟之心）
      └ 语法层（本层）—— 领导：调度数据的增删改查
           ├ 存储层     管理永久存储区          storage
           └ 输入输出层  控制数据的流入流出      io_layer
                └ wire        语句 <-> 帧负载的字节
                └ socket_layer  帧 <-> socket
                └ kernel        帧 <-> 字节层

    本层底下那三层（由存储层间接管着）：
      store_format  字节怎么摆
      mortise       内存怎么缓冲
      arena         区域怎么管

**收文本、回 `Result`，就这两样。** 帧进来要解负载、要卡批的大小、要
把结果编成帧——全是输入输出层的活。

本层自己扛的三件事：

  一、目录。表和列的定义存在库里（`\\x00t` + 表名），不是纯内存。
      所以进程重启之后 `查询 * 从 网页` 还能跑——一个每次重启都要
      重新注册一遍模式的库，没法当库用。同时留了 Python 侧注册的口子
      （`register`），两种来源共存。

  二、维度映射。引擎的 `put(key, value, dim)` 收了 dim，但只拿它记一个
      区的上界，而且只增不减。真正能用来裁查询的区间在**本层**：每条
      记录写进来时带着维度值，本层知道哪个区里有哪些维度。

      **只封区才记账，活跃区永远不记。** 活跃区的维度区间每一刻都在变，
      记下来就是一份注定会过期的元数据；而按过期区间裁剪会漏掉整整
      一个区——静默地漏，最难查的那种。没有区间记录的区一律算候选，
      代价只是多扫一块盘。

  三、行。一条记录 = 主键编码成键 + 各列编码成值。删除是往同一个键上
      写一个墓碑值——**引擎不认识"删除"**，它只看到一次普通写入，
      那正是它该看到的。
"""

import functools
import json
import re
import struct

import arena as arena_mod
import schema
import sql
import store_format as sf
import user as user_mod
from schema import BLOB, FLOAT, INT, TEXT, TIME

__all__ = ['Syntax', 'StatementError', 'Result']

_NULL_VAL = object()      # 「算不出字面量」和「字面量是空」是两件事

# 标量函数的**规范名 -> 中文名**，只在报错和起列名时用。
# 直接引 `sql` 那张表，不在这儿抄一份：抄一份的话，将来加了函数只改一处，
# 另一处会以"报错时说出一个不存在的名字"的样子烂掉。
_SCALAR_CN = sql._SCALAR_NAMES

# 一行编码之后的上限。分片是按 1MB 切的，但**切不了一行**——一行太大
# 就整行进一个帧，于是帧的负载会顶穿协议那边 16MB 的硬上限。在这里拦，
# 报出来的话能指到具体是哪一行；让它在帧编码里爆，就只剩一句
# "负载超限"，谁也不到是哪条记录干的。
MAX_ROW_BYTES = 8 << 20

# 一条 SQL_BATCH 里最多几条，**这个是帧的规矩，已经跟着帧搬去 `io_layer`
# 了**（`io_layer.MAX_BATCH_STATEMENTS`）。帧里能塞几条是协议的事，不是
# 语句的事：本层的 `execute_batch` 收的是 Python 列表，没有"一个帧"可谈。


class StatementError(Exception):
    """
    读懂了，但做不到：表不存在、主键重复、维度是负数……

    和 `sql.SqlError` 分家是因为这两类错的**责任方不一样**：SqlError 是
    写的人话没说对，StatementError 是话说对了但这个库给不了。
    """


# ── 结果集 ──────────────────────────────────────────────────────────

class Result(object):
    """
    一次执行的结果：列、行、影响行数、错误。

    `rows` 里每一格是 `(类型号, 值)`，值为 None 表示空。**格子留着类型**，
    是因为同一个位置这次是空、下次是数，客户端得知道该按什么解；只给
    渲染好的文本的话，空值连类型都没有了。
    """

    __slots__ = ('columns', 'rows', 'affected', 'error', 'statement')

    def __init__(self, columns=None, rows=None, affected=0, error=None,
                 statement=None):
        self.columns = columns or []      # [(列名, 类型号)]
        self.rows = rows or []            # [[(类型号, 值), ...], ...]
        self.affected = affected
        self.error = error
        self.statement = statement

    @property
    def ok(self):
        return self.error is None

    def as_lists(self):
        """值渲染成人能看的（二进制仍给 bytes）。空是 None。"""
        return [[None if v is None else schema.render(t, v)
                 for (t, v) in row] for row in self.rows]

    def __len__(self):
        return len(self.rows)

    def __str__(self):
        if self.error is not None:
            return '错误：%s' % self.error
        if not self.columns:
            return '影响 %d 行' % self.affected
        names = [name for name, _ in self.columns]
        body = [[_cell_text(c) for c in row] for row in self.as_lists()]
        widths = [len(n) for n in names]
        for row in body:
            for i, c in enumerate(row):
                if i < len(widths):
                    widths[i] = max(widths[i], len(c))
        out = [' | '.join(n.ljust(widths[i]) for i, n in enumerate(names))]
        out.append('-+-'.join('-' * w for w in widths))
        for row in body:
            out.append(' | '.join(c.ljust(widths[i])
                                  for i, c in enumerate(row)))
        out.append('(%d 行，影响 %d 行)' % (len(self.rows), self.affected))
        return '\n'.join(out)


def _cell_text(v):
    if v is None:
        return '空'
    if isinstance(v, bytes):
        # 二进制直接打印会往终端里灌控制字符，连终端状态都能改掉。
        # 截断 + 转义，看得到长度和开头就够了。
        if len(v) > 24:
            return '<%d 字节 %s…>' % (len(v), v[:16].hex())
        return '<%d 字节 %s>' % (len(v), v.hex())
    return str(v)


# ── 求值上下文 ──────────────────────────────────────────────────────

class _Ctx(object):
    """一条语句执行期间的固定东西：数据源、参数、列解析的记忆。"""

    __slots__ = ('syn', 'sources', 'params', 'memo', 'subq')

    def __init__(self, syn, sources, params):
        self.syn = syn
        self.sources = sources          # [(别名, Table), ...]
        self.params = params
        self.memo = {}                  # (限定名, 列名) -> (别名, Column)
        # id(子查询的 Select) -> (列类型表, 值表)。见 `Syntax._subquery`。
        # 单独一个字典，不跟 memo 挤 —— memo 的键是 (限定名, 列名) 这样的
        # 字符串对，两套键混在一个字典里，哪天键长得像了就互相顶掉。
        self.subq = {}


# ── 小工具 ──────────────────────────────────────────────────────────

_LIKE_CACHE = {}


def _like_regex(pattern):
    hit = _LIKE_CACHE.get(pattern)
    if hit is not None:
        return hit
    out = ['^']
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == '\\' and i + 1 < n:
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if c == '%':
            out.append('.*')
        elif c == '_':
            out.append('.')
        else:
            out.append(re.escape(c))
        i += 1
    out.append('$')
    rx = re.compile(''.join(out), re.S)
    # 不给上限的话，每条不同的 LIKE 都会留一份编译结果，一个刷查询的
    # 客户端能把内存灌满。满了整个丢掉再来——比 LRU 简单，而这里模式
    # 串的重复率极高，丢一次代价很小。
    if len(_LIKE_CACHE) > 512:
        _LIKE_CACHE.clear()
    _LIKE_CACHE[pattern] = rx
    return rx


def _truthy(val):
    """条件求值的结果 -> 真/假。空算假。"""
    if val is None:
        return False
    t, v = val
    if v is None:
        return False
    if t in (INT, TIME):
        return v != 0
    if t == FLOAT:
        return v != 0.0
    if t == TEXT:
        return v != ''
    return bool(v)


def _numeric(val):
    """取数值。取不出来返回 None（空也返回 None）。"""
    if val is None:
        return None
    t, v = val
    if v is None:
        return None
    if t in (INT, TIME, FLOAT):
        return v
    if t == TEXT:
        try:
            return int(v, 10)
        except ValueError:
            try:
                return float(v)
            except ValueError:
                return None
    return None


def _numeric_type(val):
    if val is None:
        return INT
    return val[0]


def _as_int(v, what):
    """
    `截取`/`四舍五入` 那种"要个位数的参数"取值。**给了小数就报**，
    不悄悄 `int()` 掉 —— `截取(名, 1.5, 2)` 静默变成从第 1 个字符起，
    写的人根本不知道自己写错了。
    """
    if isinstance(v, bool) or not isinstance(v, int):
        raise StatementError('%s 要是整数，这里给的是 %r' % (what, v))
    return v


def _fit_int(r):
    """整数运算的结果落回 64 位里。越界就报，不截断成一个假数。"""
    if not schema.INT64_MIN <= r <= schema.INT64_MAX:
        raise StatementError('整数运算结果越界（64 位有符号）')
    return int(r)


def _scalar_type(name, arg_types, nargs):
    """
    标量函数的结果类型。`_result_columns` 拿它填**列头**。

    必须补上。留空的话列头一律退回 TEXT（见 `_result_columns` 最后那句），
    于是 `查询 长度(姓名)` 会声明成文本列 —— 格子自己带着类型能对上，
    列头对不上。这一列要是全空，客户端拿到的就是"一列空文本"，
    而它本来该是"一列空整数"。**格子的类型和列头的类型是两件事，
    对不上的时候客户端只能挑一个信。**
    """
    t0 = arg_types[0] if arg_types else None
    if name == 'LENGTH':
        return INT
    if name in ('UPPER', 'LOWER', 'TRIM', 'TYPEOF'):
        return TEXT
    if name == 'NOW':
        return TIME
    if name == 'SUBSTR':
        # 文本切出来还是文本，二进制切出来还是二进制
        return t0 if t0 in (TEXT, BLOB) else None
    if name == 'ABS':
        # 整数取绝对值还是整数。**这条不能猜成 FLOAT**——类型一变，
        # 列头跟着变，客户端解出来就是另一个东西。
        return t0 if t0 in (INT, TIME, FLOAT) else None
    if name == 'CONCAT':
        return BLOB if BLOB in arg_types else TEXT
    if name == 'REPLACE':
        return BLOB if BLOB in arg_types else (
            TEXT if all(t == TEXT for t in arg_types) else None)
    if name == 'ROUND':
        # 整数、且没写小数位 → 还是整数（和 `_scalar` 里那条一致）
        if nargs == 1 and t0 in (INT, TIME):
            return INT
        return FLOAT
    return None


def _param_type(v):
    if isinstance(v, bool):
        return INT
    if isinstance(v, int):
        return INT
    if isinstance(v, float):
        return FLOAT
    if isinstance(v, (bytes, bytearray, memoryview)):
        return BLOB
    return TEXT


def _apply_cmp(op, c):
    if op == '=':
        return c == 0
    if op == '!=':
        return c != 0
    if op == '<':
        return c < 0
    if op == '<=':
        return c <= 0
    if op == '>':
        return c > 0
    if op == '>=':
        return c >= 0
    raise StatementError('认不出的比较符 %s' % op)


def _hashable(v):
    return tuple(v) if isinstance(v, list) else v


# ── 谓词里的类型对齐 ────────────────────────────────────────────────
#
# `其中 <列> <比较> <常数>` 这条路，两边类型不同时**必须按列的类型把常数
# 转过来**。不转就是静默的错答案，而且是两个方向都坏：
#
#     `其中 抓取时间 > '2099-01-01'`  -> 恒真（看着像有数据）
#     `其中 抓取时间 = '2026-09-13'`  -> 恒假（看着像数据没了）
#
# 原因是 `compare` 兜底那一条（`schema.compare` 末尾）：类型不同又不可比
# 时**按类型号定序**（TIME=5 > TEXT=3）。那是给 `排序`/`去重`/`分组` 用的
# 全序 —— "空比谁都大"是排得出来的必要条件，那里不能动。但谓词问的是
# "这个值等于/大于那个值吗"，拿一个"稳定但随便"的次序去答，答案就是错的。
#
# **尺子只有一把**：`schema.coerce`，就是 `插入` 用的那个。`插入 … 值
# (…, '2026-09-13')` 收得下的写法，`其中 抓取时间 = '2026-09-13'` 也得
# 认得 —— 同一个字符串写进去是毫秒、查出来不匹配，正是本项目最怕的形状。
#
# **`_dim_window` 走的是同一个 `_coerce_cached`**，两边不许各转各的：
# 窗口和行过滤对同一个字面量必须给出同一个解释，否则窗口会把行过滤认得的
# 行裁到外面去 —— 那是**静默漏行**，本项目最怕的第二种形状。

_PRED_CACHE = {}
_PRED_MISS = object()

# 这三类值 `compare` 本来就比得了（走同一个数值分支）。所以它们之间
# **一律不转**：`其中 整数列 > 1.5` 是一条完全正常的查询，转成整数列会
# 撞上"整数列不收小数"而报错 —— 那是把一个好查询改坏了。
_NUMERIC_FAMILY = (schema.INT, schema.FLOAT, schema.TIME)


def _pred_comparable(ta, tb):
    """这两类值 `compare` 本来就比得出结果吗。"""
    return ta == tb or (ta in _NUMERIC_FAMILY and tb in _NUMERIC_FAMILY)


def _coerce_cached(t, v):
    """
    `schema.coerce` 加一层缓存。

    这是热路径：`_eval` 每一行都要走一次，而 `coerce` 对一个时间字符串
    走的是 `strptime`。一条查询里同一个字面量只该被解释一次。

    **键里必须带类型**：同一个 `'2026-09-13'` 拿去比时间列和拿去比文本列
    转出来是两个东西，只按值缓存会串味。
    """
    try:
        key = (t, v)
        hit = _PRED_CACHE.get(key, _PRED_MISS)
    except TypeError:            # 不可哈希的值，不缓存
        return schema.coerce(t, v)
    if hit is not _PRED_MISS:
        return hit
    out = schema.coerce(t, v)
    _PRED_CACHE[key] = out
    return out


def _pred_value(col, v):
    """
    常数 `v` 按 `col` 这一列的类型转过来，返回 `(类型, 值)`。转不了就抛。

    **不将就**：把 `'abc'` 硬当成时间、或者干脆不比直接给个"没有"，两种
    都比报错难查得多 —— 前者存下一个错的值，后者看着像"数据本来就没有"。
    """
    try:
        return (col.type, _coerce_cached(col.type, v))
    except schema.SchemaError as exc:
        raise StatementError(
            '拿 %r 去比 %s 列比不了：%s（%s 列收的是%s）'
            % (v, col.name, exc, col.name,
               schema.TYPE_NAMES.get(col.type, '?')))


def _dedupe(rows, key):
    """按 `key` 去重，保留第一次出现的那个。和 `去重` 用的是同一判断。"""
    seen = set()
    out = []
    for r in rows:
        k = key(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


# 报错时说人话的词。`op` 是解析层给的规范名，别把 UNION 直接甩给用户。
_SETOP_NAMES = {'UNION': '并集', 'INTERSECT': '交集', 'EXCEPT': '差集'}


def _walk(node):
    """
    把一个表达式里所有 tuple 节点翻出来。

    **子查询是边界。** 它有自己的数据源和一套自己的分组/排序，对**外层**
    来说只是个不透明节点。它要是个能翻进去的东西，下面三件事全会错：

      - `查询 (查询 计数(*) 从 u) 从 t`：子查询里的 `计数(*)` 会被
        `_find_agg` 认成外层的聚合，外层整条查询**悄悄从逐行变成按组**，
        三行输出变成一行——而那一行看着还挺合理；
      - `_value_of` 那句"列不在 分组 里"会把子查询里的列当成外层的列；
      - `_reject_cols`（插入的值、维度参数）会把它当成"引用了列"。

    **今天它挡不住什么，因为 `('subq', 查询)` 的载荷是个语句对象，而
    这上面只翻 tuple 和 list。** 留着是为了把这份"翻不进去"写成明话：
    哪天有人想给 `_walk` 加上"对象也翻它的 __slots__"，这三条会一起
    复活，而且都是安静的那种。要数子查询里的 `?` 请走
    `sql.count_params`——那个**会**下去，子查询里的 `?` 也是参数。
    """
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, tuple):
            yield item
            if item and item[0] == 'subq':
                continue
            stack.extend(item)
        elif isinstance(item, list):
            stack.extend(item)


def _has_col(node):
    for t in _walk(node):
        if t and t[0] == 'col':
            return True
    return False


def _find_agg(e):
    for t in _walk(e):
        if t and t[0] == 'call':
            return t
    return None


def _freeze(e):
    """
    表达式 -> 可哈希的等价物。

    聚合调用是 `('call', 名字, [参数...], 去重)`，中间那层是 **list**，
    所以整个元组不可哈希——不能直接拿来当字典的键。列表换成元组就行，
    而结构相同就是同一件事，正是分组成员表要的那个判断。
    """
    if isinstance(e, (tuple, list)):
        return tuple(_freeze(x) for x in e)
    return e


def _collect_aggs(e, out, seen=None):
    if seen is None:
        seen = set()
    for t in _walk(e):
        if t and t[0] == 'call':
            k = _freeze(t)
            if k not in seen:
                seen.add(k)
                out.append(t)


def _any_agg(exprs):
    return any(_find_agg(e) is not None for e in exprs if e is not None)


def _expr_name(e):
    """表达式 -> 结果集里的列名。只求能读，不求好看。"""
    if not isinstance(e, tuple) or not e:
        return '?'
    k = e[0]
    if k == 'col':
        return e[2] if e[1] is None else '%s.%s' % (e[1], e[2])
    if k == 'lit':
        return str(schema.render(e[1], e[2]))
    if k == 'param':
        return '?%d' % (e[1] + 1)
    if k == 'star':
        return '*' if e[1] is None else '%s.*' % e[1]
    if k == 'call':
        inner = ', '.join(_expr_name(a) for a in e[2])
        return '%s(%s)' % (e[1], inner)
    if k == 'func':
        # 列名给**中文名**。客户端拿它当列头显示，`LENGTH(姓名)` 这种
        # 英文函数名夹在中文列名里很扎眼，而写的人用的是中文写法。
        cn = _SCALAR_CN.get(e[1], e[1])
        return '%s(%s)' % (cn, ', '.join(_expr_name(a) for a in e[2:]))
    if k == 'case':
        # 列头照写法的样子给：`当 状态 = 1 那么 '一' 否则 '其他' 结束`。
        # 太长的话客户端自己会截，这里不截 —— 截了就没法分辨是哪一句。
        bits = ['%s 那么 %s' % (_expr_name(b[1]), _expr_name(b[2]))
                for b in e[1]]
        if e[2] is not None:
            bits.append('否则 %s' % _expr_name(e[2]))
        return '当 %s 结束' % ' '.join(bits)
    if k == 'arith':
        return '%s %s %s' % (_expr_name(e[2]), e[1], _expr_name(e[3]))
    if k == 'neg':
        return '-%s' % _expr_name(e[1])
    if k == 'nullval':
        return '空'
    if k == 'default':
        return '默认'
    return k


# ── 帧编解码已经搬走 ──────────────────────────────────────────────
#
# **本层不认得帧。** `encode_sql_request` / `decode_sql_request` /
# `encode_result_frames` / `decode_result_frames` 现在在 `wire.py`——
# 服务端那扇门（`io_layer`）和客户端那条腿（`socket_layer.Client`）
# 两边都要用，挂在哪一层名下都是错的。
#
# 这一层收的是**语句文本**，回的是 `Result` 对象。帧怎么摆是线上怎么摆，
# 跟盘上怎么摆是两件事，两者的变更节奏完全不同。


# ── 主类 ────────────────────────────────────────────────────────────

class Syntax(object):
    """
    一个语法层实例 = 一个存储区 + 一份目录 + 一份维度映射。

    **单写者。** 和 Arena 一样，本类不做任何加锁——同一个存储区在一个
    进程里只该有一个 Syntax。别在这里加锁：锁一进来，"谁在什么时候
    看到哪个区"就变成了隐式约定，比现在这个显式约束难查得多。
    """

    def __init__(self, root=None, config=None, arena=None, 密文=None,
                 max_result_rows=200000, max_scan_rows=2000000,
                 max_join_rows=100000, max_setop_rows=100000):
        if arena is not None:
            self.arena = arena
        else:
            if root is None:
                raise StatementError('要么给 root，要么给一个现成的 arena')
            # `密文` 一路透传给 Arena（`none` 就是今天那个明文库）。
            # **语法层不碰它、不看它、不知道它是什么**——它只是这一路
            # 装配里的一个过路参数，就像 `config` 一样。
            self.arena = arena_mod.Arena(root, config, 密文=密文)

        # 结果集、扫描、连接三个上限。**不是防坏人，是防自己**：一条
        # 忘了写 限制 的查询会把整张表拉进内存，而这里没有任何一处会
        # 先问一句"这表多大"。超了就报错，报的错里带上该怎么改。
        self.max_result_rows = max_result_rows
        self.max_scan_rows = max_scan_rows
        self.max_join_rows = max_join_rows
        # 集合运算两边的行数之和的上限。**这个是新加的不是复用的**：
        # 连接的上限管的是"右表整张拉进来"，而 并集 要去重，拉进来的是
        # **两边之和**，共用同一个数会让并集比连接早一倍就喊停，
        # 而那声喊停会显得莫名其妙。
        self.max_setop_rows = max_setop_rows

        self.tables = {}          # 表名 -> Table
        self._by_id = {}          # 表号 -> Table
        self._dim = {}            # (表号, 区号) -> (低, 高)，已封区的区间
        self._live = {}           # (表号, 区号) -> [低, 高]，只对没封的区
        self._sealing = False
        self._next_table_id = 1
        self._loaded = False
        # 用户名 -> `user.User`。**和 `tables` 平行的另一本册子**：表的
        # 记录在 `\x00t` 上，用户的在 `\x00u` 上，行在 `\x01` 上，三样
        # 互不遮挡（见 `schema.user_key`）。
        self.users = {}
        # 在 `批量 开始` 和 `批量 提交` 之间。期间**不落盘**：写还是照写
        # （同一个 Arena 里自己看得见），就是不给它封区、不推缓冲层。
        #
        # **按会话记，不是一个布尔。** 从前这里是个 `_in_batch` 布尔，挂在
        # 这个进程级的 `Syntax` 上，而服务端是多连接共用一个 `Syntax` 的：
        # A 发一句 `批量 开始`，B 的所有写就跟着不落盘了，B 那边一行都没
        # 收到任何动静 —— 而 B 以为自己的写已经落盘。现在按 `身份.会话号`
        # 分开记，各人的批各人管。本地腿只有一个会话，行为跟从前一样。
        self._批会话 = set()
        # 当前这条语句是谁发的。**只在 `_run` 里活着**：进去设、`finally`
        # 复位。见 `_run` 里那段说明——它是"一次一个调用者"这条前提换来的，
        # 本层不做并发，服务端那边一个请求跑到底（`transport` 是单线程
        # 事件循环），所以这个游标够用。
        self._身份 = None

    # ── 生命周期 ────────────────────────────────────────────────────

    def create(self):
        self.arena.create()
        self.tables = {}
        self._by_id = {}
        self._dim = {}
        self._live = {}
        self._next_table_id = 1
        # 新库**一个用户都没有**。这不是"没配权限"，是"还没设管理员"——
        # 光凭这一条，`龙龟之心.起服务` 就拒绝对外开端口（见那里的说明），
        # 因为没有用户的库里，"谁能管用户"这个问题没有答案，而默认答案
        # （谁都行）正是最坏的那个。
        self.users = {}
        self._loaded = True
        self._write_catalog()
        return self

    def open(self):
        self.arena.open()
        self._load_catalog()
        self._seed_stale_active()
        return self

    def _seed_stale_active(self):
        """
        开库时，把"已经被别人写过"的活跃区在**每张表**上标成松散。

        `_live` 只由本进程的 `_note_dim` 喂，开库时是空的。于是上个进程
        写进活跃区的那批行，维度账上根本不存在——等这个区封掉，
        `_drain_closed` 记下的是**本进程那批行**的区间，比真实的小一圈。
        之后任何一条落在那一圈之外的带维度查询，都会把这个区整个裁掉，
        而它的输出看着完全正常。**这是静默丢行，不是慢。**

        开库时那个活跃区里只要已经有记录，就说明它上面的账本层已经接不
        上了——标成松散，那个区此后一律算候选。代价是每次查询多扫一个
        区，换掉一条会丢行的路。
        """
        rid = self.arena.active_region
        if rid is None:
            return
        info = [i for i in self.arena.regions() if i['region_id'] == rid]
        if not info or not info[0]['records']:
            return
        for tid in self._by_id:
            self._note_dim(tid, rid, None, loose=True)

    def close(self):
        """
        关库。

        **还在批里就拒绝关。** 关库必然要落盘 —— `arena.close()` 走的是
        完整的榫卯次序，那是最后一次落盘机会，也是这个引擎刻意留的
        "关机别丢东西"的路。可用户写着 `批量 开始` 就是明说了"这些先别
        算数"。两种做法都是替他拿主意：顺手提交了，他以为没提交；丢掉
        了，他以为还在。

        所以不选，让他说。`批量 提交` 再关，或者把批里那些撤掉再关 ——
        撤的路本库还没有，那就是"先把该提交的提交掉"。

        **有意不记活跃区的维度区间。** 关库不是封区：进程停在这里，
        活跃区并没有变成"不会再有新维度"，下次开库照样往里写。顺手
        记一个截至今的区间，就成了那份"注定过期"的元数据。
        """
        if self._批会话:
            raise StatementError(
                '还有会话停在 `批量 开始` 和 `批量 提交` 之间，现在关库会'
                '替它决定那些写算不算数。先让那边的 `批量 提交` 跑掉'
                '（本库没有回滚，攒下的写只能提交）')
        self.arena.close()

    def _flush(self, force=False, 会话号=None):
        """
        落盘。**批里不落。** 批的收益全在"少落几次盘"上，所以批里连
        每个 `execute_batch` 末尾那次 force 也要压住。

        **只压自己那个会话的批。** 从前这里看的是一个进程级的布尔，
        于是 A 的批把 B 的写一起按住了——B 那边一点动静都没有，而 B
        以为落过盘了。`会话号` 不给（`None`）时**照落**：多落一次盘
        最多是慢，少落一次是丢数据，两边的代价不对称。
        """
        if 会话号 is not None and 会话号 in self._批会话:
            return
        self.arena.flush(force=force)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # 以前这里有个 `kernel()`，借给字节门一把"装着 Arena 的钥匙"。**它删了**：
    # 字节门搬进输入输出层之后，手里拿到的是**整个 `Syntax`**（见
    # `io_layer.byte_handler`），走的是下面那几个口子。留着这个法子就是
    # 留了第二条够得着 Arena 的路——而"一个存储区只能有一条路"正是这次
    # 搬家要办的事。

    # ── 够存储区用的那几个口子 ──────────────────────────────────────
    # 存储层的职分是"管永久存储区"，可存储区是**本层**开的（见 `__init__`），
    # 所以它那几个动作在这里转一手。**这不是一叠转发壳子**：下面每一处都
    # 夹着本层的账，绕过去账就错——而账错的样子是"结果看着正常，只是
    # 少扫/少记"，不会报错。

    def put(self, key, value=b'', dim=None):
        """
        直存。键和值都是**不透明字节**——引擎不解释它们。

        **走 `_put`，不走 `arena.put`。** `_put` 是本层所有写入的唯一
        收口，也是唯一会注意到"区换号了"的地方。直通 `arena` 的话，区
        封掉了本层的区间记账却不知道，要等**下一次带表的写**顺路补——
        中间这段时间那个区在账上等于没有。而账上"没有"还会被读成一件事
        （见 `_dim_range`：查不到记录的区一律算候选），所以是慢，不是错。
        但那条"慢"是本层自己造出来的，不该有。
        """
        try:
            return self._put(key, value, dim)
        except arena_mod.ArenaError as exc:
            # **引擎的拒绝在本层收成自己的话。** `_run` 早就这么干了
            # （见它那个 `except (…, arena_mod.ArenaError)`），这里只是把
            # 同一个口径补到裸口上：字节门在**输入输出层**，它不碰盘、
            # 认不得 `ArenaError`，也不该认得——让它去 import 存储引擎的
            # 异常类，就是给那条"输入输出层不认得盘"的界线开口子。
            raise StatementError(str(exc))

    def get(self, key):
        """直取。取不到回 None（**不是空串**——空串是一个真实的值）。"""
        return self.arena.get(key)

    def scan(self, cursor=None, limit=256, regions=None):
        """扫一页。`regions` 给了就只扫那几个区——维度裁剪落地的口子。"""
        return self.arena.scan(cursor, limit, regions)

    def regions(self):
        """这个永久存储区里有哪些区、各是什么状态（活/封存、记录数、维度界）。"""
        return self.arena.regions()

    def seal(self):
        """
        封存当前活跃区，返回区号（本来就没有活跃区时回 None）。

        **封完必须补一下 `_drain_closed`。** 封存是"这个区不会再变"的
        那个时刻，也就是维度区间第一次能定下来的时刻——区间只有封存区
        才作数（活跃区的区间每一刻都在变，记下来就是一份注定过期的元数据）。

        自动封区那条路是 `_put` 发现"区换号了"顺手记的。可**用户自己
        喊封存时区是被换掉的，`_put` 从头到尾没被调到**——不补这一下，
        这个区的区间就永远躺在内存的 `_live` 里，下次开库查不到记录，
        一律算候选：还是慢，不是错，但这一区白扫了。
        """
        rid = self.arena.seal_active()
        if rid is not None and not self._sealing:
            self._drain_closed(rid)
        return rid

    def flush(self, force=True, sync=False):
        """
        落盘。`force` 为假时只在攒够一批（4MB 或 1 秒）才真写。

        **这里是显式喊落盘，所以批里也落。** 本层的 `_flush` 会压住批内
        落盘（批的收益全在少落几次上），但那个压的是 `execute_batch` 末尾
        那次自动落，跟"人喊了一句落盘"不是一件事。
        """
        return self.arena.flush(force=force, sync=sync)

    def verify(self):
        """巡检一整个永久存储区。返回问题列表，空列表表示健康。"""
        return self.arena.verify()

    def stats(self):
        """
        引擎和缓冲层的数（命中率、淘汰、脏字节）。**不含表数**——那是
        存储层在自己 `stats` 里往上拼的。
        """
        return self.arena.stats()

    # ── 目录 ────────────────────────────────────────────────────────

    def _write_catalog(self):
        """
        目录。**用户名清单在这里，用户记录不在**——和表一个路子：
        目录里存名字，`\\x00u` 上存这个人本身。

        清单不是多余的：`arena.scan` 不认前缀、一开口就是全库的键，
        "列出所有用户"要是靠扫，就得把整个库扫一遍才认得出一两条用户
        记录。有了清单就是**按名点查**，和开库读表定义是同一个形状。
        """
        blob = json.dumps({
            'v': 1,
            'next_table_id': self._next_table_id,
            'tables': sorted(self.tables),
            'users': sorted(self.users),
        }, ensure_ascii=False, sort_keys=True).encode('utf-8')
        self._put(schema.catalog_key(), blob)

    def _load_catalog(self):
        self.tables = {}
        self._by_id = {}
        self._dim = {}
        self._live = {}
        self.users = {}
        raw = self.arena.get(schema.catalog_key())
        if raw is None:
            # 库是新的（或者上次建到一半）。**不当作错误**——建库和写
            # 目录之间崩掉，会留下一个空库，那是个能接着用的东西。
            self._next_table_id = 1
            self._loaded = True
            return
        d = json.loads(raw.decode('utf-8'))
        if d.get('v') != 1:
            raise StatementError('目录的版本认不出：%r（这个库可能来自'
                                 '更新的版本）' % d.get('v'))
        self._next_table_id = d.get('next_table_id', 1)
        for name in d.get('tables', []):
            blob = self.arena.get(schema.table_key(name))
            if blob is None:
                # 目录点了名，表的定义却不在。**这是个真问题，要报。**
                # 装作没有这张表的话，后面一句 建表 会建出一张同名但
                # 结构完全不同的新表，而老行还在盘上、还认得出旧表号，
                # 于是同一个名字下同时活着两套行——谁也看不出来。
                raise StatementError(
                    '目录里有表 %s，但它的定义不见了。这个库需要修复'
                    '（表定义该在键 %r 上）' % (name, schema.table_key(name)))
            tbl = schema.Table.from_json(blob)
            self.tables[tbl.name] = tbl
            self._by_id[tbl.table_id] = tbl
        for name in d.get('users', []):
            blob = self.arena.get(schema.user_key(name))
            if blob is None:
                # 和上面表定义那一条**同一个道理**，所以走同一条规矩：
                # 目录点了名、记录不在，就是库坏了，报出来。
                #
                # 这里比表那条更不该通融：静默跳过的话，那个人**从库里
                # 消失了**——而他的行还在（用户没有行，可他建的表、他
                # 授过的权都还在，`grant` 指着他。跳过之后一切照常跑，
                # 只是没人再拦得住任何人了）。
                raise StatementError(
                    '目录里有用户 %s，但它的记录不见了。这个库需要修复'
                    '（用户记录该在键 %r 上）'
                    % (name, schema.user_key(name)))
            self.users[name] = user_mod.反序列化(name, blob)
        self._loaded = True

    def _write_table_record(self, tbl):
        """只写表的定义，**不碰目录**。改名要分两步落地时用得上（见
        `_rename_index`）：定义先按新名字落盘，名字后进目录。"""
        self._put(schema.table_key(tbl.name), tbl.to_json().encode('utf-8'))

    def _persist_table(self, tbl):
        self._write_table_record(tbl)
        self._write_catalog()

    # `handle` 已经不在这里了。帧入口归**输入输出层**：那边解帧、
    # 卡批的上限和"批里不拆"，然后调下面的 `execute_batch`，再编码回帧。
    # 本层只收文本、只回 `Result`——收帧的话，一条协议改动就要动语法层，
    # 而这两件事的变更节奏完全不同。

    def register(self, table):
        """
        Python 侧的模式来源：直接把一个 `schema.Table` 挂进目录。

        和 `建表` 走的是同一条落盘路径，所以注册过一次之后，下次开库
        不需要再注册。**不是"内存里的临时表"**——先注册再建表的那种
        双轨制会让同一个名字底下有两份定义，而它们迟早会不一致。
        """
        if not self._loaded:
            self.open()
        old = self.tables.get(table.name)
        if old is not None:
            if old.to_json() != table.to_json():
                raise StatementError(
                    '表 %s 已经在库里了，而且定义不一样。库里的：%s；'
                    '现在给的：%s。要改结构请用 改表'
                    % (table.name, old.describe(), table.describe()))
            return old
        if table.table_id < self._next_table_id:
            raise StatementError(
                '表 %s 想要表号 %d，但库里已经发到 %d 了。表号不能重发'
                '——重发的表号会和一张已经删掉的表撞上行键，那些行还躺在盘上'
                % (table.name, table.table_id, self._next_table_id))
        self.tables[table.name] = table
        self._by_id[table.table_id] = table
        self._next_table_id = table.table_id + 1
        self._persist_table(table)
        return table

    # ── 用户存取 ────────────────────────────────────────────────────
    #
    # 用户的**记录**在 `\x00u` 上，**名字清单**在目录里——和表一个路子。
    # 两处都要动，次序照 `_drop_table_named` 那条：先改目录、后写墓碑，
    # 崩在中间留下的是"目录里没有、记录还活着"（没人引用的垃圾），反过来
    # 就是开库时报"目录点了名、记录不见了"的库坏。

    def _persist_user(self, user):
        self._put(schema.user_key(user.name),
                  user_mod.序列化(user))
        self._write_catalog()

    def 有用户(self):
        """库里有没有用户。**起服务前必查这一条**（见 `龙龟之心.起服务`）。"""
        if not self._loaded:
            self.open()
        return bool(self.users)

    def 设初始管理员(self, 名字, 口令):
        """
        建库后设第一个管理员。**只能在库里一个用户都没有的时候用。**

        **它不是一个 SQL 语句，这是有意的。** `建用户` 要 `管用户` 权限，
        而第一个管理员没有地方去拿这个权限——鸡生蛋。开一个"没有用户时
        谁都能建用户"的口子也不行：那条口子开在**语句**上，于是任何能连上
        端口的人在库刚建好、还没设管理员的那一小段时间里都能给自己发一个
        管理员。做成一个 Python 方法，够得着它的只有本地那一侧。

        （所以 `龙龟之心.起服务` 要拒绝一个没有用户的库：真有哪个部署漏了
        这一步，宁可它不开端口，也不要它开着端口等人来抢管理员。）
        """
        if not self._loaded:
            self.open()
        if self.users:
            raise StatementError(
                '库里已经有用户了，不能再设初始管理员。加人请用 `建用户`')
        user = user_mod.建(名字, 口令, superuser=True)
        self.users[user.name] = user
        self._persist_user(user)
        return user

    def 核对身份(self, 名字, 口令):
        """
        `(用户名, 口令)` -> `用户` 或 `None`。**登录用的只读口子。**

        服务端那扇门（`io_layer.login_handler`）和本地腿（`_LocalLeg.登录`）
        **都走这里**，两侧各读一遍盘是不行的：两条路的口径会分岔，而分岔
        的表现是"本地登得进、远程登不进"，或者更糟——一边比的是明文。

        **认不出的人和口令错的，返回的是同一个 `None`。** 分成两种结果的话，
        调用方迟早会把其中一种写进给用户的提示里，那就等于告诉外面"这个
        用户名是存在的"。
        """
        if not self._loaded:
            self.open()
        user = self.users.get((名字 or '').strip())
        if user is None:
            return None
        if not user_mod.核对(user, 口令):
            return None
        return user

    def _当前用户(self):
        """当前这条语句是谁发的。**没身份就是 `None`，一律没权限**——
        见 `user.判权限` 的头一条。"""
        return self._身份.user if self._身份 is not None else None

    def _批会话号(self):
        """
        当前这条语句来自哪个会话。批状态按它分开记。

        没身份时给 `user.本地会话号`（负数）。本地腿在 `Gate.登录` 之前
        就是没身份的，而本地只有一个调用者，所以那一个固定号就够了。
        **它必须和远程的会话号不撞**——撞上的表现是"A 的批把 B 的写按住
        了"，正是这次要治的毛病换个地方复发。
        """
        if self._身份 is not None:
            return self._身份.会话号
        return user_mod.本地会话号

    def _没有这张表(self, name):
        """
        "没有这张表"那句话。**两处都走这一个函数**，这是它存在的全部
        理由：权限不够和真的不存在，**必须给出一模一样的字符串**。

        不这么做的话，一个被拒的用户只要挨个试表名、看回复里有没有那句
        "库里现在有：…"，就能把整库的表名一张一张问出来——而他现在
        连一张表都读不了，这正是权限该挡住的事。

        清单按**可读**过滤（`user.可读`），所以念出来的永远是他本来就
        看得见的那些。名字本身要排除掉：不给权限的用户去查一张他能插
        但读不了的（`_table(…, '读')` 会拒他）表名，而那句话里正列着
        这张表的名字——"没有 订单 这张表，库里现在有：订单"，这种自相
        矛盾的说法比不报还糟。对超管来说 `name` 本来就不在库里的可读
        清单里（真不存在才走到这儿），所以超管拿到的这句话和后端没做
        权限之前**逐字节相同**。
        """
        user = self._当前用户()
        if not self.tables:
            raise StatementError('库里一张表都没有，先 建表')
        plain = sorted(n for n, t in self.tables.items()
                       if not t.index_of and n != name
                       and user_mod.可读(user, n))
        hidden = sum(1 for t in self.tables.values() if t.index_of)
        hint = '（另有 %d 张索引表没列出来，用 `显示 索引 <表名>` 看）' \
            % hidden if hidden else ''
        raise StatementError('没有 %s 这张表。库里现在有：%s%s'
                             % (name, '、'.join(plain), hint))

    def _表级拦下(self, name, 档们):
        """
        表级权限没过，该报哪句。**报哪句由"他看不看得见这张表"决定。**

           看得见（这张表真在库里，且 `可读`）→ **明说缺哪个权限**
           看不见                                  → 装成"没这张表"

        为什么不一律装成"没这张表"：那个做法**只对读有意义**。一个只有
        `插` 没 `改` 的人，`显示 表列表` 里就明摆着有这张表、他刚刚还能
        查它，这时回一句"没有 订单 这张表"是自相矛盾的——更糟的是那句话
        以"库里现在有："收尾，而后面的清单把这张表**排除掉了**（见
        `_没有这张表` 里排除 `name` 的理由），于是念出来是半句话。

        为什么不一律明说：那就把存在性探针放回来了。一个读都读不了的人
        挨个试表名，回复里"你没有权限"和"没这张表"一比，整库表名就问
        出来了——而那正是他连一张表都看不见的时候。

        所以分界线是**看得见 / 看不见**，不是"读 / 写"。看得见的人本来
        就知道它存在（`显示 表列表` 给他看的就是这些），跟他说实话不多
        泄一个字；看不见的人从这两句话里分不出任何东西。
        """
        user = self._当前用户()
        tbl = self.tables.get(name)
        if tbl is not None and user_mod.可读(user, name):
            词们 = []
            for w in 档们:
                # `'写'`（兜底那一档）摊成三样报，别把"没有 写 权限"
                # 这种话丢给用户——他要知道的是缺哪一样。
                词们.extend(user_mod.写档 if w == '写'
                            else (user_mod.档到词.get(w) or w,))
            缺 = [w for w in 词们 if not user_mod.判权限(user, w, name)]
            raise StatementError('%s 没有 %s 这张表上的 %s 权限'
                                 % (user.name, name, '、'.join(缺)))
        raise self._没有这张表(name)

    def _按名取表(self, name):
        """按名字取表，**不判权限**。给"权限已经判过"的调用点用。"""
        tbl = self.tables.get(name)
        if tbl is None:
            raise self._没有这张表(name)
        return tbl

    def _过表级关口(self, name, 需要):
        """
        `_table` 的权限那一半：这张表上，当前这个人够不够格干这件事。

        `需要` 的取值：
          - `'读'` / `'插'` / `'改'` / `'删'`：要这张表上对应的那一样；
          - `'写'`：随便哪一样写权限都行。**这是默认值，用途是兜底** ——
            哪个调用点忘了标自己是什么档，得到的是"错拒"（会响、看得见），
            绝不是"静默放行"。所以 `'管'` 绝不能是默认值，它正好是反向的；
          - `'管'`：**不判表级**。给 DDL 那几条用（`改表`/`建索引`/`删索引`）：
            它们要的是库级的 `改表`/`建索引`/`删索引`，那几样本来就不分表
            （见 `user.管表权限`），在这里再要一道表级权限，等于让那几样
            库级权限变成废的。

        **认不出的档当场报。** 悄悄当成放行就是开了个后门，而它只会在
        有人写错字的那天出现。
        """
        user = self._当前用户()
        if 需要 == '管':
            # 库级那道关口已经在 `_run` 里过了，这里只管把名字取出来。
            return True
        if 需要 == '写':
            词们 = user_mod.写档
        else:
            词 = user_mod.档到词.get(需要)
            if 词 is None:
                raise StatementError('认不出的表级档 %r' % (需要,))
            词们 = (词,)
        return any(user_mod.判权限(user, w, name) for w in 词们)

    def _table(self, name, 需要='写'):
        """
        按名字取表。**索引表也取得回来，这里不滤。**

        索引表在 `显示 表列表` 里是藏着的，但"藏起来不给人看"和
        "当它不存在"是两件事，而这两件事混起来就是 S4 那条坑：
        过滤一旦做进这个函数，`建表` 的同名检查就再也看不见索引表，
        人就能建出一张名字跟索引表重名、行键前缀却不同的表 —— 两边
        都读得到，谁也不知道对方在。

        所以：**过滤只许做在 `_show` 里。**

        权限也收在这里，理由同上：这是**表名的唯一解析口**（11 处调用
        全是它），于是 `join`、子查询、以及以后新加的语句形状**自动全
        覆盖**，一行走查都不用写。反过来"在 `_run` 里按 `st.table` 收表
        名"是错的：`_walk` 翻不进 `('subq', …)`，子查询里的表根本不在
        外层节点的 `table` 上，那样收会**静默漏掉 join 和子查询** ——
        不是报错，是越权读。

        **权限判定排在存在性判定前面**，所以一个连表名都看不见的人分不出
        "你没有"和"它没有"（两句由 `_表级拦下` 挑，挑的规矩写在那儿）。
        """
        if not self._过表级关口(name, 需要):
            self._表级拦下(name, (需要,))
        return self._按名取表(name)

    def _writable(self, name, 档, *还要):
        """
        写语句的表入口：**索引表一律拒绝**。

        索引表里的行是从基表推出来的。让 `插入` 直接往里写，那条条目
        跟基表任何一行都不对应 —— 而 `更新` / `删除` 更糟：它删的是
        索引条目，基表那一行还在，于是那行**再也查不出来**了。这是
        最纯的静默漏行：没有报错、没有变慢，就是少了一行。

        要动索引只有两条路：`重建索引`，或者把基表那一行改掉。

        `档` **必填**（它没有默认值，忘了给是 `TypeError`）。这条不是
        啰嗦：这个函数每个调用点都**知道**自己在干哪种写，写出来是一句话
        的事，而默认一个值就等于给"忘了标"留了个位置——那是权限判据，
        不是样式。`还要` 是给 `替换` 用的：它既是插也是改，两样都得有。

        权限那句和 `_table` 走同一个地方挑（`_表级拦下`），所以"看不见的
        表"在两处报的是同一句话——不然从"插不进去"和"查不到"这两句的差别
        里，照样能把库里的表名问出来。
        """
        档们 = (档,) + 还要
        for w in 档们:
            if not self._过表级关口(name, w):
                self._表级拦下(name, 档们)
        tbl = self._按名取表(name)
        if tbl.index_of:
            raise StatementError(
                '表 %s 是 %s 按 (%s) 的索引**表**，不接受 插入/更新/删除。'
                '它的行是从基表推出来的，直接写会和基表对不上；要重做用'
                ' `重建索引 %s 按 (%s)`，要改数据请改基表 %s'
                % (tbl.name, tbl.index_of[0], ', '.join(tbl.index_of[1]),
                   tbl.index_of[0], ', '.join(tbl.index_of[1]), tbl.index_of[0]))
        return tbl

    # ── 写入与维度记账 ──────────────────────────────────────────────

    def _put(self, key, value, dim=None, table_id=None, loose=False):
        """
        本层所有写入都从这里走。**只有这里会注意到"区换了"**——而
        "区换了"正是本层唯一需要引擎告诉它的时刻。
        """
        before = self.arena.active_region
        rid, blk, slot = self.arena.put(key, value, dim)
        if table_id is not None:
            self._note_dim(table_id, rid, dim, loose)
        if before is not None and rid != before and not self._sealing:
            self._drain_closed(before)
        return rid, blk, slot

    def _note_dim(self, table_id, rid, dim, loose=False):
        """
        记下某个表的某一行落在哪个区、带什么维度。

        有两种行会让整个区"松散"（= 每次都算候选）：

        **一、维度是空。** 维度列允许为空，而空值没有大小——它没法进
        任何一个区间。那么这个区封存之后，按有维度的行记出来的区间就
        **不包含**那条空维度的行：之后任何一条带维度条件的查询都会把它
        漏掉，连 `抓取时间 为空` 都查不到它。

        **二、维度是 `维度 <表达式>` 显式给的。** 裁剪拿"维度列上的谓词"
        去比"各区的维度区间"，这一步隐含了**维度列的值就等于维度**。
        用 `维度` 覆盖之后这一条就不成立了：一行的维度可以指向任何地方。
        极端情况下 `插入 … 值 (…, 1000) 维度 999999`，这行按 `时候=1000`
        再也查不出来——它落在按 999999 记账的区里，而查询的窗口是 1000，
        那个区被裁掉了。

        两种都是**静默丢行**：查询结果看着完全正常，只是少了几条，而且
        少的是哪几条没人知道。所以宁可把区标成候选，多扫一点。
        """
        key = (table_id, rid)
        cur = self._live.get(key)
        if cur is None:
            cur = self._live[key] = [None, None, False]
        if loose or dim is None:
            cur[2] = True
        if dim is None:
            return
        if cur[0] is None or dim < cur[0]:
            cur[0] = dim
        if cur[1] is None or dim > cur[1]:
            cur[1] = dim

    def _drain_closed(self, rid):
        """
        一个区封存了：把它里面各表的维度区间写进库。

        `_live` 里可能同时有**好几张表**在这个区里，所以这里处理的是
        一批，不是一条。

        记区间本身也是写，也可能把区写封——所以外面套的是循环不是递归：
        批量插入时这件事每封一个区就发生一次，递归会把栈按区数堆起来。
        循环上限给死，真到了上限就收手：这些区没有区间记录，查询时会被
        当成候选，慢一点，绝不会错。
        """
        self._sealing = True
        try:
            for _ in range(64):
                pending = [k for k in self._live if k[1] == rid]
                if not pending:
                    return
                nxt = None
                for tid, r in pending:
                    lo, hi, loose = self._live.pop((tid, r))
                    self._dim[(tid, r)] = (lo, hi, loose)
                    before = self.arena.active_region
                    # 整区都没有一条带维度的行时 lo/hi 还是 None。**这时候
                    # 得写成整条数轴**，不能往盘上写个 0——0 会把区间说成
                    # "就一个点 0"，那是个纯属编造的区间，比不写还坏。
                    # loose 必然是 True（有行不带维度才会走到这），所以这个
                    # 区无论如何都会被当候选，这里的上下界只是看着别太假。
                    new_rid, _, _ = self.arena.put(
                        schema.dim_key(tid, r),
                        struct.pack('<qqB',
                                    lo if lo is not None else schema.INT64_MIN,
                                    hi if hi is not None else schema.INT64_MAX,
                                    1 if loose else 0))
                    if before is not None and new_rid != before:
                        nxt = before
                if nxt is None:
                    return
                rid = nxt
        finally:
            self._sealing = False

    def _dim_range(self, table_id, rid):
        key = (table_id, rid)
        hit = self._dim.get(key)
        if hit is not None:
            return hit
        raw = self.arena.get(schema.dim_key(table_id, rid))
        if raw is None or len(raw) < 16:
            # 记不起来的区。调用方一律把它算成候选——**这个方向不能反**。
            # 把"不知道"当成"这个区间里没有"，会静默漏掉整整一个区的行，
            # 而且结果看起来完全正常。
            return None
        # 第三个字节是"这个区里有不带维度的行"，老记录（16 字节）没有
        # 这一位，按"没有"读——那时的写入方还没记这一位，而当时能写进去
        # 的都是带维度的行。
        loose = raw[16] if len(raw) > 16 else 0
        rng = (struct.unpack('<qq', raw[:16]) + (bool(loose),))
        self._dim[key] = rng
        return rng

    def _candidates(self, tbl, lo, hi):
        """
        可能含有维度区间 [lo, hi] 的区域号集合。

        **宁可多给，绝不少给。** 活跃区永远入选（它还没记账）；没有区间
        记录的区永远入选；区间上标了"松散"的区永远入选；只有拿得准的
        区间会真的排掉一个区。
        """
        active = self.arena.active_region
        out = set()
        live_states = (sf.RegionState.SEALED, sf.RegionState.ACTIVE)
        for info in self.arena.regions():
            if info['state'] not in live_states:
                continue
            if info['records'] == 0:
                continue
            rid = info['region_id']
            if rid == active:
                out.add(rid)
                continue
            rng = self._dim_range(tbl.table_id, rid)
            if rng is None or rng[2] or (rng[1] >= lo and rng[0] <= hi):
                out.add(rid)
        return out

    # ── 执行入口 ────────────────────────────────────────────────────

    def execute(self, text, params=(), 身份=None):
        """跑一段文本，返回**最后一条**语句的结果。"""
        return self.execute_batch([(text, params)], 身份=身份)[-1]

    def execute_batch(self, statements, one_per_text=False, 身份=None):
        """
        [(文本, 参数), ...] 或 [文本, ...]，一批跑完只落一次盘。

        **批里没有回滚。** 前面几条成了就是成了——这不是事务，只是把
        "N 次落盘"压成"一次落盘"。要的正是这个：批量插入的耗时几乎全在
        落盘次数上，而不回滚是这个代价的另一面。说清楚就行，不能一边
        省着这个钱一边假装有那个保证。

        `one_per_text`：一段文本里只许有一句，多了就报错、不执行。
        **这是帧那条路的规矩**（一帧里 N 条文本 -> N 个结果，帧和语句要
        按序对得上，拆了就错位），由输入输出层传进来。为什么这个开关在
        这里而不在输入输出层自己判：**判它要先解析**，而在外面解析一遍、
        进来再解析一遍，批的每一句就白解了一次——批量插入这条路上那是
        实打实的两倍解析开销。

        **报错的那条文本不会被执行**：`one_per_text` 判在前面，坏的那条
        只产出一个错误结果，同一批里别的照跑（和批里其他错一个待遇）。
        """
        out = []
        for item in statements:
            if isinstance(item, tuple):
                text, params = item
            else:
                text, params = item, ()
            if params is None:
                # 客户端不给参数时送 `None` 是最常见的写法。当空处理，
                # 不当"少给了"——`查询 …` 一个 `?` 都没有的时候，
                # 原文是 `len(None)` 抛 TypeError，而那个报错跟
                # "你没给参数"差着十万八千里。
                params = ()
            try:
                stmts = sql.parse_all(text)
            except (sql.SqlError, schema.SchemaError) as exc:
                out.append(Result(error=str(exc), statement=text))
                continue
            if not stmts:
                out.append(Result(statement=text))
                continue
            if one_per_text and len(stmts) > 1:
                out.append(Result(
                    error='批里的一条只能是一句，这条里有 %d 句。**批里不拆**'
                          '——拆了之后结果帧和语句就对不上号了，客户端会'
                          '拿第 3 句话的结果去对第 5 句话' % len(stmts),
                    statement=text))
                continue
            try:
                self._check_params(stmts, params)
            except (StatementError, sql.SqlError, schema.SchemaError) as exc:
                out.append(Result(error=str(exc), statement=text))
                continue
            for st in stmts:
                out.append(self._run(st, params, text, 身份))
        # `身份` 只影响"要不要压住这次落盘"：这个会话自己在批里就别落，
        # 别的会话在批里**不关他的事**（见 `_flush`）。
        self._flush(force=True,
                    会话号=身份.会话号 if 身份 is not None else None)
        return out

    def _check_params(self, stmts, params):
        """
        一段文本**整体**要几个 `?`，和给的对不对得上。

        必须按整段查，不能按单句查：`?` 的下标在一段文本里是绝对的，
        `查询 … x=?; 查询 … y=?` 第一句的树里只有下标 0、第二句只有 1，
        单看哪一句都数不出"一共 2 个"。而一段文本只带一份参数
        （`execute_batch` 的 `(文本, 参数)` 是配对的），对不上就是客户端
        数错了，得在**写下任何一行之前**说清楚。

        多给了也算错。多给不报的话，客户端的参数会**整体错位**——每个 `?`
        都读到前一个的值，答案全错而没人知道。
        """
        want = sql.count_params(stmts)
        if want != len(params):
            raise StatementError('这段文本有 %d 个 ?，给了 %d 个参数'
                                 % (want, len(params)))

    def _run(self, st, params, text=None, 身份=None):
        """
        **每一条语句的唯一关卡。** 权限就判在这里，而且只判"库级"那一半
        —— 表级那半在 `_table`/`_writable` 里（见 `_table` 的说明）。

        ## 身份游标

        `self._身份` 在这句活着的时候有效，`finally` 复位。为什么用游标
        而不是把 `身份` 加进那 21 个执行函数的签名：**那些函数没有一个
        需要它**，只有 `_show` 要看一眼（`_show(self, st, params, text=None)`
        今天够不着身份）。为了一条语句多加一个参数，改的是 21 个签名。

        这条做法**成立的前提只有一条：一次只有一个调用者。** 服务端是
        单线程事件循环（`transport` 那边一个请求从头跑到尾，没有交错），
        本类自己也写着"别在这里加锁"。所以游标不会被另一条语句踩掉。
        **这个前提松掉的那天，游标跟着一起错**——所以要加并发，加的
        不是锁，是把 `身份` 收回成参数。
        （`_run` 也不会被递归调用：`_rebuild_index` 是直接调 `_drop_index`
        / `_create_index` 两个函数，不走这条路。）

        ## `raise` 必须在 `try` 里面

        这不是顺手。`execute_batch` 逐句调这个方法，**外面没有 try**：
        在 `try` 之前 raise 会让异常穿出整批（违反"批里一条出错不中断"），
        而且会跳过那一批末尾的 `_flush(force=True)` —— 前面几句已经跑成
        的写就**不落盘**了。一个"没权限"的报错把别人的写弄丢，这代价
        比报错本身大得多。放在 `try` 里则和现有报错形状一模一样：
        变成 `Result(error=...)`，返回给调用方，批照跑。
        """
        if not self._loaded:
            self.open()
        try:
            self._身份 = 身份
            # `?` 的下标在一段文本里是**绝对**的（见 `sql.Parser.__init__`），
            # 所以这里只能查"参数够不够这一句用"，**不能查相等**。
            # 查相等的话，`查询 … x=?; 查询 … y=?` 这种一段文本两句、
            # 一共两个 `?` 的写法，第一句会被判成"有 1 个 ?，给了 2 个"
            # 而整段跑不动 —— 而"够不够"和"多没多"这两件事，前者只有
            # 这一句说了不算（它看不见后面的语句），后者由 `_check_params`
            # 按整段文本去查。
            want = sql.count_params(st)
            if want > len(params):
                raise StatementError('第 %d 个参数没给（这一句要用到第 %d 个）'
                                     % (len(params) + 1, want))
            # ── 库级权限 ────────────────────────────────────────────
            # **`_权限要求` 里没有这一条语句 = 拒绝**，不是放行。这条
            # 方向不能反：认不出来就放行的话，将来新加的第 22 种语句
            # 在补上分类之前是**对所有人敞开**的，而它敞着的时候没有任何
            # 征兆。反过来最多是新语句暂时用不了——一用就报，一报就修。
            #
            # 底下那条模块级断言盯着同一件事，但它在导入时炸、管的是
            # "忘了分类"，这里管的是"运行到了一句没分类的话"。
            要求 = user_mod._权限要求.get(type(st))
            if 要求 is None:
                raise StatementError('还不认识 %s 这类语句要什么权限，'
                                     '先拒绝' % type(st).__name__)
            if not user_mod.可过(self._当前用户(), 要求):
                raise StatementError(self._无权文案(st, 要求))
            method = _DISPATCH.get(type(st))
            if method is None:
                raise StatementError('还不会执行 %s' % type(st).__name__)
            return method(self, st, params, text)
        except (StatementError, sql.SqlError, schema.SchemaError,
                arena_mod.ArenaError) as exc:
            return Result(error=str(exc), statement=text)
        finally:
            # **`finally`，不是"跑到最后一句时"**：上面任何一处 raise
            # 都会从这里出去，而游标留着不干净的话，下一条语句开头那段
            # 极短的时间里它还是上一个人的身份。
            self._身份 = None

    def _无权文案(self, st, 要求):
        """
        库级权限不够时那句话。

        **没登录和权限不够分开说。** 前者是"你得先登录"，后者是"你登录了
        但没这个权"——两件事的下一步动作完全不同（一个去登录，一个去找
        管理员要权限），混成一句会让人在错的地方使劲。

        这里说的是**库级**那几样（`建表`/`删表`/`管用户`…），它们不分表，
        所以报出来不泄露任何一张表的名字、也不泄露它存不存在。表级那半
        由 `_没有这张表` 说，那边要装成"不存在"。
        """
        if self._当前用户() is None:
            return '这条语句要登录之后才能跑。请先登录'
        缺 = [w for w in 要求 if not user_mod.判权限(self._当前用户(), w)]
        # 不写"跑不了 XXX"那种带语句类名的尾巴：`Result` 里本来就带着
        # 原始文本（`statement=text`），界面上是贴着那条语句显示这个错的。
        # 再缀一个 `Select` / `DropTable` 只会在中文报错里多出一串英文。
        return '%s 没有 %s 权限' % (self._当前用户().name, '、'.join(缺))

    # ── DDL ─────────────────────────────────────────────────────────

    def _check_table(self, tbl):
        if not tbl.columns:
            raise StatementError('表 %s 一列都没有' % tbl.name)
        seen = set()
        for c in tbl.columns:
            if c.name in seen:
                raise StatementError('列名 %s 重复了' % c.name)
            seen.add(c.name)
        for p in tbl.pk:
            col = tbl.require(p)
            # 主键列强制非空。**这不是在替用户做决定**：主键编进键里，
            # 允许它为空就等于允许"键里有一截是空的"，而空值在编码里
            # 和任何值都不相等——同一个空主键会散成无数条永远查不回来
            # 的行。这个约束没有别的合理选择。
            col.nullable = False
        if tbl.dim_column:
            col = tbl.require(tbl.dim_column)
            if col.type not in (INT, TIME):
                raise StatementError(
                    '维度列 %s 是%s。**维度必须是整数或时间**——裁剪查询'
                    '靠的是维度能排序、能比大小，文本和二进制排不出一个'
                    '有意义的次序，硬用只会给出错的答案'
                    % (col.name, schema.TYPE_NAMES.get(col.type, '?')))

    def _batch_begin(self, st, params, text=None):
        """
        进批模式。**已经在批里就报**，不静默当无事发生 ——
        `批量 开始; 批量 开始; …写…; 批量 提交` 这种写错了的脚本，
        真正的提交在第二个 `开始` 那儿就断了线，而人以为自己被罩着。
        """
        if self._批会话号() in self._批会话:
            raise StatementError(
                '已经在 `批量 开始` 和 `批量 提交` 之间了，这里又来一个'
                ' `批量 开始`。批不嵌套，套起来的那个外层没有任何效果')
        self._批会话.add(self._批会话号())
        return Result()

    def _batch_commit(self, st, params, text=None):
        """
        出批模式并**真的落盘**。这是 `提交` 存在的全部意义 ——
        以前它是个空操作，所谓"攒批"是假的，每句写都各落各的盘。

        **没在批里就报**：裸一个 `提交` 说明有人以为自己在批里，
        而他以为被罩住的那批，其实每句都已经落过盘了。这不是无害的
        多余语句，是他对当前状态的判断错了。
        """
        if self._批会话号() not in self._批会话:
            raise StatementError(
                '没在批里，没什么可提交的。`批量 提交` 要配一个 `批量 开始`'
                '（本库的写默认就是落盘的，不用提交）')
        self._批会话.discard(self._批会话号())
        self._flush(force=True, 会话号=self._批会话号())
        return Result()

    def _create_table(self, st, params, text=None):
        if st.name in self.tables:
            if st.if_not_exists:
                return Result(affected=0)
            raise StatementError('表 %s 已经存在了。要重新定义先 删表，'
                                 '或者换个名字' % st.name)
        if not st.pk:
            raise StatementError('表 %s 没定主键' % st.name)
        table_id = self._next_table_id
        tbl = schema.Table(st.name, table_id)
        for cd in st.columns:
            tbl.add_column(cd.name, cd.type, cd.nullable, cd.default)
        tbl.pk = list(st.pk)
        tbl.dim_column = st.dim_column
        self._check_table(tbl)
        self.tables[tbl.name] = tbl
        self._by_id[table_id] = tbl
        self._next_table_id = table_id + 1
        self._persist_table(tbl)
        return Result(affected=0)

    def _drop_table(self, st, params, text=None):
        tbl = self.tables.get(st.name)
        if tbl is None:
            if st.if_exists:
                return Result(affected=0)
            raise StatementError('没有 %s 这张表' % st.name)
        return self._drop_table_named(tbl.name)

    def _drop_table_named(self, name):
        """删表的实体。`删表` 和 `删索引` 共用这一份。"""
        tbl = self.tables[name]
        # **连带删索引。** 留一张指着不存在基表的索引表，是白干活而不只是
        # 浪费：它一个候选都带不出来（闸一点查永远读不到），可每次查询还是
        # 要为它算前缀、扫一遍索引表。反过来的方向不存在——索引表底下没有
        # 别的东西，所以这不递归。
        if not tbl.index_of:
            for child in [t.name for t in self.tables.values()
                          if t.index_of and t.index_of[0] == tbl.name]:
                self._drop_table_named(child)
        # **行数据不删。** 只追加的存储里"删掉一片"就是重写整个库。把
        # 定义从目录里摘掉，那批行键就再没人认领了——它们在盘上待着，
        # 占空间，但不参与任何查询。
        del self.tables[tbl.name]
        del self._by_id[tbl.table_id]
        # **先写目录，后写墓碑。** 次序和别处相反，这里是故意的：万一
        # 崩在中间，留下的是"目录里没有、墓碑还写着活着"——一条没人
        # 引用的垃圾记录。反过来的话，目录里点着名、定义却已经成了
        # 墓碑，开库时直接判定为库坏了。
        self._write_catalog()
        self._put(schema.table_key(tbl.name), b'{"v":1,"dropped":true}')
        return Result(affected=0)

    def _alter_table(self, st, params, text=None):
        # `管`：要的是库级的 `改表`（`_run` 那边已经过了）。表级那四样
        # 管的是**行**，而改表动的是结构——没有"只能改这些表的行、不能
        # 改那张表的结构"这种说法，也不该由这儿凭空加一条。
        tbl = self._table(st.name, '管')
        if tbl.index_of:
            # 索引表的列布局是**推出来的**：`值1..值n` 对应基表哪几列、
            # `秩` 是维度列，全是 `index_of` 和基表一起决定的。加一列、
            # 删一列、改个维度，改的都是"基表那边推过来的东西"，而基表
            # 一无所知 —— 下一次查询照样按老形状去切条目键。
            raise StatementError(
                '表 %s 是 %s 按 (%s) 的索引**表**，改它的结构没有意义：'
                '它的列是照着基表推出来的。要换形状就 删索引 + 建索引，'
                '要一步到位用 `重建索引 %s 按 (新列…)`'
                % (tbl.name, tbl.index_of[0], ', '.join(tbl.index_of[1]),
                   tbl.index_of[0]))
        # **删列碰上索引就拒绝。** 索引条目的键是按列的类型和值编出来的
        # （`index_key` 一路 `require(列名)` 取类型），列一没，那个索引下次
        # 查询就切不动自己的键——它会在**读的时候**抛错，或者更糟：建索引
        # 时算出来的键和现在的键对不上，静默漏行。所以在这里拦住，把两条路
        # 明写出来。
        #
        # **改列名不走这条路**：改名不像删列，它有地方可去（见下面 `moves`）。
        hit = []
        if st.action == 'drop':
            hit = [t for t in indexes_of(self, tbl)
                   if st.column in t.index_of[1]]
            if hit and not st.cascade:
                raise StatementError(
                    '列 %s 上有索引（%s），删它会把索引弄坏：索引条目的键'
                    '是按那一列的类型和值编的，列没了它就切不动自己的键。'
                    '两条路：① 先 `删索引 %s 按 (%s)` 再删这一列；'
                    '② 想一条语句做完就带上 `连带删索引`：'
                    '`改表 %s 删列 %s 连带删索引`'
                    % (st.column, '、'.join(t.name for t in hit), tbl.name,
                       ', '.join(hit[0].index_of[1]), tbl.name, st.column))
        # **改列名带着索引一起走。** 索引条目的键编的是**值**，不是列名
        # （`index_key` 是按列取类型再编值），所以改名一个字节的行都不用搬：
        # 索引表要改的只有两样——它记着的列名清单，和照那个清单算出来的
        # 名字。**表号一个不动**：行键前缀是表号，换号等于整套条目作废、
        # 白重建一遍。
        #
        # 新名字要**在改任何东西之前**算出来并查重：撞上别人的名字得拒绝，
        # 而拒绝必须发生在基表和索引都还完好的时候。
        moves = []
        if st.action == 'rename':
            if st.cascade:
                # 删列那条路上 `连带删索引` 是逃生门；改名不需要逃生门——
                # 索引有地方可去。收下这个关键字再**默默不执行它**是最坏
                # 的：人以为索引被删了，其实还在，而且改了个名字。
                raise StatementError(
                    '改列名不用带 `连带删索引`：索引表会跟着一起改名，条目'
                    '一条都不会丢。真想连索引都不要了，先 `删索引` 再改这一列')
            taken = set(self.tables)
            for idx in indexes_of(self, tbl):
                if st.column not in idx.index_of[1]:
                    continue
                cols = tuple(st.new_name if c == st.column else c
                             for c in idx.index_of[1])
                new_name = index_name(tbl.name, cols)
                if new_name == idx.name:
                    # **原地改名（`改名 题目 到 题目`）要在这里就断掉。**
                    # 放它过去的话，"老名字退成墓碑"那一步会拿一个**就是
                    # 新名字**的名字去写墓碑——活着的索引定义当场变成墓碑，
                    # 而目录还点着它，下次开库直接判库坏。
                    continue
                other = self.tables.get(new_name)
                if new_name in taken or (other is not None and other is not idx):
                    raise StatementError(
                        '列 %s 改叫 %s 之后，索引 %s 该叫 %s，可库里已经有'
                        '一张表叫这个名字了（%s）。**不让它悄悄换个名字**：'
                        '换完你就再也不知道该用哪个名字去 `显示 索引` 了。'
                        '请先 删表 %s 或者给它改名，再改这一列'
                        % (st.column, st.new_name, idx.name, new_name,
                           '同一句里另一个索引也要这个名' if other is None
                           else other.describe(), new_name))
                taken.add(new_name)
                moves.append((idx, cols, new_name))
        dead = []
        if st.action == 'add':
            cd = st.column
            if not cd.nullable and cd.default is schema.NO_DEFAULT:
                raise StatementError(
                    '加一个非空列就得给默认值：老行里没有这一格，不给'
                    '默认值它们就都是空的，而空值在非空列上不允许')
            tbl.add_column(cd.name, cd.type, cd.nullable, cd.default)
        elif st.action == 'drop':
            tbl.drop_column(st.column)
        elif st.action == 'rename':
            tbl.rename_column(st.column, st.new_name)
            for idx, cols, new_name in moves:
                dead.append(self._rename_index(idx, new_name,
                                               (tbl.name, cols)))
        elif st.action == 'dim':
            col = tbl.require(st.column)
            if col.type not in (INT, TIME):
                raise StatementError('维度列必须是整数或时间，%s 是%s'
                                     % (col.name,
                                        schema.TYPE_NAMES.get(col.type, '?')))
            tbl.dim_column = st.column
        else:
            raise StatementError('还不认识这个改表动作：%s' % st.action)
        self._check_table(tbl)
        # 这一步顺带把目录改掉（索引的新名字此刻进目录、老名字出目录）。
        # 新名字的定义在上面 `_rename_index` 里已经先落到盘上了，所以
        # **任何一个崩溃窗口里，目录里点着的名字都有一份好定义**。
        self._persist_table(tbl)
        # **索引放在改完之后删。** 反过来的话，列要是改不成（主键列、
        # 唯一列都会拒绝），索引已经被带走了，人白白丢一条索引。
        for t in hit:
            self._drop_table_named(t.name)
        # 改名那条路的老名字退成墓碑，**放在目录之后**：崩在中间留下的是
        # "目录里没有、墓碑还写着活着"的垃圾记录。反过来的话，目录点了名、
        # 定义却已经是墓碑，开库时直接判定为库坏了。
        for old_name in dead:
            self._put(schema.table_key(old_name), b'{"v":1,"dropped":true}')
        return Result(affected=0)

    # ── 索引表 ──────────────────────────────────────────────────────

    def _rename_index(self, idx, new_name, index_of):
        """
        基表改了列名，把它的索引表跟着改。返回它的**老名字**（调用方
        负责在那个名字上写墓碑，得等目录更新之后）。

        **表号不动。** 条目键的头一段是 `row_prefix(table_id)`，换号等于
        整套条目一个都读不到——那等于把索引扔了重建一遍，改名本来一分钱
        都不用花。

        **声明列一个不动。** 索引表的列是 `值1..值n` + `秩`，按**次序**
        跟着基表的列走，名字里没有基表列名，所以改名不碰它们。真要改的
        只有 `index_of` 里记着的列名清单（它得跟基表对得上）和照那个清单
        算出来的表名。

        次序（崩在哪一步都安全，见 `_alter_table`）：
        ① 新名字的定义先落盘——此刻目录里还是老名字，老定义也还在，
           整个库是**旧样子且完整**的；多出来的是一份没人引用的新记录。
        ② 调用方接着写基表和目录，目录换到新名字。
        ③ 最后老名字退成墓碑。
        """
        old_name = idx.name
        idx.name = new_name
        idx.index_of = index_of
        # 目录（`self.tables`）按名字查，得跟着换；`_by_id` 认的是表号，
        # 表号没动，那一份不用碰。
        del self.tables[old_name]
        self.tables[new_name] = idx
        self._write_table_record(idx)
        return old_name

    def _create_index(self, st, params, text=None):
        base = self._table(st.table, '管')     # 要的是库级的 `建索引`
        if base.index_of:
            # 索引表里的行是从基表**推出来**的，而推它的那件事不认识
            # "索引的索引"。允许它就会得到一张没人维护的空表：查的时候
            # 一条候选都给不出来，于是**一行都不会少地少掉**。
            raise StatementError(
                '表 %s 本身就是 %s 按 (%s) 的索引表，索引上不再建索引'
                % (base.name, base.index_of[0], ', '.join(base.index_of[1])))
        cols = []
        for name in st.columns:
            col = base.require(name)        # 列不在就报，报的是基表的名字
            if any(c is col for c in cols):
                raise StatementError('列 %s 在这个索引里出现了两次' % name)
            cols.append(col)
        name = index_name(base.name, st.columns)
        # **重名要报，不能"顺手换个名字"。** 这里报出来的是 S4 那条：
        # 同名的表只要行键前缀不同，两边都能读能写，谁也不挡谁 ——
        # 而 `_index_name` 是按规则算出来的，人完全可以手写一个
        # `建表 网页_标题_索引 (…)` 撞上来。撞了就报，由人决定。
        if name in self.tables:
            other = self.tables[name]
            if other.index_of == (base.name, tuple(st.columns)):
                raise StatementError(
                    '表 %s 上已经有 按 (%s) 的索引了（索引表叫 %s）。'
                    '想重做用 `重建索引 %s 按 (%s)`'
                    % (base.name, ', '.join(st.columns), name,
                       base.name, ', '.join(st.columns)))
            raise StatementError(
                '索引表该叫 %s，可库里已经有一张表叫这个名字了，'
                '而它不是这个索引（%s）。**不让它悄悄换个名字**：'
                '换完你就再也不知道该用哪个名字去 `显示 索引` 了。'
                '请先 删表 %s 或者给它改名'
                % (name, other.describe(), name))
        idx = schema.Table(name, self._next_table_id,
                           index_of=(base.name, tuple(st.columns)))
        # 值列跟基表那几列**同类型** —— 条目键里编的就是这个类型。
        for i, col in enumerate(cols, 1):
            idx.add_column('值%d' % i, col.type, True)
        # `秩` 是这张表的维度列，值就是该行的**保序秩**。为什么不拿值列
        # 当维度：维度盘上是有符号 64 位，文本和实数进不去（`_dim_of_value`
        # 对它们返回 None），于是区会被标成松散、裁剪彻底失效 ——
        # 结果全对，只是白做。秩是整数，进得去，裁剪就真的跳区。
        idx.add_column('秩', INT, True)
        idx.dim_column = '秩'
        self._check_table(idx)
        # **次序是这一步的全部内容。** 表上可能已经有行，那些行一条
        # 条目都没有 —— 不补的话，索引建好那一刻起，凡是走索引的查询
        # 都会给出**空结果**：不是报错、不是变慢，是"查了，没有"。所以
        # 建索引必须回填。回填要扫全表、要写很多行，中间随时可能崩，
        # 于是次序得让**每一个崩溃窗口都是安全的**：
        #
        #   ① 先把表号**落库订下来**（目录里 `next_table_id` 前进，但
        #      名字还不进表清单）。票号一旦落库，这批字节就再也不会被
        #      后来的表认领 —— 否则崩在回填中间，那些孤立条目会顶着一个
        #      还没被别人用掉的表号前缀，等下一张新建的表撞上来，它们
        #      会**冒充那张表的行**冒出来。
        #   ② 再回填。这时候索引在内存里认得出，但目录里没有它。
        #   ③ 最后才把表定义和名字写进目录。**在这一步之前崩掉，索引
        #      就等于没建成**：字节留在盘上没人认领，查询走全扫，答案
        #      一个都不会错，只是没快起来。
        #
        # 于是"目录里点着名的索引"就等价于"回填已经做完了"。这个等价
        # 关系是后面 `检查索引` 敢把"缺条目"当成**真问题**来报的底气。
        self._next_table_id = idx.table_id + 1
        self._write_catalog()               # ① 订表号
        self.tables[name] = idx
        self._by_id[idx.table_id] = idx
        for _key, values in self._each_row(base):    # ② 回填
            self._index_put(base, values, only=idx)
        self._persist_table(idx)            # ③ 名字进目录
        return Result(affected=0)

    def _drop_index(self, st, params, text=None):
        base = self._table(st.table, '管')     # 要的是库级的 `删索引`
        name = index_name(base.name, st.columns)
        idx = self.tables.get(name)
        # **按算出来的名字找，还要回头对一遍身份。** 光看名字不够：
        # 人可以把索引表改名，也可以先建一张同名的普通表。列清单是
        # 有次序的，所以比对时也按次序比。
        if idx is None or idx.index_of != (base.name, tuple(st.columns)):
            have = [t for t in self.tables.values()
                    if t.index_of and t.index_of[0] == base.name]
            if have:
                raise StatementError(
                    '表 %s 上没有 按 (%s) 的索引。它现在的索引是：%s'
                    % (base.name, ', '.join(st.columns),
                       '；'.join('按 (%s)' % ', '.join(t.index_of[1])
                                 for t in have)))
            raise StatementError('表 %s 上一个索引都没有' % base.name)
        return self._drop_table_named(name)

    def _rebuild_index(self, st, params, text=None):
        """
        `重建索引 <表> 按 (列…)`：就是**真的**删了再建，不是原地清空重写。

        删+建换一个新表号，等于换一套行键前缀：旧行一个都读不到，旧字节
        成孤儿留在盘上。而"垮掉最多是多几个没人指向的字节"正是这个引擎
        **允许**的活法（没有删区、压实也没实现）。原地清空则要在已有行键
        上写一大片墓碑，那片墓碑还得被之后每一次查询扫到。

        单写者模型让这件事干净：同进程只有一个 Arena，重建期间没有并发
        写，于是唯一的切换点是目录，而它两边都自洽——切之前指向旧表（完整），
        切之后指向新表（完整）。
        """
        self._drop_index(st, params, text)
        return self._create_index(st, params, text)

    # ── 用户与权限 ──────────────────────────────────────────────────
    #
    # 这五条都只动用户那本册子（`\\x00u` + 目录里的名字清单），**一行数据
    # 都不碰**。它们要的权限（`管用户`）在 `_run` 那边已经判过了，所以这
    # 几个函数里一行权限代码都没有——那是收口的形状，不是漏了。

    def _create_user(self, st, params, text=None):
        name = (st.name or '').strip()
        if name in self.users:
            raise StatementError('用户 %s 已经在了。要换口令用 `改密 %s \'新口令\'`'
                                 % (name, name))
        user = user_mod.建(name, st.password)
        self.users[user.name] = user
        self._persist_user(user)
        return Result(affected=0)

    def _drop_user(self, st, params, text=None):
        name = (st.name or '').strip()
        user = self.users.get(name)
        if user is None:
            raise StatementError('没有 %s 这个用户' % name)
        # **管理员不可删。** 删掉最后一个管理员 = 这个库再也没人能管用户，
        # 而"解药"只剩下去盘上改字节。这条不是"从前的通融"——是这次做
        # 权限时定下的：库必须**永远**留至少一个管理员在。
        if user.superuser:
            raise StatementError(
                '用户 %s 是管理员，不能删。删了之后这个库就没人能管用户了'
                '（要收回他的权限也不行，见 `收回` 的说明）' % name)
        del self.users[name]
        self._write_catalog()               # 先摘名字，和删表一个次序
        self._put(schema.user_key(name), b'{"v":1,"dropped":true}')
        return Result(affected=0)

    def _alter_user(self, st, params, text=None):
        name = (st.name or '').strip()
        user = self.users.get(name)
        if user is None:
            raise StatementError('没有 %s 这个用户' % name)
        user_mod.改口令(user, st.password)
        self._persist_user(user)
        return Result(affected=0)

    def _grant(self, st, params, text=None):
        return self._改授权(st, 给=True)

    def _revoke(self, st, params, text=None):
        return self._改授权(st, 给=False)

    def _改授权(self, st, 给):
        """
        `授权` / `收回` 的实体。两条只差一个方向，分开写会把"管理员不许
        被收回"这条规矩抄两遍。

        **通配 `全部`**：库级那九样全给 + `table_privs['*']`（所有表的
        查/插/改/删）。它是**展开**成那些词存下去的，不是把 `全部` 本身
        当个词存着——存着的话，`判权限` 就得在每个地方都记得处理它，
        而漏一处就是"授了全部却什么都干不了"。

        **管理员的权限不可以收回，也不可以再授。** 他本来就是一律放行
        （`user.判权限` 头一条），授什么都不会改变什么，收什么也收不掉
        ——收得掉的话，界面上会出现一个"权限列是空的、但什么都能干"的
        人，那比报错难查得多。
        """
        name = (st.name or '').strip()
        user = self.users.get(name)
        if user is None:
            raise StatementError('没有 %s 这个用户' % name)
        if user.superuser:
            raise StatementError(
                '用户 %s 是管理员，权限不用授也收不掉——他本来就不受限'
                '（`user.判权限` 头一条）。要限制他就另建一个用户，'
                '别把他变成管理员' % name)
        if st.privilege == sql.权限全部:
            if 给:
                user.global_privs.update(user_mod.库级权限)
                user.table_privs['*'] = set(user_mod.表级权限)
            else:
                user.global_privs.clear()
                user.table_privs.pop('*', None)
        elif st.table is None:
            if 给:
                user.global_privs.add(st.privilege)
            else:
                user.global_privs.discard(st.privilege)
        else:
            有 = user.table_privs.setdefault(st.table, set())
            if 给:
                有.add(st.privilege)
            else:
                有.discard(st.privilege)
            if not 有:
                # 空集合留着的话，`可读` 会因为"这个键在"而放行——见
                # `user.可读`：它判的是集合非空，不是键在不在。这里
                # 顺手清掉，让盘上那份也干净。
                del user.table_privs[st.table]
        # 表名**不校验存在**：可以给一张还没建的表的权限（先授权、后建表），
        # 也可以留着删掉的表的名字。校验的话，"删表 → 表还在的人的权限
        # 跟着消失"这件事会变成"删表顺便改了别人的权限"，而那是两回事。
        self._persist_user(user)
        return Result(affected=0)

    def _drain_stale(self):
        """
        把 `_live` 里"区已经封了、账却还挂着"的条目补记进库。

        `_drain_closed` 只管**本进程眼看着它封**的那一下。可区还有别的
        封法：压实搬运把活跃区写满而封掉的、上次会话里封掉的。那些区在
        `_live` 里的账没人收，而"等它封区再记"这件事永远不会来了。

        **补记成整条数轴 + loose**：宁可这个区每次都进候选，也不能漏行。
        """
        sealed = {i['region_id'] for i in self.arena.regions()
                  if i['state'] == sf.RegionState.SEALED}
        for rid in sorted({k[1] for k in self._live} & sealed):
            self._drain_closed(rid)

    def _compact(self, st, params, text=None):
        """
        `压实`：整库一趟，把被覆盖的旧版本占的区真正从盘上回收掉。

        引擎那边（`Arena.compact`）只按键的字节走"谁先出现谁赢"，它不知道
        什么是表。**本层要做的是认账**：搬运走的是 `arena.put`，绕过了
        `_note_dim`——所以收到副本的那些区里混着一批"本层没记过维度"的
        行。不认这笔账，`_candidates` 会拿着一个偏小的区间把那几个区整个
        排掉，那批行**静默消失**，而查询结果看着完全正常。

        认账只标 `copied_regions` 那一小撮，其余区的维度裁剪照旧精确。
        不搞"整库一刀切成候选"——那是拿全库的查询速度换省事。
        """
        if self._批会话:
            # **这道口子和 `_flush` 那道不一样：这里谁在批里都拦。**
            # 压实要**重写盘上的字节、还要删文件**，而批模式的意思是
            # "这些写先别落盘"——别人的批里攒着的那些写还只在内存里，
            # 压实的搬运却按盘上的字节走，两下碰在一起，那批写的落点
            # （区号）可能已经被压实挪过了。
            #
            # `_flush` 那边可以只压自己（多落一次最多是慢），这里不行：
            # 一次整库的搬运没法只对一部分会话生效。所以照 `close()` 的
            # 办法直接拒掉——两种通融都是替人拿主意：顺手提交了，他以为
            # 没提交；只压一半，他以为压完了。
            raise StatementError(
                '`压实` 不能在 `批量 开始` 和 `批量 提交` 之间跑。它要重写'
                '盘上的区、还要删文件，而批模式的意思是"先别落盘"。'
                '先 `批量 提交`，再压实')

        out = self.arena.compact()

        # ── 认账 ────────────────────────────────────────────────────
        # 副本落在哪些区，就把**所有表**的账在那些区上标成松散。是"所有
        # 表"不是"某张表"：区是多张表共用的，而引擎的搬运按字节走，它
        # 根本分不出这批副本属于谁。`loose` 是粘的，之后按正常路径往这
        # 个区里写也不会把它抹掉。
        for tid in self._by_id:
            for rid in out['copied_regions']:
                self._note_dim(tid, rid, None, loose=True)

        # 搬运把活跃区写满而封掉的那些区，账还挂在 `_live` 里等着——但
        # 区已经封了，那个"等"不会来了。收掉。
        self._drain_stale()

        # 区都没了的账留着只会越长越大。**清得掉是因为区号不回收**：不会
        # 有哪个新区分到同一个号，继承一笔不相干的区间。哪天真回收了，
        # 这里就得跟着改。
        live = {i['region_id'] for i in self.arena.regions()}
        self._live = {k: v for k, v in self._live.items() if k[1] in live}
        self._dim = {k: v for k, v in self._dim.items() if k[1] in live}

        after = self.arena.stats()
        cols = [('丢掉的区', INT), ('搬运行数', INT),
                ('回收字节', INT), ('剩余区', INT)]
        rows = [[(INT, out['dropped_regions']),
                 (INT, out['moved']),
                 (INT, out['reclaimed_bytes']),
                 (INT, after['regions'])]]
        return Result(cols, rows, affected=out['moved'])

    def _check_index(self, st, params, text=None):
        """
        `检查索引 <表>`：**只读，只报不修。**

        两边各收一个**键的集合**，再看谁缺谁：
        `该有的` = 基表每一行算出来的条目键；`有的` = 索引表上**有活记录**
        的键。差集就是缺（假阴性）和多（假阳性）。

        两个细节是这里全部的内容：

        1. **比较的是键的集合，不是"有效版本活不活"。** 只追加的盘上同一个
           键可能有好几版，有效版本是墓碑、老版本还活着，是**正常状态**。
           看有效版本会把它报成"缺"，可读路径照样能靠那条老记录找到那一行
           —— 索引**并没有**漏行。报缺必须只报真的漏行：这个键在盘上一条
           活记录都没有。所以"有的"是"有活记录"，不是"有效版本活着"。
        2. **不拿 `arena.get` 一行行点查。** 那查到的是有效版本，正落在
           上面那个坑里。两遍全扫贵一点，但报出来的数是可观测行为本身。

        **为什么读路径发现不了"缺条目"**：缺条目的表现只是"这次查询少了
        一个候选"，而候选少一个什么都不会发生——不报错、不变慢、答案里
        就是少一行。所以这是唯一能发现它的手段，也就必须由它来发现。

        发现缺失**只报不修**：在一次只读操作里做写，违背这套东西的分层。
        要修是显式的 `重建索引`。

        权限上它是**只读**：要的是库级的 `显示`（`_run` 那边过的）加这张
        表上的 `查`。一条只读语句要表级 `改` 的话，那才是说不通。
        """
        base = self._table(st.table, '读')
        idxs = indexes_of(self, base)
        if not idxs:
            raise StatementError('表 %s 上一个索引都没有，没得检查' % base.name)
        cols = [('索引表', TEXT), ('表号', INT), ('基表行数', INT),
                ('缺条目', INT), ('多条目', INT), ('判定', TEXT)]
        rows = []
        for idx in sorted(idxs, key=lambda t: t.table_id):
            # ① 基表这边：每一行都该有一条条目。键里嵌着基表主键，所以
            #    不同的行算出来的键必然不同，这个集合也就是"行的集合"。
            want = set()
            for _key, values in self._each_row(base):
                want.add(index_key(idx, base, values))
            # ② 索引这边：盘上**有活记录**的键。一个键只算一次，看到过
            #    就不再算 —— 只追加的盘上同一个键会有好几版。
            have = set()
            seen = set()
            prefix = schema.row_prefix(idx.table_id)
            cursor = None
            while True:
                cursor, items, more = self.arena.scan(cursor, 512, None)
                for _rid, _blk, _slot, key, value in items:
                    if not key.startswith(prefix) or key in seen:
                        continue
                    seen.add(key)
                    if schema.is_live(value):
                        have.add(key)
                if not more:
                    break
            miss = len(want - have)
            extra = len(have - want)
            if not miss and not extra:
                verdict = '好'
            elif not miss:
                verdict = '只多 %d 条（假阳性，答案不受影响）' % extra
            else:
                verdict = ('缺 %d 条 —— 查这个索引会**漏行**，'
                           '跑 `重建索引 %s 按 (%s)`'
                           % (miss, base.name, ', '.join(idx.index_of[1])))
            rows.append([(TEXT, idx.name), (INT, idx.table_id), (INT, len(want)),
                         (INT, miss), (INT, extra), (TEXT, verdict)])
        return Result(cols, rows)

    def _show(self, st, params, text=None):
        if st.what == 'tables':
            user = self._当前用户()
            cols = [('表名', TEXT), ('表号', INT), ('列数', INT),
                    ('主键', TEXT), ('维度列', TEXT)]
            # 索引表**只在这里藏**，`_table()` 那边一点都不动（见那里的
            # 说明）。藏起来是为了这张清单说的是"你建过哪些表"——索引表
            # 是 `建索引` 顺手造出来的，混在里面只会让人分不清哪张是
            # 自己的。要看它们有 `显示 索引 <表名>`。
            #
            # 第二道过滤是**权限**：看不见的表不列。"写蕴含读"那条在
            # `user.可读` 里实现（只有 `插` 没 `查` 的人也得看得见），
            # 这里别自己再判一遍——两处判据迟早会分岔。
            rows = [[(TEXT, t.name), (INT, t.table_id), (INT, len(t.columns)),
                     (TEXT, '/'.join(t.pk)), (TEXT, t.dim_column)]
                    for t in sorted(self.tables.values(),
                                    key=lambda x: x.table_id)
                    if not t.index_of and user_mod.可读(user, t.name)]
            return Result(cols, rows)
        if st.what == 'users':
            # **不是 `_权限要求` 那张表能管的事**：`Show` 那一条只说了
            # `显示`，而"看谁在这个库里"是另一件事，得单独要 `管用户`。
            # 这张清单里是权限本身的形状，能看就等于知道该去改谁。
            if not user_mod.判权限(self._当前用户(), '管用户'):
                raise StatementError(
                    '看用户清单要 `管用户` 权限。它是权限本身的形状，'
                    '看得见就等于知道该去改谁')
            cols = [('用户名', TEXT), ('管理员', INT), ('库级', TEXT),
                    ('表级', TEXT)]
            rows = []
            for name in sorted(self.users):
                o = user_mod.概览(self.users[name])
                # **`概览` 里没有摘要**，所以这里"想漏也漏不出去"
                # ——见 `user.概览` 那段说明。
                表级 = '；'.join('%s: %s' % (k, '/'.join(v))
                                for k, v in sorted(o['表级'].items()))
                rows.append([(TEXT, o['名字']), (INT, 1 if o['管理员'] else 0),
                             (TEXT, '/'.join(o['库级'])), (TEXT, 表级)])
            return Result(cols, rows)
        if st.what == 'indexes':
            base = self._table(st.table, '读')
            cols = [('索引表', TEXT), ('表号', INT), ('按几列', INT),
                    ('按', TEXT)]
            rows = [[(TEXT, t.name), (INT, t.table_id),
                     (INT, len(t.index_of[1])),
                     (TEXT, '、'.join(t.index_of[1]))]
                    for t in sorted(self.tables.values(),
                                    key=lambda x: x.table_id)
                    if t.index_of and t.index_of[0] == base.name]
            return Result(cols, rows)
        if st.what == 'columns':
            tbl = self._table(st.table, '读')
            cols = [('列号', INT), ('名字', TEXT), ('类型', TEXT),
                    ('可空', INT), ('默认', TEXT), ('主键', INT), ('维度', INT)]
            rows = []
            for c in tbl.columns:
                dv = c.default_value()
                rows.append([
                    (INT, c.col_id), (TEXT, c.name),
                    (TEXT, schema.TYPE_NAMES.get(c.type, '?')),
                    (INT, 1 if c.nullable else 0),
                    (TEXT, None if dv is None else schema.render(c.type, dv)),
                    (INT, 1 if c.name in tbl.pk else 0),
                    (INT, 1 if c.name == tbl.dim_column else 0),
                ])
            return Result(cols, rows)
        if st.what == 'regions':
            if st.table is None:
                raise StatementError(
                    '显示 区域 后面要跟表名——维度区间是按表记的，'
                    '不指表就没有那两列可看')
            return self._show_regions(self._table(st.table, '读'))
        raise StatementError('显示 还不认识 %s' % st.what)

    def _show_regions(self, tbl):
        """
        区域清单，叠加本层手里的维度区间。

        **引擎给不出后两列。** `arena.regions()` 里的 dim_low/dim_high 是
        建区那一刻记下来的，之后不再更新——那是一个区的"出生证"，不是
        "这里现在有什么"。真正能用的区间在本层的 `_dim` / `_live` 里。
        """
        cols = [('区域号', INT), ('状态', TEXT), ('记录数', INT),
                ('本表维度低', INT), ('本表维度高', INT), ('活跃', INT),
                ('本表区间来源', TEXT), ('含空维度', INT)]
        states = {sf.RegionState.ACTIVE: '活跃',
                  sf.RegionState.SEALED: '封存',
                  sf.RegionState.DEAD: '废弃',
                  sf.RegionState.BROKEN: '损坏'}
        rows = []
        active = self.arena.active_region
        for info in self.arena.regions():
            rid = info['region_id']
            key = (tbl.table_id, rid)
            if key in self._live:
                rng = tuple(self._live[key])
                src = '内存（还没封区）'
            else:
                rng = self._dim_range(tbl.table_id, rid)
                src = '库里' if rng is not None else '没记，当候选'
            rows.append([
                (INT, rid),
                (TEXT, states.get(info['state'], str(info['state']))),
                (INT, info['records']),
                (INT, rng[0] if rng else None),
                (INT, rng[1] if rng else None),
                (INT, 1 if rid == active else 0),
                (TEXT, src),
                # 这一列存在是为了让"这个区为什么没被裁掉"看得见。标了 1
                # 的区里有一条不带维度的行，任何区间都盖不住它，所以它
                # 每次都进候选——**这是对的，不是没优化好**。
                (INT, 1 if (rng and rng[2]) else 0),
            ])
        return Result(cols, rows)

    # ── 行的编解码 ──────────────────────────────────────────────────

    def _row_dict(self, tbl, cells, alias=None):
        """
        {(别名, 列名): (类型, 值)}。

        列号在盘上，列名在定义里——两边靠 `col_id` 对上。**老行里没有的
        列**（加列之前写的那些）这一格就是缺的，填默认值；没默认值的填
        空。这正是"每格带列号"换来的东西：加列不用重写一行数据。
        """
        alias = alias or tbl.name
        row = {}
        for c in tbl.columns:
            entry = cells.get(c.col_id)
            if entry is None:
                row[(alias, c.name)] = (c.type, c.default_value())
            else:
                row[(alias, c.name)] = entry
        return row

    def _decode_row(self, value, key):
        """
        值字节 -> 格子字典。坏行返回 None，**不抛**——扫描是尽力而为的
        遍历，一条坏记录不该把整条查询打断；它会以"这行不见了"的形式
        出现，而不是以报错的形式。点查那边不一样，那边会当没查到。
        """
        try:
            return schema.decode_cells(schema.unwrap_row(value))[0]
        except (ValueError, schema.SchemaError):
            return None

    def _fit(self, col, val):
        """把求值结果规整成这一列的类型。空值看列允不允许空。"""
        if val is None:
            if not col.nullable:
                raise StatementError('列 %s 不能是空' % col.name)
            return None
        _t, v = val
        try:
            return schema.coerce(col.type, v)
        except schema.SchemaError as exc:
            raise StatementError('列 %s：%s' % (col.name, exc))

    def _row_values(self, row):
        return {name: entry[1] for (_alias, name), entry in row.items()}

    def _each_row(self, tbl):
        """
        按区扫一遍这张表的活行，一条条吐 `(键, 值字典)`。**不攒进内存。**

        和 `_scan_rows` 分开是因为那个函数的活儿是"把结果集取出来"，
        它自己有上限、最后返回一个 list。回填要的正好相反：条数可能很多、
        顺序无所谓、每条处理完就扔。硬用 `_scan_rows` 等于"为了建索引
        先把整张表拉进内存"。

        **`seen` 那一步不能省。** 区降序意味着同一个键第一次出现的就是
        有效版本，后面出现的全是老版本。少了它，一条**已删**的行会被它
        在旧区里的老版本复活：老版本是活的，于是回填会给一条不存在的行
        造出索引条目。那个条目本身不会带出错答案（闸一点查读不到），
        但它会永远留在盘上，还让 `检查索引` 报出一个假的"多"。
        `seen` 的内存跟着行数涨 —— 这是"扫全表"固有的代价，回填和
        `检查索引` 都是显式的全表操作，认了。
        """
        prefix = schema.row_prefix(tbl.table_id)
        seen = set()
        cursor = None
        while True:
            cursor, items, more = self.arena.scan(cursor, 512, None)
            for _rid, _blk, _slot, key, value in items:
                if not key.startswith(prefix):
                    continue
                if key in seen:
                    continue
                seen.add(key)       # 先记键再判死活：有效版本是墓碑时，
                if not schema.is_live(value):   # 老版本得**一起**被跳掉
                    continue
                cells = self._decode_row(value, key)
                if cells is None:
                    continue
                yield key, self._row_values(self._row_dict(tbl, cells))
            if not more:
                return

    def _index_put(self, tbl, values, tomb=False, only=None):
        """
        把这一行在**每一张**索引上的条目写下去（`tomb` 时写墓碑）。

        **这里只管"写哪些"，不管"什么时候写"。** 次序是那条不变式的
        全部内容，而它是调用点的性质：让索引变多的写排在基表写之前、
        让索引变少的写排在基表写之后。放进这个函数就等于把次序藏起来，
        而藏起来的次序迟早会被"顺手合并两条写"改掉 —— 改完不报错、
        不变慢，只是崩溃窗口里**少一行**。

        没有索引的表返回 0，一个字节都不会写。

        `only` 是回填用的：只往那**一条**索引里写。表上本来那几条索引
        早就把这一批行写过了，再写一遍是纯粹的浪费（每建一条索引，
        全表就要重写几遍）。**不是因为重写会写错** —— 键和值都一样，
        写进去还是那条，只是没有任何人会因此得到什么。
        """
        n = 0
        for idx in ([only] if only is not None else indexes_of(self, tbl)):
            key = index_key(idx, tbl, values)
            # **墓碑带原来那条的维度。** 不带的话它可能落在一个不在
            # 候选区里的区——那个区的区间是按活条目记的，而墓碑也要被
            # 扫到，否则删除看不见，那条条目会以"还在"的样子出来。
            dim = index_dim(idx, tbl, values)
            tid = _dim_track_id(idx)
            if tomb:
                self._put(key, schema.TOMBSTONE, dim, table_id=tid)
                n += 1
                continue
            cells = []
            for i, name in enumerate(idx.index_of[1], 1):
                col = tbl.require(name)
                cells.append((idx.require('值%d' % i).col_id, col.type,
                              values.get(name)))
            # `秩` 存的就是维度的那个数。**这两者必须一字不差地相等**，
            # 否则 `_dim_loose` 判它松散、整个区每次都算候选 —— 结果
            # 全对，只是索引白建（这正是"看着在优化其实没有"）。
            cells.append((idx.require('秩').col_id, INT, dim))
            self._put(key, self._wrap(idx, cells), dim, table_id=tid,
                      loose=_dim_loose(idx, dim, {'秩': dim}))
            n += 1
        return n

    def _index_changed(self, tbl, old_values, new_values):
        """这一行的改动会不会动到索引条目。"""
        for idx in indexes_of(self, tbl):
            if index_values(idx, tbl, old_values) != \
                    index_values(idx, tbl, new_values):
                return True
        return False

    def _row_dim(self, tbl, row):
        if not tbl.dim_column:
            return None
        entry = row.get((tbl.name, tbl.dim_column))
        if entry is None:
            return None
        return _dim_of_value(entry[1])

    def _dim_of(self, tbl, expr, values, ctx):
        """
        这一行该带什么维度值。

        显式写了 `维度 <表达式>` 就用它，否则取维度列的值。**两者都
        没有就是 None**——不是 0。0 是个合法的维度值，用 0 表示"没有"
        会让所有不带维度的行挤进同一个假的区间里。
        """
        if expr is not None:
            self._reject_cols(expr, '维度 后面要给个常数，不能引用列')
            val = self._eval(expr, {}, ctx)
            if val is None:
                return None
            return _dim_of_value(val[1])
        if not tbl.dim_column:
            return None
        return _dim_of_value(values.get(tbl.dim_column))

    def _reject_cols(self, e, why):
        if _has_col(e):
            raise StatementError(why)

    def _wrap(self, tbl, cells):
        raw = schema.wrap_row(schema.encode_cells(cells))
        if len(raw) > MAX_ROW_BYTES:
            raise StatementError(
                '这一行编码之后有 %d 字节，超过上限 %d。看哪一列最大——'
                '大块的东西（网页正文、图片）更适合存成二进制分几行放，'
                '而不是挤在一行里'
                % (len(raw), MAX_ROW_BYTES))
        return raw

    # ── 写 ──────────────────────────────────────────────────────────

    def _insert(self, st, params, text=None):
        # **`替换` 是插 + 改两档。** 它不是"插的一个选项"：那个 `替换`
        # 会覆盖掉已经在那儿的行，而覆盖别人的行和往空处写一行是两件事
        # ——不然一个只有 `插` 的人就能拿 `替换` 把整张表逐行改写一遍。
        #
        # 判它是**静态的**（`st.overwrite` 是解析期定下的标志），所以
        # 这里不需要去问"那行在不在"——也就不存在"拒绝的时候顺带泄露了
        # 行存不存在"这条路径。
        tbl = self._writable(st.table, *(('插', '改') if st.overwrite
                                         else ('插',)))
        ctx = _Ctx(self, [(tbl.name, tbl)], params)
        cols = list(st.columns) if st.columns else [c.name for c in tbl.columns]
        seen = set()
        for name in cols:
            if name in seen:
                raise StatementError('列 %s 在列清单里出现了两次' % name)
            seen.add(name)
            tbl.require(name)
        n = 0
        for row_exprs in st.rows:
            if len(row_exprs) != len(cols):
                raise StatementError('列清单有 %d 列，这一行给了 %d 个值'
                                     % (len(cols), len(row_exprs)))
            # 没点名的列填默认值。**这比"必须把每一列都写全"好用**：
            # 加了列之后，老语句不用回头改。
            values = {c.name: c.default_value() for c in tbl.columns}
            for name, e in zip(cols, row_exprs):
                col = tbl.require(name)
                if isinstance(e, tuple) and e and e[0] == 'default':
                    values[name] = col.default_value()
                    continue
                self._reject_cols(e, '插入的值里不能引用列——这一行还没'
                                     '写下去，没有任何旧值可读')
                values[name] = self._fit(col, self._eval(e, {}, ctx))
            for c in tbl.columns:
                if values.get(c.name) is None and not c.nullable:
                    raise StatementError('列 %s 不能是空' % c.name)
            key = tbl.key_for([values[p] for p in tbl.pk])
            if not st.overwrite:
                old = self.arena.get(key)
                if old is not None and schema.is_live(old):
                    raise StatementError(
                        '主键（%s）已经有行了。要覆盖请把 插入 换成 替换'
                        % _pk_text(tbl, values))
            cells = [(c.col_id, c.type, values[c.name]) for c in tbl.columns]
            dim = self._dim_of(tbl, st.dim, values, ctx)
            # **索引先写，基表后写。** 这一行是"让索引变多的写"，而
            # 崩在这两步之间留下的是"索引多一条、基表没这行"——闸一
            # 按条目里的主键去点查，查不到，丢弃。反过来（基表先写）
            # 崩在中间就是**基表有行、索引没有**：那行从此再也查不出来。
            self._index_put(tbl, values)
            self._put(key, self._wrap(tbl, cells), dim,
                      table_id=_dim_track_id(tbl),
                      loose=_dim_loose(tbl, dim, values))
            n += 1
        return Result(affected=n)

    def _update(self, st, params, text=None):
        tbl = self._writable(st.table, '改')
        ctx = _Ctx(self, [(tbl.name, tbl)], params)
        rows = self._match(tbl, st.where, ctx)
        n = 0
        for key, row in rows:
            # 设 的右边**可以引用列**（`设 计数 = 计数 + 1`），读的是这
            # 一行的旧值——和 SQL 一样，一条更新里所有的右边看到的是
            # 同一份旧快照，不是左边改一个右边就看到新值。
            old_values = self._row_values(row)
            values = dict(old_values)
            for name, e in st.sets:
                col = tbl.require(name)
                values[name] = self._fit(col, self._eval(e, row, ctx))
            for c in tbl.columns:
                if values.get(c.name) is None and not c.nullable:
                    raise StatementError('列 %s 不能是空' % c.name)
            cells = [(c.col_id, c.type, values[c.name]) for c in tbl.columns]
            new_key = tbl.key_for([values[p] for p in tbl.pk])
            # 索引条目的键里**嵌着基表主键**，所以"改主键"和"改被索引的
            # 列"是同一件事：那条老条目整体作废，得配一条新的。漏了这条
            # 新的，按索引列查的时候新主键那行**一条候选都没有**。
            touched = new_key != key or \
                self._index_changed(tbl, old_values, values)
            dim = self._dim_of(tbl, st.dim, values, ctx)
            tid = _dim_track_id(tbl)
            # ① 新条目先写（索引变多），② 才轮到基表，③ 最后才删旧条目
            if touched:
                self._index_put(tbl, values)
            self._put(new_key, self._wrap(tbl, cells), dim, table_id=tid,
                      loose=_dim_loose(tbl, dim, values))
            if new_key != key:
                # 主键改了。**旧的键得留个墓碑**，否则它指向的那行还在，
                # 扫描时会以旧主键的样子再出来一次——同一个东西两个身份。
                self._put(key, schema.TOMBSTONE, self._row_dim(tbl, row),
                          table_id=tid)
            # ③ 旧条目退役。**必须在基表写完之后**：万一崩在②和③之间，
            # 留下的是"新旧条目都在"——闸二复验时旧条目看到的值已经和
            # 基表对不上，筛掉。反过来先删旧的，崩在中间就是基表已经
            # 是新值、索引里却一条都没有。
            if touched:
                self._index_put(tbl, old_values, tomb=True)
            n += 1
        return Result(affected=n)

    def _delete(self, st, params, text=None):
        tbl = self._writable(st.table, '删')
        ctx = _Ctx(self, [(tbl.name, tbl)], params)
        rows = self._match(tbl, st.where, ctx)
        for key, row in rows:
            # 墓碑带着这一行原来的维度走。**不带的话墓碑可能落在一个
            # 不在候选区里的区**——那个区的维度区间是按活行记的，而
            # 墓碑也要被扫到，否则删除看不见，那行会以"还在"的样子出来。
            self._put(key, schema.TOMBSTONE, self._row_dim(tbl, row),
                      table_id=_dim_track_id(tbl))
            # **基表先删，索引后删。** 删除是"让索引变少的写"，按不变式
            # 排在基表写**之后**。反过来（先删索引条目）崩在中间，留下
            # 的是**基表那行还活着、索引里已经没有它的条目** —— 按索引
            # 列查，一条候选都出不来，那行再也不会出现在答案里。
            # 顺着排的话，崩在中间多出来的是一条指向不存在行的条目，
            # 闸一点查读不到，丢掉就是。
            self._index_put(tbl, self._row_values(row), tomb=True)
        return Result(affected=len(rows))

    # ── 找行 ────────────────────────────────────────────────────────

    def _pk_equalities(self, tbl, where, ctx, alias=None):
        """
        条件里**恰好**把所有主键列都定成等值的话，给出这个等值组合；
        否则 None（那就得扫）。

        只认最外层的与（AND）链。或 一出现就没法收成一个点——
        `a = 1 或 a = 2` 是两个点，不是"a 的等值"。

        `alias` 是这条查询里给这张表起的别名。`查询 * 从 网页 w 其中
        w.网址 = ?` 和 `… 其中 网址 = ?` 是同一件事，两条都该走点查；
        只认表名的话带别名的那条会退化成全扫 —— 结果对，只是白扫。
        """
        if where is None:
            return None
        out = {}
        if not self._collect_eq(tbl, where, ctx, out, alias):
            return None
        if any(p not in out for p in tbl.pk):
            return None
        return out

    def _collect_eq(self, tbl, e, ctx, out, alias=None):
        if not isinstance(e, tuple) or not e:
            return False
        if e[0] == 'and':
            return (self._collect_eq(tbl, e[1], ctx, out, alias)
                    and self._collect_eq(tbl, e[2], ctx, out, alias))
        if e[0] == 'cmp' and e[1] == '=':
            left, right = e[2], e[3]
            if not (isinstance(left, tuple) and left and left[0] == 'col'):
                return False
            qual, name = left[1], left[2]
            if name not in tbl.pk:
                return False
            if qual is not None and qual != tbl.name and qual != alias:
                return False
            val = self._literal_value(right, ctx)
            if val is _NULL_VAL:
                return False
            if name in out and out[name] != val:
                # 同一个主键列被定了两个不同的值。这个点不存在，但
                # 这里不急着下结论——返回 False 让它退回全扫，让键比对
                # 去判。**走错路的代价是多扫一遍，走对了是少扫一遍，
                # 两边都不出错。**
                return False
            out[name] = val
            return True
        return False

    def _literal_value(self, e, ctx):
        """能当场算出值的表达式 -> 值（可能是 None）。算不出给 _NULL_VAL。"""
        if not isinstance(e, tuple) or not e:
            return _NULL_VAL
        k = e[0]
        if k == 'lit':
            return e[2]
        if k == 'nullval':
            return None
        if k == 'param':
            if ctx is None or e[1] >= len(ctx.params):
                return _NULL_VAL
            return ctx.params[e[1]]
        return _NULL_VAL

    def _match(self, tbl, where, ctx):
        """
        找出符合条件的行，返回 [(行键, 行字典)]。

        **不做"够数就收工"的优化**：更新和删除要的是全部匹配行，不像
        查询那样可以提前停。
        """
        eq = self._pk_equalities(tbl, where, ctx)
        if eq is not None:
            key = tbl.key_for([eq[p] for p in tbl.pk])
            raw = self.arena.get(key)
            if raw is None or not schema.is_live(raw):
                return []
            cells = self._decode_row(raw, key)
            if cells is None:
                return []
            row = self._row_dict(tbl, cells)
            # **拿回来还要再比一遍。** 点查只用了主键，条件里其余部分
            # 没验过；而且 `a=1 与 a=2` 这种自相矛盾的条件也会走到这里。
            # 键比对是最后一道防线，它把"索引说它对"降级成"它真的对"。
            if where is not None and not _truthy(self._eval(where, row, ctx)):
                return []
            return [(key, row)]
        return self._scan_rows(tbl, where, ctx, None, self.max_scan_rows)

    def _scan_rows(self, tbl, where, ctx, candidates, cap,
                   stop_after=None, alias=None):
        """
        扫候选区，去重，过滤条件，返回 [(行键, 行字典)]。

        去重的依据：**区域号降序意味着同一个键第一次出现的就是最新的**。
        所以见到过的键直接跳过就行，不用比版本号——那个次序是 arena
        承诺的，这里只是用它。

        `cap` 是这次扫描最多看多少条记录。**必须有个数**：一个键的旧版本
        全留在封存区里，`seen` 会跟着扫描量一起涨，没有上限的话一条查询
        就能把内存吃干净。
        """
        prefix = schema.row_prefix(tbl.table_id)
        seen = set()
        out = []
        cursor = None
        scanned = 0
        while True:
            cursor, items, more = self.arena.scan(cursor, 512, candidates)
            for _rid, _blk, _slot, key, value in items:
                if not key.startswith(prefix):
                    continue
                scanned += 1
                if scanned > cap:
                    raise StatementError(
                        '这次扫描看了 %d 条记录，超过上限 %d。加个 限制，'
                        '或者把条件收紧一点——没有上限的话这条查询会把'
                        '整张表拉进内存' % (scanned, cap))
                if key in seen:
                    continue
                seen.add(key)
                if not schema.is_live(value):
                    continue
                cells = self._decode_row(value, key)
                if cells is None:
                    continue
                row = self._row_dict(tbl, cells, alias)
                if where is not None and not _truthy(self._eval(where, row, ctx)):
                    continue
                out.append((key, row))
                if stop_after is not None and len(out) >= stop_after:
                    return out
            if not more:
                break
        return out

    # ── 维度窗口 ────────────────────────────────────────────────────

    def _dim_window(self, tbl, where, ctx=None, lossy=False, col=None,
                    to_dim=None):
        """
        从条件里挖出维度列的区间，挖不出来给 None（那就是全扫）。

        `col` / `to_dim` 是给索引表那条路留的两个口子：那条路上的"维度"
        不是表里的列，而是**基表某一列的保序秩**（`schema.rank`），所以
        比较的右边也得跟着做同样的映射。默认（两个都不给）就是老样子：
        维度列本身，映射是恒等。

        `lossy=True` 表示**维度不是列本身的值，而是它的保序秩**
        （`schema.rank`，索引表那条路）。秩是有损的：前 8 个字节一样的
        两个文本同秩。所以 `>` 不能收成 `v + 1` —— 秩为 `v` 的那些值里
        可能有比比较值大的，收紧了就是**静默漏行**。宽松的那一边永远是
        安全的：窗口宽一格只是多带几个假阳性，复验会筛掉。

        只认最外层的与（AND）链。**往窄了收永远安全，往宽了放才危险**：
        每个合取项都是"命中的行必须满足"的条件，所以按它收紧窗口不会
        漏行；而 `或`、`不介于`、`不等于` 这些是补集或并集，往宽了说
        是整条数轴（等于没裁），往窄了说是静默漏掉一整个区。一律不碰。

        **`介于` 必须认。** 它是维度列上最自然的范围写法；不认的话
        这条查询就一路全扫，而表面上裁剪还"好好的"—— 那种错最难发现，
        因为结果全对，只是慢。

        参数要在这里就解开（`ctx`），不能等执行时。`介于 ? 到 ?` 是
        客户端最常用的形态，解不开就等于裁剪对最常用的那条路失效。
        """
        if where is None:
            return None
        if col is None:
            col = tbl.dim_column
            if not col:
                return None

        # 恒等映射那条路上，右边得按**这一列声明的类型**解释。见
        # `_coerce_cached` 上面那一段：窗口和行过滤必须用同一次转换，
        # 各转各的就会分家，而分家的方向是**静默漏行**。
        # 走 `to_dim`（索引表那条路）时用不上它 —— 那边右边先转成秩。
        dim_type = tbl.require(col).type

        def is_dim_col(e):
            return (isinstance(e, tuple) and e and e[0] == 'col'
                    and e[1] is None and e[2] == col)

        def as_int(e):
            v = self._literal_value(e, ctx)
            if v is _NULL_VAL or v is None:
                return None
            if to_dim is not None:
                # 映射不认这个值（比如实数遇上 NaN）就当作挖不出来：
                # 少裁一点是安全的，猜一个窗口出来才是危险的。
                try:
                    return to_dim(v)
                except (schema.SchemaError, StatementError):
                    return None
            if isinstance(v, (str, bytes)):
                # `其中 抓取时间 > '2026-09-01'` —— 右边是个时间字符串，
                # 按列的类型转成毫秒才挖得出窗口。转不了（比如拿 'abc'
                # 比时间列）就当作挖不出来：**少裁一点永远是对的**，
                # 报错留给行过滤那边去报（它才有"这是哪一列"的上下文）。
                try:
                    v = _coerce_cached(dim_type, v)
                except schema.SchemaError:
                    return None
            if isinstance(v, bool) or not isinstance(v, int):
                return None
            return v

        lo = hi = None

        def tighten(a, b):
            nonlocal lo, hi
            if a is not None:
                lo = a if lo is None else max(lo, a)
            if b is not None:
                hi = b if hi is None else min(hi, b)

        stack = [where]
        while stack:
            e = stack.pop()
            if not isinstance(e, tuple) or not e:
                continue
            k = e[0]
            if k == 'and':
                stack.append(e[1])
                stack.append(e[2])
                continue
            if k == 'between':
                # `不介于` 是补集，跳过。第 5 位就是那个"不"。
                if e[4] or not is_dim_col(e[1]):
                    continue
                a, b = as_int(e[2]), as_int(e[3])
                if a is None or b is None:
                    continue
                tighten(a, b)
                continue
            if k != 'cmp' or not is_dim_col(e[2]):
                continue
            op = e[1]
            v = as_int(e[3])
            if v is None:
                continue
            if op == '>':
                tighten(v + (0 if lossy else 1), None)
            elif op == '>=':
                tighten(v, None)
            elif op == '<':
                tighten(None, v - (0 if lossy else 1))
            elif op == '<=':
                tighten(None, v)
            elif op == '=':
                tighten(v, v)
            # `!=` 是补集，跳过。
        if lo is None and hi is None:
            return None
        # 条件本身自相矛盾时 lo > hi。**这不是错误**，答案是"一行都没有"，
        # 交给裁剪去处理——一个不可能命中的窗口会自然排出所有区。
        return (lo if lo is not None else schema.INT64_MIN,
                hi if hi is not None else schema.INT64_MAX)

    # ── 查询 ────────────────────────────────────────────────────────

    def _exec_select(self, st, params, text=None):
        tbl = self._table(st.table, '读')
        sources = [(st.alias or tbl.name, tbl)]
        for j in st.joins:
            # **连接右表也走 `_table`，这就是收口的意义。** 按
            # `st.table` 收表名的做法看不见这一张（`_walk` 翻不进
            # `('subq', …)`），于是"只授了左表"的人能顺着 join 读右表。
            # 这里多写一个 `'读'` 是明账，而漏判是暗账。
            sources.append((j.alias or j.table, self._table(j.table, '读')))
        ctx = _Ctx(self, sources, params)

        rows = self._gather(st, tbl, ctx)
        # **`计数(*)` 不带 分组 也是一条分组查询**——它把整张表当成一组。
        # 只有 `分组 按 ...` 才算分组是一种很好犯的错：那样写的话
        # `查询 计数(*) 从 t` 会掉进普通的逐行求值，然后在 `_eval` 里
        # 撞上"这是个聚合函数"。
        grouped = bool(st.group) or _any_agg(
            [it.expr for it in st.items]
            + ([st.having] if st.having is not None else [])
            + [o.expr for o in st.order])

        # 没分组时 holder 就是源行本身，分组时是 (桶, 分组键)。**两种
        # 形状不一样是有意的**：分组之后"源行"已经没有意义了（一组有
        # 很多行），硬套一个代表行会让 `_eval` 对着某一行的值算出结果，
        # 而那个结果跟这一组没关系。
        work = self._group(st, tbl, ctx, rows) if grouped else rows

        if st.having is not None:
            kept = []
            for holder in work:
                val = self._value_of(st, st.having, holder, ctx, grouped)
                if _truthy(val):
                    kept.append(holder)
            work = kept

        projected = [(self._project(st, h, ctx, grouped, sources), h)
                     for h in work]

        if st.distinct:
            seen = set()
            uniq = []
            for vals, holder in projected:
                k = tuple((t, _hashable(v)) for t, v in vals)
                if k in seen:
                    continue
                seen.add(k)
                uniq.append((vals, holder))
            projected = uniq

        if st.order:
            projected = self._sort(st, projected, ctx, grouped)

        if st.limit is not None or st.offset is not None:
            off = st.offset or 0
            end = None if st.limit is None else off + st.limit
            projected = projected[off:end]

        if len(projected) > self.max_result_rows:
            raise StatementError('结果有 %d 行，超过上限 %d。加个 限制'
                                 % (len(projected), self.max_result_rows))

        cols = self._result_columns(st, ctx, sources)
        # 求值出来的空是 None，而结果集里的空是 `(列类型, None)`。**在
        # 这里补上类型**：客户端要靠列类型决定怎么解这一格，一格连类型
        # 都没有的话，"这一列这行是空"和"这一列压根不是这个类型"在
        # 网上长得一样。
        return Result(cols, [[(cols[i][1], None) if v is None else v
                              for i, v in enumerate(vals)]
                             for vals, _h in projected])

    def _exec_query(self, q, params):
        """一条查询：`Select` 或者 `SetOp`。子查询回的就是这一层。"""
        if isinstance(q, sql.SetOp):
            return self._exec_setop(q, params)
        return self._exec_select(q, params)

    def _exec_setop(self, st, params, text=None):
        """
        并集 / 交集 / 差集。

        **两边的列数和类型都要一样，不一样就报。** 不统一、不挑一个、
        不按"谁宽听谁的"硬升——升型会**安静地改掉**一批值的类型，
        而客户端是照列类型解格子的。真要不一致，写的人自己 `转成整数`
        或 `转文本` 说清楚，那是他的意思，不是我们替他猜的意思。
        """
        left = self._exec_query(st.left, params)
        right = self._exec_query(st.right, params)
        cols = left.columns
        if len(cols) != len(right.columns):
            raise StatementError(
                '%s 两边的列数不一样：左边 %d 列，右边 %d 列'
                % (_SETOP_NAMES[st.op], len(cols), len(right.columns)))
        for i, (lc, rc) in enumerate(zip(cols, right.columns)):
            if lc[1] != rc[1]:
                raise StatementError(
                    '%s 第 %d 列的类型不一样：左边 `%s` 是%s，右边 `%s` 是%s'
                    '。集合运算不猜该用哪个，请自己统一'
                    % (_SETOP_NAMES[st.op], i + 1, lc[0],
                       schema.TYPE_NAMES.get(lc[1], '?'), rc[0],
                       schema.TYPE_NAMES.get(rc[1], '?')))

        # **先量一下再合。** 默认的 并集 要去重，去重要额外养一张和总行数
        # 一样大的去重表；几百万行的两半合起来能把这个进程撑死，而"进程被
        # 系统杀掉"是这套东西最不想要的死法。超了就报，报的错里说清是多少
        # 行、上限多少、怎么绕。
        #
        # 上限只管**去重**那一步：`并集 全部` 不建那张表，所以它真的是绕路
        # ——报错里指了这条路，这条路就得真走得通。交集/差集天然去重，
        # 躲不掉，也就别在错里提一条走不通的路。
        if st.op != 'UNION' or not st.all:
            total = len(left.rows) + len(right.rows)
            if total > self.max_setop_rows:
                hint = ('`并集 全部` 不去重、省下这一步，'
                        if st.op == 'UNION' else '')
                raise StatementError(
                    '%s 两边合起来有 %d 行，超过上限 %d。这个上限是防'
                    '内存被撑爆的，不是防结果的：%s或者用 `限制` / 加个'
                    ' `其中` 把两边收窄'
                    % (_SETOP_NAMES[st.op], total, self.max_setop_rows, hint))

        def key(row):
            return tuple((t, _hashable(v)) for t, v in row)

        if st.op == 'UNION':
            rows = left.rows + right.rows
            if not st.all:
                rows = _dedupe(rows, key)
        else:
            rk = set(key(r) for r in right.rows)
            keep = (lambda r: key(r) in rk) if st.op == 'INTERSECT' \
                else (lambda r: key(r) not in rk)
            rows = _dedupe([r for r in left.rows if keep(r)], key)

        if st.order:
            rows = self._sort_setop(st.order, cols, rows)
        off = st.offset or 0
        end = None if st.limit is None else off + st.limit
        rows = rows[off:end]
        if len(rows) > self.max_result_rows:
            raise StatementError('结果有 %d 行，超过上限 %d。加个 限制'
                                 % (len(rows), self.max_result_rows))
        return Result(cols, rows)

    def _sort_setop(self, order, cols, rows):
        """
        集合运算结果上的排序。

        **这里没有数据源**，所以排序键只能按**结果列**说：写结果集的
        列名，或者写列号（`排序 按 2 降序`）。源表里那些列名在这儿是
        认不出来的——`Select` 那条路上按源行列名排序，是因为它手里有源行；
        集合运算手里只有结果，按源行列名排是排不了的。认不出来就报，
        不猜。
        """
        names = [n for n, _t in cols]
        idx = []
        for o in order:
            e = o.expr
            if isinstance(e, tuple) and e and e[0] == 'col' and e[1] is None:
                if e[2] not in names:
                    raise StatementError(
                        '集合运算的结果里没有 %s 这一列。按名字排只能写'
                        '结果集的列（%s），或者写列号'
                        % (e[2], ' / '.join(names)))
                idx.append((names.index(e[2]), o.desc))
            elif isinstance(e, tuple) and e and e[0] == 'lit' and e[1] == INT:
                n = e[2]
                if not 1 <= n <= len(cols):
                    raise StatementError('排序 按 %d：结果只有 %d 列'
                                         % (n, len(cols)))
                idx.append((n - 1, o.desc))
            else:
                raise StatementError(
                    '集合运算的 排序 只能按结果列的名字或列号。'
                    '要按算式排请先把它写成结果里的一列（`… 作为 名字`）')

        def cmp(a, b):
            for i, desc in idx:
                c = schema.compare(a[i], b[i])
                if c:
                    return -c if desc else c
            return 0

        decorated = list(rows)
        decorated.sort(key=functools.cmp_to_key(cmp))
        return decorated

    def _gather(self, st, tbl, ctx):
        """把符合条件的行都取出来。有点查走点查，没有就扫。"""
        alias = st.alias or tbl.name
        if st.joins:
            left = [r for _k, r in self._scan_rows(
                tbl, None, ctx, None, self.max_scan_rows, None, alias)]
            rows = self._join(st, left, ctx)
            if st.where is not None:
                rows = [r for r in rows if _truthy(self._eval(st.where, r, ctx))]
            return rows

        # **主键全等就直取。** `_match`（更新/删除）早就这么干了，
        # 查询这条路一直没接上：`查询 … 其中 主键 = 值` 明明是个 O(1)
        # 的哈希直取，却一直在候选区里从头扫。候选区再小也是扫，
        # 而 `arena.get` 是**一次定位**。
        eq = self._pk_equalities(tbl, st.where, ctx, alias)
        if eq is not None:
            one = self._gather_one(tbl, st.where, ctx, eq, alias)
            if one is not None:
                return one
            # 键拼不出来（比如 `整数列 = 1.5`，`coerce` 不收）。**那就
            # 老老实实扫**，别把"拼不出键"变成一个新的报错 —— 点查只是
            # 抄近路，抄不了就该走大路，而不是把大路封了。

        # **有索引就走索引。** 索引只影响快慢、不影响对错，所以判据可以
        # 保守：挑不出来就照老路走，一步都不会走错。
        picked = self._pick_index(tbl, st.where, ctx)
        if picked is not None:
            rows = self._gather_via_index(picked, tbl, st, ctx, alias)
            if rows is not None:
                return rows

        window = self._dim_window(tbl, st.where, ctx)
        candidates = None
        if window is not None:
            lo, hi = window
            if lo > hi:
                return []
            candidates = self._candidates(tbl, lo, hi)
        stop = None
        if not st.order and not st.group and st.limit is not None:
            # 没有排序、没有分组、又有 限制，那就不必扫完——**这是唯一
            # 能提前收工的场合**。有 排序 就必须全扫，因为最后一行也
            # 可能是最小的那一行。
            stop = (st.limit or 0) + (st.offset or 0)
        return [r for _k, r in self._scan_rows(
            tbl, st.where, ctx, candidates, self.max_scan_rows, stop, alias)]

    def _pick_index(self, tbl, where, ctx):
        """
        挑一条索引来走；挑不出来返回 None。

        **判据只有一个，而且客观**：`where` 里存在某条索引的**前缀**能
        在秩上收成一个窗口 —— 也就是**第一条被索引的列**上有 `=` /
        `>` / `<` / `>=` / `<=` / `介于`，右边还得是解得开的常数。

        多条命中时取**前缀最长**的那条：前缀越长，绑住的列越多，将来
        能用来筛条目的东西也越多。今天几条索引收出来的窗口是一样的
        （只用得上第一列），所以这条规则眼下不影响结果 —— 写下来是因为
        "取哪条"必须有个定死的答案，不能看字典的遍历次序。

        放弃的条件（任何一条命中就不走索引）：没有条件、这张表没有
        索引、条件里没有可用的前缀、右边解不开（列引用、子查询、
        `?` 没给参数）。**每一条都是往"全扫"那边倒**，而全扫永远是对的。
        """
        if where is None:
            return None
        best = None
        for idx in indexes_of(self, tbl):
            first = idx.index_of[1][0]
            col = tbl.require(first)
            w = self._dim_window(tbl, where, ctx, lossy=True, col=first,
                                 to_dim=lambda v, t=col.type:
                                 schema.rank(t, v))
            if w is None:
                continue
            if best is None or len(idx.index_of[1]) > len(best[1].index_of[1]):
                best = (idx, w)
        return best

    def _gather_via_index(self, picked, tbl, st, ctx, alias):
        """
        走索引取行。三道闸，缺一道都会给出安静的错答案。

        返回 `[行]`，或者 `None` 表示"这条路走不通，退回老路"。
        **`[]` 和 `None` 是两件事**：`[]` 是"真的没有符合条件的行"，
        `None` 是"这次没走成索引"。混起来的话，一次解不开的条件会被
        当成"一行都没有"，而正确答案可能是几百行。
        """
        idx, (lo, hi) = picked
        if lo > hi:
            return []
        stop = None
        if not st.order and not st.group and st.limit is not None:
            # `限制` 那条提前收工的路子在这儿一样成立，理由和 `_gather`
            # 里写的一样：没有排序、没有分组时，最后一行是什么无关紧要。
            stop = (st.limit or 0) + (st.offset or 0)
        # 条目键和基表主键**各去一次重**。条目键去重是因为同一个键可能
        # 在盘上出现两次（老槽位 + 新槽位）；基表主键去重是因为**一条
        # 基表行可能被好几条条目带出来**（组合索引、更新没退掉的旧条目、
        # 秩相同造成的假阳性）。少任何一次，同一行就会在结果里出现两遍
        # —— 多出来的行一样是错的答案，只是方向相反。
        seen_entry = set()
        seen_row = set()
        out = []
        n = 0
        for key, value in self._scan_index_keys(idx,
                                                self._candidates(idx, lo, hi)):
            if key in seen_entry:
                continue
            seen_entry.add(key)
            n += 1
            if n > self.max_scan_rows:
                raise StatementError(
                    '这次走索引看了 %d 条索引条目，超过上限 %d。加个 限制，'
                    '或者把条件收紧一点' % (n, self.max_scan_rows))
            # **闸一：从条目里切出基表主键，点查那一行。**
            #
            # 点查不是扫描：`arena.get` 一次定位。这一步顺带把"条目比
            # 基表多"的那些消化掉 —— 崩在两次写之间留下的孤儿条目、
            # 更新时退役的老条目、删除留下的墓碑，全在这里读不到活行。
            pk = index_split(idx, tbl, key)
            if pk is None:
                continue            # 切不动就作废，绝不猜一个主键去点查
            try:
                base_key = tbl.key_for(pk)
            except schema.SchemaError:
                continue
            if base_key in seen_row:
                continue
            raw = self.arena.get(base_key)
            if raw is None or not schema.is_live(raw):
                continue
            # **闸二：完整复验。** 不是只比被索引的那一列，是把整个
            # `where` 再跑一遍。索引只保证"这些是候选"，保证"这些都对"
            # 的是这一遍 —— 沿用 `_match` 里那句"键比对是最后一道防线"。
            cells = self._decode_row(raw, base_key)
            if cells is None:
                continue
            row = self._row_dict(tbl, cells, alias)
            if st.where is not None and not _truthy(self._eval(st.where, row, ctx)):
                continue
            seen_row.add(base_key)      # **过了闸二才算数**，见上
            out.append(row)
            if stop is not None and len(out) >= stop:
                break
        return out

    def _scan_index_keys(self, idx, candidates):
        """把索引表上候选区里的活条目 `(键, 值)` 依次吐出来。"""
        prefix = schema.row_prefix(idx.table_id)
        cursor = None
        while True:
            cursor, items, more = self.arena.scan(cursor, 512, candidates)
            for _rid, _blk, _slot, key, value in items:
                if not key.startswith(prefix):
                    continue
                if not schema.is_live(value):
                    continue
                yield key, value
            if not more:
                return

    def _gather_one(self, tbl, where, ctx, eq, alias):
        """
        主键全等 → 直取那一行。返回 `[行]` 或 `[]`；
        **拼不出键返回 None**，让调用方退回扫描。

        两件事必须做全，少一件都是安静的错答案：

        1. **还要再比一遍条件。** 点查只用了主键，`where` 里其余部分
           （`其中 网址 = ? 与 状态 = 1`）压根没验过；而且 `主键 = 1 与
           主键 = 2` 这种自相矛盾的条件也走得到这里。键比对是最后一道
           防线 —— 沿用 `_match` 里那句一样的注释、一样的做法。
        2. **值是规整过的。** `key_for` 内部走 `coerce`，所以
           `网址 = 123` 会去查键 `'123'`；而扫描那条路拿 `123` 去和
           `'https://…'` 比，是**类型不同**、永远不等。两条路要给出同一个
           答案，靠的就是第 1 条那一遍复验（`compare` 说类型不同就不等）。
        """
        try:
            key = tbl.key_for([eq[p] for p in tbl.pk])
        except schema.SchemaError:
            return None
        raw = self.arena.get(key)
        if raw is None or not schema.is_live(raw):
            return []
        cells = self._decode_row(raw, key)
        if cells is None:
            return []
        row = self._row_dict(tbl, cells, alias)
        if where is not None and not _truthy(self._eval(where, row, ctx)):
            return []
        return [row]

    def _join(self, st, left, ctx):
        """
        嵌套循环连接。左表已经取好了，右表整张拉进内存。

        **右表整张拉进来是有代价的**，所以有个上限：连一张几百万行的表
        会让你等很久，然后内存告急。超了就报错，报的错里说清楚是哪张表
        ——不然用户只会看到进程被系统杀掉。

        左连接给**没配上的左行**补右半边的空，右连接反过来给**没配上的
        右行**补左半边的空。补的那半边是"到这一步为止**累积**的左边"，
        不是只有 `FROM` 那张表——`a 左连接 b 右连接 c` 里，c 没配上时要
        补的是 a 和 b 两家的列。左边有哪几张表、各自什么列，`ctx.sources`
        里现成（`_exec_select` 把 FROM 和每一个连接都塞了进去），而
        `st.joins[k]` 对应 `sources[k + 1]`，所以轮到它时左边就是前 k+1 个。

        补出来的空行**照样要过 `where`**（`where` 在连接之后跑，`_gather`
        里给 `_scan_rows` 的那个条件只管基表）。这是标准语义，不是漏洞：
        `其中 b.列 为空` 正是用来把"没配上的右行"拣出来的写法。

        说清楚一件事：**这一步在结果上其实看不出来。** `_eval` 的 `col`
        分支对"键压根不在"和"值是空"都返回空（`syntax.py` 里那三行），
        所以补不补、补的是 `(类型, None)` 还是光秃秃一个 `None`，查询
        结果一模一样 —— `类型(网页.网址)` 在补过的行上也答 `空`。
        留着它是为了和 `左连接` 那边**对称**（那边一直这么补）：两边都
        不必要，但只在一半的地方省掉，将来 `_eval` 要是改了脾气，行为
        就会左右不对称，而那种 bug 找起来要命。**别为了它写断言**，
        断言分不出补没补。
        """
        out = list(left)
        for step, j in enumerate(st.joins):
            rtbl = self._table(j.table, '读')
            alias = j.alias or j.table
            rrows = [r for _k, r in self._scan_rows(
                rtbl, None, ctx, None, self.max_join_rows, None, alias)]
            if len(rrows) > self.max_join_rows:
                raise StatementError('连接用的表 %s 超过 %d 行'
                                     % (rtbl.name, self.max_join_rows))
            # 右连接要补空的那些列：到这一步为止累积的左边。
            left_side = ctx.sources[:step + 1]
            merged = []
            hit_right = set()
            for lrow in out:
                hit = False
                for ri, rrow in enumerate(rrows):
                    both = dict(lrow)
                    both.update(rrow)
                    if _truthy(self._eval(j.on, both, ctx)):
                        merged.append(both)
                        hit = True
                        hit_right.add(ri)
                if not hit and j.kind == 'left':
                    both = dict(lrow)
                    for c in rtbl.columns:
                        both[(alias, c.name)] = (c.type, None)
                    merged.append(both)
            if j.kind == 'right':
                for ri, rrow in enumerate(rrows):
                    if ri in hit_right:
                        continue
                    both = {}
                    for salias, stbl in left_side:
                        for c in stbl.columns:
                            both[(salias, c.name)] = (c.type, None)
                    both.update(rrow)
                    merged.append(both)
            out = merged
            if len(out) > self.max_result_rows:
                raise StatementError('连接之后有 %d 行，超过上限 %d。'
                                     '加个 限制' % (len(out),
                                                   self.max_result_rows))
        return out

    def _group(self, st, tbl, ctx, rows):
        """分组 + 聚合。返回 [(分组键的值列表, _Bucket)]。"""
        keys = st.group
        aggs = []
        seen = set()
        for item in st.items:
            _collect_aggs(item.expr, aggs, seen)
        if st.having is not None:
            _collect_aggs(st.having, aggs, seen)
        for o in st.order:
            _collect_aggs(o.expr, aggs, seen)

        buckets = {}
        order_seen = []
        for row in rows:
            kv = tuple(self._eval(k, row, ctx) for k in keys)
            b = buckets.get(kv)
            if b is None:
                b = _Bucket(aggs)
                buckets[kv] = b
                order_seen.append(kv)
            b.add(row, aggs, self, ctx)
        if not order_seen and not keys:
            # 没有分组、只有聚合，却一行都没匹配上。SQL 给一行（计数 0）、
            # 求和给空。**这一行不给的话** `查询 计数(*) 从 空表` 会返回
            # 零行，而零行和"计数是 0"在结果集里长得一样，客户端分不出。
            buckets[()] = _Bucket(aggs)
            order_seen.append(())
        return [(buckets[kv], kv) for kv in order_seen]

    def _project(self, st, holder, ctx, grouped, sources):
        """把一行（或一个分组）变成结果集里的一行。"""
        vals = []
        for item in st.items:
            e = item.expr
            if isinstance(e, tuple) and e and e[0] == 'star':
                if grouped:
                    raise StatementError(
                        '* 不能和 分组 一起用——分完组之后每一行代表一组，'
                        '而 * 要求逐列出来。要哪些列请写清楚')
                for alias, t in sources:
                    if e[1] is not None and e[1] != alias and e[1] != t.name:
                        continue
                    for c in t.columns:
                        entry = holder.get((alias, c.name))
                        vals.append(entry if entry is not None
                                    else (c.type, None))
                continue
            vals.append(self._value_of(st, e, holder, ctx, grouped))
        return vals

    def _value_of(self, st, e, holder, ctx, grouped):
        if not grouped:
            return self._eval(e, holder, ctx)
        bucket, kv = holder
        expr = self._subst(e, bucket, kv, st.group)
        for t in _walk(expr):
            if t and t[0] == 'col':
                raise StatementError(
                    '列 %s 不在 分组 里。分组之后每一行代表一整组，只有'
                    '分组用的列和聚合函数能出现在查询项里'
                    % (t[2] if t[1] is None else '%s.%s' % (t[1], t[2])))
        return self._eval(expr, {}, ctx)

    def _subst(self, e, bucket, kv, keys):
        """
        把聚合函数和分组列**换成算好的字面量**，剩下的交给 `_eval` 走原路。

        这样做的好处是 `求和(字数) / 计数(*)` 这种"聚合套算术"不用单写
        一套求值器：替换完它就是一棵普通表达式树。
        """
        # **list 也要递归。** 下面那句泛型递归只走 `e[1:]` 的**直接**元素，
        # 碰到 list 会原样返回 —— 而 `当 … 那么 …` 的分支表、`属于 (…)` 的
        # 候选表都是 list。不递归的话，分支里的聚合替换不掉，会在下面
        # `_value_of` 那句"列不在 分组 里"上炸（或者更糟：炸不出来，算错的）。
        # 这句一加，`x 属于 (最大(a), 最大(b))` 那个一直不好使的写法也通了。
        if isinstance(e, list):
            return [self._subst(x, bucket, kv, keys) for x in e]
        if not isinstance(e, tuple) or not e:
            return e
        for i, k in enumerate(keys):
            if k == e:
                val = kv[i]
                return ('nullval',) if val is None else ('lit', val[0], val[1])
        if e[0] == 'call':
            st = bucket.states.get(_freeze(e))
            if st is None:
                raise StatementError('聚合 %s 没被算过' % _expr_name(e))
            res = st.result()
            return ('nullval',) if res is None else ('lit', res[0], res[1])
        if e[0] in ('lit', 'nullval', 'param', 'col', 'star', 'default'):
            return e
        return tuple([e[0]] + [self._subst(x, bucket, kv, keys)
                               for x in e[1:]])

    def _result_columns(self, st, ctx, sources):
        out = []
        for item in st.items:
            e = item.expr
            if isinstance(e, tuple) and e and e[0] == 'star':
                for alias, t in sources:
                    if e[1] is not None and e[1] != alias and e[1] != t.name:
                        continue
                    for c in t.columns:
                        out.append((c.name, c.type))
                continue
            name = item.alias or _expr_name(e)
            t = self._expr_type(e, ctx)
            out.append((name, t if t is not None else TEXT))
        return out

    def _expr_type(self, e, ctx):
        if not isinstance(e, tuple) or not e:
            return None
        if e[0] == 'col':
            _alias, col = self._resolve(ctx, e[1], e[2])
            return col.type
        if e[0] == 'lit':
            return e[1]
        if e[0] == 'call':
            if e[1] == 'COUNT':
                return INT
            if e[1] in ('SUM', 'AVG'):
                return FLOAT
            if e[1] in ('MAX', 'MIN') and e[2] and e[2][0][0] != 'star':
                return self._expr_type(e[2][0], ctx)
            return None
        if e[0] == 'func':
            return _scalar_type(e[1], [self._expr_type(a, ctx) for a in e[2:]],
                                len(e) - 2)
        if e[0] == 'subq':
            # 列类型从**已经算出来的**那一次取（`_result_columns` 是在
            # 取完数之后才跑的，所以走到这儿通常已经缓存过了）。没缓存
            # 说明子查询一行都没出、`_project` 一次没跑——那就退回 None，
            # 让 `_result_columns` 给它 TEXT 列头。列头只影响显示：
            # 每一格存的是它自己的 (类型, 值)。
            hit = ctx.subq.get(id(e[1]))
            return hit[0][0] if hit and hit[0] else None
        if e[0] == 'case':
            # 各分支结果**类型一致**才敢报那个类型，不一致就报 None
            # （`_result_columns` 会退回 TEXT）。列头只影响"怎么显示"，
            # 每一格存的是它自己的 (类型, 值)，所以退成 TEXT 不会把
            # 整数格子读成文本 —— 但**能报准就报准**，别让客户端拿到
            # 一个全都写着"文本"的列头。
            types = [self._expr_type(b[2], ctx) for b in e[1]]
            if e[2] is not None:
                types.append(self._expr_type(e[2], ctx))
            if not types:
                return None
            first = types[0]
            if first is None:
                return None
            return first if all(t == first for t in types) else None
        return None

    def _sort(self, st, projected, ctx, grouped):
        # 先把每行的排序键算出来，**不要每比一次算一次**——比较次数是
        # n log n，而每次求值要跑整棵表达式树，差一个数量级。
        decorated = []
        for vals, holder in projected:
            kv = [self._order_value(o.expr, vals, holder, ctx, grouped, st)
                  for o in st.order]
            decorated.append((kv, vals, holder))

        def cmp(a, b):
            for i, o in enumerate(st.order):
                c = schema.compare(a[0][i], b[0][i])
                if c:
                    return -c if o.desc else c
            return 0

        decorated.sort(key=functools.cmp_to_key(cmp))
        return [(vals, holder) for _kv, vals, holder in decorated]

    def _order_value(self, e, vals, holder, ctx, grouped, st):
        # 排序键先按结果集的列名找。`按 条数 降序` 里的 条数 是个别名，
        # 源行里根本没有这一列——查不到别名再去源行找，两条路都要有。
        if isinstance(e, tuple) and e and e[0] == 'col' and e[1] is None:
            for i, item in enumerate(st.items):
                name = item.alias
                if name is None and item.expr[0] != 'star':
                    name = _expr_name(item.expr)
                if name == e[2]:
                    return vals[i]
        if not grouped:
            return self._eval(e, holder, ctx)
        bucket, kv = holder
        expr = self._subst(e, bucket, kv, st.group)
        return self._eval(expr, {}, ctx)

    # ── 表达式求值 ──────────────────────────────────────────────────

    def _resolve(self, ctx, qualifier, name):
        key = (qualifier, name)
        hit = ctx.memo.get(key)
        if hit is not None:
            return hit
        if qualifier is not None:
            for alias, t in ctx.sources:
                if alias == qualifier or t.name == qualifier:
                    col = t.column(name)
                    if col is None:
                        raise StatementError('表 %s 里没有列 %s'
                                             % (t.name, name))
                    ctx.memo[key] = (alias, col)
                    return ctx.memo[key]
            raise StatementError('没有 %s 这个表或别名' % qualifier)
        hits = []
        for alias, t in ctx.sources:
            col = t.column(name)
            if col is not None:
                hits.append((alias, col))
        if not hits:
            raise StatementError('没有 %s 这一列' % name)
        if len(hits) > 1:
            # **歧义要报，不能挑一个。** 挑一个的话，连接查询里同名列会
            # 安静地取到另一张表的那个，而结果看起来完全合理。
            raise StatementError('列名 %s 有歧义，请写成 表名.%s'
                                 % (name, name))
        ctx.memo[key] = hits[0]
        return hits[0]

    def _col_decl(self, e, ctx):
        """
        这个表达式如果是**一列**，给出那一列（声明的类型在它身上）；不是
        就给 None。

        **要的是声明的类型，不是那一行的值。** 那一行可能是空，空值没有
        类型；而"这一列是时间"是模式里写死的，什么时候都问得到。
        """
        if isinstance(e, tuple) and e and e[0] == 'col':
            return self._resolve(ctx, e[1], e[2])[1]
        return None

    def _align_col_val(self, e_col, ctx, val):
        """
        常数 `val`（一个 `(类型, 值)`）按 `e_col` 那一列的类型转过来。
        不是列、两边本来就比得了、或者任一边是空，都原样放回。

        给 `属于 (子查询)` 那条路用：那边右边是**值**，没有表达式节点。
        """
        if val is None:
            return val
        col = self._col_decl(e_col, ctx)
        if col is None or _pred_comparable(val[0], col.type):
            return val
        return _pred_value(col, val[1])

    def _align_pred(self, ea, eb, a, b, ctx):
        """
        谓词两边类型不同时，把**常数那一边按列的类型转过来**。

        只在这两种场合动手：一边是列、另一边不是。两边都是列（或都不是）
        原样放回 —— 那是另一件事，不在这里管。

        **只在"本来就比不了"的时候转**（`_pred_comparable`）：整数/实数/
        时间是一个数值族，`compare` 本来就算得对，去转只会把
        `其中 整数列 > 1.5` 这种好查询撞成报错。
        """
        if a is None or b is None or _pred_comparable(a[0], b[0]):
            return a, b
        ca = self._col_decl(ea, ctx)
        cb = self._col_decl(eb, ctx)
        if ca is not None and cb is None:
            return a, _pred_value(ca, b[1])
        if cb is not None and ca is None:
            return _pred_value(cb, a[1]), b
        return a, b

    def _eval(self, e, row, ctx):
        """求一个表达式。返回 (类型, 值) 或 None（空）。"""
        if not isinstance(e, tuple) or not e:
            raise StatementError('认不出的表达式：%r' % (e,))
        k = e[0]

        if k == 'lit':
            return (e[1], e[2])
        if k == 'nullval':
            return None
        if k == 'param':
            if e[1] >= len(ctx.params):
                raise StatementError('第 %d 个参数没给' % (e[1] + 1))
            v = ctx.params[e[1]]
            return None if v is None else (_param_type(v), v)
        if k == 'default':
            raise StatementError('默认 只能用在 插入 的值里')
        if k == 'col':
            alias, col = self._resolve(ctx, e[1], e[2])
            entry = row.get((alias, col.name))
            if entry is None:
                return None
            t, v = entry
            return None if v is None else (t, v)
        if k == 'star':
            raise StatementError('* 只能整列出现在查询项里')
        if k == 'not':
            return (INT, 0 if _truthy(self._eval(e[1], row, ctx)) else 1)
        if k == 'and':
            if not _truthy(self._eval(e[1], row, ctx)):
                return (INT, 0)
            return (INT, 1 if _truthy(self._eval(e[2], row, ctx)) else 0)
        if k == 'or':
            if _truthy(self._eval(e[1], row, ctx)):
                return (INT, 1)
            return (INT, 1 if _truthy(self._eval(e[2], row, ctx)) else 0)
        if k == 'neg':
            val = self._eval(e[1], row, ctx)
            n = _numeric(val)
            if n is None:
                return None
            return (_numeric_type(val), -n)
        if k == 'arith':
            return self._arith(e[1], self._eval(e[2], row, ctx),
                               self._eval(e[3], row, ctx))
        if k == 'cmp':
            a = self._eval(e[2], row, ctx)
            b = self._eval(e[3], row, ctx)
            # **任一边是空，比较就是假。** 解析期对 `= 空` 那句"结果永远是
            # 假"（sql.py 的 `_cmp`）是这个口径，运行期得说同一句话——不然
            # 同一件事写死值和写列名会得到两个答案：`其中 x = 空` 当场被
            # 拒，`其中 x = y`（y 那行是空）却是真。
            #
            # **不能改成"空等于空为真"就完事**：空值在这里是个"这格没数"，
            # 不是 SQL 里那个"未知"。`a = b` 问的是"两边的数一样吗"，任何
            # 一边没有数，答案都是"不"。这也让 `<>` 和 `=` 保持互斥——
            # 两个都是假，而不是一个真一个假。
            #
            # 注意别顺手把 `compare` 改了：那是 `排序`/`去重`/`分组` 用的
            # **全序**，那里"空比谁都大"是排得出来的必要条件（空值得有个
            # 位置），跟"两格相不相等"是两回事。两个语义各归各的。
            if a is None or b is None:
                return (INT, 0)
            a, b = self._align_pred(e[2], e[3], a, b, ctx)
            return (INT, 1 if _apply_cmp(e[1], schema.compare(a, b)) else 0)
        if k == 'null':
            val = self._eval(e[1], row, ctx)
            isnull = val is None
            return (INT, 1 if (isnull != bool(e[2])) else 0)
        if k == 'between':
            val = self._eval(e[1], row, ctx)
            lo = self._eval(e[2], row, ctx)
            hi = self._eval(e[3], row, ctx)
            val, lo = self._align_pred(e[1], e[2], val, lo, ctx)
            val, hi = self._align_pred(e[1], e[3], val, hi, ctx)
            inside = (schema.compare(val, lo) >= 0
                      and schema.compare(val, hi) <= 0)
            return (INT, 1 if (inside != bool(e[4])) else 0)
        if k == 'in':
            val = self._eval(e[1], row, ctx)
            hit = False
            for v in e[2]:
                cand = self._eval(v, row, ctx)
                val, cand = self._align_pred(e[1], v, val, cand, ctx)
                if schema.compare(val, cand) == 0:
                    hit = True
                    break
            return (INT, 1 if (hit != bool(e[3])) else 0)
        if k == 'like':
            val = self._eval(e[1], row, ctx)
            pat = self._eval(e[2], row, ctx)
            if val is None or pat is None:
                return (INT, 0)
            if not isinstance(val[1], str) or not isinstance(pat[1], str):
                return (INT, 0)
            hit = _like_regex(pat[1]).match(val[1]) is not None
            return (INT, 1 if (hit != bool(e[3])) else 0)
        if k == 'subq':
            # 子查询出现在"只能放一个值"的地方：`其中 x = (查询 …)`、
            # `查询 (查询 …) 从 t`。要求**一列**，而且至多**一行**。
            # 多行不挑一个——挑哪个都不对，而且挑错了没人看得出来。
            types, vals = self._subquery(e[1], ctx)
            if not vals:
                return None                     # 一行都没有就是空
            if len(vals) > 1:
                raise StatementError(
                    '这个子查询给了 %d 行，但它出现在只能放一个值的地方。'
                    '要跟一串值比请用 `属于`，要一行请加 限制 1'
                    % len(vals))
            return vals[0]
        if k == 'insub':
            val = self._eval(e[1], row, ctx)
            hit = False
            for v in self._subquery(e[2][1], ctx)[1]:
                if schema.compare(val,
                                  self._align_col_val(e[1], ctx, v)) == 0:
                    hit = True
                    break
            return (INT, 1 if (hit != bool(e[3])) else 0)
        if k == 'case':
            # 按序求条件，**第一个为真的分支赢**，一个都不中才走 `否则`。
            # 没有 `否则` 又一条都不中就是空 —— 不是 0，也不是报错：
            # 写 `当 状态 = 1 那么 '一' 结束` 的人，第 2 行本来就该是空。
            for _w, cond, val in e[1]:
                if _truthy(self._eval(cond, row, ctx)):
                    return self._eval(val, row, ctx)
            if e[2] is None:
                return None
            return self._eval(e[2], row, ctx)
        if k == 'func':
            return self._scalar(e[1], [self._eval(a, row, ctx)
                                       for a in e[2:]], e)
        if k == 'call':
            raise StatementError('%s 是聚合函数，只能跟 分组 一起用'
                                 % _expr_name(e))
        raise StatementError('认不出的表达式：%s' % (k,))

    def _subquery(self, sel, ctx):
        """
        跑一个子查询，返回 `(列类型表, 值表)`。值表里空是 `None`，和
        `_eval` 的约定一致（`Result.rows` 里空是 `(类型, None)`，是另一套
        写法，在这里就抹平，别让两套约定漏到求值器里去）。

        **结果按 `id(sel)` 缓存在语句级的 ctx 上。** 本库**没有关联子查询**
        ——子查询用的是自己的数据源，外层列在里面根本看不见，写了会报
        "没有 X 这一列"。既然答案和"这一行"无关，同一个子查询跑几次都
        是同一个结果，缓存就是对的；不缓存的话 `其中 x 属于 (查询 …)`
        是**每行跑一遍全表**。

        缓存键用 `id()` 而不是树本身：树里那个 `Select` 对象活得比这条
        语句长，不会被回收，`id` 不会串到别的对象上。
        """
        hit = ctx.subq.get(id(sel))
        if hit is not None:
            return hit
        # **参数用的是同一份**：`?` 的下标在一段文本里是绝对的
        # （见 `sql.Parser.__init__`），子查询里的 `?` 和外层的是同一批。
        res = self._exec_query(sel, ctx.params)
        if len(res.columns) != 1:
            raise StatementError(
                '子查询只能给一列，这里给了 %d 列。要一列请把别的列去掉；'
                '要几列并在一起请用 `并集` 反过来说' % len(res.columns))
        types = [t for _n, t in res.columns]
        vals = [None if r[0][1] is None else r[0] for r in res.rows]
        hit = (types, vals)
        ctx.subq[id(sel)] = hit
        return hit

    def _scalar(self, name, args, node):
        """
        标量函数。逐行算，一行一个输入（`拼接`/`截取` 除外，那俩吃多个）。

        **空值规则和算术一致：任何参数是空，结果就是空。** 不是三值逻辑，
        是"这个格子没数，所以算不出来"（见 `_arith` 那段）。唯一的例外是
        `类型(空)` —— 它问的**就是**"这是什么"，答 `'空'` 才是它该干的事。
        """
        # 先把参数的值取出来（已经是 (类型, 值) 或 None）
        vals = [None if a is None else a[1] for a in args]
        types = [None if a is None else a[0] for a in args]

        if name == 'TYPEOF':
            # **唯一的例外，所以它排在最前面。** 其余每个函数都在下面
            # 那句"有空就返回空"上返回 None，`类型(空)` 要是也走那条，
            # 用户拿到的是一格空 —— 而它问的问题本身是有答案的。
            if args[0] is None:
                return (TEXT, '空')
            return (TEXT, schema.TYPE_NAMES.get(types[0], '?'))

        if any(v is None for v in vals):
            return None

        if name == 'LENGTH':
            t, v = types[0], vals[0]
            if t == BLOB:
                return (INT, len(v))
            if t != TEXT:
                raise StatementError('长度 只认文本和二进制，这里是%s。'
                                     '要数字的位数请用 绝对值 或 四舍五入'
                                     % schema.TYPE_NAMES.get(t, '?'))
            # **按字符数，不按字节数。** 一个字一个长度是写的人要的答案；
            # 按字节数的话 '中' 会答 3，而没人想要那个答案。
            return (INT, len(v))

        if name == 'SUBSTR':
            t, v = types[0], vals[0]
            if t not in (TEXT, BLOB):
                raise StatementError('截取 只认文本和二进制，这里是%s'
                                     % schema.TYPE_NAMES.get(t, '?'))
            start = _as_int(vals[1], '截取 的第二个参数')
            if len(vals) == 3:
                count = _as_int(vals[2], '截取 的第三个参数')
            else:
                count = None
            # **下标从 1 起，负数从尾巴数。** 这是 SQL 的约定，也是中文
            # 用户脑子里想的那个（"第 1 个字符"）。从 0 起的话，
            # `截取(名, 1, 1)` 会悄悄少一个字，而那是个很安静的错。
            n = len(v)
            if start > 0:
                i = start - 1
            elif start < 0:
                i = n + start
            else:
                i = 0
            if start == 0:
                # 0 在 SQL 里是空串。**不猜**是"从 1 起"还是"从 0 起"。
                return (t, v[:0])
            i = max(0, min(i, n))
            j = n if count is None else max(0, min(i + count, n))
            return (t, v[i:j])

        if name == 'CONCAT':
            parts = []
            for t, v in zip(types, vals):
                if t not in (TEXT, BLOB):
                    # 数字也能拼，但得说清楚是怎么变的，不能悄悄 str()
                    raise StatementError('拼接 只认文本和二进制，'
                                         '有一项是%s。要拼数字请先转成文本'
                                         % schema.TYPE_NAMES.get(t, '?'))
                parts.append(v)
            if not parts:
                return (TEXT, '')
            # 混了文本和二进制就按二进制走，因为文本能当二进制读，
            # 反过来不行。
            if all(t == TEXT for t in types):
                return (TEXT, ''.join(parts))
            return (BLOB, b''.join(bytes(p) for p in parts))

        if name == 'REPLACE':
            t, v = types[0], vals[0]
            if all(t == TEXT for t in types):
                return (TEXT, v.replace(vals[1], vals[2]))
            if all(t in (TEXT, BLOB) for t in types):
                return (BLOB, bytes(v).replace(bytes(vals[1]),
                                               bytes(vals[2])))
            raise StatementError('替换 只认文本和二进制，'
                                 '有一项是%s'
                                 % schema.TYPE_NAMES.get(
                                     next(t for t in types
                                          if t not in (TEXT, BLOB)), '?'))

        if name in ('UPPER', 'LOWER', 'TRIM'):
            t, v = types[0], vals[0]
            if t != TEXT:
                raise StatementError(
                    '%s 只认文本，这里是%s'
                    % (_SCALAR_CN[name], schema.TYPE_NAMES.get(t, '?')))
            if name == 'UPPER':
                return (TEXT, v.upper())
            if name == 'LOWER':
                return (TEXT, v.lower())
            return (TEXT, v.strip())

        if name == 'ROUND':
            t, v = types[0], vals[0]
            n = _numeric(args[0])
            if n is None:
                raise StatementError('四舍五入 只认数字，这里是%s'
                                     % schema.TYPE_NAMES.get(t, '?'))
            digits = 0 if len(vals) == 1 else _as_int(
                vals[1], '四舍五入 的第二个参数')
            if digits < 0:
                # 负数位是"舍到十位/百位"。给了就说做不到，别当没看见。
                raise StatementError('四舍五入 的小数位数不能是负的')
            r = round(n, digits)
            # **整数输入返回整数**，不然 `四舍五入(1)` 会给出 1.0，
            # 而类型变了列头也跟着变，客户端解出来就是另一个东西。
            if t in (INT, TIME) and digits == 0:
                return (INT, _fit_int(r))
            return (FLOAT, float(r))

        if name == 'ABS':
            n = _numeric(args[0])
            if n is None:
                raise StatementError('绝对值 只认数字，这里是%s'
                                     % schema.TYPE_NAMES.get(types[0], '?'))
            return (_numeric_type(args[0]), abs(n))

        if name == 'NOW':
            return (TIME, schema.now_millis())

        raise StatementError('认不出的标量函数 %s' % name)

    def _arith(self, op, a, b):
        x = _numeric(a)
        y = _numeric(b)
        # **空一参与运算，结果就是空。** 这不是 SQL 的三值逻辑，是"这个
        # 格子没有数，所以算不出来"——区别在于本库不把空值在比较里也这么
        # 处理（比较里空值是个比谁都大的普通值）。
        if x is None or y is None:
            return None
        t = FLOAT if (a and a[0] == FLOAT) or (b and b[0] == FLOAT) else INT
        if op == '+':
            r = x + y
        elif op == '-':
            r = x - y
        elif op == '*':
            r = x * y
        elif op == '/':
            if y == 0:
                raise StatementError('除以零')
            return (FLOAT, float(x) / y)
        elif op == '%':
            if y == 0:
                raise StatementError('对零取余')
            r = x % y
        else:
            raise StatementError('认不出的运算符 %s' % op)
        if t == INT:
            return (INT, _fit_int(r))
        return (FLOAT, float(r))


# ── 聚合 ────────────────────────────────────────────────────────────

class _AggState(object):
    """
    一个聚合函数在一个分组上的累加器。

    `计数(*)` 数行数，`计数(列)` 数非空，`计数(去重 列)` 数不重复的非空
    值——这三件事长得像，结果差很远，所以是三条独立的路。
    """

    __slots__ = ('name', 'distinct', 'is_star', 'n', 'total', 'seen', 'best')

    def __init__(self, call):
        self.name = call[1]
        args = call[2]
        self.distinct = bool(call[3])
        self.is_star = bool(args) and args[0][0] == 'star'
        self.n = 0
        self.total = 0
        self.seen = set() if self.distinct else None
        self.best = None        # 最大/最小：存 (类型, 值)
        self._check()

    def _check(self):
        if self.is_star and self.name != 'COUNT':
            raise StatementError('%s(*) 没有意义，只有 计数(*) 认' % self.name)
        if self.is_star and self.distinct:
            raise StatementError('计数(去重 *) 和 计数(*) 是一回事，'
                                 '直接写 计数(*) 就行')

    def add(self, val):
        if self.name == 'COUNT':
            if self.is_star:
                self.n += 1
                return
            if val is None:
                return
            if self.distinct:
                k = _hashable(val[1])
                if k in self.seen:
                    return
                self.seen.add(k)
            self.n += 1
            return
        if val is None:
            # **空值不进 求和/平均/最大/最小。** 求和里空当 0 是错的
            # （"没有值"和"值是 0"是两回事）；平均里空当 0 更错，
            # 分母会被算大。
            return
        if self.distinct:
            k = _hashable(val[1])
            if k in self.seen:
                return
            self.seen.add(k)
        if self.name in ('MAX', 'MIN'):
            c = schema.compare(val, self.best)
            if self.best is None or (c > 0 if self.name == 'MAX' else c < 0):
                self.best = val
            return
        num = _numeric(val)
        if num is None:
            return
        self.total += num
        self.n += 1

    def result(self):
        if self.name == 'COUNT':
            return (INT, self.n)
        if self.name == 'SUM':
            if self.n == 0:
                return None
            if isinstance(self.total, float):
                return (FLOAT, float(self.total))
            return (INT, int(self.total))
        if self.name == 'AVG':
            if self.n == 0:
                return None
            return (FLOAT, float(self.total) / self.n)
        if self.name in ('MAX', 'MIN'):
            return self.best
        raise StatementError('认不出的聚合函数 %s' % self.name)


class _Bucket(object):
    """一个分组：每个聚合函数一份累加器。"""

    __slots__ = ('states',)

    def __init__(self, aggs):
        self.states = {_freeze(a): _AggState(a) for a in aggs}

    def add(self, row, aggs, syn, ctx):
        for call in aggs:
            args = call[2]
            if not args or args[0][0] == 'star':
                self.states[_freeze(call)].add(None)
                continue
            self.states[_freeze(call)].add(syn._eval(args[0], row, ctx))


# ── 小函数 ──────────────────────────────────────────────────────────

def _dim_of_value(v):
    """
    一个值能不能当维度用。不能就给 None。

    负数和越界的都挡掉：负数写进去会变成一个巨大的正数，把区间整个撑飞
    ——那时候裁剪会以为某个区覆盖了从 0 到 2^64 的范围，于是每次查询都
    扫全部。**不报错，只是慢**，这种最难查。所以在入口就说清楚。

    **上界是 2^63-1，不是 2^64-1。** 封区时上下界是按
    `struct.pack('<qqB', …)`（**有符号**）写下去的，所以
    `2^63` 到 `2^64-1` 这一段今天能在入口过、却会在封区时抛一个
    跟"维度"毫无关系的 `struct.error` ——而且是在很久以后、由某次
    不相干的写引出来的。宁可在这里当场说清楚。
    """
    if v is None or isinstance(v, bool) or not isinstance(v, int):
        return None
    if v < 0:
        raise StatementError('维度不能是负数：%d' % v)
    if v > schema.INT64_MAX:
        raise StatementError(
            '维度超过 64 位有符号整数的上界 %d：%d。维度盘上是按**有符号**'
            '64 位存的，再大就只能等到封区时炸一个读不懂的错'
            % (schema.INT64_MAX, v))
    return v


def _dim_loose(tbl, dim, values):
    """
    这一行的"维度"跟它的"维度列的值"是不是两回事。

    裁剪拿维度列上的谓词去比各区的维度区间，**这一步隐含了这两者相等**。
    一不等，那个区就得一直算候选，否则按维度列查会静默漏掉这一行。

    比的是值本身，不是"写没写 `维度`"：`维度 时候` 和 `维度 ?`（参数正好
    等于维度列的值）都还是相等的，没必要因此把整个区放宽——那种写法很
    常见，一放宽裁剪就全废了。
    """
    if not tbl.dim_column:
        return True
    return dim != _dim_of_value(values.get(tbl.dim_column))


def _dim_track_id(tbl):
    """
    这个表写进去的行要不要参与维度记账。

    **没有维度列的表不参与。** 它的行维度永远是 None，参与进来就会把
    每一个区都标成"松散"，`显示 区域` 里满屏的 1；而维度裁剪本来就
    不对它生效（`_dim_window` 直接返回 None），标了也是白标。
    所以 None 在这里的意思很明确：不是"这行没维度"，是"这张表不玩
    维度"。
    """
    return tbl.table_id if tbl.dim_column else None


def _pk_text(tbl, values):
    return '，'.join('%s=%s' % (p, schema.render(tbl.require(p).type,
                                                values[p]))
                     for p in tbl.pk)


# ── 索引表 ──────────────────────────────────────────────────────────
#
# 索引表是一张**普通表**：占真表号、走同一套行键和读写，只是多一个
# `index_of` 字段让上层认得出它。这不是偷懒 —— 另起一套存储形态意味着
# 另起一套崩溃恢复、另一套编码、另一套区域记账，而这个引擎的每一条
# 不变式都是围着"普通表"建立的。

INDEX_SUFFIX = '索引'


def index_name(base, columns):
    """
    索引表的名字：`<基表>_<列1>_<列2>_索引`。

    **从基表名和列清单算出来，不另存一份。** 存一份就要保证它和那两样
    东西永远一致，而"两处记同一件事"迟早会不一致 —— `删索引` 正是拿
    这个名字去找表的，对不上就删不掉一张确实存在的索引。
    """
    return '%s_%s_%s' % (base, '_'.join(columns), INDEX_SUFFIX)


def index_v(t, v):
    """
    索引条目键里的一截：**空值也得能表示**。

    `encode_value` 编不出 `None`（它编的是"有值"的那些格），而索引列
    允许为空。所以自己加一字节前缀：`\\x00` 是空、`\\x01` 是值。
    不加这一字节的话，空值和"值恰好编成空字节串"会拼出同一个键 ——
    在索引里这两件事必须分得开，否则一行的条目会顶掉另一行的。
    """
    if v is None:
        return b'\x00'
    return b'\x01' + schema.encode_value(t, schema.coerce(t, v))


def indexes_of(syn, base):
    """
    这张基表上所有的索引表。

    **每次现扫，不建缓存。** 表就那么几张，扫一遍是个微不足道的开销；
    而缓存要跟着 `建索引` / `删索引` / `删表` / 开库四处更新，漏一处
    就会变成"索引条目写到了别的表上"或"该写没写"——后者是静默漏行。
    这个引擎的写法是宁可多扫一块盘。
    """
    return [t for t in syn.tables.values()
            if t.index_of and t.index_of[0] == base.name]


def index_values(idx, base, values):
    """这一行在某个索引的"被索引列"上的值（按索引声明的次序）。"""
    return [values.get(name) for name in idx.index_of[1]]


def index_key(idx, base, values):
    """
    索引条目的键：`行前缀 + V(值1)…V(值n) + V(基表主键1)…`。

    **尾部那截基表主键是必需的。** 索引是**非唯一**的：同一组值在基表
    里可以有任意多行。值一样时靠尾部主键把它们分开；少了这一截，后写
    的那行会顶掉前一行 —— 而在这个只追加的引擎里，"顶掉"就是**静默
    少行**：查出来只有一条，谁也看不出来还有别的。

    也不用 `Table.key_for`：索引表的键是"索引列 + 基表主键"两段拼的，
    跟它自己声明的列没有关系（它压根没声明主键），所以专门拼。
    """
    out = bytearray(schema.row_prefix(idx.table_id))
    for name in idx.index_of[1]:
        col = base.require(name)
        out += index_v(col.type, values.get(name))
    for name in base.pk:
        col = base.require(name)
        out += index_v(col.type, values.get(name))
    return bytes(out)


def index_split(idx, base, key):
    """
    条目键 -> 基表主键值（按 `base.pk` 的次序）。

    **反向切分靠的是 `encode_value` 的自定长**：每一截自带长度，所以
    键里不用另存偏移。切到哪一列是**按声明次序一路数过来的** —— 值
    和值之间没有分隔符，找也找不到，只有"从头一截一截解"这一条路。

    切不动（键不是这张索引表的，或者字节被截过）就返回 `None`，调用方
    一律当作"这条条目作废"。**不许猜**：猜出来的主键会去点查别人的行，
    于是查询结果里混进一条根本不该出现的记录，而它看起来完全正常。
    """
    head = schema.row_prefix(idx.table_id)
    if key[:len(head)] != head:
        return None
    types = [base.require(n).type for n in idx.index_of[1]] + \
            [base.require(n).type for n in base.pk]
    pos = len(head)
    vals = []
    try:
        for t in types:
            mark = key[pos:pos + 1]
            if mark == b'\x00':
                vals.append(None)
                pos += 1
                continue
            if mark != b'\x01':
                return None
            v, pos = schema.decode_value(t, key, pos + 1)
            vals.append(v)
    except (ValueError, IndexError, schema.SchemaError):
        return None
    return vals[len(idx.index_of[1]):]


def index_dim(idx, base, values):
    """
    这一条索引条目该带什么维度：**第一列被索引值的保序秩**。

    组合索引只用第一列的秩。为什么不是"所有列拼成一个数"：维度是有
    符号 64 位，塞得下一列就塞不下第二列的位宽了，硬拼就是把两列各自
    的取值范围都砍掉一半以上 —— 那样裁出来的窗口**比该有的窄**，
    是静默漏行。只用第一列则窗口宽而保守，多出来的候选交给复验。

    这也正是"前缀匹配"的物理来源：读的时候能把窗口收窄的，只有排在
    最前面那一列。

    表上是 `INT64_MAX` 而不是别的 —— 秩的定义里空值排最后，和全库
    「空比任何值都大」是同一个约定。
    """
    name = idx.index_of[1][0]
    return schema.rank(base.require(name).type, values.get(name))


# ── 语句分发 ────────────────────────────────────────────────────────

def _map(fn):
    """把签名统一的执行函数包成 `(syn, st, params, text)` 的样子。"""
    def call(syn, st, params, text=None):
        return fn(syn, st, params, text)
    return call


_DISPATCH = {
    sql.BatchBegin: _map(Syntax._batch_begin),
    sql.BatchCommit: _map(Syntax._batch_commit),
    sql.Compact: _map(Syntax._compact),
    sql.CreateTable: _map(Syntax._create_table),
    sql.DropTable: _map(Syntax._drop_table),
    sql.AlterTable: _map(Syntax._alter_table),
    sql.CreateIndex: _map(Syntax._create_index),
    sql.DropIndex: _map(Syntax._drop_index),
    sql.RebuildIndex: _map(Syntax._rebuild_index),
    sql.CheckIndex: _map(Syntax._check_index),
    sql.Insert: _map(Syntax._insert),
    sql.Update: _map(Syntax._update),
    sql.Delete: _map(Syntax._delete),
    sql.Select: _map(Syntax._exec_select),
    sql.SetOp: _map(Syntax._exec_setop),
    sql.Show: _map(Syntax._show),
    sql.CreateUser: _map(Syntax._create_user),
    sql.DropUser: _map(Syntax._drop_user),
    sql.AlterUser: _map(Syntax._alter_user),
    sql.Grant: _map(Syntax._grant),
    sql.Revoke: _map(Syntax._revoke),
}


# 上面这张表（谁由哪个函数执行）和 `user._权限要求`（每个要什么权限）
# **必须一一对应**。加第 22 种语句时忘了往权限表里补一行，那就是一条
# **对所有人敞开**的语句——而出事之前没人会去比对这两张表。
#
# 所以让它在**导入时**就炸，照 `protocol.py` 那条 `assert _HEADER.size`
# 的样子：加语句的人一定会跑一次程序，那天就会撞上。
assert set(user_mod._权限要求) == set(_DISPATCH), \
    '语句分发和权限表对不上：%s' % sorted(
        c.__name__ for c in set(user_mod._权限要求) ^ set(_DISPATCH))
