# -*- coding: utf-8 -*-
"""
霸下传输层 — 连接、收发与韧性机制
=================================

这一层的 KPI 不是"正确实现了协议"，而是**出故障时还能活着**。
教科书式实现把精力花在穷举状态和错误码上，对现实中真正杀死你的
四件事基本不设防。这里反过来，四件事各有一套机制：

  半开连接    对端进程没了、NAT 表项过期、网线被拔——TCP 不会告诉你。
              等 keepalive 要两小时，等于没有。真正可靠的是**写超时**：
              往一个死连接写，最终必然失败。用发送缓冲持续送不出去
              的时长判定，比任何心跳都实在。

  慢客户端    客户端不读结果集时，服务端不能被它拖住。这里用
              **水位背压**：发送缓冲越过高水位就停止读该连接，让 TCP
              窗口自然收缩，把压力顶回对端，而不是在服务端堆积内存。
              这是最容易把数据库整体搞死的地方。

  粘包半包    假设一次 recv 得到一个帧是错的。全部交给 FrameDecoder，
              长度读满才交付。

  畸形包      长度域超限直接断连（见 protocol.MAX_PAYLOAD）；
              校验失败则退到魔术字后重同步，而不是信任一个坏掉的长度域。

另外两个设计选择：

  非阻塞 + selectors，不用线程/连接。慢客户端天然变成"这个 fd 不可写"，
  背压不需要任何额外机制；也不需要为一个连接配一个线程。

  CPU 空转无关——事件循环的 tick 同时承担心跳和死连接巡检。

会话与重连续传
--------------

**会话活在连接之外。** 连接断了，会话还在——这才是"续"有东西可续的
前提。会话号由服务端在握手时分配（`HELLO` / `WELCOME`），是**随机 64 位**：
它是持有的凭证，自增的话任何能连上的人写个 1 就能接管别人的会话。

客户端断线后 `resume()`：换一条新连接、用原会话号挂回去、把**发出去还没
收到答案**的请求原样重发。服务端按下面的规矩办：

    序号 <= 水位 且答案还留着   →  **重放**，不重入引擎
    序号 <= 水位 但答案没留住   →  **拒绝**（REPLAY_LOST），不重跑
    序号 >  水位               →  **从没处理过**，照常执行

这三条把"重发"唯一定义了。**中间那条是要害**：照原样重跑一遍，对只追加
的引擎就是多写一个版本——静默多出来的一行。
"""

import errno
import random
import selectors
import socket
import struct
import time
from collections import OrderedDict, deque

import protocol
from protocol import (
    Frame, FrameDecoder, FrameCorrupt, Flag, Op, ProtocolError,
)

__all__ = ['Limits', 'Connection', 'ReplayGuard', 'Session', 'Server',
           'Client', 'ok_frame', 'err_frame', 'err_text']

# 会话号：8 字节无符号。0 是保留值，意思是"给我一个新的"。
_SID = struct.Struct('<Q')


class Limits(object):
    """韧性相关的阈值。集中放置，方便按部署环境调。"""

    RECV_SIZE = 64 * 1024

    # 背压水位。发送缓冲越过 high 就停止读该连接；排空到 low 以下才恢复。
    # 两档之间留空隙是为了避免在边界上反复抖动。
    HIGH_WATER = 4 * 1024 * 1024
    LOW_WATER = 1 * 1024 * 1024

    # 发送缓冲持续非空超过这个时长，判定对端已死。这是半开连接的主检测。
    WRITE_TIMEOUT = 30.0

    # 空闲多久发一次心跳；再久没收到任何字节就断开。
    IDLE_PING = 60.0
    DEAD_TIMEOUT = 300.0

    MAX_CONNS = 1024

    # 接收缓冲持续增长超过此值而没有凑出一个完整帧，说明对端在灌半包
    # 垃圾。断开，不为它保留内存。
    STUCK_BUFFER = 8 * 1024 * 1024

    TICK = 0.5

    # 会话保留。连接断了会话不立刻死——它得活着等客户端重连回来。
    # 闲置超过此值才回收，回收后客户端拿旧号来会拿到 SESSION_UNKNOWN。
    SESSION_IDLE = 900.0
    # 同时存在的会话数上限。满了先踢最久没动的**空闲**会话；
    # 还挂着连接的会话不动——那是在用的，踢了就是无故断人。
    MAX_SESSIONS = 4096

    # 每会话重放表的上限。**按字节算的那两个才是真闸门**：
    # 只按条数限，一条数 MB 的应答算一格，几条就把内存吃穿。
    GUARD_ENTRIES = 4096
    GUARD_BYTES = 8 * 1024 * 1024
    GUARD_MAX_ENTRY = 1 * 1024 * 1024


