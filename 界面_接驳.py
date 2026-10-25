#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
界面_接驳.py — 把 `baxiadb-v1.6` 那套界面接到霸下内核上

**这是三个界面文件底下唯一要重写的一层。** 面板那一层（布局、控件、文案、
颜色、交互）是从 `baxiadb-v1.6/config_gui/ui/` 原样搬的，一个字没动；它们
底下原来撑着的是参照项目**自己的**一套东西，霸下这边没有：

    参照项目                              霸下
    ─────────────────────────────────    ──────────────────────────────
    engine.catalog                        内核.进出（闸口）
      .database_names                     → 盘上认得的库（装配层扫目录推来）
                                            + 开着的那个；**一次只开一个**
      .get_database(名).table_names       → `显示 表列表`（只有开着的库有）
      .table(名).column_names             → `显示 结构 <表>`
    Planner / Optimizer / Executor         → `闸口.fetch` / `闸口.send`
    engine.direct_query                    → 同上
    engine.save_all() / catalog.save()     → 引擎每次写都直接落盘，没有"保存"这一步
    engine.current_database                → 开着的那个库的名字，摆设

库里为什么一次只开一个
======================

参照项目那边 `catalog` 里好几个库是**同时活着**的（一个进程一套自己的
存储）。霸下这边底下是**一个存储区一个 Arena 一个写者**，那个结构不给
"同时开好几个"。所以：

    盘上认得的库      扫目录就有，不打开也不用打开（`目录.设清单`）
    一次开着的库      只有一个，切库 = 整条腿换掉（连工人线程带内核）

这一条不是偷懒，是躲不掉：真要同时开 N 个，就得 N 条工人线程、N 份登录
身份，而每个库的用户表是各管各的（`schema.user_key` 存在文件里）。

所以这一层干两件事：**装一个长得像 `engine` 的壳**（面板不用改读法），和
**一条工人线程**（面板不能直接碰闸口，见下）。

闸口为什么不能直接碰
====================

`Gate` 那条腿不是线程安全的（引擎是单写者），PySide6 又规定控件只能在主线程
碰。两条合起来只剩一种摆法：

    界面线程                            工人线程（唯一碰闸口的）
      控件 / 重绘 / 事件    ──投活──▶    闸口.fetch / send / fill / page
           ▲                                    │
           └──────── 信号（Qt 自动排队）◀───────┘

闸口那个引用**只存在 `工人` 对象里**，界面拿不到——这是形状挡的，不是纪律。

目录是"同步读缓存、异步刷新"
============================

参照项目的面板是**同步**读目录的（`self.engine.catalog.database_names` 当场
要一个值）。闸口那边是异步的。两边要凑上只剩一条路：**目录读的是一份缓存**，
刷新由工人去做，刷完了换掉整份缓存。面板的读法一个字不用改。

**查询那一条不能这么办**——`计数(*)` 是全表扫，同步等就是把窗口冻住。所以
取数一律是「投一件活 + 给一个回话」，回话在**主线程**里被叫。
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QThread, Signal, Slot

import schema as _schema


class 干等超时(Exception):
    """
    `工位.干等` 等到超时了，活还没跑完。

    ⛔ **超时不等于那件活没在跑。** 工人线程是**一条**，超时那一刻它还压在
    那件活上；这时候调用方要是接着去动那条闸口（换库、关库、扔闸口），
    就是在跟一个正在读写的 Arena 抢——`.py` 里另外几处"必须先停干净"的
    规矩（`工位.停` 那段铁律）说的就是这件事。

    所以拿到这个错**只能往下报**，不能当"没发生过"。
    """


# ══ 一、工人线程：唯一碰闸口的地方 ═════════════════════════════════════


