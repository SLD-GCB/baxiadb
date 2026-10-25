# -*- coding: utf-8 -*-
"""
霸下语法层 — 值模型、表结构与键编码
====================================

引擎（`arena` / `mortise` / `store_format`）把键和值当**不透明的字节**，
只保证写进去的原样读得回来。这一层做的事就是给那些字节赋予意义。

具体是三件：

  值怎么编   五种标量（整数/实数/文本/二进制/时间），每种一套**自定界**
             的编码。自定界不是讲究——主键可以由多列拼成，拼起来必须
             能毫无歧义地再拆开，所以每个值自己带着"我有多长"。
             整数用 zigzag 变长，小的占一个字节；文本二进制带长度前缀。

  行怎么编   每个单元格带列号，不是按位置排。**这是为了让改表不用重写
             数据**：加一列，老行的字节一个不动，读出来缺的那格是空；
             删一列，老行里那格的字节还在，只是没人再去看它。
             代价是每格 7 个字节的头，换的是一句改表不用搬全表。
             值得。

  键怎么编   行键 = `\\x01` + 表号 + 主键编码
             系统键 = `\\x00` + 名字
             第一个字节就把两片空间隔死。主键不管怎么编、多长、什么
             类型，都撞不进系统区——这一点由编码结构保证，不靠约定。

**只有这一层认识"删除"。** 引擎不认识它，理由写在 arena.py 的
「引擎不管什么」那一节。这里用一个单字节的墓碑值表达，读到它就是
"这行没了"。判断在现场做，不靠引擎替我们记住。

关于时间列
----------

盘上就是整数（epoch 毫秒），和整数列同一套编码。加它不是为了省字
节，是因为爬存一体的数据的维度列十有八九是时间：没有它，每次写要
在调用方手算毫秒、每次读要自己格式化回来，两边的错法还不一样。

**数字一律按毫秒解释。** 于是有个必然会踩的坑：把秒当毫秒写进去，
得到的是 1970 年。所以 `coerce` 在这里设了一道闸——一个正整数如果
小得只可能是秒（小于 1973 年，即 100000000000），直接报错并在消息
里说"乘 1000"，而不是默默存下一个 1970 年的值。宁可吵一声。
"""

import datetime
import json
import struct

__all__ = [
    'INT', 'FLOAT', 'TEXT', 'BLOB', 'TIME', 'TYPE_NAMES', 'TYPE_ALIASES',
    'NULL_LEN', 'ROW_TAG', 'TOMB_TAG', 'TOMBSTONE',
    'encode_value', 'decode_value', 'coerce', 'compare', 'render', 'rank',
    'wrap_row', 'unwrap_row', 'is_live',
    'encode_cells', 'decode_cells',
    'Column', 'Table', 'SchemaError', 'NO_DEFAULT',
    'row_prefix', 'row_key', 'catalog_key', 'table_key', 'dim_key',
]

# ── 类型 ────────────────────────────────────────────────────────────
# 号不连续是故意的：以后要在中间插一种类型（比如 8 字节整数）时，
# 老库里的类型号不用挪，也就不会把已存的数据解释错。

INT = 1        # 有符号 64 位，zigzag 变长
FLOAT = 2      # IEEE754 双精度，8 字节定长
TEXT = 3       # UTF-8，长度前缀
BLOB = 4       # 原样字节，长度前缀
TIME = 5       # epoch 毫秒，编码同 INT

TYPE_NAMES = {INT: '整数', FLOAT: '实数', TEXT: '文本',
              BLOB: '二进制', TIME: '时间'}

# 建表时怎么称呼这些类型。中英都收，缩写和全称都收——
# 让人猜"到底该写 VARCHAR 还是 TEXT"是没必要的心智负担。
TYPE_ALIASES = {
    '整数': INT, 'INT': INT, 'INTEGER': INT, 'BIGINT': INT, 'T_INT': INT,
    '实数': FLOAT, '浮点': FLOAT, '小数': FLOAT,
    'FLOAT': FLOAT, 'DOUBLE': FLOAT, 'REAL': FLOAT, 'T_FLOAT': FLOAT,
    '文本': TEXT, '字符串': TEXT, '字符': TEXT,
    'TEXT': TEXT, 'STRING': TEXT, 'VARCHAR': TEXT, 'CHAR': TEXT,
    'T_TEXT': TEXT,
    '二进制': BLOB, '字节串': BLOB,
    'BLOB': BLOB, 'BYTES': BLOB, 'BINARY': BLOB, 'T_BLOB': BLOB,
    '时间': TIME, '时间戳': TIME,
    'TIME': TIME, 'TIMESTAMP': TIME, 'DATETIME': TIME, 'T_TIME': TIME,
}

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1

