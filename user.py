# -*- coding: utf-8 -*-
"""
用户、身份、权限。

这个库的权限模型是**两层**，两层管的是两件不同的事：

  - **表级**（`查` `插` `改` `删`）：某一张表上能不能干这件事。**是分表的**，
    这才是"精细"两个字落地的地方；
  - **库级**（`建表` `删表` `改表` `建索引` `删索引` `压实` `显示` `批量`
    `管用户`）：不分表的那些动作。`建表` 尤其**不可能**分表——那张表还没
    出生。

**认不出的语句一律拒绝，不存在"默认放行"这条退路。** 这条不是风格问题：
权限这东西一旦有一条静默放行的分支，那一条分支就是全部的安全性所在，
而它偏偏是最不会被测到的那一条。见 `_权限要求`。

**口令只存摘要**：`salt` 和 `pwd_hash` 两样，配 `hmac.compare_digest` 比。
明文一个字都不落盘，`概览()` 那份（`显示 用户` 和界面用的）也**构造不出来**
明文——它不是"记得别带上"，是压根没放进那个字典。
"""

import hashlib
import hmac
import json
import os

import sql


# ── 权限词 ──────────────────────────────────────────────────────────
#
# 词汇本身在 `sql.py` 里（解析器认词要用它），这里只是搬过来当短名字。
# **一份词汇，两个用处**：语法层拿它认词，这一层拿它当键。分成两份迟早
# 会对不上，而对不上的表现是"授了权但判不过"或者更糟——"判得过但授不了"。

表级权限 = sql.表级权限
库级权限 = sql.库级权限
权限全部 = sql.权限全部

# 库级里**管表**的那几个。它们不分表，所以**给了就等于全库的表都能碰**——
# `可读()` 拿这个集合去回答"这人看得见哪些表"（见那里的说明）。
管表权限 = frozenset(('建表', '删表', '改表', '建索引', '删索引'))


# ── 口令 ────────────────────────────────────────────────────────────

_算法 = 'pbkdf2_sha256'
_哈希 = 'sha256'
# 迭代次数。**这个数和盐一样，跟每条记录一起存**（见 `摘要` 的说明）。
_轮数 = 200_000
_盐长 = 16


def 摘要(口令, salt, 轮数=_轮数):
    """
    口令 -> 摘要。`salt` 和 `轮数` 都由调用方给。

    **轮数不写死在这里**，因为写死它就等于"这个数一辈子不能改"：改的那
    一天，库里所有老口令一起验不过，而那些口令的主人手里只有原文，没有
    补救的路——除了叫他们挨个重设一遍。存进记录就没这个代价：老的按老的
    轮数验，新的按新的。

    `pbkdf2_hmac` 是标准库的（`hashlib`），没引第三方。
    """
    return hashlib.pbkdf2_hmac(_哈希, 口令.encode('utf-8'), salt, 轮数)


class User(object):
    """
    一个用户。

    `salt` / `pwd_hash` / `轮数` 三样是口令那摊事，谁都不许拿出来给界面
    看——要给界面看请走 `概览()`。

    `global_privs` 是 `set`，元素是 `库级权限` 里的词；
    `table_privs` 是 `dict`，表名 -> `set`，元素是 `表级权限` 里的词。
    **表名 `'*'` 是通配**：`授权 甲 全部` 落下来的就是它。
    """

    __slots__ = ('name', 'salt', 'pwd_hash', '轮数', 'superuser',
                 'global_privs', 'table_privs')

    def __init__(self, name, salt, pwd_hash, 轮数, superuser=False,
                 global_privs=None, table_privs=None):
        self.name = name
        self.salt = salt
        self.pwd_hash = pwd_hash
        self.轮数 = 轮数
        self.superuser = superuser
        self.global_privs = set(global_privs or ())
        self.table_privs = dict(table_privs or {})


