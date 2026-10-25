# -*- coding: utf-8 -*-
"""
霸下语句层 — 词法与语法
========================

把一段文本变成一棵树。这一层**不碰存储、不碰网络、不认识任何表**——
它唯一的产出是结构，唯一的输入是字符串。所以它可以单独跑、单独看，
坏了也一眼知道是这里坏的。

中文怎么断词
------------

**按空格断。** 中文没有词间空格，`查询姓名从用户表` 这串字机器没法
知道该从哪里切。有两种做法：一是查词典做最长匹配，二是要求把词分开
写。这里选后者，理由不是省事：

  词典切分**一定会有切错的**，而且切错是静默的——`用户表查询` 是
  一张表加一个动词，还是一个叫"用户表查询"的表？没有哪个算法能
  永远答对。一个偶尔悄悄理解错的数据库，比一个要求你多打几个空格
  的数据库难用得多。

  而且中文本来就有用空格断词的习惯。多打的空格，换来的是"我写什么
  它就懂什么"，这笔账划得来。

标点则不限：全角半角都认，`，`和`,`、`；`和`;`、`（）`和`()`
一视同仁。中文句号`。`当成语句结束（等同于`;`）——写中文的人手比
脑子快，这一下能省掉一批"少了个分号"。

中英双写
--------

每个关键字都有中文和英文两个写法，映射到同一个记号，`查询` 和
`SELECT` 完全等价，可以混着写。表名列名也随便用中文。

关于日期不写引号这件事
----------------------

`2026-09-12` 不加引号的话，有几个记号在机器眼里是 `2026 - 09 - 12`，
一次普通的减法，结果是 2005——一个安静的错误答案。所以词法分析器
在这里**故意把数字中间的 `-` 和 `:` 粘住**（`2026-09-12`、
`10:30:00` 都粘成一个记号），粘完发现它既不是整数也不是实数，就
报错并告诉你加引号。宁可吵一声，不要算出 2005。
"""

import re

import schema

__all__ = ['parse', 'parse_all', 'count_params', 'SqlError', 'KEYWORDS',
           'Select', 'SelectItem', 'Join', 'OrderItem', 'SetOp', 'Insert',
           'Update', 'Delete', 'CreateTable', 'ColumnDef', 'DropTable',
           'AlterTable', 'Show', 'BatchBegin', 'BatchCommit',
           'CreateIndex', 'DropIndex', 'RebuildIndex', 'CheckIndex']


class SqlError(Exception):
    """语句看不懂。消息里带出错的位置，别让人自己数。"""


# ── 词法 ────────────────────────────────────────────────────────────

# 空白。全角空格（U+3000）也算——从别处粘过来的中文里常有。
_WS = ' \t\r\n　'

# 半角标点。`.` 和 `:` 在里面，是为了让 10:30:00 和 1.5 能被粘住。
_PUNCT = '()[],;.*=<>!+-/%|^:'

# 全角标点 -> 半角。映射完再按半角那一套走，词法分析器只有一条路径。
_FULLWIDTH = {
    '（': '(', '）': ')', '［': '[', '］': ']', '【': '(', '】': ')',
    '，': ',', '、': ',', '。': ';', '；': ';', '：': ':',
    '＝': '=', '＜': '<', '＞': '>', '！': '!', '＋': '+', '－': '-',
    '＊': '*', '／': '/', '％': '%', '？': '?', '｜': '|', '　': ' ',
    '“': '"', '”': '"', '‘': "'", '’': "'", '《': '<', '》': '>',
}

# 引号：`'` 和 `"` 都当字符串（写中文的人更常见双引号），
# `` ` `` 当"这是个名字，别当关键字看"。
_QUOTES = "'\"`"

_NUM_RE = re.compile(r'^\d+$')
_FLOAT_RE = re.compile(r'^(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$')

# 记号种类
T_KW = 'kw'          # 关键字（已归一成英文规范名）
T_NAME = 'name'      # 名字：表名、列名、别名
T_STR = 'str'        # 字符串字面量
T_NUM = 'num'        # 数字字面量
T_PUNCT = 'punct'    # 标点
T_PARAM = 'param'    # ? 占位符
T_EOF = 'eof'


class Token(object):
    __slots__ = ('kind', 'text', 'value', 'pos')

    def __init__(self, kind, text, pos, value=None):
        self.kind = kind
        self.text = text
        self.pos = pos
        self.value = value

    def __repr__(self):
        return '<%s %r@%d>' % (self.kind, self.text, self.pos)


# ── 关键字表 ────────────────────────────────────────────────────────
# 全部映射到一个英文规范名。解析器只认规范名，于是中文英文走的是
# 完全同一条路——加一种说法只需要在这里添一行，不用动解析器。

