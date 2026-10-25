# -*- coding: utf-8 -*-
"""
mysql_门.py — MySQL 兼容层的门：监听、每连接一个状态机、把活投给工位。

这一层**自己不编一个字节、不认一句 SQL、也不拿一个闸口引用**。它只做四件：

    听 127.0.0.1:3306（或配置里那个口）
    每进来一条连接，起一条 socket 线程，先握手、再验身份
    收一条命令 → 交给 `mysql_翻译` 认出要干什么 → **投给工位** → 编应答
    攒批：把逐行 INSERT 攒成一批一次落盘

形状
====

    爬虫（pymysql）──socket──▶ 监听线程
                                  │ accept
                                  ▼
                             连接线程（每条连接一条）
                               │ 翻译（纯 CPU，就地做）
                               │ 攒批（就地攒）
                               └── 工位.干等(活) ──▶ 工人线程（唯一碰 Storage 的）
                                                     闸口 = 这条连接自己的那个

**连接线程从始至终不碰闸口。** 它只说"我是一号连接"，`工位` 那边拿
会话号换闸口——`io_layer.py:641-645` 那条"闸口那个引用只存在工人对象里"
原样保住。所有闸口调用都排在**同一条工人线程**上，所以 `Syntax._身份`
那根游标「一次只有一个调用者」（`syntax.py:1353-1371`）的前提照样成立。

三个决定，理由都在这儿
======================

**一、命令期的 socket 是阻塞的，没有读超时。**

`报文流._收够` 里那个 `buf` 是**局部的**：一次 `recv` 等超时就抛，局部
buffer 跟着丢掉——**已经读到一半的字节就没了**。于是"超时之后接着读"
会把整条流读错位，而错位的表现是后面每一条命令都"包序号不对"。
要么把分片缓冲改成跨超时保留（动字节层），要么**根本不给它超时的机会**。

选后者：命令期 `settimeout(None)`，`recv` 一直等到有字节为止。对端真死
了（没发 FIN）靠 `SO_KEEPALIVE` 兜——内核会在一段时间后把连接判死，
`recv` 抛 `OSError`。**这样一改，那个半包隐患从根上不存在了。**

**二、攒批的到点落盘靠一条计时线程，不靠 socket 超时。**

理由同上（socket 不能有超时）。一条 `门` 一条计时线程，每 50ms 扫一遍
所有连接，谁的批到点了就替它落。落盘本身是 `工位.干等`，所以它排在
工人线程上，和其它活串行。

**三、读之前先落盘——本连接 + 别的连接。**

```python
读之前：把攒着的写全落下去，再读
```

只落自己的那条不够：爬虫写、界面看，是两条不同的路——库里的数据得
先真的在盘上（其实是先真的进了引擎），界面才看得见。这条不成立的话，
症状是"爬虫说插进去了、界面上没有"，而**过一会儿它又自己出现了**
（到点落盘了），于是没人当它是 bug。

⚠️ 有意的语义偏离，写在明处
============================

**COMMIT 之前客户端就看到了 `affected=1` / `last_insert_id`，而那些写
还没进引擎。** 到 COMMIT（或到点、或到读）才真落，落的时候才可能发现
错（主键撞了之类），那时才回 ERR。这和 MySQL 的时序不同——MySQL 是在
`INSERT` 那句上就能报错的。

换来的是**攒批**：霸下每条语句都真写盘（`syntax.py:1331` 结尾那句
`self._flush(force=True)`），逐行插就是逐行落盘，攒批省的正是这个。

**批里没有回滚**（`syntax.py:1280`）——成了的就是成了。失败的那批要
报清楚"共 N 条、失败 M 条、成了的没退"，不能只回一句"失败了"。

**断线时落盘，不回滚。** 爬虫三处插入后面都紧跟 `COMMIT`
（`config.py:4941` 那段），所以"插了没提交就断线"在真实调用里不存在；
反过来丢数据代价大得多。
"""

from __future__ import annotations

import socket
import threading
import time

import mysql_配置 as _配置
import mysql_协议 as _协议
import mysql_翻译 as _翻译

__all__ = ['门', '门错', '一条线']

#: 命令号（`connections.py` 那边的常量对过来）。
COM_QUIT = 0x01
COM_INIT_DB = 0x02
COM_QUERY = 0x03
COM_PING = 0x0E
COM_STMT_PREPARE = 0x16        # 下面四个是预处理语句那一族
COM_STMT_EXECUTE = 0x17
COM_STMT_SEND_LONG_DATA = 0x18
COM_STMT_CLOSE = 0x19
COM_STMT_RESET = 0x1A
COM_STMT_FETCH = 0x1C

_STMT族 = (COM_STMT_PREPARE, COM_STMT_EXECUTE, COM_STMT_SEND_LONG_DATA,
           COM_STMT_CLOSE, COM_STMT_RESET, COM_STMT_FETCH)