class 工人(QObject):
    """
    活在工人线程里，**独占闸口**。

    一件"活"就是一个收闸口做参数的函数（`def 活(闸口): ...`）。界面把活投
    过来，工人拿闸口跑它，把值或错误发回去。

    **闸口那个引用只存在这个对象里。** 界面拿不到它，所以它想在界面线程上
    调也调不着——这是形状，不是纪律。
    """

    完成 = Signal(str, object)     # (活号, 值)
    出错 = Signal(str, str)        # (活号, 错误文本)

    def __init__(self):
        super().__init__()
        self._闸口 = None
        self._号 = 0
        # 这条工人线程的 ident。**开线程之后由 `记线程` 在那个线程里填**——
        # 在主线程上问不到，`QThread` 的 ident 要等它真跑起来才存在。
        # 用途只有一个：让"这个方法只能在工人线程上调"能被**当场发现**，
        # 而不是靠写注释提醒（见 `工位.会话闸口`）。
        self.线程号 = None

    def 接上(self, 闸口):
        self._闸口 = 闸口

    @Slot()
    def 记线程(self):
        self.线程号 = threading.get_ident()

    @Slot(object, object)
    def 干活(self, 号, 活):
        try:
            值 = 活(self._闸口)
        except Exception as exc:       # 什么错都要回话，不能把工人打死
            self.出错.emit(号, '%s：%s' % (type(exc).__name__, exc))
            return
        self.完成.emit(号, 值)