def 建(名字, 口令, superuser=False):
    """
    造一个新用户。**权限一个都不给**——新用户从"什么都看不见"开始。

    不给个"默认读者"之类的起步权限，是因为默认权限是那种所有人都会忘掉
    它存在的东西：给了之后每加一张表都自动多一个能看到它的人，而没有任何
    一个地方写着这件事。`授权` 是明写的一句话，代价是管理员多打几个字。
    """
    名字 = (名字 or '').strip()
    if not 名字:
        raise ValueError('用户名不能是空的')
    salt = os.urandom(_盐长)
    return User(名字, salt, 摘要(口令, salt, _轮数), _轮数, superuser)


def 核对(user, 口令):
    """口令对不对。**用 `compare_digest`** —— `==` 是短路比较，比到第
    一个不同就返回，比多长能从耗时上看出来。这里比的是摘要不是原文，
    但仍然不该给出一条"逐字节猜"的路。"""
    if user is None:
        return False
    if not isinstance(口令, str):
        return False
    got = 摘要(口令, user.salt, user.轮数)
    return hmac.compare_digest(got, user.pwd_hash)


def 改口令(user, 口令):
    """就地换口令。盐也换新的——**同一个口令在两个用户手里摘要一样，
    这件事本身就是一条能看出来的信息**。"""
    user.salt = os.urandom(_盐长)
    user.轮数 = _轮数
    user.pwd_hash = 摘要(口令, user.salt, user.轮数)


# ── 序列化 ──────────────────────────────────────────────────────────

_记录版本 = 1


def 序列化(user):
    """盘上那条记录。**裸 JSON blob**，和目录、表定义、墓碑一个路子
    （系统键不走 `encode_cells`，那套是给行的）。

    `名字` 存进记录里**是冗余的**（键里已经编了一份）。留着它是因为它
    能当一个廉价的完整性检查：读回来发现两个名字对不上，说明盘上那条
    记录不是这个人的——这种事只会在人为改动或恢复错了文件时发生，而
    那正是最不该静默咽下去的时候。
    """
    return json.dumps({
        'v': _记录版本,
        '名字': user.name,
        '算法': _算法,
        '轮数': user.轮数,
        '盐': user.salt.hex(),
        '摘要': user.pwd_hash.hex(),
        '管理员': bool(user.superuser),
        '库级': sorted(user.global_privs),
        '表级': dict((k, sorted(v)) for k, v in user.table_privs.items()),
    }, ensure_ascii=False, sort_keys=True).encode('utf-8')