# 小于这个数的正整数只可能是"秒"，不可能是"毫秒"。见 coerce 里的说明。
_MS_SANITY_FLOOR = 100000000000       # 1973-03-03 前后


class SchemaError(Exception):
    """模式层面的拒绝：类型对不上、主键缺失、表不存在，等等。"""


# ── 变长整数 ────────────────────────────────────────────────────────

def _put_uvarint(out, n):
    """无符号变长整数，每字节 7 位，最高位是"还有后续"。"""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def _get_uvarint(buf, pos):
    shift = 0
    acc = 0
    while True:
        if pos >= len(buf):
            raise ValueError('变长整数在中间断了')
        b = buf[pos]
        pos += 1
        acc |= (b & 0x7F) << shift
        if not b & 0x80:
            return acc, pos
        shift += 7
        if shift > 63:
            raise ValueError('变长整数超过 64 位')


def _zigzag(n):
    """把有符号整数折成无符号：小负数也只剩一两个字节。

    Python 的整数是任意精度的，算术右移对负数补的是符号位，
    所以 `n >> 63` 在 int64 范围内正好是 0（非负）或 -1（负）。
    """
    return (n << 1) ^ (n >> 63)


def _unzigzag(u):
    return (u >> 1) ^ -(u & 1)


# ── 值的编码 ────────────────────────────────────────────────────────
# 关键性质：**每个值自己带着长度**。所以一串值首尾相接之后还能原样
# 拆开，复合主键就是这么拼出来的。

def encode_value(t, v):
    if t in (INT, TIME):
        out = bytearray()
        _put_uvarint(out, _zigzag(int(v)))
        return bytes(out)
    if t == FLOAT:
        return struct.pack('<d', float(v))
    if t == TEXT:
        raw = v if isinstance(v, (bytes, bytearray)) else str(v).encode('utf-8')
    elif t == BLOB:
        raw = bytes(v)
    else:
        raise SchemaError('认不出的类型号 %d' % t)
    out = bytearray()
    _put_uvarint(out, len(raw))
    out += raw
    return bytes(out)


def decode_value(t, buf, pos):
    """返回 (值, 新位置)。"""
    if t in (INT, TIME):
        u, pos = _get_uvarint(buf, pos)
        return _unzigzag(u), pos
    if t == FLOAT:
        if pos + 8 > len(buf):
            raise ValueError('实数少字节：需要 8，剩 %d' % (len(buf) - pos))
        return struct.unpack_from('<d', buf, pos)[0], pos + 8
    if t in (TEXT, BLOB):
        n, pos = _get_uvarint(buf, pos)
        if pos + n > len(buf):
            raise ValueError('值长度 %d 越界，只剩 %d' % (n, len(buf) - pos))
        raw = bytes(buf[pos:pos + n])
        return (raw.decode('utf-8') if t == TEXT else raw), pos + n
    raise ValueError('认不出的类型号 %d' % t)


# ── 时间的读写 ──────────────────────────────────────────────────────

_EPOCH = datetime.datetime(1970, 1, 1)
_MS = datetime.timedelta(milliseconds=1)

_TIME_FORMATS = (
    '%Y-%m-%d %H:%M:%S.%f',
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%d %H:%M',
    '%Y-%m-%d',
)


def parse_time(s):
    """时间字符串 -> epoch 毫秒。收不下就抛，不做猜测。"""
    text = s.strip().replace('T', ' ')
    if text.endswith('Z'):
        text = text[:-1].strip()
    for fmt in _TIME_FORMATS:
        try:
            dt = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return (dt - _EPOCH) // _MS
    raise SchemaError('认不出这个时间：%r。'
                      '可以写 2026-09-12 或 2026-09-12 10:30:00'
                      '（也可以直接给毫秒数）' % s)