class 工位(QObject):
    """
    界面对着的那一头。**界面只会说"去干这个活"，它拿不到闸口。**

        self._工位.干(lambda 闸口: 闸口.fetch('显示 表列表'), self._收表列表)

    `回话` 在主线程里被叫，签名叫 `回话(值, 错)`——两个里总有一个是 `None`。
    **投活是非阻塞的**，界面投完就回去重绘了。

    MySQL 兼容层要的那几样（`干等` / `造闸口` / 会话表）
    --------------------------------------------------

    适配器跑在**另外几条 socket 线程**上（`mysql_门`），它跟面板不是一类
    调用方：面板在界面线程上、投完就回去重绘；socket 线程要的是**"这条
    语句执行完了没有"**，它得当场拿到结果才能往下编应答。所以多一个
    **阻塞**的入口 `干等`。

    **但闸口还是只许在工人线程上碰。** 所以 socket 线程手上拿的不是闸口，
    是一个**连接号**；闸口存在下面 `_会话` 那张表里，**只在工人线程上读写**
    （见 `开会话` 那一段）。
    """

    派活 = Signal(str, object)

    def __init__(self, 闸口, 形态='本地', 父=None, 造闸口=None):
        super().__init__(父)
        self.形态 = 形态
        self._等 = {}
        # 造会话闸口的法子。**由装配层给**，因为它才知道这条腿是哪条腿：
        #   本地腿  lambda: io_layer.Gate.attach(内核.存储层)
        #   远程腿  lambda: io_layer.Gate.connect(主机, 端口)
        # **只有装配层拿得到 `内核.存储层`**，这一层拿不到也不该拿到。
        self._造闸口 = 造闸口
        # {连接号: 闸口}——**只在工人线程上读写**（见 `开会话`）。
        self._会话 = {}
        # 投活那个计数器现在是**几条 socket 线程 + 界面线程**一起用的，
        # 所以得上锁。撞号的后果不是"慢"，是**甲的回话被叫给了乙**
        # （`_收完成` 是按号 pop 的）：甲拿到乙的结果，而乙永远等不到。
        self._锁 = threading.Lock()
        # `工位` 是在界面线程上造的（面板就是这么造的）。记下来给 `干等`
        # 当闸门用，见那一段。
        self._主线程号 = threading.get_ident()
        self._线程 = QThread()
        self._工人 = 工人()
        self._工人.moveToThread(self._线程)
        self._工人.接上(闸口)
        self.派活.connect(self._工人.干活)
        self._工人.完成.connect(self._收完成)
        self._工人.出错.connect(self._收出错)
        # `started` 是在**新线程里**发的，正好用来记那个线程的 ident
        self._线程.started.connect(self._工人.记线程)
        self._线程.start()

    def _取号(self):
        with self._锁:
            self._工人._号 += 1
            return str(self._工人._号)

    def 干(self, 活, 回话):
        """投一件活。**非阻塞**——回话在主线程里被叫。"""
        号 = self._取号()
        self._等[号] = 回话
        self.派活.emit(号, 活)

    def _收完成(self, 号, 值):
        回话 = self._等.pop(号, None)
        if 回话 is not None:
            回话(值, None)

    def _收出错(self, 号, 错):
        回话 = self._等.pop(号, None)
        if 回话 is not None:
            回话(None, 错)

    # ── 干等：给 socket 线程用的阻塞入口 ────────────────────────────

    def 在工人线程上(self):
        """这会儿是不是在工人线程上跑。给下面几处"只许在这儿调"的把关。"""
        return (self._工人.线程号 is not None
                and threading.get_ident() == self._工人.线程号)

    def 干等(self, 活, 超时=60.0, 会话号=None):
        """
        **在这个调用线程上阻塞**，等这件活跑完，把值直接给回来。

        这是给**非 Qt 的调用方**（`mysql_门` 那几条 socket 线程）用的。
        `干` 那边是"投完就回去重绘"，socket 线程没有事件循环可以回去，
        它必须当场拿到结果才能往下编 MySQL 的应答。

        `会话号` 给了就用**那个连接自己的闸口**跑（见 `开会话`）；不给就用
        界面那条闸口。**闸口始终没离开工人线程**——`活` 是在工人线程上被
        调用的，会话号到闸口的换算也在那儿做。

        ⛔ **不许在界面线程上调。** 它会把窗口整个冻住，冻多久取决于活有多
        重（一条 `计数(*)` 是全表扫）——那种卡死看起来像程序坏了，而真因
        只是这一句写错了地方。所以这里当场拦，见 `_主线程号`。

        ⛔ **超时了就往下报，别接着动这条闸口**——理由写在 `干等超时` 那段。

        实现上**不登记回话**：活外面包一层"把值写进匣子"的壳，`派活` 出去
        之后在这个线程上等 `threading.Event`。所以不新增任何信号、也不动
        `_收完成`——工人照常发完成信号，`_等` 里查不到号，空跑一次就完了。
        """
        if threading.get_ident() == self._主线程号:
            raise RuntimeError(
                '干等 不许在界面线程上调：它会把窗口冻住，而冻多久取决于'
                '活有多重。界面要用非阻塞的 `干`。')
        return self._等活(活, 超时, 会话号)

    def _等活(self, 活, 超时, 会话号=None):
        """
        `干等` 的本体，**不带"你在哪个线程"那道闸**。

        分出来只有一个理由：`停` 要在界面线程上把会话闸口收干净，那一次
        阻塞是有界的、也是必须的，不能反过来被 `干等` 那道闸拦住。
        """
        匣 = {'值': None, '错': None, '好了': threading.Event()}

        def 包(_闸口):
            # 这一段跑在**工人线程**上。会话号到闸口的换算也在这儿做，
            # 所以 `_会话` 从头到尾没被别的线程碰过。
            try:
                if 会话号 is None:
                    匣['值'] = 活(_闸口)
                else:
                    匣['值'] = 活(self._会话[会话号])
            except Exception as exc:   # 什么错都要写进匣子，不能让等着的那头干等
                匣['错'] = '%s：%s' % (type(exc).__name__, exc)
            finally:
                匣['好了'].set()
            return None                # 不往回带值，值走匣子

        号 = self._取号()
        self.派活.emit(号, 包)
        if not 匣['好了'].wait(超时):
            raise 干等超时('等了 %s 秒还没干完' % 超时)
        if 匣['错'] is not None:
            raise RuntimeError(匣['错'])
        return 匣['值']

    # ── 会话表：每个 MySQL 连接一条自己的闸口 ───────────────────────
    #
    # 为什么一个连接一条闸口，而不是共用界面那条
    # ------------------------------------------
    #
    # `Gate._身份` 是**连接级**的（`io_layer.py:641-645` 那段：「挂到进程上
    # 就成了"谁最后登录谁说了算"，多连接就串号」）。共用一条闸口，爬虫一连
    # 上来就把界面用户的身份顶掉了，而界面上**看不出任何异常**——之后每一条
    # 语句都是拿爬虫的身份去判权限的。
    #
    # 所以每个 MySQL 连接自己一条闸口、自己登录自己的霸下账号。`Gate.attach`
    # 只是把同一份 `Storage` 接上闸口（不碰盘、不开库），一个 Arena 上挂
    # 多少条闸口都行；而它们**全部排在这一条工人线程上跑**，所以
    # `Syntax._身份` 那根游标（`syntax.py:1353-1371` 说的"一次只有一个
    # 调用者"）前提照样成立。

    def 开会话(self, 连接号, 登录=None, 超时=30.0, 地址=None):
        """
        给这个连接号造一条**新闸口**，顺手登录。返回登录到的用户名。

        `登录` 是个收闸口做参数的函数，在**工人线程上**被叫，返回用户名；
        返回 `None` 表示没登进去（口令不对之类），这时这条闸口当场丢掉、
        连接号不登记。**闸口一个字都不出这一层。**

        为什么登录也在这儿做、而不是把闸口交出去让外面登录：交出去就等于
        "闸口那个引用只存在工人对象里"那条形状破了（见文件头）。

        ⚠ **`地址` 这个工位自己一个字不用**，它只是**路过**：玄武那层要
        按来源判（黑名单、撞库拉黑），而来源只有 `mysql_门` 知道。关键字
        有默认值，所以别的调用方（和鸭子类型的别的工位）照旧。
        """
        if self._造闸口 is None:
            raise RuntimeError(
                '这个工位没给"造闸口"，开会话开不了——'
                '装配层得把 `工位(..., 造闸口=...)` 填上')

        def 活(_):
            闸 = self._造闸口()
            谁 = None
            if 登录 is not None:
                谁 = 登录(闸)
                if 谁 is None:
                    _丢会话闸口(闸)
                    return None
            self._会话[连接号] = 闸
            return 谁

        return self._等活(活, 超时)

    def 关会话(self, 连接号, 超时=30.0):
        """把这个连接号那条闸口收掉。**没登记过就什么也不做。**"""
        def 活(_):
            _丢会话闸口(self._会话.pop(连接号, None))

        return self._等活(活, 超时)

    def 会话闸口(self, 连接号):
        """
        这个连接号那条闸口。**没有就 `KeyError`。**

        ⛔ **只能在工人线程上调。** 这里回的是**闸口本身**，一旦它跑到了
        别的线程上，文件头那条"闸口只存在工人对象里"就没了，之后谁在这条
        闸口上乱来都查不出来。所以当场把关，不当注释提醒。
        """
        if not self.在工人线程上():
            raise RuntimeError(
                '会话闸口 只能在工人线程上调：它回的是闸口本身，'
                '出了工人线程那条"闸口只存在工人对象里"的形状就破了')
        return self._会话[连接号]

    def 会话数(self):
        """这会儿登记着几个连接。给状态栏用。**只在工人线程上准。**"""
        return len(self._会话)

    def _关掉所有会话(self):
        """**只在工人线程上跑**：把所有会话闸口收掉。`停` 用。"""
        for 号 in list(self._会话):
            _丢会话闸口(self._会话.pop(号, None))

    def 停(self):
        """
        停工人线程。**返回 `wait` 的结果**——`True` 才是真停了。

        这个返回值不能扔。`quit()` 只是让事件循环在**当前这件活跑完之后**
        退出；工人手上正压着一条全表扫的时候三秒根本不够，`wait` 会超时
        返回 `False`，而**线程还在跑、闸口还被它捏着**。

        退出程序那条路上扔了没事（进程马上没了）；**切库那条路上扔了要
        出事**：接着就是关旧库，那等于关一个正被工人读写的 Arena。

        所以切库那一头必须先看这个返回值，`False` 就别往下走。

        `_等` 里那些还没回话的**一并清掉**。清了就永远不会被叫——这是
        故意的：线程都停了，再叫出来只会让界面拿一个已经不存在的结果去
        重绘，而等着的那些面板本来就要跟着这次切库一起没了。没停干净
        就**不清**，让还活着的那条线程把话说完。

        会话闸口在 `quit()` **之前**收：收它们要在工人线程上做，线程一停
        就没地方做了，而那几条闸口是挂在**这个库**上的——切库那条路上库
        马上就要关，留着它们就是留着一批指着死库的闸口。
        这一步的阻塞是有界的（就是扔几条闸口），所以用 `_等活` 绕过
        `干等` 的界面线程那道闸；万一超时也不拦着停，照旧往下走——
        `wait` 的返回值才是这一节的主判据，它说什么就是什么。
        """
        try:
            self._等活(lambda _: self._关掉所有会话(), 3.0)
        except Exception:
            pass                       # 收不干净也让线程停，别把停卡在这儿
        self._线程.quit()
        停了 = self._线程.wait(3000)
        if 停了:
            self._等.clear()
        return 停了