KEYWORDS = {
    # 语句
    '建表': 'CREATE', '创建表': 'CREATE', '新建表': 'CREATE',
    'CREATE': 'CREATE',
    '删表': 'DROP', '删除表': 'DROP', 'DROP': 'DROP',
    '改表': 'ALTER', '修改表': 'ALTER', 'ALTER': 'ALTER',
    '显示': 'SHOW', '列出': 'SHOW', 'SHOW': 'SHOW',
    '插入': 'INSERT', '新增': 'INSERT', 'INSERT': 'INSERT',
    # 主键撞上了就覆盖。**和 `插入` 是两条语句，不是同一个的两个选项**：
    # 一个说"这条不该在"，一个说"不管在不在，现在长这样"。用错的那个
    # 人拿到的是安静的错答案——本该报重复的没报，本该覆盖的没覆盖。
    '替换': 'REPLACE', '覆盖': 'REPLACE', 'REPLACE': 'REPLACE',
    '更新': 'UPDATE', 'UPDATE': 'UPDATE',
    '删除': 'DELETE', 'DELETE': 'DELETE',
    '查询': 'SELECT', '选择': 'SELECT', '查': 'SELECT', 'SELECT': 'SELECT',

    # ── 用户与权限 ──────────────────────────────────────────────
    # 这一族和 建表/删表 并列，不是它们的附属。**词表里认的是一整个
    # 词**（词法那边按整段标识符串查表，不做最长匹配），所以 `删用户`
    # 和 `删表` 不会互相吃掉，`新增用户` 也不会被 `新增` 抢走。
    '建用户': 'CREATEUSER', '创建用户': 'CREATEUSER', '新增用户': 'CREATEUSER',
    'CREATEUSER': 'CREATEUSER',
    '删用户': 'DROPUSER', '删除用户': 'DROPUSER', '移除用户': 'DROPUSER',
    'DROPUSER': 'DROPUSER',
    '改密': 'SETPASSWORD', '改密码': 'SETPASSWORD', '修改密码': 'SETPASSWORD',
    'SETPASSWORD': 'SETPASSWORD',
    # 授权/收回：**后面跟表名就是表级，不跟就是库级**。而跟不跟由权限词
    # 自己决定——见 `Grant` 的说明。
    '授权': 'GRANT', 'GRANT': 'GRANT',
    '收回': 'REVOKE', '取消授权': 'REVOKE', 'REVOKE': 'REVOKE',
    # `用户` 当关键字**不会**打断把 `用户` 当列名/表名用：`expect_name`
    # 那条路本来就诚心收关键字当名字（那里写着"不然 `时间 时间` 这种
    # 列名叫时间、类型也是时间就写不出来了"）。同一个道理。
    '用户': 'USERS', '用户列表': 'USERS', 'USERS': 'USERS',
    # 英文两段式（`CREATE USER` / `DROP USER`）要看得见 `USER` 这一段。
    'USER': 'USERS',

    '批量': 'BATCH', 'BATCH': 'BATCH',
    '开始': 'BEGIN', 'BEGIN': 'BEGIN',
    '提交': 'COMMIT', 'COMMIT': 'COMMIT',
    # 压实：整库一趟，把被覆盖的旧版本占的区真正从盘上回收。
    # **光杆一条，不带参数**——判活死本来就得自顶向下扫全库一趟，按区
    # 单压每次仍要扫比它新的所有区，那是平方级的活干两遍。所以没有
    # `压实 区 <号>` 这种写法，别加。
    '压实': 'COMPACT', '整理': 'COMPACT', 'COMPACT': 'COMPACT',
    # 索引。**只做非唯一索引** —— `唯一` 那条路维持现在的拒绝与指路。
    '建索引': 'CREATEINDEX', '创建索引': 'CREATEINDEX',
    'CREATEINDEX': 'CREATEINDEX',
    '删索引': 'DROPINDEX', '删除索引': 'DROPINDEX',
    'DROPINDEX': 'DROPINDEX',
    '检查索引': 'CHECKINDEX', 'CHECKINDEX': 'CHECKINDEX',
    '重建索引': 'REBUILDINDEX', 'REBUILDINDEX': 'REBUILDINDEX',
    # 英文那两条的**头一段**（`REBUILD INDEX` / `CHECK INDEX`）。
    # 中文是一段，英文是两段，所以这两个词得单独有记号。
    'REBUILD': 'REBUILD', 'CHECK': 'CHECK',
    # `索引` 和 `索引列表` 都指向同一个：`显示 索引 网页` 和
    # `显示 索引列表 网页` 是一句话。**不给它单独一个 INDEX** ——
    # 两个记号名而解析器只认一个，就是那句"看着认识、其实不认"的源头。
    '索引': 'INDEXES', '索引列表': 'INDEXES',
    'INDEXES': 'INDEXES', 'INDEX': 'INDEXES',

    # 结构词
    '表': 'TABLE', 'TABLE': 'TABLE',
    '表列表': 'TABLES', '所有表': 'TABLES', 'TABLES': 'TABLES',
    '结构': 'COLUMNS', '列定义': 'COLUMNS', 'COLUMNS': 'COLUMNS',
    'DESCRIBE': 'COLUMNS', 'DESC': 'COLUMNS',
    '区域': 'REGIONS', '区': 'REGIONS', 'REGIONS': 'REGIONS',
    'INTO': 'INTO',
    '从': 'FROM', 'FROM': 'FROM',
    '值': 'VALUES', 'VALUES': 'VALUES',
    '设': 'SETK', '设置': 'SETK', 'SET': 'SETK',
    '加列': 'ADDCOL', '添加列': 'ADDCOL',
    '删列': 'DROPCOL', '删除列': 'DROPCOL',
    '改名': 'RENAME', '重命名': 'RENAME', 'RENAME': 'RENAME',
    'TO': 'TO',
    # `改表 … 删列 x 连带删索引`：那条列上有索引时，删列的逃生门。
    # 不写它就拒绝删 —— 索引的条目键里编的就是那一列，留着索引等于留着
    # 一张按不存在的列切分的表。
    '连带删索引': 'CASCADE', 'CASCADE': 'CASCADE',
    '如果不存在': 'IFNOTEXISTS', 'IF': 'IF', 'EXISTS': 'EXISTS',

    # 子句
    '其中': 'WHERE', '条件': 'WHERE', 'WHERE': 'WHERE',
    '分组': 'GROUP', 'GROUP': 'GROUP',
    '按': 'BY', 'BY': 'BY',
    '排序': 'ORDER', 'ORDER': 'ORDER',
    '限制': 'LIMIT', 'LIMIT': 'LIMIT',
    '偏移': 'OFFSET', 'OFFSET': 'OFFSET',
    '去重': 'DISTINCT', 'DISTINCT': 'DISTINCT',
    '作为': 'AS', 'AS': 'AS',
    '连接': 'JOIN', 'JOIN': 'JOIN',
    '内连接': 'INNER', '内': 'INNER', 'INNER': 'INNER',
    '左连接': 'LEFT', '左': 'LEFT', 'LEFT': 'LEFT',
    '右连接': 'RIGHT', '右': 'RIGHT', 'RIGHT': 'RIGHT',
    'ON': 'ON', '在': 'ON',

    # 谓词
    '与': 'AND', '和': 'AND', '且': 'AND', 'AND': 'AND',
    '或': 'OR', 'OR': 'OR',
    '非': 'NOT', '不': 'NOT', 'NOT': 'NOT',
    '介于': 'BETWEEN', 'BETWEEN': 'BETWEEN',
    '之间': 'BETWEEN2', '至': 'BETWEEN2', '到': 'BETWEEN2',
    '属于': 'IN', '其中之一': 'IN', 'IN': 'IN',
    '像': 'LIKE', 'LIKE': 'LIKE',
    '为': 'IS', '是': 'IS', 'IS': 'IS',
    '为空': 'ISNULL', '是空': 'ISNULL',
    '不为空': 'NOTNULL', '不是空': 'NOTNULL',
    '空': 'NULL', 'NULL': 'NULL',

    # 比较。**中文的比较词和符号完全等价**——写 `年龄 大于 18` 比写
    # `年龄 > 18` 更像中文，而一个号称能用中文写的查询语言如果只在
    # 关键字上中文、一到比较就退回符号，那是半拉子。
    '大于': 'GT', '超过': 'GT', '多于': 'GT',
    '小于': 'LT', '少于': 'LT', '低于': 'LT',
    '大于等于': 'GE', '不小于': 'GE', '至少': 'GE', '不低于': 'GE',
    '小于等于': 'LE', '不大于': 'LE', '至多': 'LE', '不超过': 'LE',
    '等于': 'EQ', '不等于': 'NE', '不同于': 'NE',
    # 否定形式连写成一个词。不连写的话 `不属于` 会被当成一个列名，
    # 而报出来的错会完全指不到点子上。
    '不属于': 'NOTIN', '不介于': 'NOTBETWEEN', '不像': 'NOTLIKE',

    '升序': 'ASC', 'ASC': 'ASC',
    '降序': 'DESC', 'DESC': 'DESC',

    # 聚合
    '计数': 'COUNT', '个数': 'COUNT', 'COUNT': 'COUNT',
    '求和': 'SUM', '总和': 'SUM', 'SUM': 'SUM',
    '平均': 'AVG', '均值': 'AVG', 'AVG': 'AVG',
    '最大': 'MAX', '最大值': 'MAX', 'MAX': 'MAX',
    '最小': 'MIN', '最小值': 'MIN', 'MIN': 'MIN',

    # 字面量
    '真': 'TRUE', 'TRUE': 'TRUE',
    '假': 'FALSE', 'FALSE': 'FALSE',
    '默认': 'DEFAULT', 'DEFAULT': 'DEFAULT',

    # 类型
    '整数': 'T_INT', 'INT': 'T_INT', 'INTEGER': 'T_INT', 'BIGINT': 'T_INT',
    '实数': 'T_FLOAT', '浮点': 'T_FLOAT', '小数': 'T_FLOAT',
    'FLOAT': 'T_FLOAT', 'DOUBLE': 'T_FLOAT', 'REAL': 'T_FLOAT',
    '文本': 'T_TEXT', '字符串': 'T_TEXT', '字符': 'T_TEXT',
    'TEXT': 'T_TEXT', 'STRING': 'T_TEXT', 'VARCHAR': 'T_TEXT',
    'CHAR': 'T_TEXT',
    '二进制': 'T_BLOB', '字节串': 'T_BLOB',
    'BLOB': 'T_BLOB', 'BYTES': 'T_BLOB', 'BINARY': 'T_BLOB',
    '时间': 'T_TIME', '时间戳': 'T_TIME',
    'TIME': 'T_TIME', 'TIMESTAMP': 'T_TIME', 'DATETIME': 'T_TIME',

    # 标量函数。**中英同义，归一到同一个规范名，走同一棵树。**
    # `替换` 和语句头的 `REPLACE` 是同一个记号，靠**位置**分开：
    # 句子开头那个是语句（`parse_statement` 先看第一个记号），
    # 表达式里出现的只能是函数调用。同名的关键字当函数用，这个库
    # 本来就这么干（见 `expect_name` 那句注释）。
    '长度': 'LENGTH', 'LENGTH': 'LENGTH', 'LEN': 'LENGTH',
    '截取': 'SUBSTR', '子串': 'SUBSTR', 'SUBSTR': 'SUBSTR',
    'SUBSTRING': 'SUBSTR', 'MID': 'SUBSTR',
    '拼接': 'CONCAT', '连接串': 'CONCAT', 'CONCAT': 'CONCAT',
    '替换': 'REPLACE', 'REPLACE': 'REPLACE',
    '转大写': 'UPPER', '大写': 'UPPER', 'UPPER': 'UPPER', 'UCASE': 'UPPER',
    '转小写': 'LOWER', '小写': 'LOWER', 'LOWER': 'LOWER', 'LCASE': 'LOWER',
    '去空白': 'TRIM', '去空格': 'TRIM', 'TRIM': 'TRIM',
    '四舍五入': 'ROUND', 'ROUND': 'ROUND',
    '绝对值': 'ABS', 'ABS': 'ABS',
    '现在': 'NOW', '当前时间': 'NOW', 'NOW': 'NOW',
    '类型': 'TYPEOF', 'TYPEOF': 'TYPEOF',

    # 条件表达式。**这几个词不能当列名用了** —— 词法一视同仁，`当` 落进
    # `parse_atom` 的 `tok.kind in (T_NAME, T_KW)` 那一支就会被**静默当成
    # 列名**，那不是报错，是安静地算错。所以 `parse_atom` 接住它的分支
    # 必须**排在最前面**。真要用这几个字当列名，拿反引号括起来
    # （`` `当` ``，见 `_QUOTES`）。
    #
    # 分支开头 `当` 和 `如果` 都行 —— 第一个分支用 `当` 起头读着顺
    # （`当 状态 = 1 那么 '一' 如果 状态 = 2 那么 '二' 否则 '其他' 结束`），
    # 但连着写两个 `当` 会看成"两个 CASE"，所以第二个起头的同义词收 `如果`。
    # 两个词在解析器里是**同一个分支开头**，没有优先级差别。
    '当': 'CASE', '情形': 'CASE', '情况': 'CASE', 'CASE': 'CASE',
    '如果': 'WHEN', 'WHEN': 'WHEN',
    '那么': 'THEN', 'THEN': 'THEN',
    '否则': 'ELSE', '不然': 'ELSE', 'ELSE': 'ELSE',
    '结束': 'END', 'END': 'END',

    # 集合运算。`并集 全部` 才不去重（对齐 SQL 的 UNION / UNION ALL）。
    '并集': 'UNION', '联合': 'UNION', 'UNION': 'UNION',
    '交集': 'INTERSECT', 'INTERSECT': 'INTERSECT',
    '差集': 'EXCEPT', 'EXCEPT': 'EXCEPT',
    '全部': 'ALL', 'ALL': 'ALL',

    # 列约束
    '主键': 'PRIMARY', 'PRIMARY': 'PRIMARY', 'KEY': 'KEY',
    '维度': 'DIMENSION', '维度列': 'DIMENSION', 'DIMENSION': 'DIMENSION',
    '非空': 'NOTNULL', '允许空': 'NULLABLE', '可空': 'NULLABLE',
    '唯一': 'UNIQUE', '唯一的': 'UNIQUE', 'UNIQUE': 'UNIQUE',
}

# 类型关键字的规范名 -> schema 里的类型号
TYPE_WORDS = {
    'T_INT': schema.INT, 'T_FLOAT': schema.FLOAT, 'T_TEXT': schema.TEXT,
    'T_BLOB': schema.BLOB, 'T_TIME': schema.TIME,
}

_AGG_WORDS = {'COUNT', 'SUM', 'AVG', 'MAX', 'MIN'}

# 标量函数。**和 `_AGG_WORDS` 是两张表，不能并。** 聚合那套机器
# （`_find_agg` / `_collect_aggs` / `_any_agg` / `_subst` / `_AggState`）
# 全以"节点是 call"为判据、不看名字，`parse_atom:1110` 又拿 `_AGG_WORDS`
# 判断"这个关键字是不是直接开头一个调用"。把 `长度(姓名)` 混进聚合那张
# 表，整条查询会掉进分组，然后在"认不出的聚合函数"那里炸 —— 而那句
# 报错跟真正的原因（就是个普通函数）差着十万八千里。
#
# 每个函数能吃几个参数也写在这儿，**在解析期就查**。等到求值才报错的
# 话，前面的行可能已经写下去了。
_SCALAR_WORDS = {
    'LENGTH': (1, 1), 'SUBSTR': (2, 3), 'CONCAT': (1, 255),
    'REPLACE': (3, 3), 'UPPER': (1, 1), 'LOWER': (1, 1), 'TRIM': (1, 1),
    'ROUND': (1, 2), 'ABS': (1, 1), 'NOW': (0, 0), 'TYPEOF': (1, 1),
}