def now_millis():
    """
    此刻，毫秒。

    **和 `parse_time` / `format_time` 同一把尺子**：朴素本地时间。
    `parse_time` 走的是 `strptime`（朴素），要是这里换成 UTC，`现在()`
    和用户手写的时间串之间会差出一个时区 —— 而且夏令时一来还变形，
    那种错最难查，因为它只在一年里的某些日子出现。
    """
    return (datetime.datetime.now() - _EPOCH) // _MS


def format_time(ms):
    dt = _EPOCH + datetime.timedelta(milliseconds=ms)
    if ms % 1000:
        return '%s.%03d' % (dt.strftime('%Y-%m-%d %H:%M:%S'), ms % 1000)
    return dt.strftime('%Y-%m-%d %H:%M:%S')


# ── 值的规整 ────────────────────────────────────────────────────────

def coerce(t, v):
    """
    把外面给的值规整成该类型的规范形式。不合规就**抛**，不将就。

    这里所有的严格都是有意的。一个数据库最不该做的事，是把一个它
    看不懂的值悄悄改成另一个值存下来——错的类型往后会以别人完全
    想不到的方式冒出来，而且到那时已经查不出是哪一次写入干的。
    """
    if v is None:
        return None

    if t == INT:
        # **布尔要单独挡掉**：Python 里 True 就是 1，不挡的话
        # `插入 真` 会安安静静变成整数 1，而写的人以为在写布尔。
        if isinstance(v, bool):
            raise SchemaError('整数列不收真假值，请写 1 或 0')
        if isinstance(v, int):
            n = v
        elif isinstance(v, float):
            if v != int(v):
                raise SchemaError('整数列不收小数：%r' % v)
            n = int(v)
        elif isinstance(v, str):
            try:
                n = int(v.strip(), 10)
            except ValueError:
                raise SchemaError('这不是个整数：%r' % v)
        else:
            raise SchemaError('整数列认不出这个值：%r' % (v,))
        if not INT64_MIN <= n <= INT64_MAX:
            raise SchemaError('整数越界（64 位有符号）：%d' % n)
        return n

    if t == FLOAT:
        if isinstance(v, bool):
            raise SchemaError('实数列不收真假值')
        if isinstance(v, str):
            try:
                v = float(v.strip())
            except ValueError:
                raise SchemaError('这不是个实数：%r' % v)
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise SchemaError('实数列认不出这个值：%r' % (v,))
        # 无穷和 NaN 会让排序和比较失去传递性，后面的排序结果就不可信了。
        # 与其让它们在盘上活着，不如现在拒绝。
        if f != f or f in (float('inf'), float('-inf')):
            raise SchemaError('实数列不收无穷或 NaN')
        return f

    if t == TIME:
        if isinstance(v, bool):
            raise SchemaError('时间列不收真假值')
        if isinstance(v, str):
            text = v.strip()
            try:
                return int(text, 10)
            except ValueError:
                return parse_time(text)
        if isinstance(v, float):
            if v != int(v):
                raise SchemaError('时间列不收小数秒，请给整数毫秒')
            v = int(v)
        if not isinstance(v, int):
            raise SchemaError('时间列认不出这个值：%r' % (v,))
        # 秒 / 毫秒这道闸。见模块开头那段说明。
        if 0 < v < _MS_SANITY_FLOOR:
            raise SchemaError(
                '时间 %d 看着像秒。本列按**毫秒**存——'
                '如果你给的是秒，乘 1000；如果是 1970 年代初的时间，'
                '请直接写日期字符串' % v)
        return v

    if t == TEXT:
        if isinstance(v, (bytes, bytearray)):
            return bytes(v).decode('utf-8')
        if isinstance(v, str):
            return v
        return str(v)          # 数字或别的，转成文本是它想要的

    if t == BLOB:
        if isinstance(v, (bytes, bytearray, memoryview)):
            return bytes(v)
        if isinstance(v, str):
            return v.encode('utf-8')
        raise SchemaError('二进制列认不出这个值：%r' % (v,))

    raise SchemaError('认不出的类型号 %d' % t)