def _丢会话闸口(闸):
    """
    放掉一条**会话**闸口。

    ⛔ **本地腿来的闸口只能扔，不能关。** `_LocalLeg.close()` 是
    `self.storage.close()`（`io_layer.py:568-569`）——那是把**整个库**关掉。
    所以一次 MySQL 客户端断开就会把用户正开着的库关掉，而界面上只会看到
    "库莫名其妙不好使了"。

    这条规矩不是新立的，`龙龟之心` 里已经踩过两回、都写着了：
    `龙龟之心.py:221` 和 `:278` 那两段「**扔，不是关**」。

    反过来**远程腿必须关**：那条闸口底下是一条真 socket（`Gate.connect`），
    扔了不关就是漏一条连接，爬虫反复重连能漏一堆。

    判据是 `闸口.leg`（'本地' / '远程'）。**会话闸口只可能是这两种来路**
    ——要么 `Gate.attach`（借），要么 `Gate.connect`（开）——所以这里够用。
    注意别拿它去判一条 `Gate.open/create` 出来的闸口：那种"本地"是**自己
    的**库，该关。
    """
    if 闸 is None:
        return
    if 闸.leg == '本地':
        return                          # 借来的库，扔；关它就是把库关了
    闸.close()


# ══ 二、霸下的类型号 ═══════════════════════════════════════════════════
#
# 从 `schema.TYPE_NAMES` 借来，**不抄一份**：类型名只有一处权威。