#: 握手期给多久。**只有这一段有超时**——对面连上来一句话不说，不能
#: 把一条线程永远占着。
握手超时 = 10.0

#: 单条命令给多久。这是**活跑完**的时间，不是网络时间——一条
#: `计数(*)` 是全表扫，爬虫灌到百万行之后会慢，但不该慢到分钟级。
命令超时 = 300.0

#: 收尾时（断开 / 关程序）给落盘多久。**比 `命令超时` 短得多**——
#: 关程序的时候不能因为工人线程正忙一条全表扫就卡满五分钟。
#: 落不下去的那批会留在队里、并记一条日志，**不是默默没了**。
收尾超时 = 5.0

#: 计时线程多久扫一遍。
计时步长 = 0.05

#: 开服时 listen 的排队长度。
_积压 = 64

#: 连接号从几开始。1 起——**0 留给"没有连接"**。
_首个连接号 = 1

#: `SO_KEEPALIVE` 那几条。Windows 上要 `SIO_KEEPALIVE_VALS` 才动得了
#: 间隔，跨平台不了，所以只打开开关、用系统默认（两小时）。
#: **这不是"及时发现问题"，是"最终不会永远吊着"。**
_SIO_KEEPALIVE_VALS = getattr(socket, 'SIO_KEEPALIVE_VALS', None)


class 门错(Exception):
    """门自己起的错（端口占着、地址写错了之类）。**给装配层catch。**"""


# ══ 一、一条连接自己的那点状态 ════════════════════════════════════════

class 一条线(object):
    """
    一条 MySQL 连接的**本地**状态。只在它自己的 socket 线程上读写，
    `锁` 护的是攒批那一堆（计时线程会伸手来拿）。
    """

    __slots__ = ('号', '会话', '流', '地址', '身份', '活的', '线程',
                 '待落', '待落字节', '首条时刻', '锁')

    def __init__(self, 号, 会话, 流, 地址):
        self.号 = 号
        self.会话 = 会话            # mysql_翻译.会话
        self.流 = 流                # mysql_协议.报文流
        self.地址 = 地址            # 对端 IP（白名单比的是这个）
        self.身份 = None            # 登录到的霸下账号名
        self.活的 = True

        # ── 攒批 ────────────────────────────────────────────────────
        self.待落 = []              # [(霸下语句, 参数), …]
        self.待落字节 = 0
        self.首条时刻 = None        # 这一批的第一条是什么时候进来的
        self.锁 = threading.Lock()

        self.线程 = None

    # ── 攒批（`锁` 护着，计时线程和连接线程都会碰）────────────────

    def 收进批(self, 批):
        """把一批语句攒着。回**攒完之后一共有多少条**。"""
        字节 = sum(len(s) + sum(_参数大小(p) for p in 参 or ())
                   for s, 参 in 批)
        with self.锁:
            if not self.待落:
                self.首条时刻 = time.monotonic()
            self.待落.extend(批)
            self.待落字节 += 字节
            return len(self.待落)

    def 取走批(self):
        """**拿走并清空**当前攒着的。回 `[(语句, 参数), …]`。"""
        with self.锁:
            批 = self.待落
            self.待落 = []
            self.待落字节 = 0
            self.首条时刻 = None
            return 批

    def 退回批(self, 批):
        """
        **落盘没成交，把这批放回去。** 头插。

        ⛔ **这一条不写就有静默丢数据。** `取走批` 是"拿走并清空"，所以
        落盘那一步只要抛（`干等超时` 就会抛——工人线程正忙别的），那批
        语句就既没进引擎、也不在队里了。**而客户端早就收到过
        `affected=1`。** 客户端以为成了、库里没有、内存里也没了，
        而且不会有任何一处报错。

        放回时**头插**：这批比后面攒的那些早，次序不能反——
        反了的话，同一张表里两条改同一行的语句会对调，
        `替换` 那条路会取到不同的终值。
        """
        with self.锁:
            if not 批:
                return
            if not self.待落:
                self.首条时刻 = time.monotonic()
            self.待落[:0] = 批
            self.待落字节 += sum(len(s) + sum(_参数大小(p) for p in 参 or ())
                                for s, 参 in 批)

    def 丢了批(self):
        """丢掉还没落盘的。回丢掉了几条（`ROLLBACK` 和报错时要说清楚）。"""
        with self.锁:
            n = len(self.待落)
            self.待落 = []
            self.待落字节 = 0
            self.首条时刻 = None
            return n

    def 攒着几条(self):
        with self.锁:
            return len(self.待落)

    def 该落了(self, 现在, 条数, 字节, 秒):
        """按阈值判一下这一批到点没有。"""
        with self.锁:
            if not self.待落:
                return False
            if len(self.待落) >= 条数 or self.待落字节 >= 字节:
                return True
            return (现在 - (self.首条时刻 or 现在)) >= 秒