def compare(a, b):
    """
    比较两个 (类型, 值)，或者 None（空）。

    **空值比任何值都大**，于是升序时空值排在最后、降序时排在最前。
    这只是个约定，但必须是全库统一的约定——排序和去重都靠它。
    """
    if a is None and b is None:
        return 0
    if a is None:
        return 1
    if b is None:
        return -1
    ta, va = a
    tb, vb = b
    if ta == tb or ta in (INT, FLOAT, TIME) and tb in (INT, FLOAT, TIME):
        return (va > vb) - (va < vb)
    # 类型不同又不可比。按类型号定序，至少是稳定的——一个随便但一致的
    # 次序，也比每次排序给出不同结果强。
    return (ta > tb) - (ta < tb)


def rank(t, v):
    """
    一个值在它那一类里的**保序秩**：`a <= b` 就一定有
    `rank(a) <= rank(b)`（反过来不成立，见下）。

    要它是干什么的：**维度列只能是整数**（`_dim_of_value` 那条线，
    引擎的维度是有符号 64 位打包的），而索引列可能是文本。秩就是把
    "这个能排序"搬进整数的桥 —— 索引表拿它当自己的维度列，于是范围
    查询在索引表上**真的能跳区**，而不是名义上有维度、实际上每次全扫。

    **有损，而且是有意有损，用它的地方必须按这个前提写。**
    值域要落在 `[0, 2^63-1]`：`TEXT` 的全部次序、`INT` 的全部取值
    都装不进那么多个整数（`INT` 有 2^64 个取值，整数只有 2^63 个），
    所以只能保序、不能保区分。落在同一格的值会被判成同一个秩。
    于是按秩裁出来的窗口**只会偏宽，不会偏窄**：偏宽是假阳性，后面
    复验筛得掉；偏窄才是静默漏行。所以用它的地方一律**只收包含边界**
    （`_dim_window(..., lossy=True)`），不许用 `±1` 去收紧 —— 那正是
    "前 8 个字节一样的两个值被裁到区外、结果少一行"的那个坑。

    空给 `INT64_MAX`，和全库「**空比任何值都大**」是同一个约定，
    不是特例。
    """
    if v is None:
        return INT64_MAX
    if t == INT or t == TIME:
        if isinstance(v, bool) or not isinstance(v, int):
            raise SchemaError('整数列上的秩算不出非整数：%r' % (v,))
        if not INT64_MIN <= v <= INT64_MAX:
            raise SchemaError('整数越界（64 位有符号）：%d' % v)
        # 平移到 [0, 2^64-1] 再右移一位压进 [0, 2^63-1]。右移是单调的，
        # 丢的是最低那一位的区分度（相邻两个整数会同秩）。
        return (v + (1 << 63)) >> 1
    if t == FLOAT:
        if v != v:
            # NaN 和谁都不相等，`compare` 给出的次序对它没有意义，
            # 硬编一个秩出来等于替调用方猜。猜错是**静默错答案**，
            # 所以这里报出来。
            raise SchemaError('NaN 没有次序，当不了索引列的值。'
                              '要么别索引这一列，要么把它换成别的值')
        bits = struct.unpack('<Q', struct.pack('<d', v))[0]
        # IEEE754 的保序整数映射：正数置符号位，负数整个取反。
        # 做完之后 `bits` 的相对大小就和浮点值的一致了。
        if bits >> 63:
            bits = ~bits & 0xFFFFFFFFFFFFFFFF
        else:
            bits |= 1 << 63
        return bits >> 1
    if t == TEXT:
        raw = v.encode('utf-8')
    elif t == BLOB:
        raw = bytes(v)
    else:
        raise SchemaError('认不出的类型号 %d' % t)
    # 只取前 8 个字节当大端整数。
    # **为什么这跟 `compare` 是同一个序**：`compare` 对文本走 Python 的
    # `str` 比较 = 码点序，而 UTF-8 的字节序就是码点序；对二进制走
    # `bytes` 比较 = 无符号字节字典序，而"前 8 字节的大端整数"正是它的
    # 前缀序。短于 8 字节的补零 —— 补零不会把次序弄反，只会让
    # "一个是另一个的前缀"那两个同秩（那也是偏宽，是安全的那一边）。
    return int.from_bytes((raw + b'\x00' * 8)[:8], 'big') >> 1