def 类型名(号):
    return _schema.TYPE_NAMES.get(号, '?')


# ══ 三、模型：长得像参照项目那套目录模型 ═══════════════════════════════
#
# 参照项目的面板读的就是这些属性（`col.default_value()`、`tbl.primary_key`、
# `db.table_names` …）。**名字一个都不能改**，改了面板就得跟着动，那就不是
# "搬过来"了。
#
# 有几样霸下根本没有，这里给的是**诚实的空值**，不是假装有：
#   自增 / 唯一索引 / 字符集 / 排序规则 —— 霸下不做这几样。
# 界面上那几格会因此一直是空的或灰的，这是真话，不是坏了。


class 列(object):
    """一列。字段名照参照项目的 `Column` 模型来。"""

    __slots__ = ('name', 'sql_type', 'type_code', 'nullable', 'primary_key',
                 'auto_increment', 'unique', 'comment', 'ordinal', '_默认')

    def __init__(self, name='', sql_type='文本', type_code=0, nullable=True,
                 primary_key=False, comment='', ordinal=0, 默认=None):
        self.name = name
        self.sql_type = sql_type
        self.type_code = type_code
        self.nullable = nullable
        self.primary_key = primary_key
        self.comment = comment
        self.ordinal = ordinal
        self._默认 = 默认
        # 霸下不做这三样。**是"没有"，不是"关着"**——见文件头。
        self.auto_increment = False
        self.unique = False

    def default_value(self):
        return self._默认

    def __repr__(self):
        return '<列 %s %s>' % (self.name, self.sql_type)