# 规范名 -> 显示用中文名（只有 `_expr_name` 用它给列起名字）
_SCALAR_NAMES = {
    'LENGTH': '长度', 'SUBSTR': '截取', 'CONCAT': '拼接', 'REPLACE': '替换',
    'UPPER': '转大写', 'LOWER': '转小写', 'TRIM': '去空白',
    'ROUND': '四舍五入', 'ABS': '绝对值', 'NOW': '现在', 'TYPEOF': '类型',
}

# 报错时按这个次序罗列。用固定次序不用 `_SCALAR_WORDS` 的 key 次序：
# 集合的迭代次序不承诺，同一句话每次报出来的次序不一样，看着像另一个错。
_SCALAR_ORDER = ('LENGTH', 'SUBSTR', 'CONCAT', 'REPLACE', 'UPPER', 'LOWER',
                 'TRIM', 'ROUND', 'ABS', 'NOW', 'TYPEOF')


def tokenize(text):
    """文本 -> 记号列表（末尾一定有一个 T_EOF）。"""
    toks = []
    i, n = 0, len(text)
    while i < n:
        raw = text[i]

        # 全角先归一成半角，后面只有一条路径
        ch = _FULLWIDTH.get(raw, raw)

        if ch in _WS:
            i += 1
            continue

        # 注释：-- 到行尾，# 到行尾，/* ... */
        if ch == '-' and text.startswith('--', i):
            j = text.find('\n', i)
            i = n if j < 0 else j + 1
            continue
        if ch == '#':
            j = text.find('\n', i)
            i = n if j < 0 else j + 1
            continue
        if ch == '/' and text.startswith('/*', i):
            j = text.find('*/', i + 2)
            i = n if j < 0 else j + 2
            continue

        # 字符串 / 引号名字
        if ch in _QUOTES:
            quote = ch
            start = i
            i += 1
            buf = []
            while True:
                if i >= n:
                    raise SqlError('引号没闭合（第 %d 个字符开始的那个）' % start)
                c = _FULLWIDTH.get(text[i], text[i])
                if c == quote:
                    # 连写两个引号 = 一个引号字符，这是 SQL 的老规矩，
                    # 比反斜杠转义在中文文本里安全得多（中文里 \ 很常见）
                    if i + 1 < n and _FULLWIDTH.get(text[i + 1],
                                                    text[i + 1]) == quote:
                        buf.append(quote)
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(c)
                i += 1
            body = ''.join(buf)
            if quote == '`':
                toks.append(Token(T_NAME, body, start))
            else:
                toks.append(Token(T_STR, body, start, body))
            continue

        # 参数
        if ch == '?':
            toks.append(Token(T_PARAM, ch, i))
            i += 1
            continue

        # 标点
        if ch in _PUNCT:
            two = ch + _FULLWIDTH.get(text[i + 1], text[i + 1]) \
                if i + 1 < n else ch
            if two in ('!=', '<>', '<=', '>=', '==', '||', '::'):
                # `||` 是字符串拼接，`::` 是类型转换——本库都没有，
                # 但粘成一个记号能让人看见"这是个整体"，报错更好读
                toks.append(Token(T_PUNCT, two, i))
                i += 2
                continue
            toks.append(Token(T_PUNCT, ch, i))
            i += 1
            continue

        # 剩下的：一整段连着的东西——名字、关键字、或者数字
        start = i
        while i < n:
            c = _FULLWIDTH.get(text[i], text[i])
            if c in _WS or c in _PUNCT or c in _QUOTES or c == '?' or c == '#':
                # 数字中间夹的 - 和 : 和 . 不收：2026-09-12 和 10:30:00
                # 要粘成一个记号整条去报错，而不是被切成减法算出一个
                # 安静的错误答案。见模块开头那段说明。
                if (i > start and c in '-:.'
                        and text[i - 1].isdigit()
                        and i + 1 < n
                        and _FULLWIDTH.get(text[i + 1], text[i + 1]).isdigit()):
                    i += 1
                    continue
                break
            i += 1
        run = text[start:i]
        if not run:
            raise SqlError('第 %d 个字符认不出：%r' % (i, text[i]))

        if _NUM_RE.match(run):
            toks.append(Token(T_NUM, run, start, int(run)))
        elif _FLOAT_RE.match(run) and run[0].isdigit():
            toks.append(Token(T_NUM, run, start, float(run)))
        elif run[0].isdigit():
            # 走到这里说明它像个数字但不是数字，多半是日期或时刻没加引号
            raise SqlError(
                '认不出这个记号：%s。如果它是时间，请加引号写成 \'%s\''
                % (run, run))
        else:
            canon = KEYWORDS.get(run)
            if canon is not None:
                toks.append(Token(T_KW, run, start, canon))
            else:
                toks.append(Token(T_NAME, run, start))

    toks.append(Token(T_EOF, '', n))
    return toks


# ── 语法树 ──────────────────────────────────────────────────────────
# 表达式用元组，语句用带 __slots__ 的类。表达式要频繁地递归遍历，
# 元组够用且省；语句字段多、要读，类好读得多。

def lit(type_, value):
    return ('lit', type_, value)


def star(qualifier=None):
    return ('star', qualifier)


def col(name, qualifier=None):
    return ('col', qualifier, name)


class SelectItem(object):
    __slots__ = ('expr', 'alias')

    def __init__(self, expr, alias=None):
        self.expr = expr
        self.alias = alias

    def __repr__(self):
        return '%r AS %s' % (self.expr, self.alias) if self.alias \
            else repr(self.expr)


class OrderItem(object):
    __slots__ = ('expr', 'desc')

    def __init__(self, expr, desc=False):
        self.expr = expr
        self.desc = desc


class Join(object):
    __slots__ = ('table', 'alias', 'on', 'kind')

    def __init__(self, table, alias, on, kind='inner'):
        self.table = table
        self.alias = alias
        self.on = on
        self.kind = kind


class Select(object):
    __slots__ = ('items', 'table', 'alias', 'joins', 'where', 'group',
                 'having', 'order', 'limit', 'offset', 'distinct')

    def __init__(self):
        self.items = []
        self.table = None
        self.alias = None
        self.joins = []
        self.where = None
        self.group = []
        self.having = None
        self.order = []
        self.limit = None
        self.offset = None
        self.distinct = False


class SetOp(object):
    """
    两个查询的集合运算。`left` / `right` 是 `Select` 或另一个 `SetOp`
    ——串三支以上就是左结合的嵌套，和 `1 - 2 - 3` 一个路子。

    `order` / `limit` / `offset` **只在这里有**，分支的 `Select` 上写的
    排序和限制会被搬到这儿来（见 `Parser.parse_query`），因为
    `查询 a 并集 查询 b 排序 按 x` 里那个 `排序` 管的是**整个结果**，
    不是 `b`。搬过来、并且禁止中间分支写尾巴，两头都堵上才没有"看着
    像在排整个结果、其实只排了右半边"这种安静的错。
    """
    __slots__ = ('left', 'op', 'right', 'all', 'order', 'limit', 'offset')

    def __init__(self, left, op, right, all_=False):
        self.left = left
        self.op = op            # UNION / INTERSECT / EXCEPT
        self.right = right
        self.all = all_         # 只有 UNION 用得上
        self.order = []
        self.limit = None
        self.offset = None


class Insert(object):
    __slots__ = ('table', 'columns', 'rows', 'dim', 'overwrite')

    def __init__(self, table, columns, rows, dim=None, overwrite=False):
        self.table = table
        self.columns = columns
        self.rows = rows
        self.dim = dim
        self.overwrite = overwrite


class Update(object):
    __slots__ = ('table', 'sets', 'where', 'dim')

    def __init__(self, table, sets, where=None, dim=None):
        self.table = table
        self.sets = sets
        self.where = where
        self.dim = dim


class Delete(object):
    __slots__ = ('table', 'where')

    def __init__(self, table, where=None):
        self.table = table
        self.where = where


class ColumnDef(object):
    __slots__ = ('name', 'type', 'nullable', 'default', 'is_pk', 'is_dim')

    def __init__(self, name, type_, nullable=True, default=schema.NO_DEFAULT,
                 is_pk=False, is_dim=False):
        self.name = name
        self.type = type_
        self.nullable = nullable
        self.default = default
        self.is_pk = is_pk
        self.is_dim = is_dim


class CreateTable(object):
    __slots__ = ('name', 'columns', 'pk', 'dim_column', 'if_not_exists')

    def __init__(self, name, columns, pk, dim_column, if_not_exists=False):
        self.name = name
        self.columns = columns
        self.pk = pk
        self.dim_column = dim_column
        self.if_not_exists = if_not_exists


class DropTable(object):
    __slots__ = ('name', 'if_exists')

    def __init__(self, name, if_exists=False):
        self.name = name
        self.if_exists = if_exists


class AlterTable(object):
    __slots__ = ('name', 'action', 'column', 'new_name', 'cascade')

    def __init__(self, name, action, column=None, new_name=None,
                 cascade=False):
        self.name = name
        self.action = action        # add / drop / rename / dim
        self.column = column        # add 时是 ColumnDef，其余是列名
        self.new_name = new_name
        # 删列/改列名时，那一列上有索引怎么办。**默认拒绝**：索引条目的键
        # 里编的就是那一列的类型和值，列没了索引就切不动自己的键了。
        # 写 `连带删索引` 才放行，放行时把沾到那一列的索引一起删掉。
        self.cascade = cascade


class Show(object):
    __slots__ = ('what', 'table')

    def __init__(self, what, table=None):
        self.what = what            # tables / columns / regions / indexes
        self.table = table


class CreateIndex(object):
    """
    `建索引 <表名> 按 (列1, 列2, …)`：建一张**非唯一**索引。

    多列是组合索引，但只有**前缀匹配**用得上：`其中 列1=? 与 列2=?`
    用得上，单写 `其中 列2=?` 用不上（还是全扫）。这不是偷懒 —— 本引擎
    **没有 seek**，索引只提供候选集，能切的东西就只有前缀。

    `唯一` 不在这个语法里：本库只做非唯一索引，`唯一` 那条路维持它
    现在的拒绝与指路（见 `parse_create` 里那段）。
    """
    __slots__ = ('table', 'columns')

    def __init__(self, table, columns):
        self.table = table
        self.columns = list(columns)