def 反序列化(名字, blob):
    """
    盘上那条记录 -> `User`。

    **认不出的一律抛**（版本、算法、权限词），不当作"没有"。和
    `schema.decode_symbol` 那条规矩一样：往后加新东西时，老代码必须
    响亮地失败，不能安静地少读一截——少读一截的表现是"这人权限变小了"，
    而那是一种没人会发现、直到有人该被挡住却没被挡住时才发作的错。
    """
    try:
        d = json.loads(blob.decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError('用户 %s 的记录读不出来：%s' % (名字, exc))
    if not isinstance(d, dict):
        raise ValueError('用户 %s 的记录不是一个字典' % 名字)
    v = d.get('v')
    if v != _记录版本:
        raise ValueError('用户 %s 的记录版本是 %r，这个版本的程序只认 %d'
                         '——这个库可能来自更新的版本' % (名字, v, _记录版本))
    if d.get('算法') != _算法:
        raise ValueError('用户 %s 的口令算法是 %r，认不出'
                         '（这个库可能来自更新的版本）' % (名字, d.get('算法')))
    if d.get('名字') != 名字:
        raise ValueError('用户 %s 的记录里写的是 %r——键和内容对不上，'
                         '这个库坏了' % (名字, d.get('名字')))
    库级 = _认权限词('用户 %s 的库级权限' % 名字, 库级权限, d.get('库级'))
    表级 = d.get('表级') or {}
    if not isinstance(表级, dict):
        raise ValueError('用户 %s 的表级权限不是一个字典' % 名字)
    表级 = dict((k, _认权限词('用户 %s 在表 %s 上的权限' % (名字, k),
                              表级权限, w)) for k, w in 表级.items())
    try:
        salt = bytes.fromhex(d['盐'])
        pwd_hash = bytes.fromhex(d['摘要'])
        轮数 = int(d['轮数'])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError('用户 %s 的口令那几项读不出来：%s' % (名字, exc))
    return User(名字, salt, pwd_hash, 轮数, bool(d.get('管理员')),
                库级, 表级)


def _认权限词(哪里, 词表, 值):
    """一段权限清单 -> `set`。**认不出的词直接抛**，见 `反序列化`。"""
    out = set()
    for w in (值 or ()):
        if w not in 词表:
            raise ValueError('%s 里有个认不出的权限词 %r' % (哪里, w))
        out.add(w)
    return out


def 概览(user):
    """
    给 `显示 用户` 和界面的那一份。

    **这个函数存在的全部理由，是让"不许回摘要"变成"没法回摘要"**：
    它一个字段一个字段地构造，`salt`/`pwd_hash` 就从来没进过这个字典。
    要是改成像 `vars(user)` 那样整份倒出去，将来某天加一个字段就会
    顺手把它漏出去，而且不会有任何地方响一声。
    """
    return {
        '名字': user.name,
        '管理员': bool(user.superuser),
        '库级': sorted(user.global_privs),
        '表级': dict((k, sorted(v)) for k, v in user.table_privs.items()),
        # 口令我给不回来，只给"设过没有"。今天它是恒真的（`建用户`
        # 口令必填），留着是为了将来真有"禁用账号"时界面上有个位置放。
        '有口令': bool(user.pwd_hash),
    }


# ── 身份 ────────────────────────────────────────────────────────────

# 本地腿的会话号。**必须和远程的会话号不撞**：批状态是按会话号记的
# （`syntax._批会话`），撞上的表现是"A 的批把 B 的写按住了"——正是
# 这次要治的那个毛病换个地方复发。远程那边号来自 `transport.Session`，
# 是会话计数器给的整数，从 0 往上；这里用负数，永久隔开。
本地会话号 = -1


class 身份(object):
    """
    一条语句是谁发的、从哪个会话来的。

    **它由服务端自己造，不能由请求方声明。** 本地腿的身份来自
    `Gate.登录`；远程腿的来自登录那一帧之后绑在 `Session` 上的东西。
    任何"把身份写在 SQL 文本里/DEFINED 成参数"的做法都等于让客户端
    自己填它——那样表级权限在远程那条路上就是零，见 `io_layer` 的
    `_RemoteLeg` 那段说明。
    """

    __slots__ = ('user', '会话号')

    def __init__(self, user, 会话号):
        self.user = user
        self.会话号 = 会话号


# ── 判权限 ──────────────────────────────────────────────────────────

def 可过(user, 要求):
    """
    `要求` 里**每一条**都得有。

    **空的 `要求` = 放行。** 那不是"没检查"，是"这条语句没有库级要求"：
    比如 `查询`，它一条库级要求都没有，全交给表级那道关口（`_table`）。
    把"没有要求"和"要求为空"统一成同一个东西，是为了让 `_权限要求` 那张
    表能写全 21 条——写不全的那些才是洞。
    """
    for w in 要求:
        if not 判权限(user, w):
            return False
    return True


def 判权限(user, 权限词, 表=None):
    """
    这人在这个词上有权吗。

    `表` 给不给决定问的是哪一层：
      - 不给：问库级（`权限词` 必须是 `库级权限` 里的词）；
      - 给了：问这张表上的表级权限，`'*'` 那条通配也算。

    **`user is None` 一律 False。** 没登录 = 没有权限。这条不能反过来
    写成"没身份就跳过检查"——那条路上没有第二道门。
    """
    if user is None:
        return False
    if user.superuser:
        return True
    if 表 is None:
        if 权限词 in 表级权限:
            # 表级词却不给表名，是调用方写错了。**不静默当成库级判**——
            # `查`/`插` 这些词在库级里根本没有，那样判的结果是恒 False，
            # 看起来像"权限不够"，实际是"程序写错了"，而这两种得区分开。
            raise ValueError('%s 是表级权限词，判它得给表名' % 权限词)
        return 权限词 in user.global_privs
    for k in (表, '*'):
        if 权限词 in user.table_privs.get(k, ()):
            return True
    return False


def 可读(user, 表名):
    """
    能不能**看见**这张表。

    两件事都靠它：`显示 表列表` 里列不列它，以及"没有这张表"那句话里
    念不念它的名字。

    **「写」蕴含「读」**：只有 `插`/`改`/`删` 而没有 `查` 的人，也必须
    看得见那张表。不这么做的话，一个授了写却看不见表的用户在界面上根本
    装不出这张表——那份写权限永远用不上，等于没授。这条规矩实现一次就好，
    别让每个调用点各自去判。

    **库级里管表的几个也算看得见**：`删表` 一给就是全库的表都能删，
    "能删却看不见"只会让人以为这表没了。反过来 `显示` 不算——它管的是
    "准不准问"，问出来的东西仍由表级权限决定，这正是"精细"该有的样子。
    """
    if user is None:
        return False
    if user.superuser:
        return True
    if user.table_privs.get('*'):
        return True
    if user.table_privs.get(表名):
        return True
    return bool(user.global_privs & 管表权限)


# ── 每条语句要什么 ──────────────────────────────────────────────────
#
# 库级要求。表级那部分**不在这里**——它由 `_table`/`_writable` 两个关口
# 自动判（包括 join 和子查询里的表，见 `syntax._run` 的说明）。
# 这里的 `()` 不是说"随便"，是说"这条语句没有库级要求"。
#
# ⚠ **这张表和 `syntax._DISPATCH` 必须一一对应**，`syntax.py` 末尾有一条
# 模块级断言盯着这件事：将来加了第 22 种语句忘了往这里补一行，是
# `ImportError` 而不是静默越权。
_权限要求 = {
    sql.Select:      (),
    sql.SetOp:       (),
    sql.Insert:      (),
    sql.Update:      (),
    sql.Delete:      (),
    sql.CreateTable: ('建表',),
    sql.DropTable:   ('删表',),
    sql.AlterTable:  ('改表',),
    sql.CreateIndex: ('建索引',),
    sql.DropIndex:   ('删索引',),
    # 重建索引是**两件事**（先删后建，见 `syntax._rebuild_index`），
    # 所以两样都要。只要其中一样就能重建，等于绕开另一样的授权。
    sql.RebuildIndex: ('删索引', '建索引'),
    sql.CheckIndex:  (),            # 只读，读的是那张表：走 `_table` 的 `读` 档
    sql.Show:        ('显示',),     # 具体显示什么另说，见 `syntax._show`
    sql.BatchBegin:  ('批量',),
    sql.BatchCommit: ('批量',),
    sql.Compact:     ('压实',),
    sql.CreateUser:  ('管用户',),
    sql.DropUser:    ('管用户',),
    sql.AlterUser:   ('管用户',),
    sql.Grant:       ('管用户',),
    sql.Revoke:      ('管用户',),
}


# `_table(name, 需要)` 的档 -> 表级权限词。
#
# `'写'` 不在里面，它是 `_table` 的**默认档**，含义是"随便哪一样写权限"，
# 由 `syntax._table` 自己解释。那个默认值的用途是**兜底**：哪个调用点
# 忘了标自己是什么档，得到的是"错拒"（响的、看得见的），而不是"静默放行"。
# 所以 `'管'` 绝不能当默认值——那正好是反向的。
档到词 = {
    '读': '查',
    '插': '插',
    '改': '改',
    '删': '删',
}
写档 = ('插', '改', '删')