class 表(object):
    """一张表。字段名照参照项目的 `Table` 模型来。"""

    __slots__ = ('name', 'comment', 'columns', 'primary_key', 'dim_column',
                 'table_id', 'engine')

    def __init__(self, name='', comment='', columns=None, primary_key=None,
                 dim_column=None, table_id=0):
        self.name = name
        self.comment = comment
        self.columns = list(columns or [])
        self.primary_key = list(primary_key or [])
        self.dim_column = dim_column
        self.table_id = table_id
        self.engine = 'baxia'          # 只有这一种引擎，摆着好看

    @property
    def column_names(self):
        return [c.name for c in self.columns]

    @property
    def column_count(self):
        return len(self.columns)

    def column(self, name):
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)

    def has_column(self, name):
        return any(c.name == name for c in self.columns)

    def __repr__(self):
        return '<表 %s %d 列>' % (self.name, len(self.columns))


class 库(object):
    """
    一个库。

    **霸下这边库不是"库名"，是盘上一块永久存储区**（一个根目录，里面
    `catalog` + `r/`）。所以一个 `库` 有两个身份：`name` 是给人看的名字，
    `路` 是它在盘上的位置——**切库靠的是后者**，名字只用来显示。

    **同一时刻只有一个库是开着的**（一个存储区一个 Arena 一个写者）。所以
    这份模型里分两种：

        开着的那个       `_表` 里是真表，`未打开` 是 False
        盘上认得的其余   `_表` 是空的，`未打开` 是 True

    `未打开` **不是"这个库是空的"**，是"这次没打开它，里面的表不知道"。
    界面上得把这两件事分开说——写"0 张表"就是在撒谎。
    """

    __slots__ = ('name', 'comment', 'charset', 'collation', '_表',
                 '路', '未打开')

    def __init__(self, name='', comment='', 路=None, 未打开=False):
        self.name = name
        self.comment = comment
        self.路 = 路
        self.未打开 = 未打开
        # 霸下没有字符集/排序规则可言（存什么编什么），给空。
        self.charset = ''
        self.collation = ''
        self._表 = {}

    # 参照项目的 `Database` 上这些东西是属性；这里也是。

    @property
    def tables(self):
        return list(self._表.values())

    @property
    def table_names(self):
        return [t.name for t in self._表.values()]

    @property
    def table_count(self):
        return len(self._表)

    def table(self, name):
        return self._表[name]          # 没有就 KeyError，和参照项目一样

    def has_table(self, name):
        return name in self._表

    def add_table(self, tbl):
        self._表[tbl.name] = tbl

    def drop_table(self, name):
        self._表.pop(name, None)

    def __repr__(self):
        return '<库 %s %d 张表>' % (self.name, self.table_count)


# ══ 四、目录：同步读缓存、异步刷新 ═════════════════════════════════════