class DropIndex(object):
    """`删索引 <表名> 按 (列1, …)`：按被封的列把那张索引表删掉。"""
    __slots__ = ('table', 'columns')

    def __init__(self, table, columns):
        self.table = table
        self.columns = list(columns)


class RebuildIndex(object):
    """
    `重建索引 <表名> 按 (列1, …)`：等价于 删索引 + 建索引。

    **刻意不做"原地清空"**：这个引擎没有删区，压实也没实现。换一个
    新表号 = 换一套行键前缀 = 旧行一个都读不到，旧字节成孤儿留在盘上。
    而"垮掉最多是多几个没人指向的字节"正是这个引擎**允许**的活法
    （整区压实未实现是同一笔债，不是新添的）。
    """
    __slots__ = ('table', 'columns')

    def __init__(self, table, columns):
        self.table = table
        self.columns = list(columns)


class CheckIndex(object):
    """
    `检查索引 <表名>`：**只读**。全扫基表，一条条对索引里在不在。

    这是**唯一**能发现"假阴性"（活行没有活索引条目）的手段 ——
    读路径看不见那种：没有候选就什么都不会发生，不报错、不慢、
    结果也没错，就是少了行。所以它必须存在，而它只报告、不自动修：
    在一次只读操作里做写，违背这套东西的分层。
    """
    __slots__ = ('table',)

    def __init__(self, table):
        self.table = table


class BatchBegin(object):
    """
    `批量 开始`：接下来的写先攒着，等 `批量 提交` 才落盘。

    以前这两个是同一个 `Nothing`（`__slots__ = ()`，**连是开始还是提交都
    没记下来**），执行侧映射成纯空操作 —— 于是 `批量 提交` 一点效果都没有，
    写的人以为自己在攒批，其实每句都各落各的盘。攒批的意义全在"少落几次盘"
    上，所以这句必须真的把那件事做到。
    """
    __slots__ = ()


class BatchCommit(object):
    """`批量 提交`：把攒下的写落盘，并退出批模式。"""
    __slots__ = ()


class Compact(object):
    """
    `压实`：整库一趟，回收被覆盖的旧版本占的区。

    **不带参数是刻意的**，理由见 KEYWORDS 里那一行：判活死必须自顶向下
    扫全库一趟（"这个键在区 R 里还活着"等价于"没有区号更大的区里也有
    它"），所以"只压一个区"不是一个更省的形状，而是同一趟活干两遍。

    它**会永久丢掉被覆盖的旧版本**——这正是回收空间的来源。旧版本现在
    没有任何读的路径，所以不是功能损失，但不可逆。
    """
    __slots__ = ()


# ── 用户与权限 ──────────────────────────────────────────────────────
#
# 这一族语句（DCL）和上面那些的本质区别：**它们动的是"谁能动什么"，
# 不是数据本身**。所以它们的执行不经过 `_table`/`_writable` 那两个表
# 入口——权限表本身不是一张表，它不走行键，也没有"表级"可言。
#
# 口令在这些节点里是**裸字符串**，因为它就是从语句文本里读出来的那
# 一串字符。它只活在"解析完到执行完"这一段里；执行侧第一件事就是
# 把它变成摘要（见 `user.py`），此后再没有任何地方留着它。

# 权限词的**全部**词汇。这份表是语法层和执行层的交接点：语法层拿它
# 认词，`user.py` 拿它当 `global_privs` / `table_privs` 的键。
#
# **它不复用 `KEYWORDS`**，理由和 `改表` 那边按文本认 `ADD COLUMN`
# 一样：`查` 在词表里是 `SELECT` 的规范名，可这里的 `查` 是"读这张
# 表的许可"——是名词，不是动词。认文本，不认规范名，两件事就不会
# 被同一张表搅在一起。
#
# **只收中文**：这不是漏了英文，是刻意的。权限词是一份**报得出来的
# 封闭清单**（错误提示里要一条条念给用户听），而唯一性和可念性比
# 多一门拼法更要紧；何况库级那几个词（`管用户`）本来也没有公认的
# 英文写法。真想加英文，加的是这一张表，不是解析器。
表级权限 = ('查', '插', '改', '删')
库级权限 = ('建表', '删表', '改表', '建索引', '删索引', '压实', '显示', '批量',
            '管用户')
权限全部 = '全部'


class CreateUser(object):
    """
    `建用户 `甲` '口令'`。

    **口令必填**，不给"不写口令"的写法。不写 = 建一个谁都能冒充的
    账号，那和"没建"是同一个结果，但看上去像建成了——空口令这件事
    只能由人明写 `''` 来做，那是他自找的，不是我们替他默认的。
    """
    __slots__ = ('name', 'password')

    def __init__(self, name, password):
        self.name = name
        self.password = password


class DropUser(object):
    """`删用户 `甲``。"""
    __slots__ = ('name',)

    def __init__(self, name):
        self.name = name


class AlterUser(object):
    """`改密 `甲` '新口令'`。"""
    __slots__ = ('name', 'password')

    def __init__(self, name, password):
        self.name = name
        self.password = password


class Grant(object):
    """
    `授权 `甲` 查 `订单`` / `授权 `甲` 建表`。

    **跟表名的是表级，不跟的是库级**——而跟不跟由**权限词自己**决定，
    不是"看后面有没有表名"。写 `授权 `甲` 建表 `订单`` 是错的（建表是
    库级，它不吃表名），写 `授权 `甲` 查` 也是错的（查是表级，必须说清
    是哪张表）。两种错都当场报出来，不会静默把多出来的表名丢掉——
    丢掉的话，写的人以为授的是那一张表，实际授的是整库。
    """
    __slots__ = ('name', 'privilege', 'table')

    def __init__(self, name, privilege, table=None):
        self.name = name
        self.privilege = privilege
        self.table = table


class Revoke(object):
    """`收回 `甲` 查 `订单`` / `收回 `甲` 建表`。和 `Grant` 同形。"""
    __slots__ = ('name', 'privilege', 'table')

    def __init__(self, name, privilege, table=None):
        self.name = name
        self.privilege = privilege
        self.table = table


# ── 语法 ────────────────────────────────────────────────────────────

# 比较运算符
_CMP_OPS = ('=', '!=', '<>', '<', '<=', '>', '>=')
_ADD_OPS = ('+', '-')
_MUL_OPS = ('*', '/', '%')