def render(t, v):
    """
    值 -> 上线路的样子。

    文本和二进制给回原样（一个是 str 一个是 bytes），由帧编码那边
    按类型决定怎么装；数字和时间转成人看得懂的串。
    """
    if v is None:
        return None
    if t == TIME:
        return format_time(v)
    if t == TEXT:
        return v
    if t == BLOB:
        return v
    if t == FLOAT:
        # repr 在 Python 3 里给的是能原样读回来的最短表示，
        # 所以 0.1 显示成 0.1，不是 0.1000000000000000055。
        return repr(v)
    return str(v)


# ── 行 ──────────────────────────────────────────────────────────────
# 盘上那一格一格的东西。布局：
#
#   [格数:2] 然后每格 [列号:2][类型:1][长度:4][字节...]
#
# 空值的长度是 NULL_LEN，后面不跟字节。

_CELL_HEAD = struct.Struct('<HBI')
_NCELLS = struct.Struct('<H')

NULL_LEN = 0xFFFFFFFF


def encode_cells(cells):
    """cells 是 (列号, 类型, 值) 的序列，值为 None 表示空。"""
    out = bytearray(_NCELLS.pack(len(cells)))
    for col_id, t, v in cells:
        if v is None:
            out += _CELL_HEAD.pack(col_id, t, NULL_LEN)
            continue
        raw = encode_value(t, v)
        out += _CELL_HEAD.pack(col_id, t, len(raw))
        out += raw
    return bytes(out)


def decode_cells(raw, pos=0):
    """返回 ({列号: (类型, 值)}, 新位置)。坏字节抛 ValueError。"""
    if pos + _NCELLS.size > len(raw):
        raise ValueError('行头不完整')
    n = _NCELLS.unpack_from(raw, pos)[0]
    pos += _NCELLS.size
    out = {}
    for i in range(n):
        if pos + _CELL_HEAD.size > len(raw):
            raise ValueError('行在第 %d 格断掉' % i)
        col_id, t, ln = _CELL_HEAD.unpack_from(raw, pos)
        pos += _CELL_HEAD.size
        if ln == NULL_LEN:
            out[col_id] = (t, None)
            continue
        if pos + ln > len(raw):
            raise ValueError('行第 %d 格越界' % i)
        chunk = raw[pos:pos + ln]
        pos += ln
        value, used = decode_value(t, chunk, 0)
        if used != len(chunk):
            # 长度域和内容对不上。这种行不能信——它意味着要么写的时候
            # 就错了，要么盘上的字节被改过。宁可报出来。
            raise ValueError('行第 %d 格用完 %d 字节，声明的却是 %d'
                             % (i, used, ln))
        out[col_id] = (t, value)
    return out, pos


# ── 值的包装：活行 / 墓碑 ───────────────────────────────────────────

ROW_TAG = 0x01
TOMB_TAG = 0x00

# 上层表达"删除"的方式。**引擎不认识这个字节**，它只看到一次普通的
# 写入——那正是它该看到的。
TOMBSTONE = bytes([TOMB_TAG])


def wrap_row(cells_raw):
    return bytes([ROW_TAG]) + cells_raw


def unwrap_row(value):
    """
    取出行字节。墓碑返回 None。

    **认不出的标记直接抛**，不当作"没有"。往后加新标记时，老代码
    必须响亮地失败而不是安静地漏掉行——漏掉行是一种没人会发现的错。
    """
    if not value:
        raise ValueError('行值是空的，连标记都没有')
    tag = value[0]
    if tag == TOMB_TAG:
        return None
    if tag == ROW_TAG:
        return value[1:]
    raise ValueError('认不出的行标记 %d（这个库可能来自更新的版本）' % tag)


def is_live(value):
    """这个值是不是一行活着的数据。"""
    return bool(value) and value[0] == ROW_TAG


# ── 键 ──────────────────────────────────────────────────────────────

_SYS_PREFIX = b'\x00'
_ROW_PREFIX = b'\x01'

# 系统键的名字。都短，因为它们在盘上要出现很多次。
K_CATALOG = b'\x00c'      # 目录本身：下一个表号等等
K_TABLE = b'\x00t'        # + 表名
K_DIM = b'\x00d'          # + 表号(2) + 区域号(4)
K_USER = b'\x00u'         # + 用户名


def row_prefix(table_id):
    """一个表全部行键的公共前缀。扫描时靠它筛表。"""
    return _ROW_PREFIX + struct.pack('<H', table_id)


