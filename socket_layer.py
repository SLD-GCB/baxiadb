#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
socket_layer.py — 套接层

**把库开成一个服务：认帧、开端口、回帧。**

    帧   <-> 字节                protocol
    字节 <-> socket              transport
    帧   -> 门（注入进来的回调）   这一层（按操作码段分工）

**这一层谁都不认得。** 它不知道盘（区、块、槽、榫卯），不知道库里有什么，
现在**连库在哪儿都不知道**——`serve` 不收 `Storage` 了。它只做一件事：
看操作码，决定这个帧交给哪扇门。**那两扇门是注入进来的回调**
（`statements=` / `bytes_handler=`），由组装的那一头接上（见
`make_handler`）。要换传输（换成裸 socket、共享内存、别的协议），动的
是这个文件；盘的布局和语句怎么解，一个字节都不用改。

两扇门
------

库对外有两扇门，它们在**操作码**上就分好了段（见 `protocol.Op`）：

    0x1x  字节层   PUT / GET / SCAN / BATCH   帧 <-> 键值   -> 语法层
    0x2x  语句层   SQL / SQL_BATCH            帧 <-> 语句   -> 语法层

**段就是路由**，一张表就够。一个操作码只能有一个意思：两边都认就等于
它的意思取决于谁先看到它。

**两扇门都是注入的，这一层两扇都不知道里面装的是什么。** 以前字节门是
长在套接层自己身上的（向语法层借一个 `Kernel`），只有语句门是注入的——
那是不对称的，而且"借一个装着 Arena 的物件"本身就是第二条够得着 Arena
的路。现在两扇门都在输入输出层（`io_layer`），这里只递帧。

两扇门回的**东西不一样多**：字节门永远一帧，语句门是一串（结果集大了
要按 FRAME_TARGET 分片，一批语句更是一串结果）。`transport.Server` 两种
都收（`None | Frame | [Frame, ...]`），所以这里不用把它们抹平。

会话（曾经那个静默丢写的洞，现已堵上）
--------------------------------------

重放身份是 `(会话, 序号, 操作码)`，而**会话号必须有人分配**。以前协议里
没有任何地方给它赋值，所有连接的会话号都是 0，重放表又是服务端一张——
于是**每个连接的第一个请求在重放表里是同一个身份**，后到的那条拿到先前
那条的缓存答案。形态是**静默丢写**：客户端拿着一个长得像成功的应答，而
它那条语句根本没执行。

现在会话号由**服务端**在握手时分配（随机 64 位，见 `transport.Server`
的 HELLO/WELCOME），会话**活在连接之外**，重放表按会话各一份。顺带
拿到的就是"重连续传"：连接断了会话还在，客户端 `resume()` 挂回去，
把没确认的请求原样重发——服务端要么重放原答案，要么明着拒绝，**绝不
悄悄重跑**。