class Parser(object):
    def __init__(self, tokens):
        self.toks = tokens
        self.i = 0
        # 见过的 ? 个数，也是下一个的编号。
        # **这是"一段文本"的计数，不是"一条语句"的**：`parse_all` 在同一个
        # 实例上连着解析多句，编号一路累加下去，于是 `?` 的下标在一段文本里
        # 是**绝对**的。这是有意的 —— 执行侧 `_eval` 就是拿 `ctx.params[e[1]]`
        # 直接下标取的，而一段文本只带**一份**参数（`execute_batch` 的
        # `(文本, 参数)` 是配对的，协议那边一条 text 也正好一句）。
        # 所以**别在这里重置**：重置之后第二句的 `?` 会拿到 0，读到的就是
        # 第一句的参数 —— 不报错，只是答案错。校验要按"一段文本的总数"来，
        # 那个在 `syntax._run` 那边。
        self.nparams = 0

    # ---- 记号流 ------------------------------------------------------

    def peek(self, ahead=0):
        j = self.i + ahead
        return self.toks[j] if j < len(self.toks) else self.toks[-1]

    def _at_seq(self, *words):
        """
        接下来的几个记号是不是**字面**这几个词（大小写不敏感）。

        和 `at_kw` 的区别：`at_kw` 比的是**规范名**（`删表` 和 `DROP` 都
        归一成 DROP），这里比的是原样文本。给英文的 `ADD COLUMN` /
        `DROP COLUMN` 用——那两个词刻意不放进 `KEYWORDS`，`at_kw` 够不着。
        """
        for k, want in enumerate(words):
            tok = self.peek(k)
            if tok.kind not in (T_NAME, T_KW) or tok.text.upper() != want:
                return False
        return True

    def next(self):
        tok = self.peek()
        if tok.kind != T_EOF:
            self.i += 1
        return tok

    def at_kw(self, *names):
        tok = self.peek()
        return tok.kind == T_KW and tok.value in names

    def at_punct(self, *chars):
        tok = self.peek()
        return tok.kind == T_PUNCT and tok.text in chars

    def eat_kw(self, *names):
        if self.at_kw(*names):
            self.next()
            return True
        return False

    def eat_punct(self, *chars):
        if self.at_punct(*chars):
            self.next()
            return True
        return False

    def expect_kw(self, *names):
        if not self.eat_kw(*names):
            raise self.error('这里应该写 %s' % ' / '.join(names))

    def expect_punct(self, *chars):
        if not self.at_punct(*chars):
            raise self.error('这里应该写 %s' % ' '.join(chars))
        return self.next()

    def expect_name(self):
        """要一个名字。**关键字也能当名字**——不然 `时间 时间` 这种
        「列名叫时间、类型也是时间」就写不出来了，而那是很自然的写法。"""
        tok = self.peek()
        if tok.kind in (T_NAME, T_KW):
            self.next()
            return tok.text
        raise self.error('这里应该是一个名字')

    def error(self, message):
        tok = self.peek()
        near = tok.text if tok.kind != T_EOF else '（结尾）'
        return SqlError('%s，但读到了 %r（第 %d 个字符）'
                        % (message, near, tok.pos))

    # ---- 入口 --------------------------------------------------------

    def parse_all(self):
        out = []
        while self.peek().kind != T_EOF:
            if self.eat_punct(';'):
                continue
            out.append(self.parse_statement())
            self.eat_punct(';')
        return out

    def parse_statement(self):
        tok = self.peek()
        if tok.kind != T_KW:
            raise self.error('一句话得从建表/删表/改表/插入/更新/删除/'
                             '查询/显示/批量/压实/建用户/删用户/改密/'
                             '授权/收回 这些词开头')
        kw = tok.value
        # 索引那四条得**排在 CREATE / DROP 前面**：英文这边 `CREATE INDEX`
        # 是两段（中文的 `建索引` 是一段），不让它们先分岔的话，
        # `CREATE INDEX …` 会被当成 `建表` 走进 parse_create。
        # 两条写法必须落到同一棵树上，这是本库对中英双语的老约定。
        idx_kw = None
        if kw in ('CREATEINDEX', 'DROPINDEX', 'REBUILDINDEX', 'CHECKINDEX'):
            idx_kw = kw
        elif self.peek(1).value == 'INDEXES':
            idx_kw = {'CREATE': 'CREATEINDEX', 'DROP': 'DROPINDEX',
                      'REBUILD': 'REBUILDINDEX',
                      'CHECK': 'CHECKINDEX'}.get(kw)
        if idx_kw is not None:
            self.next()
            self.eat_kw('INDEXES')          # 英文那条的第二段
            if idx_kw == 'CHECKINDEX':
                # `检查索引 <表名>`：**不按列** —— 它说的是"这张表的
                # 索引对不对"，而不是"某一个索引对不对"，所以按列反而
                # 把一次能看清的事拆碎了。
                return CheckIndex(self.expect_name())
            return self.parse_index_op(idx_kw)
        # 用户那几条也一样得**排在 CREATE / DROP 前面**：英文的
        # `CREATE USER` / `DROP USER` 是两段，先分岔掉才不会被
        # `parse_create` 当成 `建表` 接走（`USER` 不是类型词，
        # 真接过去会报一句完全指不到点子上的"没定主键"）。
        us_kw = None
        if kw in ('CREATEUSER', 'DROPUSER', 'SETPASSWORD', 'GRANT', 'REVOKE'):
            us_kw = kw
        elif self.peek(1).value == 'USERS':
            us_kw = {'CREATE': 'CREATEUSER', 'DROP': 'DROPUSER'}.get(kw)
        if us_kw is not None:
            self.next()
            self.eat_kw('USERS')            # 英文那条的第二段
            return self.parse_user_op(us_kw)
        if kw == 'CREATE':
            return self.parse_create()
        if kw == 'DROP':
            return self.parse_drop()
        if kw == 'ALTER':
            return self.parse_alter()
        if kw == 'SHOW':
            return self.parse_show()
        if kw == 'INSERT':
            return self.parse_insert()
        if kw == 'REPLACE':
            return self.parse_insert(overwrite=True)
        if kw == 'UPDATE':
            return self.parse_update()
        if kw == 'DELETE':
            return self.parse_delete()
        if kw == 'SELECT':
            return self.parse_query()
        if kw == 'BATCH':
            # `批量 开始` 和 `批量 提交` 各自是一个整体。拆成两半的话
            # 光秃秃一个 `批量` 也能算一条语句，读起来不知道它在说什么。
            self.next()
            if self.eat_kw('BEGIN'):
                return BatchBegin()
            if self.eat_kw('COMMIT'):
                return BatchCommit()
            raise self.error('批量 后面要跟 开始 或 提交')
        if kw == 'BEGIN':
            self.next()
            return BatchBegin()
        if kw == 'COMMIT':
            self.next()
            return BatchCommit()
        if kw == 'COMPACT':
            # 光杆一条。**后面多一个记号就是错的**：`压实 网页` 看起来
            # 像"只压这张表"，而区是多张表共用的，按表压根本压不动——
            # 收下它却按整库压，是在替写的人拿主意。宁可当场不认。
            self.next()
            # 收尾只许是空，或者在收尾的那个分号。**分号得认**：它是句子的
            # 正常收尾（外面 `parse_all` 会吃掉它），只认 EOF 的话
            # `压实;` 会被自己人挡下来——`压实` 能跑而 `压实;` 报错，
            # 是那种没人会往语法上想的怪毛病。
            # 比的是 `text` 不是 `value`：标点这条路的 `value` 是 None，
            # 符号本身在 `text` 里。
            t = self.peek()
            if t.kind != T_EOF and t.text != ';':
                raise self.error('`压实` 后面不带东西——它压的是整个'
                                 '存储区。区是多张表共用的，压不了一张表')
            return Compact()
        raise self.error('还不认识 %r 开头的语句' % tok.text)

    def parse_index_op(self, kw):
        """
        `建索引 / 删索引 / 重建索引 <表名> 按 (列1, 列2, …)`。

        三个写成同一条路，是因为**它们的形状就是一个形状**：都是
        "哪张表、按哪几列"。列清单是**有次序**的（前缀匹配靠它），
        所以这里不做去重也不排序 —— 排一下就把 `按 (a, b)` 和
        `按 (b, a)` 变成同一张索引了，而它们不是。

        **进来的时候开头的词已经被吃掉了**（中文一段 `建索引`、英文两段
        `CREATE INDEX`），这里只管 `<表名> 按 (…)` 这一段。
        """
        table = self.expect_name()
        # `按` 是老词（`排序 按` 用的就是它），这里复用同一个记号
        self.expect_kw('BY')
        self.expect_punct('(')
        columns = self.parse_name_list()      # 空的在这儿就报，报的位置更准
        self.expect_punct(')')
        cls = {'CREATEINDEX': CreateIndex, 'DROPINDEX': DropIndex,
               'REBUILDINDEX': RebuildIndex}[kw]
        return cls(table, columns)

    # ---- 建表 / 改表 / 删表 ------------------------------------------

    def parse_create(self):
        self.next()
        inline_if = self.eat_kw('IFNOTEXISTS')
        if not inline_if and self.eat_kw('IF'):
            self.expect_kw('NOT')
            self.expect_kw('EXISTS')
        self.eat_kw('TABLE')
        name = self.expect_name()
        self.expect_punct('(')

        columns = []
        pk = []
        dim = None
        while True:
            if self.at_kw('PRIMARY'):
                self.next()
                self.eat_kw('KEY')
                self.expect_punct('(')
                pk.extend(self.parse_name_list())
                self.expect_punct(')')
            elif self.at_kw('DIMENSION'):
                self.next()
                self.expect_punct('(')
                dim = self.expect_name()
                self.expect_punct(')')
            else:
                cd = self.parse_column_def()
                if cd.is_pk:
                    pk.append(cd.name)
                if cd.is_dim:
                    dim = cd.name
                columns.append(cd)
            if not self.eat_punct(','):
                break
        self.expect_punct(')')

        if not pk:
            raise SqlError('表 %s 没定主键。本库按主键直取，没有主键就'
                           '没有 O(1) 的点查——写上 主键(列名)' % name)
        names = set(c.name for c in columns)
        for p in pk:
            if p not in names:
                raise SqlError('主键列 %s 不在列清单里' % p)
        if dim is not None and dim not in names:
            raise SqlError('维度列 %s 不在列清单里' % dim)
        return CreateTable(name, columns, pk, dim, inline_if)

    def parse_name_list(self):
        out = [self.expect_name()]
        while self.eat_punct(','):
            out.append(self.expect_name())
        return out

    def parse_column_def(self):
        name = self.expect_name()
        tok = self.peek()
        if tok.kind != T_KW or tok.value not in TYPE_WORDS:
            raise self.error('列 %s 后面要跟类型（整数/实数/文本/'
                             '二进制/时间）' % name)
        self.next()
        type_ = TYPE_WORDS[tok.value]

        nullable = True
        default = schema.NO_DEFAULT
        is_pk = False
        is_dim = False
        while True:
            if self.eat_kw('PRIMARY'):
                self.eat_kw('KEY')
                is_pk = True
            elif self.eat_kw('DIMENSION'):
                is_dim = True
            elif self.eat_kw('NOTNULL'):
                nullable = False
            elif self.eat_kw('NULLABLE'):
                nullable = True
            elif self.eat_kw('DEFAULT'):
                default = self.parse_default(type_, name)
            elif self.at_kw('UNIQUE'):
                # 用 self.error 会缀一句"但读到了 唯一"，在这种
                # "我看懂了、只是做不到"的场合纯属噪音。
                raise SqlError(
                    '本库没有唯一约束。要唯一就把它并进主键——'
                    '别的列我们给不了这个保证，与其答应了做不到，'
                    '不如现在就说清楚')
            else:
                break
        return ColumnDef(name, type_, nullable, default, is_pk, is_dim)

    def parse_default(self, type_, col_name):
        if self.eat_kw('NULL'):
            return None
        tok = self.next()
        if tok.kind == T_STR:
            value = tok.value
        elif tok.kind == T_NUM:
            value = tok.value
        elif tok.kind == T_KW and tok.value in ('TRUE', 'FALSE'):
            value = tok.value == 'TRUE'
        elif tok.kind == T_KW and tok.value == 'DEFAULT':
            raise self.error('默认值不能又是默认值')
        else:
            raise self.error('默认值要是个字面量')
        try:
            return schema.coerce(type_, value)
        except schema.SchemaError as exc:
            raise SqlError('列 %s 的默认值不合适：%s' % (col_name, exc))

    def parse_drop(self):
        self.next()
        if_exists = self.eat_kw('IFNOTEXISTS')
        if not if_exists and self.eat_kw('IF'):
            self.expect_kw('EXISTS')
            if_exists = True
        self.eat_kw('TABLE')
        return DropTable(self.expect_name(), if_exists)

    def parse_alter(self):
        self.next()
        self.eat_kw('TABLE')
        name = self.expect_name()
        # 英文的 `ADD COLUMN` / `DROP COLUMN`。中文那两个词是**独立的关键字**
        # （`加列`/`删列` → ADDCOL/DROPCOL），英文这边不是：`DROP` 早就是
        # `删表` 的同义词，而 `ADD` / `COLUMN` 压根不在 `KEYWORDS` 里。
        #
        # 两条处理办法：
        #
        # 1. **成对认**，缺一不认。`ALTER TABLE t DROP c` 不认，要写全
        #    `DROP COLUMN c`。这不只是随大流：单个 `DROP` 在别处是删表，
        #    让它在 `改表` 后面也算删列，等于同一个词在两个位置上管两件事，
    #    以后读代码的人得先去数上下文才知道这句在干嘛。
        # 2. **按字面文本认，不把它们收进 `KEYWORDS`。** 收了就成保留字，
        #    `列名叫 add` 这种写法就没了——而这个库特意让 `expect_name`
        #    收关键字当名字。按文本认则只在 `改表` 后面这一个位置上生效。
        if self._at_seq('ADD', 'COLUMN'):
            self.next()
            self.next()
            return AlterTable(name, 'add', self.parse_column_def())
        if self._at_seq('DROP', 'COLUMN') or self._at_seq('DROP', 'COLUMNS'):
            self.next()
            self.next()
            col = self.expect_name()
            return AlterTable(name, 'drop', col, cascade=self.eat_kw('CASCADE'))
        if self.eat_kw('ADDCOL'):
            return AlterTable(name, 'add', self.parse_column_def())
        if self.eat_kw('DROPCOL'):
            self.eat_kw('COLUMNS')
            col = self.expect_name()
            return AlterTable(name, 'drop', col, cascade=self.eat_kw('CASCADE'))
        if self.eat_kw('RENAME'):
            old = self.expect_name()
            # `到` 在词法表里归给了 介于（`介于 1 到 10`），可它也是改
            # 列名的自然说法。关键字表是一对一的，所以这个歧义只能在
            # **用它的地方**拆——在这里，前面刚吃了一个列名，`到` 只可能
            # 是"改成"的意思。
            #
            # 分隔词也允许整个省掉：`改名 旧名 新名` 中文里读着顺，而且
            # 前面已经吃了一个名字，再来的那个名字不可能是别的意思。
            self.eat_kw('TO', 'BETWEEN2', 'IS', 'AS')
            new = self.expect_name()
            # 改列名和删列是同一件事的两种写法：索引的 `index_of` 里记的
            # 是**列名**，改完名字那个索引就再也切不动自己的键了。
            return AlterTable(name, 'rename', old, new,
                              self.eat_kw('CASCADE'))
        if self.eat_kw('DIMENSION'):
            return AlterTable(name, 'dim', self.expect_name())
        raise self.error('改表只能加列/删列/改名/换维度列（英文要成对写：'
                         'ADD COLUMN / DROP COLUMN / RENAME 旧的 TO 新的 / '
                         'DIMENSION 列）')

    def parse_show(self):
        self.next()
        if self.eat_kw('TABLES'):
            return Show('tables')
        if self.eat_kw('COLUMNS'):
            return Show('columns', self.expect_name())
        if self.eat_kw('REGIONS'):
            name = None
            if self.peek().kind in (T_NAME, T_KW) and \
                    not self.at_kw('WHERE', 'LIMIT', 'ORDER'):
                name = self.expect_name()
            return Show('regions', name)
        if self.eat_kw('INDEXES'):
            # 表名是**要**的：`显示 索引 <表名>` 说的是"这张表上有哪些
            # 索引"。不写表名就得把所有表的索引全倒出来，而"我这张表
            # 上到底有没有索引"才是人真正要问的那个问题。
            return Show('indexes', self.expect_name())
        if self.eat_kw('INDEX'):
            return Show('indexes', self.expect_name())
        if self.eat_kw('USERS'):
            # 光杆一条，**不按用户**。它的用途是"这一库里有谁"，而
            # 看成一份清单才有意义；单看一个人那份在 `显示 权限 <名>`
            # 那个更顺手的写法出来之前，先不加一个半拉子的形状。
            return Show('users')
        raise self.error('显示 后面只能跟 表列表 / 结构 / 区域 / 索引 / 用户')

    # ---- 用户与权限 --------------------------------------------------

    def parse_user_op(self, kw):
        """
        `建用户/删用户/改密/授权/收回` 的公共入口，**开头的词已经吃掉了**。

        四条里三条是"一个名字、后面跟点什么"，形状本来就一样；分成四个
        方法只会让"名字怎么读"这件事在四个地方各写一遍。
        """
        if kw == 'CREATEUSER':
            return CreateUser(self.expect_name(), self.expect_password())
        if kw == 'DROPUSER':
            return DropUser(self.expect_name())
        if kw == 'SETPASSWORD':
            return AlterUser(self.expect_name(), self.expect_password())
        name = self.expect_name()
        priv, table = self.parse_privilege()
        if kw == 'GRANT':
            return Grant(name, priv, table)
        return Revoke(name, priv, table)

    def expect_password(self):
        """
        口令**必须写成字符串字面量**。

        不收裸名字（`建用户 甲 abc`）是有意的：裸名字那条路上，
        `建用户 甲 口令` 里那个"口令"会被当成口令的**值**，而写的人
        多半是想说列名或别的什么——一个写错的字悄悄变成一个能用
        的账号口令，这是最不该静默的那一类事。

        空口令因此只能明写 `''`。它是允许的，但得是**他写下来的**，
        不是我们替他省出来的。
        """
        tok = self.peek()
        if tok.kind != T_STR:
            raise self.error("口令要写成字符串（比如 '口令'）——"
                             '不收裸名字，那样写错一个字就会静默变成口令')
        self.next()
        return tok.value

    def parse_privilege(self):
        """
        读一个权限词，**并顺手把"该不该跟表名"当场判掉**。

        返回 `(权限词, 表名或 None)`。

        表级词（查/插/改/删）**必须**跟表名，库级词**不许**跟。两种
        越界都当场报错，绝不"多出来的名字丢掉不要"——丢掉之后写的
        人以为授的是一张表、实际授的是整库，而这一句话是不会响的。
        """
        tok = self.peek()
        if tok.kind not in (T_NAME, T_KW):
            raise self.error('这里要写一个权限词。表级：%s；库级：%s；'
                             '或者 %s'
                             % ('/'.join(表级权限), '/'.join(库级权限),
                                权限全部))
        word = tok.text
        if word in 表级权限:
            self.next()
            nxt = self.peek()
            if nxt.kind not in (T_NAME, T_KW):
                raise self.error('%s 是表级权限，后面要跟表名'
                                 '（想给整库就给 %s）' % (word, 权限全部))
            return word, self.expect_name()
        if word in 库级权限 or word == 权限全部:
            self.next()
            nxt = self.peek()
            if nxt.kind in (T_NAME, T_KW):
                # 停在 EOF 或 `;` 上才是正常收尾。别的一律报错——包括
                # 表名，也包括下一个权限词（`授权 甲 建表 批量` 这种
                # 一条语句授两档的写法不在这次的形态里）。
                raise self.error('%s 是库级权限，后面不跟表名——'
                                 '要授某一张表就把权限词换成 %s'
                                 % (word, '/'.join(表级权限)))
            return word, None
        raise self.error('不认识的权限词 %r。表级：%s；库级：%s；或者 %s'
                         % (word, '/'.join(表级权限), '/'.join(库级权限),
                            权限全部))

    # ---- 写 ----------------------------------------------------------

    def parse_insert(self, overwrite=False):
        self.next()
        self.eat_kw('INTO')
        table = self.expect_name()
        columns = None
        if self.at_punct('('):
            self.next()
            columns = self.parse_name_list()
            self.expect_punct(')')
        self.expect_kw('VALUES')
        rows = []
        while True:
            self.expect_punct('(')
            row = []
            if not self.at_punct(')'):
                row.append(self.parse_expr())
                while self.eat_punct(','):
                    row.append(self.parse_expr())
            self.expect_punct(')')
            rows.append(row)
            if not self.eat_punct(','):
                break
        dim = None
        if self.eat_kw('DIMENSION'):
            dim = self.parse_expr()
        return Insert(table, columns, rows, dim, overwrite)

    def parse_update(self):
        self.next()
        table = self.expect_name()
        self.expect_kw('SETK')
        sets = []
        while True:
            name = self.expect_name()
            # `=` 也不认的话给个提示——中文里写 "设 名字 为 'x'" 很自然
            if self.eat_kw('IS'):
                pass
            else:
                self.expect_punct('=')
            sets.append((name, self.parse_expr()))
            if not self.eat_punct(','):
                break
        where = None
        if self.eat_kw('WHERE'):
            where = self.parse_expr()
        dim = None
        if self.eat_kw('DIMENSION'):
            dim = self.parse_expr()
        return Update(table, sets, where, dim)

    def parse_delete(self):
        self.next()
        self.eat_kw('FROM')
        table = self.expect_name()
        where = None
        if self.eat_kw('WHERE'):
            where = self.parse_expr()
        return Delete(table, where)

    # ---- 查询 --------------------------------------------------------

    def parse_query(self):
        """
        一条查询：一个 `Select`，或者若干 `Select` 用 并集/交集/差集 串起来。

        和 `parse_select` 分开，是因为子查询里也能写集合运算
        （`其中 编号 属于 (查询 a 并集 查询 b)`），那边要的正是这一层。
        """
        left = self.parse_select()
        if not self.at_kw('UNION', 'INTERSECT', 'EXCEPT'):
            return left
        self._no_tail(left, '并集/交集/差集 第一支')
        node = None
        prev = left
        while self.at_kw('UNION', 'INTERSECT', 'EXCEPT'):
            if node is not None:
                # 上一支现在确定不是最后一支了，它的尾巴必须为空
                self._no_tail(prev, '还有下一支的那一支')
            op = self.next().value
            all_ = self.eat_kw('ALL')
            if all_ and op != 'UNION':
                # 交集和差集天生就去重，"不去重"没有意义。收了不报的话
                # 它会被静默忽略，写的人以为自己在要一份有重复的差集。
                # 语义不对，不是语法不对 —— 用裸 `SqlError`，别让
                # `Parser.error` 在尾巴上缀一句"但读到了 X（第 N 个字符）"，
                # 那个位置指的是 `全部` 后面，跟这件事没关系。
                raise SqlError('只有 并集 有 `全部`。交集和差集天生去重，'
                               '不去重是另一个答案，没有这个写法')
            prev = self.parse_select()
            node = SetOp(left if node is None else node, op, prev, all_)
        # 最后一支的 排序/限制 归**整个结果**。留在他身上的话，
        # `查询 a 并集 查询 b 排序 按 x` 会变成"只把 b 排一下再拼"，
        # 而写法、位置、意图全是"排整个结果"——一个安静的错答案。
        node.order, node.limit, node.offset = prev.order, prev.limit, prev.offset
        prev.order, prev.limit, prev.offset = [], None, None
        return node

    def _no_tail(self, sel, who):
        """
        集合运算的某一支上不许写 排序 / 限制 / 偏移。

        `Select` 和 `SetOp` 都可能出现在这个位置（串三支以上就是嵌套的
        `SetOp`），只有 `Select` 带得了尾巴。
        """
        if not isinstance(sel, Select):
            return
        if sel.order or sel.limit is not None or sel.offset is not None:
            raise SqlError('%s 上不能写 排序 / 限制 —— 它们只能写在**最后'
                           '一支**后面，管的是整个结果' % who)

    def parse_select(self):
        self.next()
        sel = Select()
        sel.distinct = self.eat_kw('DISTINCT')
        while True:
            sel.items.append(self.parse_select_item())
            if not self.eat_punct(','):
                break
        self.expect_kw('FROM')
        sel.table = self.expect_name()
        sel.alias = self.parse_optional_alias()

        while True:
            kind = None
            if self.at_kw('JOIN'):
                self.next()
                kind = 'inner'
            elif self.at_kw('INNER'):
                self.next()
                self.eat_kw('JOIN')
                kind = 'inner'
            elif self.at_kw('LEFT'):
                self.next()
                self.eat_kw('JOIN')
                kind = 'left'
            elif self.at_kw('RIGHT'):
                self.next()
                self.eat_kw('JOIN')
                kind = 'right'
            if kind is None:
                break
            jt = self.expect_name()
            ja = self.parse_optional_alias()
            self.expect_kw('ON')
            sel.joins.append(Join(jt, ja, self.parse_expr(), kind))

        if self.eat_kw('WHERE'):
            sel.where = self.parse_expr()
        if self.eat_kw('GROUP'):
            self.eat_kw('BY')
            while True:
                sel.group.append(self.parse_expr())
                if not self.eat_punct(','):
                    break
            if self.eat_kw('WHERE'):     # having 还没有独立的中文词
                sel.having = self.parse_expr()
        if self.eat_kw('ORDER'):
            self.eat_kw('BY')
            while True:
                e = self.parse_expr()
                desc = False
                if self.eat_kw('DESC'):
                    desc = True
                else:
                    self.eat_kw('ASC')
                sel.order.append(OrderItem(e, desc))
                if not self.eat_punct(','):
                    break
        if self.eat_kw('LIMIT'):
            sel.limit = self.parse_nonneg_int('限制')
            if self.eat_kw('OFFSET'):
                sel.offset = self.parse_nonneg_int('偏移')
            elif self.eat_punct(','):
                # MySQL 的 `限制 偏移, 条数` 也有人写
                sel.offset = sel.limit
                sel.limit = self.parse_nonneg_int('限制')
        elif self.eat_kw('OFFSET'):
            sel.offset = self.parse_nonneg_int('偏移')
        return sel

    def parse_nonneg_int(self, what):
        tok = self.next()
        if tok.kind != T_NUM or not isinstance(tok.value, int) or tok.value < 0:
            raise SqlError('%s 后面要给一个非负整数，读到了 %r'
                           % (what, tok.text))
        return tok.value

    def parse_select_item(self):
        if self.at_punct('*'):
            self.next()
            return SelectItem(star())
        # 表名.*
        if self.peek().kind in (T_NAME, T_KW) and \
                self.peek(1).kind == T_PUNCT and self.peek(1).text == '.' and \
                self.peek(2).kind == T_PUNCT and self.peek(2).text == '*':
            q = self.expect_name()
            self.next()
            self.next()
            return SelectItem(star(q))
        expr = self.parse_expr()
        return SelectItem(expr, self.parse_optional_alias())

    def parse_optional_alias(self):
        if self.eat_kw('AS'):
            return self.expect_name()
        tok = self.peek()
        # 不带 AS 的别名：下一个是名字就认。这样 `查询 姓名 名字 从 t`
        # 能用；而 `查询 姓名 从 t` 里的 `从` 是关键字，不会被当成别名。
        if tok.kind == T_NAME:
            self.next()
            return tok.text
        return None

    # ---- 表达式 ------------------------------------------------------
    # 优先级从低到高：或 -> 与 -> 非 -> 比较 -> 加减 -> 乘除 -> 一元 -> 原子

    def parse_expr(self):
        return self.parse_or()

    def parse_or(self):
        left = self.parse_and()
        while self.at_kw('OR'):
            self.next()
            left = ('or', left, self.parse_and())
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.at_kw('AND'):
            self.next()
            left = ('and', left, self.parse_not())
        return left

    def parse_not(self):
        if self.eat_kw('NOT'):
            return ('not', self.parse_not())
        return self.parse_cmp()

    # 中文比较词 -> 符号。和符号走完全同一条路，于是 `年龄 大于 18`
    # 和 `年龄 > 18` 落进的是同一棵树，不是两套实现。
    _KW_CMP = {'GT': '>', 'LT': '<', 'GE': '>=', 'LE': '<=',
               'EQ': '=', 'NE': '!='}

    def _cmp(self, left, op, negated=False):
        if op == '<>':
            op = '!='
        right = self.parse_add()
        if _is_null_literal(right):
            # `= 空` 永远不成立，写的人多半想要 `为空`。与其给出一个
            # 空结果让他去猜，不如直接告诉他该写什么。
            raise SqlError('空值不能用 %s 比（结果永远是假）。'
                           '要判空请写 为空 / 不为空' % op)
        expr = ('cmp', op, left, right)
        # 否定**不改运算符**，而是整个套一层非。改运算符（> 变 <=）
        # 在有空值的时候不等价——空值在这里是个比谁都大的值，不是
        # SQL 里那个"未知"。套一层非是唯一不会出岔子的写法。
        return ('not', expr) if negated else expr

    def parse_cmp(self):
        left = self.parse_add()
        negated = False
        if self.at_kw('NOT'):
            # 这里的非只管后一个谓词（不 属于 / 不 介于）。它不该出现在
            # 别处——整条条件的非归上面那层，它在进 parse_cmp 之前就吃掉了。
            nxt = self.peek(1)
            if nxt.kind == T_KW and nxt.value in ('BETWEEN', 'IN',
                                                  'LIKE', 'ISNULL'):
                self.next()
                negated = True
            else:
                raise self.error('不 后面要跟 介于/属于/像/为空'
                                 '（整条条件取反请把 非 写在最前面）')

        if self.at_punct(*_CMP_OPS):
            return self._cmp(left, self.next().text, negated)
        if self.at_kw(*self._KW_CMP):
            return self._cmp(left, self._KW_CMP[self.next().value], negated)

        if self.eat_kw('BETWEEN', 'NOTBETWEEN'):
            negated = negated or self.toks[self.i - 1].value == 'NOTBETWEEN'
            lo = self.parse_add()
            if self.at_kw('BETWEEN2') or self.at_kw('AND'):
                self.next()
            else:
                raise self.error('介于 后面要写 到 / 之间 / 至')
            hi = self.parse_add()
            self.eat_kw('BETWEEN2')      # `介于 1 到 10 之间` 的那个尾巴
            return ('between', left, lo, hi, negated)
        if self.eat_kw('IN', 'NOTIN'):
            negated = negated or self.toks[self.i - 1].value == 'NOTIN'
            self.expect_punct('(')
            # `属于 (查询 …)` 走**另一个标签** `insub`，不跟 `in` 挤。
            # 挤的话 `e[2]` 一会儿是候选的 list、一会儿是子查询节点，
            # 而在 `('in', 值, 候选 list, 取反)` 上做通用遍历的地方
            # （`count_params` / `_subst`）就会把子查询节点当成**一个候选**
            # 去遍历它的元素。分开就没有这个歧义。
            if self.at_kw('SELECT'):
                right = ('subq', self.parse_query())
                self.expect_punct(')')
                return ('insub', left, right, negated)
            vals = []
            if not self.at_punct(')'):
                vals.append(self.parse_expr())
                while self.eat_punct(','):
                    vals.append(self.parse_expr())
            self.expect_punct(')')
            return ('in', left, vals, negated)
        if self.eat_kw('LIKE', 'NOTLIKE'):
            negated = negated or self.toks[self.i - 1].value == 'NOTLIKE'
            return ('like', left, self.parse_add(), negated)
        # ('null', 表达式, 是不是"非空")——第三个元素是"非空"，
        # 不是"取反"。这里写反过一次，`IS NULL` 会安静地变成 `IS NOT NULL`，
        # 结果刚好相反而且看不出哪里不对。
        if self.eat_kw('ISNULL'):
            return ('null', left, negated)
        if self.eat_kw('NOTNULL'):
            return ('null', left, not negated)
        if self.eat_kw('IS'):
            neg = self.eat_kw('NOT')
            if not self.eat_kw('NULL'):
                raise self.error('为 后面只能跟 空')
            return ('null', left, neg)
        if negated:
            raise self.error('不 后面要跟 介于/属于/像/为空')
        return left

    def parse_add(self):
        left = self.parse_mul()
        while self.at_punct(*_ADD_OPS):
            op = self.next().text
            left = ('arith', op, left, self.parse_mul())
        return left

    def parse_mul(self):
        left = self.parse_unary()
        while self.at_punct(*_MUL_OPS):
            op = self.next().text
            left = ('arith', op, left, self.parse_unary())
        return left

    def parse_unary(self):
        if self.eat_punct('-'):
            return ('neg', self.parse_unary())
        if self.eat_punct('+'):
            return self.parse_unary()
        return self.parse_atom()

    def parse_atom(self):
        tok = self.peek()

        # **`当` 必须在最前面接住。** 往下落到那句
        # `tok.kind in (T_NAME, T_KW)` 的话，`当 状态 = 1 那么 …` 会被
        # 读成一个叫 `当` 的列，后面 `状态` 成了别名之类 —— 报不报错全看
        # 运气，而"读成列名"这条路是**不报错**的。这一类关键字当列名用
        # 必须走反引号，注释见 `KEYWORDS` 里那一段。
        if tok.kind == T_KW and tok.value == 'CASE':
            return self.parse_case()

        if tok.kind == T_PARAM:
            self.next()
            # **编号在这里就给死，不留到执行时再数。** 执行时数的话，
            # 得把整棵树按源码次序重走一遍才数得对（有些节点的第一个
            # 元素是运算符字符串不是子表达式，通用的深度优先走法会
            # 数岔）。在这儿数是一行的事，而且永远和读到的顺序一致。
            idx = self.nparams
            self.nparams += 1
            return ('param', idx)

        if tok.kind == T_STR:
            self.next()
            return lit(schema.TEXT, tok.value)

        if tok.kind == T_NUM:
            self.next()
            if isinstance(tok.value, int):
                return lit(schema.INT, tok.value)
            return lit(schema.FLOAT, tok.value)

        if tok.kind == T_KW and tok.value in ('TRUE', 'FALSE'):
            self.next()
            return lit(schema.INT, 1 if tok.value == 'TRUE' else 0)

        if tok.kind == T_KW and tok.value == 'NULL':
            self.next()
            return ('nullval',)

        if tok.kind == T_KW and tok.value == 'DEFAULT':
            self.next()
            return ('default',)

        if tok.kind == T_KW and tok.value in ('ISNULL', 'NOTNULL'):
            raise self.error('%r 前面要跟一列' % tok.text)

        if tok.kind == T_PUNCT and tok.text == '(':
            self.next()
            # **`(` 后面跟着 查询 就是子查询。** 子查询在这棵树里是个
            # 不透明节点 `('subq', 查询)`：它有自己的数据源、自己的一套
            # 分组/排序，外层看它只等于"一个值"或"一列值"。所以所有
            # 通用的遍历（`_walk` 那一族）**必须把它当边界、不下去** ——
            # 下去的话，子查询里的 `计数(*)` 会把外层查询也标成"有聚合"，
            # 外层的语义**悄悄变掉**，而结果看着还挺合理。
            if self.at_kw('SELECT'):
                inner = ('subq', self.parse_query())
            else:
                inner = self.parse_expr()
            self.expect_punct(')')
            return inner

        if tok.kind == T_KW and tok.value in _AGG_WORDS:
            return self.parse_call()

        if tok.kind in (T_NAME, T_KW):
            name = self.expect_name()
            # 函数调用：计数(列)
            if self.at_punct('('):
                return self.parse_call(name)
            qualifier = None
            if self.at_punct('.'):
                self.next()
                qualifier = name
                name = self.expect_name()
            return col(name, qualifier)

        raise self.error('这里应该是一个值、一列或一个条件')

    def parse_call(self, name=None):
        """
        `name` 给 None 表示那个记号还**没**吃掉——`计数(...)` 是以关键字
        直接开头的，走的就是这条；给了名字表示调用方已经吃过、只是把
        名字递过来报个到。两个入口的差别就在这里，写错过一次。
        """
        if name is None:
            name = self.next().text
        name = KEYWORDS.get(name, name)      # 中文函数名归一成规范名
        if name not in _AGG_WORDS and name not in _SCALAR_WORDS:
            raise SqlError('没有 %s 这个函数。聚合有 计数 / 求和 / 平均 / '
                           '最大 / 最小；标量有 %s'
                           % (name, ' / '.join(
                               _SCALAR_NAMES[k] for k in _SCALAR_ORDER)))
        self.expect_punct('(')
        # **标量函数不收 `去重`。** `计数(去重 列)` 是有意义的，
        # `长度(去重 列)` 不是。吃了不报的话它会被静默忽略，
        # 写的人以为筛过了。
        distinct = False
        if name in _AGG_WORDS:
            distinct = self.eat_kw('DISTINCT')
        elif self.at_kw('DISTINCT'):
            # 不点名说清楚的话，`长度(去重 a)` 会走到"这里应该写 )"
            # 那句 —— 报的位置对，说的却是另一件事。
            raise self.error('%s 不收 去重。`去重` 是聚合才有的，'
                             '它管的是"这一组里有几个不同的值"；%s 是'
                             '逐行算的，一行只有一个输入'
                             % (_SCALAR_NAMES[name], _SCALAR_NAMES[name]))
        args = []
        if self.at_punct('*'):
            if name not in _AGG_WORDS:
                raise self.error('%s(*) 是聚合才有的写法' % name)
            self.next()
            args.append(star())
        elif not self.at_punct(')'):
            args.append(self.parse_expr())
            while self.eat_punct(','):
                args.append(self.parse_expr())
        self.expect_punct(')')

        if name in _AGG_WORDS:
            return ('call', name, args, distinct)

        # 标量函数**摊平**成 ('func', 名, 参数...)，不塞 list。
        # `_subst` 的泛型递归只走 `e[1:]` 的直接元素，遇到 list 原样返回
        # （见那边那句注释），塞成 list 的话参数里的聚合替换不到。
        lo, hi = _SCALAR_WORDS[name]
        if not lo <= len(args) <= hi:
            want = ('%d 个' % lo if lo == hi
                    else '%d 到 %d 个' % (lo, hi))
            raise SqlError('%s 要 %s 参数，这里给了 %d 个'
                           % (_SCALAR_NAMES[name], want, len(args)))
        return ('func', name) + tuple(args)

    def parse_case(self):
        """
        `当 条件 那么 结果 [当 条件 那么 结果]… [否则 结果] 结束`

        产出 `('case', [('when', 条件, 结果), …], 否则结果或 None)`。

        分支装在一个 **list** 里，不在 `e[1:]` 的直接元素上 —— `_subst`
        的泛型递归走不到 list 里面去（它遇到 list 原样返回），所以
        `syntax.py` 那边**必须**补一条"list 也递归"，不补的话分组查询里
        分支里的聚合替换不掉。那条修复顺带治好了 `属于` 的候选列表：
        `('in', 值, [候选…], 取反)` 的候选同样是 list。

        分支自身是三元组（理由见下），所以"条件在第 1 位、结果在第 2 位"，
        和 `('cmp', 符号, 左, 右)` 一个路子。

        收的是**搜索式**（每个分支自带条件）。SQL 的**简单式**
        （`情形 列 当 1 那么 …`，等价于一串 `列 = 1`）这里**不做**：
        它的 `当 空 那么 …` 触及本库「`= 空` 在解析期就拒绝」和
        「空比任何值都大」两条约定，怎么定是个语义选择，得先问。
        写成 `当 列 = 1 那么 …` 完全等价，所以现在只是少一种拼法，
        不是少一种能力 —— 而且拼错的时候是**报错**，不是算错。
        """
        self.expect_kw('CASE')
        # 中文写法第一个分支用 `当` 起头（和开头的 `当` 是同一个词），
        # 英文写法是 `CASE WHEN …`。多出来的这个 WHEN 得在这儿吃掉 ——
        # 不吃的话它会落到 `parse_atom` 里被当成一个叫 WHEN 的**列名**，
        # 然后在"这里应该写 THEN"那里报一个跟真正原因无关的错。
        self.eat_kw('WHEN')
        # `当 从 t` —— 光秃秃一个 `当`。不在这儿拦的话，`从` 会被
        # `parse_atom` 当成一个**列名**吃下去，然后报"这里应该写 THEN"
        # 并且把位置指到 `t` 上。位置是对的，说的却是另一回事：真正
        # 缺的是条件。这几个词都开不了表达式，见到就是条件漏写了。
        if self.at_kw('FROM', 'THEN', 'ELSE', 'END', 'WHERE', 'GROUP',
                      'ORDER', 'LIMIT'):
            raise self.error('条件表达式缺条件：`当` 后面要跟一个条件')
        branches = []
        while True:
            cond = self.parse_expr()
            if self.at_kw('CASE'):
                # `当 条件 当 条件2 …` —— 少写了 `那么`，十有八九是漏了。
                # 不单独报的话会一路解析出个乱七八糟的东西再在别处炸。
                raise self.error('分支之间少了 `那么`。写法是 '
                                 '`当 条件 那么 结果 … 结束`')
            self.expect_kw('THEN')
            val = self.parse_expr()
            # **分支写成 `('when', 条件, 结果)` 三元组，不是裸的二元组。**
            # 全库的通用遍历（`count_params` / `_subst` / `_walk` / `_freeze`）
            # 都是走 `e[1:]` 的 —— 裸二元组的第 0 位是**条件**，会被当成
            # 标签跳过，于是条件里的 `?` 数不出来、里面的聚合也替换不掉。
            # 给条件一个下标 1 的位置，这类遍历天然就对了。
            branches.append(('when', cond, val))
            if self.at_kw('WHEN', 'CASE'):
                self.next()
                continue
            break
        other = None
        if self.eat_kw('ELSE'):
            other = self.parse_expr()
        if not self.at_kw('END'):
            # `,` `)` `从` … 都会落到这儿。说清"少了个 结束"，比让上面
            # 那句泛泛的"这里应该写 …"去猜要准。
            raise self.error('条件表达式没写完，缺 `结束`')
        self.next()
        return ('case', branches, other)


