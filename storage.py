#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
storage.py — 存储层

**存储层 = 管理整个永久存储区。** 盘上那块地方（目录、超级块、一个区一个
文件、每区一个布隆）归它管。

**它在内核里的位置是"语法层下面那一个"**——不是顶上那个。以前这张图画反了
（把存储层画在最上面，语法层在它底下），照那张图装配出来的就是"存储层持有
语法层"，而实际跑的从来是反过来：**`Syntax` 先开 Arena，存储层借它**。图已
经按真的那条路改了。

    内核（龙龟之心）
      └ 语法层 —— 领导：调度数据的增删改查
           ├ 存储层      管理永久存储区（← 本文件）
           └ 输入输出层   控制数据的流入流出

    语法层底下还有三层，由存储层间接管着：
      store_format  字节怎么摆
      mortise       内存怎么缓冲（由 arena 装配）
      arena         区域怎么管、直存直取

三条界线
--------

**存储层不认得帧。** 帧是传输协议的事，归 `socket_layer`。这里收的是
**语句文本和字节**：`execute('查询 * 从 网页')`、`put(b'k', b'v')`。
把帧放进这一层，就等于让"盘上怎么摆"去迁就"网上怎么传"——而这两件事
的变更节奏完全不同。

**存储层不认得 socket，也不认得客户端。** 一条连接还是一个进程，对它
是同一件事。它只保证一件事：**一个存储层实例 = 一个永久存储区 = 一个
Arena = 一个写者。** 这是结构性保证，不是约定：`Syntax` 开 Arena，这里
借它的，没有任何口子能开出第二个。

**存储层不解释数据。** 键和值是不透明的字节串；墓碑长什么样、主键怎么
编、维度代表什么，全是语法层的事。

    with Storage('mydb').create() as st:
        st.execute('建表 网页 (编号 整数 主键, 标题 文本)')
        st.insert('网页', [1, '首页'])
        print(st.select('网页'))