**这条边界不会自己现形。** 它是静默的：应答看不出异常，只有去查数据才
发现少了一次写。所以碰重放表之前先想清楚——**多连接共用一张重放表**是
这一层唯一真正新的维度，一条连接的场景底下它永远是对的。
"""

import threading

import kernel as kernel_mod
import protocol
# 语句层的帧负载编解码在 `wire.py`：**本模块和输入输出层都要用**，
# 挂在哪一层名下都会撞上环或者让套接层认得 SQL（`wire.py` 头上写了）。
# `Result` 还是从语法层借——它是语法层的输出类型。
import transport
import wire
from syntax import Result as _Result

__all__ = ['make_handler', 'serve', 'Client', 'door_of']


# 两扇门各认哪些操作码。**段就是路由**（见文件头"两扇门"）。
_STMT_OPS = frozenset((protocol.Op.SQL, protocol.Op.SQL_BATCH))
_BYTE_OPS = frozenset((protocol.Op.PUT, protocol.Op.GET,
                       protocol.Op.SCAN, protocol.Op.BATCH))
# 身份那一帧**是第三扇门**，不是上面两扇里的任何一个：它既不是"写一行
# 字节"，也不是"跑一条 SQL"。挂进任何一扇都会让那扇门的函数多出一个
# "这个操作码我其实不处理"的分支。
_LOGIN_OPS = frozenset((protocol.Op.LOGIN,))

# 操作码 -> 哪扇门。**`door_of` 和路由都从这一张表来，不各写一遍**：
# 两处各写一遍的话，加一个操作码时改了一处忘另一处，症状是"排错时
# 告诉你走的是甲门，实际走的是乙门"——那种错查起来最费劲。
_GATE_OF_OP = {}
for _op in _STMT_OPS:
    _GATE_OF_OP[_op] = '语句层'
for _op in _BYTE_OPS:
    _GATE_OF_OP[_op] = '字节层'
for _op in _LOGIN_OPS:
    _GATE_OF_OP[_op] = '登录层'


def door_of(op):
    """这个操作码归哪扇门（认不出回 None）。给排错看。"""
    return _GATE_OF_OP.get(op)


def _err_text(frame):
    """
    ERR 帧 -> 人能读的那句话。

    负载是 `<码>\\t<消息>`（见 `transport.err_frame`）。码留在帧里是给
    程序看的——它能据码决定"重发没用"还是"重连试试"——所以这里只把
    消息挑出来，不把两样混成一句。

    拆负载这件事**只在 `transport.err_text` 里做一次**。要码的地方
    （比如 `resume()` 回来一帧 `REPLAY_LOST`）直接用它。
    """
    return transport.err_text(frame)[1]


# ── 路由 ────────────────────────────────────────────────────────────

def make_handler(statements=None, bytes_handler=None, login=None):
    """
    给 `transport.Server` 用的 handler。

    **三扇门都得注入进来**，这里一扇都不 import。理由有两条：

    **一是环。** 三扇门的实现在 `io_layer`，而 `io_layer` 要用本模块的
    客户端（远程腿）——本模块反过来 import 它，环就成了。注入是按
    "谁组装谁接线"（`龙龟之心.py`）解决的。

    **二是不该认得。** 套接层拿到的是几个回调，**它不知道里面装的是不是
    SQL、是不是键值**。以前字节门是例外：这里自己向语法层借一个 `Kernel`
    （`kwargs: kernel=`），于是套接层认得库的一角。现在那一角也没有了。

    没接的那扇门被用到时**明说没接，别假装没有这个操作码**——"不认这个
    操作码"会让排错的人去查协议，而问题其实在组装的那一头。

    `login` 那扇门接上之后，**它和别的门有一处不一样**：另外两扇靠帧里
    的会话号取身份，这一扇是**造身份的那一扇**，它自己去查用户表、把
    结果挂到 `conn.session.身份` 上。所以 `handle` 本体一行都不用改——
    它本来就把 `conn` 原样递给门。
    """
    doors = {'语句层': statements, '字节层': bytes_handler, '登录层': login}
    arg_of = {'语句层': 'statements', '字节层': 'bytes_handler',
              '登录层': 'login'}

    def handle(frame, conn=None):
        op = frame.opcode
        door = _GATE_OF_OP.get(op)
        if door is None:
            # 认不出就说认不出。**不回一个含糊的 OK** —— 客户端拿到 OK
            # 会以为它写进去了。
            return transport.err_frame(
                frame, '套接层不认这个操作码 %s' % protocol.Op.name(op),
                kernel_mod.Code.UNSUPPORTED)
        fn = doors[door]
        if fn is None:
            return transport.err_frame(
                frame, '%s的门没接上（没给 make_handler 传 %s）'
                % (door, arg_of[door]), kernel_mod.Code.UNSUPPORTED)
        return fn(frame, conn)

    return handle


def serve(host='127.0.0.1', port=0, statements=None, bytes_handler=None,
          login=None):
    """
    绑定（不跑）。`port=0` 让系统挑一个空闲的，挑到的在返回值的 `port` 上。

    返回 `(server, port)`——server 要给 `transport.Server.serve_forever`
    或后台线程用。

    **不收 `Storage`。** 库不归这一层管：那几个回调背后是什么，这里不问。
    """
    server = transport.Server(
        host, port,
        make_handler(statements, bytes_handler, login)).bind()
    return server, server._sock.getsockname()[1]


def serve_in_thread(host='127.0.0.1', port=0, name='baxia-socket',
                    statements=None, bytes_handler=None, login=None):
    """绑定并在后台线程开跑。返回 `(server, port, thread)`。"""
    server, port = serve(host, port, statements, bytes_handler, login)
    th = threading.Thread(target=server.serve_forever, name=name, daemon=True)
    th.start()
    return server, port, th


# ── 客户端 ──────────────────────────────────────────────────────────

class Client(object):
    """
    一条连接。增删改查走**语句层**那扇门；字节层那扇门也留着，给套接层
    接入和排错用。

    收应答的规矩：**读到 `Flag.FINAL` 为止。**

    `MORE` 说的是"这个结果还有后续分片"，与"整个应答还有没有后续"无关——
    一批 250 条语句回来 250 个结果帧，**一个带 MORE 的都没有**。拿 MORE
    当收齐的判据，读一帧就停，剩下 249 帧留在原地，**下一个请求就会读到
    上一个请求的答案**：两个请求的答案错位，而错位后的样子完全正常。
    （这个洞真存在过，见开发报告第二十节。）

    收帧这件事**只在 `transport.Client.recv_response` 里实现一次**，
    这里不再抄一份——抄一份就是多一处会忘记改的地方。

    **会话（`session`）由 `transport.Client` 在握手时拿到**，这里不自己
    编。断了用 `resume()` 续，见 `transport.Client.resume`。

    参数一律走 `?` 绑定，绝不拼进语句文本。
    """

    def __init__(self, host, port, timeout=5.0, session=None):
        self.conn = transport.Client(host, port, timeout, session=session)
        self.session = self.conn.session

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def resume(self, timeout=None):
        """
        断线重连，续上同一个会话，把没收到答案的请求原样重发。

        返回 `[(操作码名, [Result, ...]), ...]`。**重发拿到的一定是原答案
        或者一帧明写的 REPLAY_LOST，不会是"又跑了一遍"。** 会话真的过期了
        就抛 `ConnectionError`——那时续不成立，得自己重新入会，并自己决定
        哪些请求值得重发。
        """
        out = []
        for seq, frames in self.conn.resume(timeout):
            out.append((protocol.Op.name(frames[0].opcode),
                        self._results(frames)))
        return out

    def _call(self, opcode, payload, timeout=None):
        frames = self.conn.call_response(
            self.conn.req(opcode, payload), timeout)
        if frames is None:
            raise TimeoutError('等 %s 的应答超时'
                               % protocol.Op.name(opcode))
        return frames

    @staticmethod
    def _results(frames):
        """
        应答帧 -> [Result]。

        ERR 是**整条请求**级别的（SQL 负载都没解开、操作码不认），它没有
        ROWS 那套结构，硬按 ROWS 解会报一个跟真实原因无关的解析错。
        """
        if frames and frames[0].opcode == protocol.Op.ERR:
            return [_Result(error=_err_text(frames[0]))]
        return wire.decode_result_frames(frames)

    # ── 身份 ────────────────────────────────────────────────────────

    def login(self, 用户名, 口令, timeout=None):
        """
        报身份。成了返回 `True`，用户名或口令不对返回 `False`。

        **身份是服务端从会话里取的，这里只是"报"** —— 客户端说得再像，
        服务端也只认自己查出来的那个用户（`protocol.Op.LOGIN` 那段写了
        为什么不能反过来）。所以这个方法的返回值只说明**这一次报对了**，
        它不是一个"从现在起我说了算"的凭据。

        **口令不对是 `False`，不是异常。** 它是服务端的正常回答（一帧
        `ERR`，码 `BAD_LOGIN`），不是连接出了问题；抛异常的话调用方会
        把它和"网络断了"写进同一个 `except`，而这两件事该做的处理正好
        相反——一个是让用户重输，一个是重连。

        超时就照旧抛 `TimeoutError`（那是真没收到应答）。
        """
        frames = self._call(protocol.Op.LOGIN,
                            wire.encode_login(用户名, 口令), timeout)
        if frames[0].opcode == protocol.Op.ERR:
            return False
        wire.decode_login_ok(frames[0].payload)
        return True

    # ── 增删改查 ────────────────────────────────────────────────────

    def sql(self, text, params=(), timeout=None):
        """一条语句。返回一个 `Result`（错了也是 Result，带 `.error`）。"""
        return self.sql_batch([text], [tuple(params or ())], timeout)[0]

    def sql_batch(self, texts, params=None, timeout=None):
        """一个帧里一批语句，一次落盘。返回与 `texts` 等长的结果列表。"""
        texts = list(texts)
        groups = [tuple(p or ()) for p in (params if params is not None
                                           else [()] * len(texts))]
        if len(groups) != len(texts):
            raise ValueError('参数组数 %d 和语句条数 %d 对不上'
                             % (len(groups), len(texts)))
        payload = wire.encode_sql_request(texts, groups)
        return self._results(self._call(protocol.Op.SQL_BATCH, payload,
                                        timeout))

    # ── 字节层那扇门 ────────────────────────────────────────────────

    def put(self, key, value=b'', dim=0, timeout=None):
        frames = self._call(protocol.Op.PUT,
                            kernel_mod.encode_put(key, value, dim), timeout)
        if frames[0].opcode == protocol.Op.ERR:
            raise kernel_mod.KernelError(_err_text(frames[0]))
        return True

    def get(self, key, timeout=None):
        """
        取不到回 None（**不是空串**——空串是一个真实的值）。

        **`found` 那一位必须看。** 应答里它单独占一个字节，就是为了把
        "没这个键"和"这个键的值是零长度"分开——这是协议里立身的一条
        （见 `kernel.py` 头上"内核不管删除"那段：只要分不开，上层就没法
        用墓碑值表达删除）。把 `found` 丢掉、直接取第 4 个返回值的话，两种
        情形都回 `b''`：**墓碑读回来和"值就是空串"长得一模一样**，而这行
        docstring 还写着"回 None"。
        """
        frames = self._call(protocol.Op.GET, kernel_mod.encode_get(key),
                            timeout)
        if frames[0].opcode == protocol.Op.ERR:
            raise kernel_mod.KernelError(_err_text(frames[0]))
        found, _rid, _blk, _slot, value = kernel_mod.decode_found(
            frames[0].payload)
        return value if found else None

    def scan(self, cursor=None, limit=kernel_mod.DEFAULT_SCAN_LIMIT,
             timeout=None):
        """扫一页。返回 (下一页游标, [(键, 值), …], 还有没有)。"""
        frames = self._call(protocol.Op.SCAN,
                            kernel_mod.encode_scan(cursor, limit), timeout)
        if frames[0].opcode == protocol.Op.ERR:
            raise kernel_mod.KernelError(_err_text(frames[0]))
        return kernel_mod.decode_records(frames[0].payload)

    def put_many(self, items, dim=0, timeout=None):
        """
        一批直存，**一个帧一次落盘**。跳过语句解析直接进引擎——灌数据
        时比的不是"写得多优雅"，是"少落几次盘"。
        """
        items = list(items)
        frames = self._call(protocol.Op.BATCH,
                            kernel_mod.encode_batch(
                                [(k, v, dim) for k, v in items]), timeout)
        if frames[0].opcode == protocol.Op.ERR:
            raise kernel_mod.KernelError(_err_text(frames[0]))
        return kernel_mod.decode_batch_result(frames[0].payload)