def _is_null_literal(expr):
    return isinstance(expr, tuple) and expr and expr[0] == 'nullval'


def parse(text):
    """一段文本 -> 一条语句。多写了语句会报错。"""
    stmts = parse_all(text)
    if not stmts:
        raise SqlError('没有语句')
    if len(stmts) > 1:
        raise SqlError('parse 只收一条语句，这里给了 %d 条' % len(stmts))
    return stmts[0]


def parse_all(text):
    """一段文本 -> 语句列表。分号可有可无，末尾有没有都行。"""
    if not isinstance(text, str):
        text = text.decode('utf-8')
    return Parser(tokenize(text)).parse_all()


def count_params(stmt):
    """
    一条语句里有多少个 `?` 占位符。

    执行前拿它跟实际给的参数个数对一遍——少给一个和多给一个都该在跑
    之前就说出来，而不是等到某一行取参数时才下标越界：那时候前面的
    行已经写下去了，报的错也指不到真正写错的地方。

    语句级的字段（表名、列名、改表的动作）不是表达式，但都挂在
    `__slots__` 上，统一走一遍属性比按类型逐个写一遍可靠——将来加了
    新语句类型也不会漏。"""

    total = 0
    stack = [stmt]  # 传一**串**语句也行：下面 `list` 那一支会摊开
    while stack:
        item = stack.pop()
        if isinstance(item, (str, bytes, int, float, bool)) or item is None:
            continue
        if isinstance(item, tuple):
            if item and item[0] == 'param':
                total = max(total, item[1] + 1)
                continue
            stack.extend(item[1:])
            continue
        if isinstance(item, list):
            stack.extend(item)
            continue
        slots = getattr(type(item), '__slots__', None)
        if slots:
            for name in slots:
                stack.append(getattr(item, name, None))
    return total