"""

import syntax as syntax_mod

# **存储层不 import kernel。** 以前这里有一行，但一次都没用到——理由现在
# 更硬了：字节门在**输入输出层**（`io_layer.byte_handler`），帧的形状在
# `kernel.py`，两样都不归这层。本层只转 `self.syntax.put/get/scan` 那几个
# 口子。留着那个 import 并不会出错，它只会让"到底谁认得帧"这件事在图上
# 又糊一层。

__all__ = ['Storage', 'insert_text']


def insert_text(table, nvalues, columns=None, dimension=None):
    """
    拼一条 `插入` 的语句文本。**值一律走 `?` 绑定，绝不拼进文本。**

    放在模块这一层而不是 `Storage.insert` 里面，是因为要用它的不止一处：
    增删改查那几个捷径从这儿拼，**输入输出层的批量灌数据也从这儿拼**。
    两处各写一份的话，改了一处另一处不会响——而它拼错的样子是"值被
    当成语法"，那是能改结果的错，不是能看出来的错。
    """
    head = '插入 %s' % table
    if columns:
        columns = list(columns)
        if len(columns) != nvalues:
            raise ValueError('列数 %d 和值数 %d 对不上'
                             % (len(columns), nvalues))
        head += ' (%s)' % ', '.join(columns)
    text = '%s 值 (%s)' % (head, ', '.join(['?'] * nvalues))
    if dimension is not None:
        text += ' 维度 ?'
    return text


class Storage(object):
    """一个存储层实例 = 一个永久存储区。"""

    def __init__(self, root, config=None, 密文=None, **limits):
        """
        `config` 是**存进超级块**的那份（容量、avg_record…）：开库必须和
        建库一致，不一致时由引擎报出来。`**limits` 转给语法层
        （`max_result_rows` 那几个上限）。

        `密文` 是**玄武那把锁上取下来的一条密钥流**（`玄武_钥.锁.密文()`），
        传下去给 Arena。**`None` = 明文库 = 今天的样子**，一个字都不差 ——
        所以这一条不是开关，是"有没有钥匙"本身。存储层**不认识它**，
        不 import 玄武、不碰它的方法，只是接着往下递（`Syntax` 也一样）。

        **装配次序是自下而上的，而且只有一条路**：`Syntax` 开 Arena，
        存储层借它。缓冲层由 `Arena` 装配（`_mount_buffer`），这里不再装
        一层——装两层缓冲就是两份脏标记表，落盘次序当场劈叉，而它劈叉
        的样子是"偶尔少几条"，不是报错。

        **Arena 归语法层独占**，这里只留一个私有的 `_arena` 当把手
        （存储区的活要够得着它）。**它不是存储层的东西**，所以不叫
        `self.arena`——那个名字会让人以为这里能开出第二个来。全项目
        能开出 Arena 的地方只有一处，就是上面那行 `Syntax(...)`。
        """
        self.root = root
        self.syntax = syntax_mod.Syntax(root=root, config=config,
                                        密文=密文, **limits)
        self._arena = self.syntax.arena       # 借来的把手，不是本层的

    # ── 生命周期：管的是一整个永久存储区 ────────────────────────────

    def create(self):
        """建一个永久存储区。已存在时由引擎决定报错还是接着用。"""
        self.syntax.create()
        return self

    def open(self):
        """打开已有的永久存储区，把目录和维度映射读回来。"""
        self.syntax.open()
        return self

    def close(self):
        """
        关库。落盘的榫卯次序（记录 → 块 → 区域头）全在这一步收口。

        **还在 `批量 开始` 和 `批量 提交` 之间会拒绝关**——那由语法层
        判，它知道批的边界，存储层不知道，也不该知道。
        """
        self.syntax.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── 增删改查：走语法层（数据调度） ──────────────────────────────

    def execute(self, text, params=(), 身份=None):
        """
        跑一段语句，返回**最后一条**的结果。错了也是 `Result`，带 `.error`。

        `身份` 是"这话是谁说的"，不传就是 `None`（= 谁也不是）。**不传的
        后果是这条语句一律被拒**，不是放行——所以老调用点不用改也不会
        变成越权，只会响亮地失败（见 `syntax._无权文案`）。
        """
        return self.syntax.execute(text, params, 身份=身份)

    def execute_batch(self, statements, 身份=None):
        """
        `[(文本, 参数), …]` 或 `[文本, …]`，一批跑完**只落一次盘**。

        和 `批量 开始 / 批量 提交` 不是一回事：这个是"一批一起提交"，
        那个是"攒着先不落盘"。省的都是落盘次数，不是可见性。

        `身份` 同 `execute`。**整批共用一个身份**——批是"一个人一次发的
        一串语句"，不存在批里一部分是甲、一部分是乙。
        """
        return self.syntax.execute_batch(statements, 身份=身份)

    def 核对身份(self, 用户名, 口令):
        """
        用户名口令核对得过就返回那个 `User`，否则 `None`。

        **登录这条路只有这一个入口**（本层再转发一句）：本地腿的
        `登录` 和帧门的 `login_handler` 都调它。**别在门里自己读表**——
        那是第二条够得着存储的路，而两条路迟早会有一条忘了跟着改。
        """
        return self.syntax.核对身份(用户名, 口令)

    def 有用户(self):
        """
        库里一个用户都没有吗。**起服务前必查**（见 `龙龟之心.起服务`）：
        没有用户的库不能开端口，否则谁先连上谁就是管理员。
        """
        return self.syntax.有用户()

    def 设初始管理员(self, 名字, 口令):
        """
        建库后设第一个管理员。库里已经有用户时**报错**，不覆盖。

        **它是本层唯一一个不叫 `execute` 的写入口，这是有意的**：它不在
        语法层出成语句（见 `Syntax.设初始管理员`——开成语句的话，库刚建好
        的那一小段时间里任何能连上端口的人都能给自己发一个管理员）。
        做成 Python 方法，够得着它的就只有本地这一侧。
        """
        return self.syntax.设初始管理员(名字, 口令)

    # 下面几个是**语句文本的拼装器**，不是另一条执行路径——它们最后都
    # 汇进 `execute`。存在只为一件事：**值一律走 `?` 绑定，绝不拼进
    # 语句文本**。拼进去的第一个后果就是带引号的值被当成语法，那是能
    # 改结果的错，不是能看出来的错。

    def insert(self, table, values, columns=None, dimension=None):
        """
        增。`values` 按位置对应 `columns`；不给 `columns` 就按建表列序。

        `dimension` 是给**引擎维度**用的（区的裁剪边界），不是表里的列。
        一般不写；写了就是把这一行归到那个维度上去，代价见开发报告第四节。
        """
        values = list(values)
        text = insert_text(table, len(values), columns, dimension)
        ps = list(values)
        if dimension is not None:
            ps.append(dimension)
        return self.execute(text, ps)

    def select(self, table, columns='*', where=None, params=(),
               order=None, limit=None):
        """查。`where` / `order` 是**语句片段**（列名和运算符），值走 `params`。"""
        text = '查询 %s 从 %s' % (columns, table)
        if where:
            text += ' 其中 %s' % where
        if order:
            text += ' 排序 按 %s' % order
        if limit is not None:
            text += ' 限制 %d' % limit
        return self.execute(text, params)

    def update(self, table, sets, where=None, params=(), dimension=None):
        """改。`sets` 是 `[(列名, 值), …]`，值一律走 `?` 绑。"""
        assigns, ps = [], []
        for name, value in sets:
            assigns.append('%s = ?' % name)
            ps.append(value)
        if not assigns:
            raise ValueError('一个列都没给，这条 更新 会改出零个字段')
        text = '更新 %s 设 %s' % (table, ', '.join(assigns))
        if where:
            text += ' 其中 %s' % where
            ps.extend(params or ())
        elif params:
            raise ValueError('给了参数却没给 其中 —— 那是要更新整张表，'
                             '请把意图写出来')
        if dimension is not None:
            text += ' 维度 ?'
            ps.append(dimension)
        return self.execute(text, ps)

    def delete(self, table, where=None, params=()):
        """
        删。**不给 `where` 就报错**，不默默删整张表。

        语句层面 `删除 从 t` 不带条件是合法的（那是它的语义），这条限制
        只在这条捷径上——它的名字叫 `delete`，看起来像个单行操作，
        不该顺手把整张表带走。
        """
        if not where:
            raise ValueError('没给 其中 的 删除 会删掉整张表，请写清楚条件')
        return self.execute('删除 从 %s 其中 %s' % (table, where), params)

    # ── 直存直取：转语法层，不再直通引擎 ────────────────────────────
    #
    # 这三条原本是 `self.arena.<同名>`，**绕过了语法层**。绕过的不只是
    # 一层壳：语法层是所有写入的收口，区封掉时它的区间记账要在那里补
    # （`_put` 里那行 `_drain_closed`）。不经过它，账不会报错，只会少记
    # ——之后那些区白扫。
    #
    # **它们留着而不是删掉**，是因为存储层对外还得能直取字节（协议的
    # `0x1x` 那扇门是帧形状的，Python 这边也得有一条）。留着的是**口子**，
    # 收掉的是**旁路**。

    def put(self, key, value=b'', dim=None):
        """直存。键和值都是**不透明字节**——引擎不解释它们。"""
        return self.syntax.put(key, value, dim)

    def get(self, key):
        """直取。取不到回 None（**不是空串**——空串是一个真实的值）。"""
        return self.syntax.get(key)

    def scan(self, cursor=None, limit=256, regions=None):
        """扫一页。`regions` 给了就只扫那几个区——维度裁剪落地的口子。"""
        return self.syntax.scan(cursor, limit, regions)

    # ── 管整个永久存储区 ────────────────────────────────────────────

    def regions(self):
        """
        这个永久存储区里有哪些区、各是什么状态（活/封存、记录数、维度界）。

        **是一份只读快照，不是调度依据。** 拿它决定下一个该写哪儿是错的：
        活跃区随时可能封存换新，等你照着它动手的时候它已经过期了。要写就
        走 `put`，由引擎自己决定。
        """
        return self.syntax.regions()

    def seal(self):
        """
        封存当前活跃区（定格：布隆落盘 → 区域头写全 → 置 SEALED）。

        封存之后这个区**彻底只读、地址永久有效**，而且语法层会开始记它的
        维度区间——**裁剪只对封存区生效**，活跃区的区间每一刻都在变，记
        下来就是一份注定过期的元数据。所以这个方法也是"让裁剪开始有用"
        的那个开关。

        **记区间那件事在语法层做**（它才知道哪张表有行在这个区里），
        所以这里转它，不是转引擎。
        """
        return self.syntax.seal()

    def flush(self, force=True, sync=False):
        """落盘。`force` 为假时只在攒够一批（4MB 或 1 秒）才真写。"""
        return self.syntax.flush(force=force, sync=sync)

    def verify(self):
        """
        巡检一整个永久存储区：区文件在不在、区域头对不对、布隆可不可信、
        榫卯契口有没有断。返回问题列表，**空列表表示健康**。

        它和语法层的 `检查索引` 是两件事：这个查盘上字节的自洽，
        那个查"索引和基表还对不对得上"。
        """
        return self.syntax.verify()

    def stats(self):
        """
        一层看全部。缓冲层的数（命中率、淘汰、脏字节）在 `['buffer']` 里
        ——它对"为什么这条查询变慢了"比任何一条语句都说明问题。
        """
        out = self.syntax.stats()
        out['tables'] = len(self.syntax.tables)
        return out