# 一次应答里**可能**出现多帧的操作码。其余操作码的应答恒为一帧，那些帧
# 本身就是终帧——收方不必靠 FINAL 也知道读完了。
#
# 这条区分是要紧的：只有它成立，"读到 FINAL 为止"才是完备的收帧规矩。
_MULTI_FRAME = frozenset((Op.DATA, Op.ROWS))


def ok_frame(req, payload=b'', flags=0):
    return Frame(Op.OK, payload, Flag.RESPONSE | flags,
                 req.session, req.seq)


def err_frame(req, message, code=0):
    return Frame(Op.ERR, ('%d\t%s' % (code, message)).encode('utf-8'),
                 Flag.RESPONSE, req.session, req.seq)


class ReplayGuard(object):
    """
    有界重放表：(会话 ID, 请求序号, 操作码) -> 那一次应答的**整个帧串**。
    身份的构成见 protocol.Frame.key——操作码那一维不能省。

    这是"内核无状态、可重放"落地的支点。客户端断线重连后，只需原样
    重发未确认的请求，这里直接把上次的答案再发一遍，不重算一次。

    三条边界：

      **整串都留，不再只留单帧。** 以前只留单帧，理由是"分片响应带游标
      语义，不该由协议层重放"。代价是一批 250 条语句的应答一帧都留不下
      （250 个结果，没有一个带 MORE），于是重发时**整批重跑一遍**——
      250 行变 500 行，而且一声不响。宁可留不下时明说，也不能留半截。

      **上限按字节算，不只按条数。** 一条数 MB 的应答按条数算只占一格，
      几条就能把内存吃穿。协议层不能变成结果集缓存——那是内存泄漏的
      另一种写法。

      **留不下不是错误，是水位的事。** `put` 返回 False 只是没留；
      真正把它变成一次明确拒绝的是 `Session.answered`。两件事分开，
      是因为"没留住"必须能被回答，而"没答过"不能。
    """

    __slots__ = ('_items', '_bytes', '_capacity', '_max_bytes', '_max_entry')

    def __init__(self, capacity=4096, max_bytes=8 * 1024 * 1024,
                 max_entry=1 * 1024 * 1024):
        self._items = OrderedDict()
        self._bytes = 0
        self._capacity = capacity
        self._max_bytes = max_bytes
        self._max_entry = max_entry

    def get(self, key):
        hit = self._items.get(key)
        if hit is not None:
            self._items.move_to_end(key)
            return hit[0]
        return None

    def put(self, key, frames):
        """塞得下就留，返回是否留下。挤掉的是最旧的——最新的最可能被重发。"""
        frames = tuple(frames)
        size = sum(len(f.payload) for f in frames) + 32 * len(frames)
        if size > self._max_entry:
            return False
        old = self._items.pop(key, None)
        if old is not None:
            self._bytes -= old[1]
        self._items[key] = (frames, size)
        self._bytes += size
        while self._items and (len(self._items) > self._capacity or
                               self._bytes > self._max_bytes):
            _, (_, dropped) = self._items.popitem(last=False)
            self._bytes -= dropped
        return key in self._items

    def __len__(self):
        return len(self._items)