def _参数大小(p):
    if p is None:
        return 0
    if isinstance(p, (bytes, bytearray)):
        return len(p)
    if isinstance(p, str):
        return len(p)
    return 8


# ══ 二、门 ═════════════════════════════════════════════════════════════

class 门(object):
    """
    整个 MySQL 口。**一条门管一个库**——库里那个"现在开着哪个"由
    `库名取()` 现问现答（界面切库是整窗重建，连着的连接会被那道闸拒掉）。

    参数：

        `工位`       `界面_接驳.工位`。**只要两样**：`干等` 和
                     `开会话/关会话`。门不 import 它，鸭子类型就够
                     ——`mysql_门` 不该知道 Qt 的存在。
        `配置值`     `配置.py` 那份值（设置页第 5 页 MySQL 那一节存的）。
                     **键名就是 `MySQL` 打头的那十个**，一律经
                     `mysql_配置` 那几个口读，这儿不直接 `.get('MySQL…')`
        `库名取`     一个**无参可调用**，回"界面此刻开着哪个库"。
                     从 socket 线程上调，所以装配层要保证它读的是一个
                     简单属性、不是一长串带状态的调用
        `端口`       listen 的口
        `地址`       bind 的地址
        `说`         记日志的回调，`说(什么)`；不给就不记
    """

    def __init__(self, 工位, 配置值, 库名取, 地址, 端口, 说=None):
        self._工位 = 工位
        self._配置 = 配置值
        self._库名取 = 库名取
        self._地址 = 地址
        self._端口 = 端口
        self._说 = 说 or (lambda 文: None)

        self._账 = _配置.账号映射(配置值)
        self._攒批条数, self._攒批字节, self._攒批秒 = _配置.攒批参数(配置值)

        self._听 = None
        self._接受线程 = None
        self._计时线程 = None
        self._停 = threading.Event()

        self._锁 = threading.Lock()
        self._连接们 = {}          # 号 -> 一条线
        self._下一个号 = _首个连接号

    # ── 起 / 停 ─────────────────────────────────────────────────────

    def 起(self):
        """开听。端口占着之类 → 抛 `门错`，**不留半开的状态**。"""
        if self._听 is not None:
            return self
        try:
            听 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            听.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            听.bind((self._地址, self._端口))
            听.listen(_积压)
        except OSError as exc:
            try:
                听.close()
            except Exception:
                pass
            # **地址被人占了这个错必须原样带上去**：Windows 上
            # `WSAEADDRINUSE` 和"权限不够"的文案都不指向真正要改的那个
            # 配置项，装配层要拿它去拼一句人话
            raise 门错('MySQL 口 listen %s:%s 失败：%s'
                       % (self._地址, self._端口, exc))
        self._听 = 听
        self._停.clear()

        self._接受线程 = threading.Thread(
            target=self._接受, name='mysql-接受', daemon=True)
        self._接受线程.start()
        self._计时线程 = threading.Thread(
            target=self._计时, name='mysql-计时', daemon=True)
        self._计时线程.start()

        # 报 **`self.端口()`**，不是 `self._端口`：那个是"要哪个口"，给了 0
        # 的意思就是"让系统挑"，挑到的号只有问 socket 才知道。日志里写 `:0`
        # 会让人以为口没起来。
        self._说('MySQL 口听着 %s:%s' % (self._地址, self.端口()))
        return self

    def 停(self, 超时=5.0):
        """
        停听、断所有连接、**把攒着的落下去**。

        ⛔ **落盘要在"断连接"之前做**，而且不能省：断线时不落的话，
        已经跟客户端回过 `affected=1` 的那批写就没了——客户端以为成了，
        库里没有。这条是这一层最不能出错的一处。
        """
        self._停.set()
        if self._听 is not None:
            try:
                self._听.close()
            except Exception:
                pass
            self._听 = None

        with self._锁:
            线们 = list(self._连接们.values())
        for 线 in 线们:
            # 收尾用短超时：关程序的时候不能因为工人线程正忙一条
            # 全表扫就卡在那儿等满 `命令超时`。落不下去的会记一条日志，
            # 并且**留在队里**（`退回批`），不是默默没了。
            self._落这一条(线, 超时=收尾超时)
            try:
                线.流.关()
            except Exception:
                pass

        # 连接线程收尾时自己会 `关会话`；这里再扫一遍兜底——
        # 线程可能卡在别处，会话不能跟着一起漏。
        with self._锁:
            号们 = list(self._连接们)
        for 号 in 号们:
            try:
                self._工位.关会话(号, 超时=超时)
            except Exception:
                pass
        with self._锁:
            self._连接们.clear()

        for t in (self._接受线程, self._计时线程):
            if t is not None and t is not threading.current_thread():
                t.join(timeout=超时)
        self._接受线程 = self._计时线程 = None
        self._说('MySQL 口停了')

    def 听着(self):
        return self._听 is not None

    def 连接数(self):
        with self._锁:
            return len(self._连接们)

    def 端口(self):
        """真在听那个口。**端口给 0 时让系统挑**，所以这个要现问。"""
        if self._听 is None:
            return None
        try:
            return self._听.getsockname()[1]
        except Exception:
            return self._端口

    # ── 接受 ────────────────────────────────────────────────────────

    def _接受(self):
        while not self._停.is_set():
            try:
                sock, 对端 = self._听.accept()
            except OSError:
                # 听口关了（`停`）就是这条路正常的出口
                break
            except Exception as exc:
                self._说('accept 出了个错：%s' % exc)
                continue

            地址 = 对端[0] if 对端 else ''
            if not _配置.允许连(self._配置, 地址):
                # **当场断，不握手。** 连信息都不给——一个不该连的人
                # 不该知道这后面是什么。
                self._说('拒了 %s：不在允许来源名单里' % 地址)
                try:
                    sock.close()
                except Exception:
                    pass
                continue

            线程 = threading.Thread(
                target=self._服务一条, args=(sock, 地址),
                name='mysql-连-%s' % 地址, daemon=True)
            线程.start()

    # ── 一条连接的一生 ──────────────────────────────────────────────

    def _服务一条(self, sock, 地址):
        线 = None
        号 = None
        try:
            线, 号 = self._握手(sock, 地址)
            if 线 is None:
                return
            self._命令循环(线)
        except _协议.协议错 as exc:
            self._说('连接 %s 断了：%s' % (地址, exc))
        except Exception as exc:
            self._说('连接 %s 出了个错：%s：%s'
                     % (地址, type(exc).__name__, exc))
        finally:
            if 线 is not None:
                # **断开时落盘，不回滚**（见文件头"有意的语义偏离"）。
                self._落这一条(线, 超时=收尾超时)
            if 号 is not None:
                with self._锁:
                    self._连接们.pop(号, None)
                try:
                    self._工位.关会话(号)
                except Exception:
                    pass
                self._说('连接 %s（%s 号）收了' % (地址, 号))
            try:
                sock.close()
            except Exception:
                pass

    # ── 握手 + 验身份 ───────────────────────────────────────────────

    def _握手(self, sock, 地址):
        """
        把一条裸 socket 变成一条登好身份的 `一条线`。**不行就回 `(None, None)`**
        ——应答已经在里面发了，外面不用再管。

        次序（`connections.py` 那边是对着源码核过的）：

            seq0  服务端 → Handshake V10，报 mysql_native_password
            seq1  客户端 → HandshakeResponse41（带一个我们**直接丢掉**的
                            挑战应答——那是 SHA1 的，反推不出口令，没用）
            seq2  服务端 → AuthSwitchRequest 换 mysql_clear_password
            seq3  客户端 → 明文口令 + NUL
                  服务端 → `闸口.登录(映射到的霸下账号, 明文口令)`
            seq4  服务端 → OK / ERR
        """
        try:
            sock.settimeout(握手超时)
        except Exception:
            pass
        流 = _协议.报文流(sock)

        try:
            连接号 = _随机号()
            流.发(_协议.编握手(连接号))
            响应 = _协议.解握手响应(流.收())
        except (_协议.协议错, OSError) as exc:
            self._说('%s 握手没走完：%s' % (地址, exc))
            return None, None

        用户 = 响应['用户']
        库 = 响应['库'] or _配置.默认库(self._配置)

        # ── 库名那道闸：对不上**当场拒**（用户拍板）────────────────
        开着的 = ''
        try:
            开着的 = self._库名取() or ''
        except Exception as exc:
            self._说('问不出当前开的是哪个库：%s' % exc)
        if not 开着的:
            self._应错(流, 1049, '这个霸下进程现在一个库都没开着，连不了')
            return None, None
        if 库 != 开着的:
            # 文案要**带上现在开着那个的名字**：不然用户只知道"被拒了"，
            # 不知道该把哪个改对。这一条是用户拍板的"当场拒"的直接后果。
            self._应错(流, 1049,
                       '现在开的是「%s」，不是「%s」。'
                       '要写「%s」就在界面上双击切过去，再连一次'
                       % (开着的, 库, 库))
            self._说('%s 连的库「%s」对不上现在开着的「%s」，拒了'
                     % (地址, 库, 开着的))
            return None, None

        # ── 账号映射：MySQL 用户名 → 霸下账号 ───────────────────────
        霸下账号 = self._账.get(用户)
        if 霸下账号 is None:
            # **「这个用户名不在名单里」和「口令不对」回的是同一个错。**
            # 分开回就等于告诉外面"这个用户名是存在的"——那是给试探的人
            # 预备的情报。MySQL 自己也是这么做的（1045 一个码）。
            self._应错(流, 1045, "Access denied for user '%s'" % 用户)
            self._说('%s 报的用户名 %r 不在账号映射表里，拒了' % (地址, 用户))
            return None, None

        # ── 换明文口令 ─────────────────────────────────────────────
        流.发(_协议.编换插件())
        try:
            明文 = 流.收()
        except (_协议.协议错, OSError) as exc:
            self._说('%s 等口令的时候断了：%s' % (地址, exc))
            return None, None
        if 明文.endswith(b'\x00'):
            明文 = 明文[:-1]
        # ⛔ **按 latin-1 解，不是 UTF-8。** pymysql 是拿 latin-1 编的口令
        # （`connections.py:311-312`）。latin-1 是个双射，一个字节能对回
        # 一个字符、再编回同一个字节；换成 UTF-8 会**把对的口令拒掉**
        # （≥0x80 的字节过不了），而且能把两个不同的口令映成同一个字符串。
        口令 = 明文.decode('latin-1')

        # ── 让霸下自己判 ───────────────────────────────────────────
        #
        # 账号映射表里**一个口令都不存**：这里拿到的就是客户端输入的明文，
        # 直接交给 `闸口.登录`，走霸下自己的 pbkdf2（`user.py:50`）。
        谁 = None
        try:
            with self._锁:
                号 = self._下一个号
                self._下一个号 += 1
            会话 = _翻译.会话(号, 开着的)

            def 登录(闸):
                return 闸.登录(霸下账号, 口令)

            # ⚠ **`地址` 这一格是给玄武留的，`mysql_门` 自己一个字不用。**
            # `一条线.地址`（上面那个类）自己写着"白名单比的是这个"——
            # 可它原来传不到工位那一层，而玄武判来源要的就是它。
            # 关键字有默认值，不给的工位（鸭子类型）照样能用。
            谁 = self._工位.开会话(号, 登录=登录, 地址=地址)
        except Exception as exc:
            self._说('%s 登录的时候出了个错：%s' % (地址, exc))
            self._应错(流, 1105, '登录的时候服务端出了个错，看服务端日志')
            return None, None

        if 谁 is None:
            self._应错(流, 1045, "Access denied for user '%s'" % 用户)
            self._说('%s 登录霸下账号「%s」没成（口令不对），拒了'
                     % (地址, 霸下账号))
            return None, None

        线 = 一条线(号, 会话, 流, 地址)
        线.身份 = 谁
        with self._锁:
            self._连接们[号] = 线

        # 命令期：**没有超时**，理由见文件头第一条。
        try:
            sock.settimeout(None)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception:
            pass

        self._应OK(流, 0, 0, self._状态位(线))
        self._说('%s 进来了：MySQL 用户 %s → 霸下账号 %s（%s 号），库「%s」'
                 % (地址, 用户, 霸下账号, 号, 开着的))
        return 线, 号

    # ── 命令循环 ────────────────────────────────────────────────────

    def _命令循环(self, 线):
        while not self._停.is_set():
            try:
                负载 = 线.流.收一条命令()
            except _协议.读超时:
                # 命令期不该出现（没设超时），真出现了就接着等
                continue

            if not 负载:
                continue
            命令 = 负载[0]
            数据 = 负载[1:]

            if 命令 == COM_QUIT:
                # **不回包**（MySQL 的规矩：COM_QUIT 之后服务端直接关）。
                # 落盘在 `_服务一条` 的 finally 里做。
                return
            if 命令 == COM_PING:
                self._应OK(线.流, 0, 0, self._状态位(线))
                continue
            if 命令 == COM_INIT_DB:
                自己 = 数据.decode('utf-8', 'surrogateescape').strip()
                开着的 = ''
                try:
                    开着的 = self._库名取() or ''
                except Exception:
                    pass
                if 自己 == 开着的:
                    self._应OK(线.流, 0, 0, self._状态位(线))
                else:
                    # `USE` 那条被拒的理由在这里同样成立：切库 = 整窗重建，
                    # 从一个 socket 线程去触发它会让界面自己跳走。
                    self._应错(线.流, 1049,
                               '现在开的是「%s」。本层不切库——'
                               '要换库请在界面上双击切过去再连' % 开着的)
                continue
            if 命令 == COM_QUERY:
                语句 = 数据.decode('utf-8', 'surrogateescape')
                self._跑一句(线, 语句)
                continue
            if 命令 in _STMT族:
                # **明着报**，不默默乱答：有人换了 `cursorclass` 之类
                # 走到这条路时，一个明确的"不支持"比一个看着像成功的
                # 东西有用得多。
                self._应错(线.流, 1295,
                           '本层不支持预处理语句（COM_STMT_*）。'
                           '请用普通游标')
                continue
            self._应错(线.流, 1047, '认不出的命令号 %d' % 命令)

    # ── 一句话的一生 ────────────────────────────────────────────────

    def _跑一句(self, 线, 语句):
        表 = 语句.strip()
        if not 表:
            self._应OK(线.流, 0, 0, self._状态位(线))
            return

        开始 = time.monotonic()

        # ⛔ **翻译在连接线程上做**（纯 CPU、只动这条连接自己的会话状态），
        # 执行才投给工人线程。这样分是因为"要不要攒批"是翻译的结论，
        # 得在这边就知道，而闸口不能出工人线程。
        try:
            答 = _翻译.翻译(语句, 线.会话)
        except _翻译.翻译错 as exc:
            self._应错(线.流, exc.码, exc.文案)
            return
        except Exception as exc:
            self._应错(线.流, 1105, '翻译这句的时候出了个错：%s：%s'
                                     % (type(exc).__name__, exc))
            return

        # ── 不用碰库的 ─────────────────────────────────────────────
        if isinstance(答, _翻译.直接错):
            self._应错(线.流, 答.码, 答.文案)
            return
        if isinstance(答, _翻译.直接OK):
            self._做动作(线, 答.动作)
            self._应OK(线.流, 答.影响, 答.自增, self._状态位(线))
            # **`要记` 的那几条无论快慢都记**：最要紧的是"客户端在设一个
            # 我们不认的会话变量"——它说明客户端的某个行为我们没满足，
            # 而那件事在别处一点痕迹都不留。`_做动作` 里的 COMMIT/ROLLBACK
            # 各自也记。
            if 答.要记:
                self._说('[%s 号] %s' % (线.号, 答.说明))
            else:
                self._记耗时(线, 语句, 开始)
            return

        # ── 攒批：能攒的当场回 OK，不碰工人线程 ────────────────────
        if isinstance(答, _翻译.写) and 答.可攒:
            n = 线.收进批(答.批)
            self._应OK(线.流, 答.影响, 答.自增, self._状态位(线))
            条数, 字节, 秒 = self._攒批条数, self._攒批字节, self._攒批秒
            if n >= 条数 or 线.待落字节 >= 字节:
                self._落这一条(线)
            # **这里刻意不记日志**：这条路上每一步都是"攒了 N 条"，
            # 逐行插的时候就是逐行一条，百万行就是百万条日志。
            # 落盘那一次已经在 `_落这一条` 里记过了，那才是要看的那条。
            return

        # ── 要读 / 要立刻写：先落盘 ────────────────────────────────
        #
        # **读之前落盘**，而且不只落自己的（见文件头第三条）。
        self._都落盘()
        try:
            回 = self._工位.干等(lambda 闸: self._在工人上(线, 答, 闸),
                                 超时=命令超时, 会话号=线.号)
        except Exception as exc:
            self._应错(线.流, 1105, '执行的时候出了个错：%s' % exc)
            return

        种类 = 回[0]
        if 种类 == '错':
            self._应错(线.流, 回[1], 回[2])
        elif 种类 == 'OK':
            self._应OK(线.流, 回[1], 回[2], self._状态位(线))
        else:
            try:
                _协议.写结果集(线.流, 回[1], 回[2],
                               status=self._状态位(线), 库名=线.会话.库名)
            except _协议.协议错 as exc:
                self._应错(线.流, 1105, '编结果集的时候出了个错：%s' % exc)
                return
        self._记耗时(线, 语句, 开始)

    #: `查后做 → 造 → 又是查后做` 最多钻几层。实际最深两层
    #: （插值 → 播种查 → 真插入），给 8 是留余量。**不能没有上限**：
    #: 翻译层哪天回了一个自己指向自己的应答，这里就是一条不会结束的
    #: 递归，而它发生在 socket 线程上——表现为那条连接永远不回话。
    _最深 = 8

    def _在工人上(self, 线, 答, 闸, 深=0):
        """
        **这一段跑在工人线程上。** 唯一碰闸口的地方。

        ⛔ 整个包在一个 `try` 里捕 `翻译错`：`造()` 和 `投影()` 是在
        **`翻译` 返回之后**才跑的（`mysql_翻译.翻译` 的那个 `try` 已经出了），
        它们照样会抛（最典型的是 `DESCRIBE` 一张不存在的表，那要到
        `_收结构` 才现形）。

        **只捕 `翻译()` 那一下是不够的**：漏掉的会以未捕获异常把这条
        socket 线程打死，客户端看到的是"连接断了"而不是"表不存在"。
        同一个错，报成两个差得很远的东西。
        """
        if 深 > self._最深:
            return ('错', 1105, '翻译层给的应答套得太深了（超过 %d 层），'
                               '已经停下' % self._最深)
        try:
            if isinstance(答, _翻译.写):
                return self._落批(闸, 答.批, 答.影响, 答.自增, 线)

            if isinstance(答, _翻译.查):
                结果 = 闸.fetch(答.语句, 答.参数)
                return self._翻结果(答, 结果)

            if isinstance(答, _翻译.查后做):
                结果 = 闸.fetch(答.语句, 答.参数)
                答2 = 答.造(结果)           # ← 会抛
                return self._在工人上(线, 答2, 闸, 深 + 1)

            return ('错', 1105, '翻译层回了一个认不出的东西')
        except _翻译.翻译错 as exc:
            return ('错', exc.码, exc.文案)

    def _翻结果(self, 答, 结果):
        if not 结果.ok:
            # 表不存在 → 1146；别的算 1105。**用霸下自己的原话**——
            # 爬虫会把这句话写进它的日志（`config.py` 那条 `db_operate`），
            # 换成一句"操作失败"就把现场抹掉了。
            return ('错', _码_of(结果.error), 结果.error or '操作失败')
        # ⛔ **先接住再拆，不能直接 `列, 行 = 答.投影(...)`。**
        # 投影回 `None` 是**合法**的那一支，意思是"这一步不回结果集，只回
        # OK"（`建表` 走的就是这条：它只是借一次 `显示 结构` 把结构缓存填
        # 上）。直接拆元组的话 `None` 会抛
        # `TypeError: cannot unpack non-iterable NoneType object`，
        # 而那个错从 socket 那头看起来是"执行的时候出了个错"——
        # 一句和真因毫不相干的话。
        出 = 答.投影(结果)                     # ← 会抛
        if 出 is None:
            return ('OK', 0, 0, '')
        列, 行 = 出
        return ('集', 列, 行)

    def _落批(self, 闸, 批, 影响, 自增, 线):
        """
        把一批语句交给霸下。**回 `('OK', …)` 或 `('错', …)`。**

        `闸.send_many` 一次落盘（`io_layer.py:110` 那边按
        `MAX_BATCH_STATEMENTS` 自己切），返回与入参等长的结果列表。

        ⛔ **批里没有回滚**（`syntax.py:1280`）——成了的就是成了。所以
        报错必须说清"共 N 条、失败 M 条、成了的没退"，只回一句"失败了"
        会让人以为一条都没进去，然后去重跑，然后撞主键。
        """
        if not 批:
            return ('OK', 影响 or 0, 自增 or 0, '')
        try:
            结果们 = 闸.send_many(批)
        except Exception as exc:
            return ('错', 1105, '交给霸下的时候出了个错：%s' % exc)

        坏的 = [(i, r) for i, r in enumerate(结果们) if not r.ok]
        if 坏的:
            i, r = 坏的[0]
            return ('错', _码_of(r.error),
                    '共 %d 条，失败 %d 条。第一条失败的是第 %d 条：%s。'
                    '**批里没有回滚，成了的那些没退**'
                    % (len(批), len(坏的), i + 1, r.error or ''))

        影响 = 影响 if 影响 is not None else sum(
            (r.affected or 0) for r in 结果们)
        return ('OK', 影响, 自增 or 0, '')

    # ── 落盘 ────────────────────────────────────────────────────────

    def _落这一条(self, 线, 超时=命令超时):
        """
        把这条连接攒着的写下去。**绝不抛。**

        回 `(成了几条, 错的文案或 None)`。

        ⛔ **两条失败要分得很开**，处理方式完全相反：

            手里这一步没成交（工人线程忙、超时、闸口炸了）
                → 那批**根本没到引擎**，`退回批` 放回去，下次再落
            引擎收下了、里面有语句失败（主键撞了之类）
                → **绝不能退**。批里没有回滚（`syntax.py:1280`），成了的
                  那些已经在盘上了；退回去重发就是拿同一批主键再撞一次，
                  而这次报的错会指向"重复"，离真因（可能是第一遍的
                  类型错）很远。
        """
        批 = 线.取走批()
        if not 批:
            return 0, None
        try:
            回 = self._工位.干等(lambda 闸: self._落批(闸, 批, None, 0, 线),
                                 超时=超时, 会话号=线.号)
        except Exception as exc:
            # 没到引擎 —— 放回去
            线.退回批(批)
            self._说('落盘（%s 号，%d 条）这一步没成交，'
                     '已放回队里等下次：%s' % (线.号, len(批), exc))
            return 0, str(exc)
        if 回[0] == '错':
            # 到了引擎、里面有失败的 —— **不放回**
            self._说('落盘（%s 号，%d 条）报了错：%s' % (线.号, len(批), 回[2]))
            return 0, 回[2]
        return len(批), None

    def _都落盘(self):
        """把**所有**连接攒着的写落下去。读之前调。"""
        with self._锁:
            线们 = list(self._连接们.values())
        for 线 in 线们:
            self._落这一条(线)

    def _计时(self):
        """到点替各连接落盘。**理由见文件头第二条。**"""
        条数, 字节, 秒 = self._攒批条数, self._攒批字节, self._攒批秒
        while not self._停.wait(计时步长):
            现在 = time.monotonic()
            with self._锁:
                线们 = list(self._连接们.values())
            for 线 in 线们:
                if 线.该落了(现在, 条数, 字节, 秒):
                    self._落这一条(线)

    def _做动作(self, 线, 动作):
        if 动作 == '落盘':
            n, 错 = self._落这一条(线)
            if 错 is not None:
                self._说('COMMIT 落盘（%s 号）没成：%s' % (线.号, 错))
            else:
                self._说('COMMIT 落盘（%s 号）：%d 条' % (线.号, n))
        elif 动作 == '丢弃':
            n = 线.丢了批()
            if n:
                self._说('ROLLBACK（%s 号）：丢了还没落盘的 %d 条' % (线.号, n))

    # ── 应答 ────────────────────────────────────────────────────────

    def _状态位(self, 线):
        开 = 线.会话.状态位 & _协议.状态.AUTOCOMMIT
        return _协议.状态.AUTOCOMMIT if 开 else 0

    def _应OK(self, 流, 影响, 自增, status):
        try:
            流.发(_协议.编OK(影响 or 0, 自增 or 0, status=status))
        except Exception as exc:
            raise _协议.协议错('回 OK 的时候断了：%s' % exc)

    def _应错(self, 流, 码, 文案):
        try:
            流.发(_协议.编ERR(码, str(文案 or '操作失败')))
        except Exception:
            # **回 ERR 的时候断了就是断了**——再抛也没人接得住，
            # 而且调用方正要收尾。这里咽掉，留给上面那条 finally 去管。
            pass

    # ── 日志 ────────────────────────────────────────────────────────

    def _记耗时(self, 线, 语句, 开始):
        """
        慢的那几条记一条。**攒批到底省了多少，看的就是这条日志**——
        `批条数` 调大之后"落盘次数"应该显著下来（每次落盘在
        `_落这一条` 那边也各记一条），而这一层不另写探针。

        ⚠️ 默认只在超过 `耗时日志秒` 时记。**别顺手把每条都记上**：
        爬虫灌百万行的时候，日志自己就成了瓶颈，而且真正要看的那几条
        会淹在里面。
        """
        try:
            秒限 = _配置.耗时日志秒(self._配置)
        except Exception:
            秒限 = 1.0
        用 = time.monotonic() - 开始
        if 秒限 > 0 and 用 < 秒限:
            return
        self._说('[%s 号] %.3fs %s'
                 % (线.号, 用, 语句.strip().replace('\n', ' ')[:60]))