class 目录(object):
    """
    面板眼里的 `engine.catalog`。

    **读的全是内存里这份缓存**，所以面板怎么同步读都不卡。刷新由 `刷新()`
    投给工人去做，刷完了一整份换掉。

    装配靠两条语句：`显示 表列表`（表名/表号/列数/主键/维度列）拿全部表的名字
    和主键，再对每张表 `显示 结构 <表>` 拿列。**后者一批发出去**（`fetch_many`），
    一个来回，不是 N 个来回。

    盘上有好几个库，这份缓存里有几个？
    ----------------------------------

    **只有开着的那个库的表清单是真拉回来的**，其余库是 `未打开` 的壳
    （见 `库` 那段）。原因是躲不掉的那条：一个永久存储区同一时刻只能有
    一个 Arena，所以"同时开着的库"永远只有零个或一个——其余的看着在，
    实际都得先切过去。

    所以这个类里有**两份东西**，来源不同：

        `_库`     开着的那个，表由 `刷新()` 从闸口拉（`_装` 整份换掉）
        `_清单`   盘上认得的库名和路径，由装配层扫目录推过来（`设清单`）

    **它自己不碰文件系统。** 谁去数目录、什么时候数，是装配层的事；
    这里只把那份清单翻成面板要读的形状。两件事混在一起写，就成了
    "面板读目录"和"界面去扫盘"两条路，而它们迟早有一条忘了改。
    """

    def __init__(self, 引擎, 库目录=None):
        self._引擎 = 引擎
        self._库目录 = 库目录
        self._库 = 库(引擎.库名, 路=getattr(引擎, '库文件路径', None))
        self._在刷 = False
        # **盘上认得的库**：`[(名, 路), …]`。由装配层扫好了推过来
        # （`设清单`），**这一层不碰文件系统**——它只管把这份清单翻成
        # 面板要读的形状。谁去数目录、什么时候数，是装配层的事。
        self._清单 = []

    # ── 盘上有哪些库 ────────────────────────────────────────────────

    def 装配(self, 库目录=None, 开着的路=None):
        """
        装配层在把 `库文件路径` 挂上 `引擎壳` **之后**叫一次。

        为什么不能摆在 `__init__` 里：`引擎壳.库文件路径` 是 `开库` 在构造
        完引擎壳之后才挂上去的（那样排是为了不给 `界面_接驳.引擎` 的构造
        签名加霸下自己的东西）。所以构造那一刻这些值还没有，只能后补。
        """
        self._库目录 = 库目录
        self._库.路 = 开着的路

    def 设清单(self, 清单):
        """装配层扫完库目录，把 `[(名, 路), …]` 推过来。"""
        self._清单 = list(清单 or [])

    @property
    def 库目录(self):
        return self._库目录

    @property
    def 开着的路(self):
        """这次开着的那个库在盘上的位置。切库靠它认人。"""
        return self._库.路

    # ── 参照项目那套读法 ────────────────────────────────────────────

    @property
    def databases(self):
        """
        盘上认得的每一个库，**当前开着的那个排在最前**。

        开着的那个带着真表；其余的是 `未打开` 的壳，只有名字和路径
        ——它们的表得先切过去才看得见（一个存储区同一时刻只能有一个
        Arena，这是躲不掉的）。**不假装它们有 0 张表**，见 `库` 那段。
        """
        出 = [self._库]
        for 名, 路 in self._清单:
            if 路 == self._库.路:
                continue                   # 就是开着的那个，已经排头了
            出.append(库(名, 路=路, 未打开=True))
        return 出

    @property
    def database_names(self):
        return [d.name for d in self.databases]

    def get_database(self, name):
        for d in self.databases:
            if d.name == name:
                return d
        raise KeyError('没有这个库：%s' % name)

    def has_table(self, db_name, table_name):
        """**只有开着那个库答得了这句。** 别的库没打开，表清单不知道，
        所以一律回 False——不是"它没有这张表"，是"这次没看"。要真问
        别的库，先切过去。"""
        if db_name and db_name != self._库.name:
            return False
        return self._库.has_table(table_name)

    def save(self):
        """参照项目里这是"把目录写回盘"。**霸下没有这一步**——每条语句
        下去就是直存直取，写完就在盘上了。留着是为了面板不用改。"""
        return True

    # ── 刷新 ────────────────────────────────────────────────────────

    def 刷新(self, 回话=None):
        """投给工人去拉一遍目录。**非阻塞。** 回话(错) 在主线程里被叫。"""
        if self._在刷:
            if 回话 is not None:
                回话(None)
            return
        self._在刷 = True

        def 活(闸口):
            首 = 闸口.fetch('显示 表列表')
            if not 首.ok:
                raise RuntimeError(首.error)
            表们 = [{'名': r[0], '号': int(r[1]), '列数': int(r[2]),
                     '主键': r[3], '维度列': r[4]} for r in 首.as_lists()]
            if 表们:
                结构 = 闸口.fetch_many(
                    ['显示 结构 `%s`' % t['名'] for t in 表们])
                错 = [r.error for r in 结构 if not r.ok]
                if 错:
                    raise RuntimeError(错[0])
            else:
                结构 = []
            return 表们, [r.as_lists() for r in 结构]

        self._引擎.投(活, lambda 值, 错: self._装(值, 错, 回话))

    def _装(self, 值, 错, 回话):
        self._在刷 = False
        if 错 is not None:
            if 回话 is not None:
                回话(错)
            return
        表们, 结构们 = 值
        # `路` 得跟着走——切库、删库、认库都靠它。整份换掉的时候漏了它，
        # 树上那个库就成了一个"不知道自己在盘上哪儿"的孤儿。
        新 = 库(self._库.name, 路=self._库.路)
        for t, 行表 in zip(表们, 结构们):
            列们 = [列(name=行[1], sql_type=行[2], type_code=_类型号(行[2]),
                      nullable=(行[3] == '1'), primary_key=(行[5] == '1'),
                      ordinal=int(行[0]),
                      默认=None if 行[4] is None else 行[4])
                   for 行 in 行表]
            for c in 列们:
                c.comment = ''
            # 主键那列 `显示 表列表` 给的是 '/' 连起来的一个串
            主键 = [n for n in (t['主键'] or '').split('/') if n]
            新.add_table(表(name=t['名'], columns=列们, primary_key=主键,
                          dim_column=t['维度列'], table_id=t['号']))
        # **整份换掉**，不是就地改：面板手里可能正拿着一份旧的在遍历。
        self._库 = 新
        if 回话 is not None:
            回话(None)