class Session(object):
    """
    一次客户端会话。**活在连接之外**——这就是"重连续传"的全部意义：
    连接断了，会话还在，重放表还在。

    为什么一个水位就够
    ------------------
    `_dispatch` 在事件循环里**同步**跑完 handler 才处理下一个帧，所以
    应答严格按到达次序产生，"答到哪儿了"一个 `answered` 就够：

        序号 <= answered 且答案留着  →  重放
        序号 <= answered 但答案没了  →  拒绝（REPLAY_LOST）
        序号 >  answered            →  从没处理过，照常执行

    不需要更细的账。真要做成乱序并发的话，这里就得换成窗口/位图，
    而那笔复杂度现在买不到任何东西。

    水位**只在答完之后抬**。抬早了，一条崩在中间的请求会被当成"答过了"，
    重发时被拒绝——而它其实从没生效过。

    会话过期时整条一起丢（含水位）。那时客户端拿旧会话号回来会拿到
    SESSION_UNKNOWN，明说比猜强。
    """

    __slots__ = ('sid', 'conn', 'guard', 'answered', 'last_seen', 'created_at',
                 '身份')

    def __init__(self, sid, conn, now, limits=Limits()):
        self.sid = sid
        self.conn = conn
        self.guard = ReplayGuard(limits.GUARD_ENTRIES, limits.GUARD_BYTES,
                                 limits.GUARD_MAX_ENTRY)
        self.answered = 0
        self.last_seen = now
        self.created_at = now
        # 这条会话登录成了谁（`user.身份`），没登录是 `None`。
        #
        # **放在 `Session` 上，不是 `Connection` 上。** 会话活在连接之外：
        # `resume()` 换一条连接挂回同一个会话，身份该跟着走——否则每次
        # 断线重连都得重新登录一遍，而"重连"这件事的全部意义就是不必重来。
        #
        # **反过来那条也要说清楚**：会话真过期了（`resume()` 抛
        # `ConnectionError`），那条路必须重新登录，不能沿用旧身份——那时
        # 拿到的是一个**新**会话（新 sid），上面的 `身份` 是空的。这是对的：
        # 身份绑的是会话，不是"这个客户端"。
        self.身份 = None

    def __repr__(self):
        return '<Session %016x conn=%s answered=%d held=%d 身份=%s>' % (
            self.sid, 'yes' if self.conn is not None else 'no',
            self.answered, len(self.guard),
            self.身份.user.name if self.身份 is not None else '（没登录）')


class Connection(object):
    """
    一条 TCP 连接。**它不是会话**——会话可以比连接活得久，这条连接断了
    只是把会话解绑（`Session.conn = None`），会话本身和它的重放表都留着。

    `session` 在入会握手之前是 None。握手之前来的任何请求都会被拒
    （见 `Server._dispatch`）——不拒的话它们会带着会话号 0 走重放路径，
    而 0 是所有没握手的客户端共用的号码。
    """

    __slots__ = ('sock', 'addr', 'decoder', 'outbuf', 'paused',
                 'last_recv', 'last_ping', 'blocked_since', 'closing',
                 'opened_at', 'session')

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.decoder = FrameDecoder()
        self.outbuf = bytearray()
        now = time.monotonic()
        self.last_recv = now
        self.last_ping = now
        self.blocked_since = None    # 发送缓冲首次非空的时刻
        self.paused = False          # 背压：暂停读该连接
        self.closing = False
        self.opened_at = now
        self.session = None          # 握手后才绑上

    def __repr__(self):
        return '<Conn %s:%d out=%d%s%s>' % (
            self.addr[0], self.addr[1], len(self.outbuf),
            ' paused' if self.paused else '',
            '' if self.session is None else ' sess=%016x' % self.session.sid)