# ══ 三、小工具 ═════════════════════════════════════════════════════════

def _码_of(错文):
    """霸下的错误文案 → MySQL 错误码。"""
    文 = 错文 or ''
    if ('没有' in 文 and '表' in 文) or '不存在' in 文:
        return 1146
    if '主键' in 文 and ('已经' in 文 or '重复' in 文 or '有行' in 文):
        return 1062
    if '权限' in 文 or '不许' in 文 or '没这个权' in 文:
        return 1142
    return 1105


_号锁 = threading.Lock()
_号池 = [0]


def _随机号():
    """
    握手包里那个 thread_id。**只要不和别的连接撞上就行**——它是给
    `SHOW PROCESSLIST` 用的，而那个我们不做。用递增而不是随机：批处理
    跑起来之后，递增的号在日志里看得出先后。
    """
    with _号锁:
        _号池[0] = (_号池[0] % 0xFFFFFFFE) + 1
        return _号池[0]


# ══ 四、给装配层用的一个人话入口 ═══════════════════════════════════════

def 起一个(工位, 配置值, 库名取, 地址, 端口, 说=None):
    """
    起一条门。**端口占着的时候给一句能直接显示给用户的话。**

    单独包一层是因为 `门.起` 抛的那句话里带着 `OSError` 的原文，
    而 Windows 的 `WSAEADDRINUSE` 文案（"每个套接字地址……通常只允许
    使用一次"）不指向真正要改的那一格——用户该改的是 `配置.py` 里的
    `MySQL端口`，或者关掉另一个占着 3306 的 MySQL。
    """
    m = 门(工位, 配置值, 库名取, 地址, 端口, 说=说)
    try:
        m.起()
    except 门错 as exc:
        raise 门错('%s\n（多半是 3306 已经被占了——关掉另一个 MySQL，'
                   '或者到「设置」里把 `MySQL端口` 换一个）' % exc)
    return m