def row_key(table_id, pk_bytes):
    return row_prefix(table_id) + pk_bytes


def catalog_key():
    return K_CATALOG


def table_key(name):
    return K_TABLE + name.encode('utf-8')


def user_key(name):
    """
    一个用户一条记录。**名字直接编进键里**，不另发号。

    表要发号是因为行键里编的是表号（`row_prefix`）。用户没有行、没有
    行键，名字就是它唯一的身份——再发一个号只会多出一份"号→名"的
    映射要维护，而那份映射坏了就是"用户还在、但没人找得到他"。

    **它和行键不在一个前缀下**：行扫描一律带 `row_prefix(tid)` 过滤，
    所以这条记录不会被任何取数路径读到，反过来也不会污染行。要拿它
    只有显式 `get` 一条路——这是刻意的，它不该出现在任何"顺便扫出来"
    的结果里。
    """
    return K_USER + name.encode('utf-8')


def dim_key(table_id, rid):
    """
    某个表在某个区里的维度区间。**一区一条**，封区时写一次。

    不把所有区的区间攒成一条记录，是因为那样每封一个区就要把整条
    重写一遍——库里区越多写得越勤，总量是平方级的。一区一条的话
    每条只写一次，永远。
    """
    return K_DIM + struct.pack('<HI', table_id, rid)


# ── 表结构 ──────────────────────────────────────────────────────────

# 「这列没写默认值」和「这列的默认值是空」是两件事，用一个哨兵分开。
# 前者插入时缺列填空，后者也填空——今天长得一样，但将来要是不是了，
# 不需要回来改数据。
NO_DEFAULT = object()


class Column(object):
    __slots__ = ('name', 'type', 'col_id', 'nullable', 'default')

    def __init__(self, name, type_, col_id, nullable=True, default=NO_DEFAULT):
        self.name = name
        self.type = type_
        self.col_id = col_id
        self.nullable = nullable
        # 存的是已经规整过的值（int/float/str/None），不是什么字面量文本。
        # 所以读回来直接能用，不用再解一遍。
        self.default = default

    @property
    def has_default(self):
        return self.default is not NO_DEFAULT

    def default_value(self):
        return None if self.default is NO_DEFAULT else self.default

    def to_dict(self):
        d = {'id': self.col_id, 'name': self.name,
             'type': self.type, 'nullable': self.nullable}
        if self.has_default:
            d['default'] = self.default
        return d

    @classmethod
    def from_dict(cls, d):
        default = d['default'] if 'default' in d else NO_DEFAULT
        return cls(d['name'], d['type'], d['id'],
                   d.get('nullable', True), default)

    def __repr__(self):
        return '<列 %s %s#%d>' % (self.name, TYPE_NAMES.get(self.type, '?'),
                                  self.col_id)


def _as_name(name):
    name = name.strip() if isinstance(name, str) else str(name)
    if not name:
        raise SchemaError('名字不能是空的')
    if len(name.encode('utf-8')) > 255:
        raise SchemaError('名字太长（上限 255 字节）：%s' % name)
    return name