class Server(object):
    """
    非阻塞服务端。

    handler 的签名：handler(frame, conn) -> None | Frame | [Frame, ...]
    抛出的异常会被转成 ERR 帧回给对方——单个请求出错不该拖垮连接。
    """

    def __init__(self, host, port, handler, limits=Limits()):
        self._host = host
        self._port = port
        self._handler = handler
        self.limits = limits
        # **会话表挂在服务端上，不在连接上。** 以前重放表是服务端一份、
        # 所有连接共用的，而会话号从来没人分配过——每个客户端都用 0，
        # 于是每条连接的第一个请求重放身份完全一样：B 的插入命中了 A 的
        # 建表缓存，**根本没执行**，而 B 收到的答复长得像真的。
        # 会话号 + 一连接一会话 + 每会话一张表，这三样合起来才堵得住。
        self.sessions = {}
        self.sel = selectors.DefaultSelector()
        self.conns = set()
        self.stats = {'accepted': 0, 'closed': 0, 'replayed': 0,
                      'errors': 0, 'rejected': 0, 'sessions': 0,
                      'replay_lost': 0, 'session_unknown': 0,
                      'no_session': 0, 'bad_session': 0}
        self._running = False
        self._sock = None

    # ---- 生命周期 ----------------------------------------------------

    def bind(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # 关掉 Nagle。数据库的请求-响应模式里，Nagle 和延迟 ACK 撞在一起
        # 能稳定制造 40ms 停顿——这是实测里最常见的"莫名其妙变慢"。
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.bind((self._host, self._port))
        sock.listen(128)
        sock.setblocking(False)
        self._sock = sock
        self.sel.register(sock, selectors.EVENT_READ, None)
        return self

    def serve_forever(self):
        self._running = True
        try:
            while self._running:
                self._tick_once()
        finally:
            self.shutdown()

    def shutdown(self):
        self._running = False
        for conn in list(self.conns):
            self._close(conn)
        if self._sock is not None:
            try:
                self.sel.unregister(self._sock)
            except (KeyError, ValueError):
                pass
            self._sock.close()
            self._sock = None
        self.sel.close()

    def _tick_once(self):
        for key, mask in self.sel.select(self.limits.TICK):
            conn = key.data
            if conn is None:
                self._accept()
                continue
            try:
                if mask & selectors.EVENT_READ:
                    self._on_readable(conn)
                if mask & selectors.EVENT_WRITE:
                    self._on_writable(conn)
            except OSError:
                self._close(conn)
            except ProtocolError:
                # 流已经不可信，就地恢复是错的，断开是唯一负责任的选择
                self.stats['errors'] += 1
                self._close(conn)
        self._housekeeping(time.monotonic())

    # ---- 连接 --------------------------------------------------------

    def _accept(self):
        while True:
            try:
                sock, addr = self._sock.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            if len(self.conns) >= self.limits.MAX_CONNS:
                # 超限直接关掉。排队等待只会让所有连接一起变慢。
                self.stats['rejected'] += 1
                try:
                    sock.close()
                except OSError:
                    pass
                continue
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setblocking(False)
            conn = Connection(sock, addr)
            self.conns.add(conn)
            self.stats['accepted'] += 1
            self.sel.register(sock, selectors.EVENT_READ, conn)

    def _close(self, conn):
        if conn.closing:
            return
        conn.closing = True
        self.conns.discard(conn)
        self.stats['closed'] += 1
        # **解绑，不删。** 会话得留着等客户端重连回来——删了就没得续了，
        # 客户端会拿到 SESSION_UNKNOWN 然后重发整批没确认的请求，而那些
        # 可能已经执行过。
        sess = conn.session
        if sess is not None and sess.conn is conn:
            sess.conn = None
            sess.last_seen = time.monotonic()
        conn.session = None
        try:
            self.sel.unregister(conn.sock)
        except (KeyError, ValueError):
            pass
        try:
            conn.sock.close()
        except OSError:
            pass

    # ---- 会话 --------------------------------------------------------

    def _new_sid(self):
        """随机会话号，避开 0（0 是"给我个新的"）和已占用的。"""
        while True:
            sid = random.getrandbits(64)
            if sid and sid not in self.sessions:
                return sid

    def _on_hello(self, conn, frame):
        """
        入会。负载 0 = 新会话；否则 = 续这个会话。

        续的时候老连接还活着就**挤掉它**：带着会话号来的新连接就是那个
        会话的主人（旧连接可能是半开的僵尸，也可能对端真的重启了）。
        让两条连接同时挂一个会话，等于让两份请求流共用一条水位——
        序号会互相踩。
        """
        p = frame.payload
        if len(p) != _SID.size:
            self._reply(conn, [err_frame(
                frame, 'HELLO 负载应为 %d 字节（会话号），实得 %d'
                       % (_SID.size, len(p)))])
            return
        want, = _SID.unpack(p)

        if want == 0:
            sess = Session(self._new_sid(), conn, time.monotonic(), self.limits)
            self.sessions[sess.sid] = sess
            self.stats['sessions'] += 1
        else:
            sess = self.sessions.get(want)
            if sess is None:
                # **不给他编一个新的。** 编了客户端以为续上了，会把没确认
                # 的请求重发一遍，而那些可能早就执行过了——只追加的引擎
                # 上就是静默多出来的行。
                self.stats['session_unknown'] += 1
                self._reply(conn, [err_frame(
                    frame, '会话 %016x 我不认识：要么它过期了，要么服务端'
                           '重启过（会话和重放表都在内存里，重启就没了）。'
                           '**不给你编一个新的**——编了你以为续上了，会把'
                           '没确认的请求重发一遍，而那些可能已经执行过。'
                           '要接着用就发 HELLO(0) 开一个全新的会话，'
                           '自己决定哪些请求值得重发。' % want,
                    protocol.SESSION_UNKNOWN)])
                return
            old = sess.conn
            if old is not None and old is not conn:
                self._close(old)          # _close 会把它从 sess 上解绑
            sess.conn = conn            # 新连接赢

        conn.session = sess
        sess.last_seen = time.monotonic()
        self._reply(conn, [Frame(Op.WELCOME, _SID.pack(sess.sid),
                                 Flag.RESPONSE, sess.sid, frame.seq)])

    def _interest(self, conn):
        events = 0
        # 背压的核心一行：暂停时不读，TCP 窗口自己会收缩
        if not conn.paused and not conn.closing:
            events |= selectors.EVENT_READ
        if conn.outbuf:
            events |= selectors.EVENT_WRITE
        try:
            self.sel.modify(conn.sock, events, conn)
        except (KeyError, ValueError):
            pass

    # ---- 收发 --------------------------------------------------------

    def _on_readable(self, conn):
        try:
            data = conn.sock.recv(self.limits.RECV_SIZE)
        except (BlockingIOError, InterruptedError):
            return
        except ConnectionResetError:
            self._close(conn)
            return

        if not data:
            # 对端正常关闭。注意：这不代表请求都处理完了，
            # 但协议层不做补偿——可重放性保证客户端能安全重发。
            self._close(conn)
            return

        conn.last_recv = time.monotonic()
        frames = conn.decoder.feed(data)  # 可能抛 ProtocolError，由上游断连
        for frame in frames:
            self._dispatch(conn, frame)

        if conn.decoder.buffered() > self.limits.STUCK_BUFFER:
            self._close(conn)

    def _on_writable(self, conn):
        if not conn.outbuf:
            conn.blocked_since = None
            self._interest(conn)
            return
        try:
            sent = conn.sock.send(conn.outbuf)
        except (BlockingIOError, InterruptedError):
            return
        except ConnectionResetError:
            self._close(conn)
            return

        if sent <= 0:
            return
        del conn.outbuf[:sent]

        if not conn.outbuf:
            conn.blocked_since = None
        if conn.paused and len(conn.outbuf) <= self.limits.LOW_WATER:
            conn.paused = False
            self._interest(conn)

    def _dispatch(self, conn, frame):
        if frame.opcode == Op.PONG:
            return
        if frame.opcode == Op.HELLO:
            self._on_hello(conn, frame)
            return
        if frame.opcode == Op.PING:
            self._reply(conn, [Frame(Op.PONG, flags=Flag.RESPONSE,
                                     session=frame.session, seq=frame.seq)])
            return

        # 心跳不在请求序号空间里：它是服务端发起的保活，不该占用序号、
        # 不该抬水位，也不该在重放表里占位置。
        heartbeat = bool(frame.flags & Flag.HEARTBEAT)

        sess = conn.session
        if sess is None:
            # 没入会就发请求。**必须拒。** 放过去的话它会带着会话号 0 走
            # 重放路径，而 0 是所有没握手的客户端共用的号码——正是那个
            # 让 B 的插入静默变成 A 的缓存的洞。
            self.stats['no_session'] += 1
            self._reply(conn, [err_frame(
                frame, '还没入会。先发一个 HELLO（负载 8 字节全零）拿会话号，'
                       '之后每个请求都要带上它', protocol.SESSION_UNKNOWN)])
            return
        if frame.session != sess.sid:
            # 连接绑的会话和帧里写的对不上。**不替它改会话号去执行**——
            # 那等于让任何人写个别的号就能往别人的会话里塞请求。
            self.stats['bad_session'] += 1
            self._reply(conn, [err_frame(
                frame, '这个帧带的会话号 %016x 和这条连接绑的 %016x 不是'
                       '一个。一条连接只服务一个会话' % (frame.session, sess.sid),
                protocol.BAD_SESSION)])
            return

        if not heartbeat:
            cached = sess.guard.get(frame.key)
            if cached is not None:
                # 重放的是**原样的帧串**，包括末片的 FINAL —— 收方拿到的
                # 东西和第一次一模一样，它分不出这是重放。
                self.stats['replayed'] += 1
                for out in cached:
                    self._enqueue(conn, out)
                return
            if frame.seq <= sess.answered:
                # **执行过了，答案却没留住。** 照原样重跑一遍，对只追加的
                # 引擎就是多写一个版本——静默多出来的一行。所以拒绝，
                # 并且把"该怎么办"一起说出来。
                self.stats['replay_lost'] += 1
                self._reply(conn, [err_frame(
                    frame, '序号 %d 已经执行过了，但那份答案没留住'
                           '（应答太大，或者被后来的请求挤出了重放表），'
                           '重放给不了你。**不替你重跑**：这个引擎只追加，'
                           '重跑一遍就是多写一行。先查一下它到底生效没有，'
                           '再决定要不要换个新序号重发' % frame.seq,
                    protocol.REPLAY_LOST)])
                return

        try:
            result = self._handler(frame, conn)
        except Exception as exc:           # noqa: BLE001 - 单请求失败不该断连
            self.stats['errors'] += 1
            self._reply(conn, [err_frame(frame, '%s: %s'
                                         % (type(exc).__name__, exc))])
            return

        sess.last_seen = time.monotonic()
        if result is None:
            return
        if isinstance(result, Frame):
            result = (result,)

        result = self._reply(conn, result)

        if not heartbeat:
            # 抬水位放在**答完之后**。抬早了，一条 handler 崩在中间的
            # 请求会被记成"答过了"，重发时被拒绝——而它其实从没生效。
            if frame.seq > sess.answered:
                sess.answered = frame.seq
            # **整串都留。** 这里以前是 `len(result) == 1 and not MORE`，
            # 于是 250 条语句的批量应答一帧都留不下，重发时整批重跑。
            # 留不下（太大）由 guard 返回 False 表示，而后果由上面的
            # 水位那条规矩兜住：明着拒绝，不悄悄重跑。
            sess.guard.put(frame.key, result)

    def _reply(self, conn, frames):
        """
        发一次应答，**末片盖上 FINAL**。

        这是收方判断"答完了"的唯一判据，而它必须在**协议层**盖：

          `MORE` 是**结果内**的分片标记，达不到这个目的——一批 250 条
          语句回的 250 帧里，一个带 MORE 的都没有（每个结果的末片都不
          带它）。客户端按 MORE 判就会在第一个结果之后停下，剩下 249 帧
          留在缓冲里，下一个请求读到的是上一个请求的答案，**而且一声
          不响**。

          字节层的 SCAN 分片同样只标 MORE 不标 FINAL。在传输层统一盖上，
          两扇门就都对了，各自不必记得这件事。

        重复盖是幂等的，语句层自己盖过也没关系。
        """
        frames = list(frames)
        if frames:
            frames[-1].flags |= Flag.FINAL
        for out in frames:
            self._enqueue(conn, out)
        return frames

    def _enqueue(self, conn, frame):
        if conn.closing:
            return
        conn.outbuf += frame.encode()
        if conn.blocked_since is None:
            conn.blocked_since = time.monotonic()
        if not conn.paused and len(conn.outbuf) >= self.limits.HIGH_WATER:
            conn.paused = True     # 停止读，把压力顶回对端
        self._interest(conn)

    def send(self, conn, frame):
        """供 handler 主动推送（订阅通知等）使用。"""
        self._enqueue(conn, frame)

    # ---- 巡检 --------------------------------------------------------

    def _housekeeping(self, now):
        for conn in list(self.conns):
            # 半开连接：发送缓冲里塞着东西，迟迟送不出去。
            # 对端进程已死时，TCP 不会主动告诉你，但写一定会失败或阻塞。
            if conn.blocked_since is not None and \
                    now - conn.blocked_since > self.limits.WRITE_TIMEOUT:
                self._close(conn)
                continue

            idle = now - conn.last_recv
            if idle > self.limits.DEAD_TIMEOUT:
                self._close(conn)
            elif idle > self.limits.IDLE_PING and \
                    now - conn.last_ping > self.limits.IDLE_PING:
                conn.last_ping = now
                self._enqueue(conn, Frame(Op.PING, flags=Flag.HEARTBEAT))

        self._reap_sessions(now)

    def _reap_sessions(self, now):
        """
        回收会话。**只回收空闲的**——还挂着连接的会话是在用的。

        空闲会话活到 SESSION_IDLE，是为了给"客户端进程整个重启了、
        过了一会儿才重连上"留出窗口。再长就没意义了：重放表占了内存，
        而客户端真要走这条路，也是拿旧会话号来试一次，试不到就开新的。
        """
        idle_cut = now - self.limits.SESSION_IDLE
        for sid, sess in list(self.sessions.items()):
            if sess.conn is None and sess.last_seen < idle_cut:
                del self.sessions[sid]

        if len(self.sessions) <= self.limits.MAX_SESSIONS:
            return
        # 超限：踢最久没动的空闲会话。踢不动的（全在用）就让它超——
        # 那说明并发连接真的开到了这个量级，硬掐反而是丢数据。
        free = sorted((s for s in self.sessions.values() if s.conn is None),
                      key=lambda s: s.last_seen)
        for sess in free[:len(self.sessions) - self.limits.MAX_SESSIONS]:
            del self.sessions[sess.sid]


class Client(object):
    """
    阻塞式客户端。不做事件循环——它的用途是对拍和套接层接入，
    不是承载并发。套接层要用时自己持有连接即可。

    **构造时就握手入会**：发 `HELLO(0)` 换一个随机会话号回来，之后每个
    帧都带上它。这不是仪式——没有会话号，"重连续传"就没有东西可以续
    （重放身份是 (会话, 序号, 操作码)，会话那一维空着，所有人的第一条
    请求都长得一样）。

    客户端记着**发出去还没收到答案**的请求。`resume()` 换一条新连接、
    用原会话号挂回去、把它们原样重发。
    """

    def __init__(self, host, port, timeout=5.0, session=None):
        self._host = host
        self._port = port
        self._timeout = timeout
        self.sock = None
        self.decoder = FrameDecoder()
        # 一个 recv 常常回来好几个帧。**解出来的帧必须先攒着**，不能
        # 只把第一个交出去——攒着的那部分丢一次就是永久丢，客户端会
        # 一直等一个永远不会再来的应答。套接层要建在这个类上面，
        # 静默丢帧是这里最不能有的东西。
        self._pending = deque()
        self._seq = 0
        self.session = 0
        # seq -> 原帧。断线重连时原样重发，**必须原样**：重放身份的
        # 负载那一维没算进去（见 Frame.key），改一个字节就是两条不同的
        # 请求，而服务端会按老答案答你。
        self._inflight = OrderedDict()
        # session= 是"挂到一个已有的会话上"：进程重启过、会话号存在别处，
        # 又知道那些没确认的请求是什么，就用它挂回去。认不出来会抛
        # ConnectionError——**不是悄悄给个新的**。
        self._connect(0 if session is None else session)

    def next_seq(self):
        self._seq += 1
        return self._seq

    def req(self, opcode, payload=b'', flags=Flag.REQUEST):
        """
        造一个属于本会话的请求帧。**别自己拼 Frame**——漏了会话号会被
        服务端按 BAD_SESSION 拒掉；自己编序号会让重放身份对不上原来的那条。
        """
        return Frame(opcode, payload, flags, self.session, self.next_seq())

    # ---- 连接与会话 --------------------------------------------------

    def _connect(self, want_sid):
        self.sock = socket.create_connection((self._host, self._port),
                                             self._timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(self._timeout)
        self.decoder = FrameDecoder()
        self._pending.clear()          # 老连接攒下的帧全作废

        self._raw_send(Frame(Op.HELLO, _SID.pack(want_sid), Flag.REQUEST))
        reply = self._raw_recv(self._timeout)
        if reply is None:
            raise ConnectionError('入会握手超时')
        if reply.opcode == Op.ERR:
            code, msg = err_text(reply)
            raise ConnectionError('入会被拒（码 %d）：%s' % (code, msg))
        if reply.opcode != Op.WELCOME or len(reply.payload) != _SID.size:
            raise ProtocolError('握手答非所问：%r' % (reply,))
        self.session = _SID.unpack(reply.payload)[0]
        return self.session

    def resume(self, timeout=None):
        """
        断线重连，挂回同一个会话，把**还没收到答案**的请求原样重发。

        返回 `[(seq, 帧串), ...]`，按发出次序。重发的请求要么拿回原答案，
        要么收到一帧 REPLAY_LOST 的拒绝——**不会被悄悄重跑一遍**。
        调用方拿到 ERR 帧时该看错误码，而不是看有没有异常。

        会话已经过期（服务端重启过 / 闲置太久）时抛 ConnectionError：
        这时"续"这件事根本不成立，**不能装作续上了**。要接着用，自己
        新建一个 Client，并且自己决定哪些请求值得重发。
        """
        timeout = self._timeout if timeout is None else timeout
        pending = list(self._inflight.items())
        sid = self.session
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            self._connect(sid)
        except ConnectionError:
            raise ConnectionError(
                '会话 %016x 续不上了（过期 / 服务端重启过）。**不假装续上了**：'
                '这些没确认的请求里有些可能已经执行过，重发会多写。'
                '自己核对之后再决定重发哪些。' % sid)
        out = []
        for seq, frame in pending:
            self._raw_send(frame)
            frames = self.recv_response(timeout)
            if frames is None:
                raise ConnectionError('重发 seq=%d 之后对端没了' % seq)
            self.ack(seq)
            out.append((seq, frames))
        return out

    # ---- 收发 --------------------------------------------------------

    def _raw_send(self, frame):
        self.sock.sendall(frame.encode())

    def _raw_recv(self, timeout):
        """收一个非心跳帧（内部用：不记账）。超时返回 None。"""
        if timeout is not None:
            self.sock.settimeout(timeout)
        while True:
            if self._pending:
                return self._pending.popleft()
            try:
                data = self.sock.recv(Limits.RECV_SIZE)
            except socket.timeout:
                return None
            if not data:
                raise ConnectionError('对端关闭')
            for frame in self.decoder.feed(data):
                if frame.opcode == Op.PING:
                    # 心跳要立刻回，不能排在应答后面等
                    self._raw_send(Frame(Op.PONG, flags=Flag.RESPONSE,
                                         session=frame.session, seq=frame.seq))
                elif frame.opcode != Op.PONG:
                    self._pending.append(frame)

    def send(self, frame):
        """发一个请求并记账。**记账是为了重连能重发。**"""
        if frame.opcode not in (Op.PING, Op.HELLO):
            self._inflight[frame.seq] = frame
        self._raw_send(frame)

    def ack(self, seq):
        """收到这个序号的应答了，不用再重发。"""
        self._inflight.pop(seq, None)

    def recv(self, timeout=None):
        """
        收一个非心跳帧。超时返回 None。

        先清空攒下的，再去读盘。**攒下的不清空就去读，会被后来的一批
        盖住顺序**——应答必须按到达次序交付，否则配对就错了。
        """
        return self._raw_recv(timeout)

    def recv_response(self, timeout=None):
        """
        收**一个请求**的完整应答帧串。

        规矩只有一条：**读到 FINAL 为止**；应答恒为单帧的操作码（见
        `_MULTI_FRAME`），那一帧本身就是终帧。

        不能按 `MORE` 判：它只是**结果内**的分片标记。一批 250 条语句回的
        250 帧里一个带 MORE 的都没有（每个结果的末片都不带它），按 MORE
        判就会在第一个结果之后停下，剩下 249 帧留在缓冲里——下一个请求
        读到的是上一个请求的答案，**而且一声不响**。

        收不齐（超时）返回 None。对端中途没了抛 ConnectionError。
        """
        frames = []
        while True:
            f = self.recv(timeout)
            if f is None:
                return None
            frames.append(f)
            if (f.flags & Flag.FINAL) or f.opcode not in _MULTI_FRAME:
                return frames

    def call(self, frame, timeout=None):
        """发一个请求，收它的第一帧应答，并销账。"""
        self.send(frame)
        reply = self.recv(timeout)
        if reply is not None:
            self.ack(frame.seq)
        return reply

    def call_response(self, frame, timeout=None):
        """发一个请求，收**完整**的应答帧串，并销账。"""
        self.send(frame)
        frames = self.recv_response(timeout)
        if frames is not None:
            self.ack(frame.seq)
        return frames

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def err_text(frame):
    """
    抠出 ERR 帧里的人话。负载是 '<码>\\t<话>'，见 err_frame。

    码和话是一起给的，因为**调用方经常两个都要**：话是给人看的，
    码是给判断用的（比如 REPLAY_LOST 是"别重发"，SESSION_UNKNOWN 是
    "得重新入会"，两者的处理完全相反）。
    """
    text = frame.payload.decode('utf-8', 'replace')
    code, sep, msg = text.partition('\t')
    if not sep:
        return 0, text
    return (int(code) if code.isdigit() else 0), msg