def _类型号(名):
    号 = _schema.TYPE_ALIASES.get(名)
    return 0 if 号 is None else 号


# ══ 五、引擎：面板眼里的后端 ═══════════════════════════════════════════


class 引擎(object):
    """
    面板眼里的 `engine`。

    参照项目那些方法在这里**全部换成投活**：`取` / `发` 是非阻塞的，回话在
    主线程里被叫。

        self._引擎.取('查询 * 从 `网页`', self._收结果)

    **`save_all` 是空壳**：霸下每条语句写完就落盘了，没有"攒着再存"这回事。
    留着是为了 `data_viewer` 那个「💾 保存数据库」按钮不报错——但按钮上那句
    话在霸下这边是假的，以后要么改文案要么去掉，别当它是真的。
    """

    def __init__(self, 工位, 库名, 形态='本地', 当前用户=''):
        self._工位 = 工位
        self.库名 = 库名
        self.形态 = 形态
        self.catalog = 目录(self)
        self._当前库 = 库名
        self.is_running = True
        # 这条连接**是谁的**（`霸下之躯.开库` 登录完填进来）。**只是给人看
        # 的**——权限不在这儿判，也不该在这儿判：闸口那头服务端从会话里
        # 取身份（见 `io_layer.login_handler`）。这里写什么都改不了任何
        # 一条语句的成败，用户面板把它显示出来而已。
        self.当前用户 = 当前用户

    # ── 参照项目那几个属性 ──────────────────────────────────────────

    @property
    def current_database(self):
        return self._当前库

    @current_database.setter
    def current_database(self, 名):
        # 赋值当摆设收下，免得面板那句"存旧的、改新的、完事改回来"报错。
        #
        # **切库不走这条路。** 切库是**整个引擎壳换掉**（连同工人线程和
        # 内核），不是把这个字段改个名——`_当前库` 只是给人看的（状态栏
        # 那个"数据库: X"），它背后那块永久存储区一个字都不会跟着变。
        self._当前库 = 名 or self.库名

    def save_all(self):
        return {'errors': []}

    def direct_query(self, 语句, 参数=()):
        raise NotImplementedError(
            'direct_query 是同步的，霸下这边取数一律走 引擎.取(语句, 回话)')

    # ── 投活 ────────────────────────────────────────────────────────

    def 投(self, 活, 回话):
        self._工位.干(活, 回话)

    # 下面四条：`回话(结果, 错)` 里那个"错"是**活本身炸了**（连不上、参数
    # 个数不对这类）。**语句报错不算**——语句错了闸口也回一个 `Result`，
    # 只是 `.error` 非空，由面板自己看（它们本来就这么写的）。

    def 取(self, 语句, 回话, 参数=()):
        """查。回话(Result, 错)，在主线程里被叫。"""
        self._工位.干(lambda 闸口: 闸口.fetch(语句, 参数), 回话)

    def 取多(self, 语句们, 回话):
        self._工位.干(lambda 闸口: 闸口.fetch_many(语句们), 回话)

    def 发(self, 语句, 回话, 参数=()):
        """改。回话(Result, 错)，在主线程里被叫。"""
        self._工位.干(lambda 闸口: 闸口.send(语句, 参数), 回话)

    def 发多(self, 语句们, 回话):
        self._工位.干(lambda 闸口: 闸口.send_many(语句们), 回话)