class Table(object):
    """一张表的定义。**列号一旦定下就不再改**，改名换序都只动名字。"""

    __slots__ = ('name', 'table_id', 'columns', 'pk', 'dim_column',
                 'next_col_id', 'index_of')

    def __init__(self, name, table_id, columns=None, pk=None,
                 dim_column=None, index_of=None):
        self.name = _as_name(name)
        self.table_id = table_id
        self.columns = list(columns or [])
        self.pk = list(pk or [])
        self.dim_column = dim_column
        self.next_col_id = max([c.col_id for c in self.columns] or [0]) + 1
        # 这是一张**索引表**时，记着它是"谁、按哪些列"的索引：
        # `(基表名, (列1, 列2, …))`。普通表是 None。
        #
        # 索引表本身是一张**普通表**（占真表号、走同一套行键和读写），
        # 这个字段只是让上层认得出它。**不在 `Table.__init__` 里按名字
        # 猜**：名字可以改，而且"这张表是索引"和"这张表叫什么"是两件事。
        self.index_of = index_of

    # ---- 列 --------------------------------------------------------

    def column(self, name):
        for c in self.columns:
            if c.name == name:
                return c
        return None

    def column_by_id(self, col_id):
        for c in self.columns:
            if c.col_id == col_id:
                return c
        return None

    def require(self, name):
        c = self.column(name)
        if c is None:
            raise SchemaError('表 %s 里没有列 %s' % (self.name, name))
        return c

    def add_column(self, name, type_, nullable=True, default=NO_DEFAULT):
        if self.column(name) is not None:
            raise SchemaError('表 %s 里已经有列 %s 了' % (self.name, name))
        col = Column(_as_name(name), type_, self.next_col_id, nullable, default)
        self.next_col_id += 1
        self.columns.append(col)
        return col

    def drop_column(self, name):
        if name in self.pk:
            raise SchemaError('%s 是主键的一部分，删不掉' % name)
        if name == self.dim_column:
            raise SchemaError('%s 是维度列，删不掉（要换就先改表设新的）' % name)
        col = self.require(name)
        self.columns = [c for c in self.columns if c is not col]
        return col

    def rename_column(self, name, new_name):
        col = self.require(name)
        other = self.column(new_name)
        if other is not None and other is not col:
            raise SchemaError('已经有一列叫 %s 了' % new_name)
        new_name = _as_name(new_name)
        # **只改名字，列号不动。** 老行里的字节按列号解释，所以改名
        # 不用碰一行数据。主键和维度列里存的是名字，顺手跟着改。
        col.name = new_name
        self.pk = [new_name if p == name else p for p in self.pk]
        if self.dim_column == name:
            self.dim_column = new_name
        return col

    # ---- 主键与维度 ------------------------------------------------

    def pk_columns(self):
        return [self.require(p) for p in self.pk]

    def dim_column_obj(self):
        return self.column(self.dim_column) if self.dim_column else None

    def key_for(self, values):
        """主键值（按 self.pk 的次序）-> 行键。"""
        out = bytearray(row_prefix(self.table_id))
        for name, v in zip(self.pk, values):
            col = self.require(name)
            out += encode_value(col.type, coerce(col.type, v))
        return bytes(out)

    def key_from_cells(self, cells):
        """从解码出来的格子里取主键，拼成行键。"""
        out = bytearray(row_prefix(self.table_id))
        for name in self.pk:
            col = self.require(name)
            entry = cells.get(col.col_id)
            if entry is None or entry[1] is None:
                raise SchemaError('主键列 %s 不能是空' % name)
            out += encode_value(col.type, entry[1])
        return bytes(out)

    # ---- 持久化 ----------------------------------------------------

    def to_json(self):
        return json.dumps({
            'v': 1,
            'name': self.name,
            'table_id': self.table_id,
            'columns': [c.to_dict() for c in self.columns],
            'pk': list(self.pk),
            'dim': self.dim_column,
            'next_col_id': self.next_col_id,
            # 普通表不带这个键（`sort_keys` + 缺省不写，老库读得出来，
            # 而且普通表的 JSON 一个字节都不变）
            **({'index_of': [self.index_of[0], list(self.index_of[1])]}
               if self.index_of else {}),
        }, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, blob):
        d = json.loads(blob.decode('utf-8') if isinstance(blob, bytes) else blob)
        if d.get('v') != 1:
            raise SchemaError('表定义的版本认不出：%r（这个库可能来自'
                              '更新的版本）' % d.get('v'))
        raw_idx = d.get('index_of')
        tbl = cls(d['name'], d['table_id'],
                  [Column.from_dict(c) for c in d['columns']],
                  d.get('pk'), d.get('dim'),
                  # 老记录没有这个键 -> None，就是普通表
                  (raw_idx[0], tuple(raw_idx[1])) if raw_idx else None)
        tbl.next_col_id = max(d.get('next_col_id', 1),
                              max([c.col_id for c in tbl.columns] or [0]) + 1)
        return tbl

    def describe(self):
        """给人看的一行说明。"""
        base = '表 %s（表号 %d，%d 列，主键 %s，维度列 %s）' % (
            self.name, self.table_id, len(self.columns),
            '/'.join(self.pk) or '无', self.dim_column or '无')
        if self.index_of:
            base += '【%s 的索引，按 %s】' % (
                self.index_of[0], '/'.join(self.index_of[1]))
        return base

    def __repr__(self):
        return '<Table %s#%d cols=%d pk=%s>' % (
            self.name, self.table_id, len(self.columns), self.pk)
