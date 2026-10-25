# -*- coding: utf-8 -*-
"""
霸下内核协议 — 帧格式与编解码
=============================

设计取向：韧性优先，而非教科书式的完备性。

三条不可让步的性质：

  无状态    每个帧自包含（会话 ID + 请求序号 + 操作码 + 负载），
            接收方不需要任何跨帧状态就能解释它。有状态的东西
            （游标、事务上下文）全部上移到套接层，内核不背这个包袱。

  可重放    同一个 (会话 ID, 请求序号) 必须能重复执行并得到相同结果。
            断线重连后客户端原样重发即可续上，不需要保活会话。

  可重同步  流里出现无法解释的字节时，靠魔术字重新对齐，而不是直接掐断。
            垃圾数据和畸形包是常态，不是异常。

帧布局（32 字节定长头 + 变长负载，全部小端）：

  偏移   长度   字段
  ──────────────────────────────────────────────────────────
  0      4     魔术字 b'BAXI'     流中重同步的锚点
  4      1     版本
  5      1     标志位 Flag.*
  6      2     操作码 Op.*
  8      8     会话 ID
  16     8     请求序号
  24     4     负载长度            长度域是攻击面，见 MAX_PAYLOAD
  28     4     CRC32              覆盖 头[0:28] + 负载
  32     N     负载
"""

import struct
import zlib

__all__ = [
    'MAGIC', 'VERSION', 'HEADER_SIZE', 'MAX_PAYLOAD',
    'Flag', 'Op', 'Frame', 'FrameDecoder',
    'ProtocolError', 'FrameTooLarge', 'FrameCorrupt',
]

MAGIC = b'BAXI'
VERSION = 1
HEADER_SIZE = 32
_CRC_OFFSET = 28

# 负载长度硬上限。
# 长度域是整条协议里唯一能被外部直接控制的分配尺寸：任何声称超限的帧
# 都判死，不进解析、不预分配。否则一个 32 字节的头部就能让服务端
# 去 malloc 4GB。
MAX_PAYLOAD = 16 * 1024 * 1024

_HEADER = struct.Struct('<4sBBHQQII')
_CRC = struct.Struct('<I')

assert _HEADER.size == HEADER_SIZE


class ProtocolError(Exception):
    """帧不可解释，且无法就地恢复。仅在必须断连时抛出。"""


class FrameTooLarge(ProtocolError):
    """声称的负载长度越过硬上限。"""


class FrameCorrupt(ProtocolError):
    """帧内容自相矛盾（长度对不上、校验不过）。"""


class Flag(object):
    """标志位。刻意保持极少——标志位一多，组合语义就没人能保证。"""
    REQUEST = 0x01
    RESPONSE = 0x02
    HEARTBEAT = 0x04
    MORE = 0x08      # 结果集还有后续分片
    FINAL = 0x10     # 末片
    RESUME = 0x20    # 断线续传：请求序号与之前某次重复，按重放处理


class Op(object):
    """操作码。少而准——多了没人记得住，也没人会去查。"""
    PING = 0x01
    PONG = 0x02

    # 入会握手。**这两帧存在的唯一理由是"重连续传"。**
    #
    # 重放身份是 (会话, 序号, 操作码)，而会话号必须**在连接之上**唯一：
    # 会话活在连接之外（连接断了会话还在），否则"重连"这件事根本没有
    # 一个东西可以续。所以会话号不能由连接推出来，得有人分配——
    # 由**服务端**分配，而且是随机的。
    #
    # 为什么是随机而不是自增：会话号是**持有的凭证**。自增的话，任何
    # 能连上的人写个 1 就能接管别人的会话，把它没确认过的请求连答案
    # 一起读走。随机 64 位猜不到，这条洞就不存在了。
    HELLO = 0x03     # 客户端：我要入会（负载 0 = 给个新的，否则 = 续这个）
    WELCOME = 0x04   # 服务端：会话号给你（负载 8 字节）
    # 身份。**它是控制段的第五个号，不是语句段加了一档** —— 这两个号管
    # 的根本不是一件事：那个是"这条语句要干什么"，这个是"这话是谁说的"。
    #
    # 为什么身份要单独一帧、而不是塞进 SQL 负载里（那个做法看着省事）：
    #
    #  一、**安全上它就是错的。** 负载里的身份是客户端自己说的一句话，
    #      任何能连上端口的人把它写成管理员就完事——表级权限在远程那条
    #      路上等于零。身份必须由服务端从**会话**里取。
    #  二、**传输上它也是错的，而且更阴。** SQL 负载的头 2 字节就是语句
    #      数；"在前面加一截身份"的话，身份为空时字节数恰好对齐，旧解码
    #      器会按错位的边界解出若干条"看起来很正常"的语句，**照常执行**，
    #      一声不响。老对端对这个新字段没有任何可以响的检查点。
    #
    # 换成新号就不一样了：旧服务端撞上它会走 `_GATE_OF_OP.get(op) -> None`
    # 那条路，回一句 `UNSUPPORTED`，在第一条 SQL 之前就拒掉了。
    LOGIN = 0x05     # 客户端：我是谁（用户名 + 口令）；服务端：认不认

    PUT = 0x10       # 直存
    GET = 0x11       # 直取
    SCAN = 0x12      # 区域扫描
    BATCH = 0x14     # 一批直存，一个帧一次落盘

    # 0x13 空着。它以前是 DEL，现在没有了——**删除不是字节层的概念**。
    # 只要这条协议还认得"删除"，实现就必然要对值的表示做假设，而那个
    # 假设会漏到盘上（旧实现写的空值墓碑，读回来和"值就是空串"分不开）。
    # 要删一个键，由上层 put 一个它自己定义的墓碑值。号留着不复用：
    # 老客户端打过来必须拿到"不认识"，而不是一个被曲解了的答案。

    # 语法层（见 syntax.py）。**另一扇门**，不是上面那些的替代——
    # 内核那条路是「帧 -> 存储动作」，这条是「帧 -> SQL -> 存储动作」。
    # 两扇门可以开在同一个端口上，共用同一个存储区。
    #
    # 分了三段号是故意的：0x0x 是控制、0x1x 是字节层、0x2x 是语句层。
    # 混着编的话，看到 0x17 就得翻遍全表才知道它属于谁。
    SQL = 0x20       # 一条语句：文本 + 参数
    SQL_BATCH = 0x21 # 一批语句，一个帧一次落盘

    OK = 0x80
    ERR = 0x81
    DATA = 0x82      # 字节层的应答体（GET / SCAN / BATCH）
    ROWS = 0x83      # 语句层的结果集

    _NAMES = {
        0x01: 'PING', 0x02: 'PONG', 0x03: 'HELLO', 0x04: 'WELCOME',
        0x05: 'LOGIN',
        0x10: 'PUT', 0x11: 'GET', 0x12: 'SCAN', 0x14: 'BATCH',
        0x20: 'SQL', 0x21: 'SQL_BATCH',
        0x80: 'OK', 0x81: 'ERR', 0x82: 'DATA', 0x83: 'ROWS',
    }

    @classmethod
    def name(cls, op):
        return cls._NAMES.get(op, 'OP_%02X' % op)


# 会话层（握手 / 重连续传）用的错误码。
#
# 和 `kernel.Code` 那几个**在同一个号码空间里**——它们都会出现在 ERR 帧
# 的负载头上，对端是同一个人。分两个类是因为归属不同（这个归协议，
# 那个归字节层），号码不重叠是硬要求：认得 3 的客户端不能拿到一个
# 意思是别的东西的 3。
SESSION_UNKNOWN = 5   # 不认识这个会话号（过期了 / 服务端重启过）
REPLAY_LOST = 6       # 这个序号已经执行过，但答案没留住，不给重放
BAD_SESSION = 7       # 帧里的会话号和这条连接绑的不是一个
# 用户名或口令不对。**两者共用一个码**：分开的话，那句报错就成了一个
# "这个用户名存在"的探针——想试用户名的人只要看报的是哪一句。
BAD_LOGIN = 8


class Frame(object):
    """一个自包含的协议帧。"""

    __slots__ = ('version', 'flags', 'opcode', 'session', 'seq', 'payload')

    def __init__(self, opcode, payload=b'', flags=0, session=0, seq=0,
                 version=VERSION):
        self.version = version
        self.flags = flags
        self.opcode = opcode
        self.session = session
        self.seq = seq
        self.payload = payload if isinstance(payload, bytes) else bytes(payload)

    @property
    def key(self):
        """
        重放身份。同一 (会话, 序号, 操作码) 的重复到达必须得到同一结果。

        **操作码必须算进去。** 只认 (会话, 序号) 的话，客户端复用了一个
        序号发另一种操作——这太容易发生了，序号计数器重置、并发协程
        各发各的，都会撞上——就会命中上一条的缓存应答，拿到一个跟这
        次请求毫不相干的答案。而且它一声不响：应答长得完全像真的。
        带上操作码，这种撞车至少得操作码也相同才成立。

        **负载本身没有算进来，这是一个已知边界。** 会话 + 序号 + 操作码
        三者全同、负载却不同，仍然会拿到旧答案。要做到万无一失得把
        负载也纳入身份，代价是每个请求多跑一遍 CRC32——解码时算的那
        一遍是为了校验，没法复用（要同时得到"头的 CRC"和"负载的 CRC"
        必然要多扫一遍）。为一个客户端违反约定的情形付这个钱不值。
        所以约定摆在这里：**序号在会话内按操作码各自唯一**。重发必须
        原样重发。
        """
        return (self.session, self.seq, self.opcode)

    def is_request(self):
        return bool(self.flags & Flag.REQUEST)

    def encode(self):
        head = _HEADER.pack(
            MAGIC, self.version, self.flags, self.opcode,
            self.session, self.seq, len(self.payload), 0,
        )
        crc = zlib.crc32(head[:_CRC_OFFSET])
        crc = zlib.crc32(self.payload, crc)
        return head[:_CRC_OFFSET] + _CRC.pack(crc) + self.payload

    @classmethod
    def decode(cls, head, payload):
        """头必须是已验证过魔术字的 32 字节。校验不过则抛 FrameCorrupt。"""
        magic, ver, flags, op, sess, seq, plen, crc = _HEADER.unpack(head)
        if magic != MAGIC:
            raise FrameCorrupt('魔术字不符')
        if plen != len(payload):
            raise FrameCorrupt('长度域与负载不符: 声称 %d, 实得 %d'
                               % (plen, len(payload)))
        calc = zlib.crc32(head[:_CRC_OFFSET])
        calc = zlib.crc32(payload, calc)
        if calc != crc:
            raise FrameCorrupt('CRC32 校验失败')
        return cls(op, payload, flags, sess, seq, ver)

    def __repr__(self):
        return '<Frame %s flags=%02x sess=%d seq=%d len=%d>' % (
            Op.name(self.opcode), self.flags, self.session, self.seq,
            len(self.payload))


class FrameDecoder(object):
    """
    字节流 -> 帧序列。负责三件现实中天天发生、教科书里算异常的事：

      粘包   一次 recv 回来好几个帧
      半包   一个帧跨了好几次 recv
      脏流   流里混着无法解释的字节（协议错位、中间设备插东西、对端发疯）

    前两件靠"长度读满才交付"解决，第三件靠魔术字重同步解决。
    本类不做 I/O，也不持有 socket——纯状态机，方便单独灌数据验证。
    """

    __slots__ = ('_buf', '_pos', '_max_payload', 'resyncs', 'corrupts', 'frames')

    def __init__(self, max_payload=MAX_PAYLOAD):
        self._buf = bytearray()
        self._pos = 0
        self._max_payload = max_payload
        self.resyncs = 0     # 重同步次数：反映链路质量，值得监控
        self.corrupts = 0    # 校验失败次数
        self.frames = 0

    def feed(self, data):
        """喂入任意切分的字节，返回本次能凑齐的帧列表（可能为空）。"""
        # 已消费的字节攒到下一轮开头一次性丢弃，避免每帧都 memmove
        if self._pos:
            del self._buf[:self._pos]
            self._pos = 0
        if data:
            self._buf += data

        out = []
        while True:
            avail = len(self._buf) - self._pos
            if avail < HEADER_SIZE:
                break

            start = self._pos
            if self._buf[start:start + 4] != MAGIC:
                if not self._resync():
                    break
                continue

            magic, ver, flags, op, sess, seq, plen, crc = \
                _HEADER.unpack_from(self._buf, start)

            # 长度域超限：这个流已经不可信，断连是唯一负责任的选择。
            # 就地重同步在这里是错的——说明对端根本不是按本协议说话。
            if plen > self._max_payload:
                raise FrameTooLarge('声称负载 %d 字节，超过上限 %d'
                                    % (plen, self._max_payload))

            if avail < HEADER_SIZE + plen:
                break  # 半包：头到了、体没到，等下一轮

            end = start + HEADER_SIZE + plen
            head = bytes(self._buf[start:start + HEADER_SIZE])
            payload = bytes(self._buf[start + HEADER_SIZE:end])

            try:
                frame = Frame.decode(head, payload)
            except FrameCorrupt:
                # 校验不过说明长度域不可信，不能信它去跳过 plen 字节。
                # 退到魔术字之后重新对齐。
                self.corrupts += 1
                self._pos = start + 4
                self._resync()
                continue

            self._pos = end
            self.frames += 1
            out.append(frame)

        return out

    def _resync(self):
        """从当前位置往后找魔术字。返回是否找到对齐点。"""
        idx = self._buf.find(MAGIC, self._pos + 1)
        if idx < 0:
            # 整个缓冲里没有魔术字。保留尾部 3 字节——魔术字可能正好
            # 被切在两次 recv 之间，丢掉就会永久错位。
            keep = len(self._buf) - 3
            self._pos = keep if keep > self._pos else self._pos
            return False
        self._pos = idx
        self.resyncs += 1
        return True

    def buffered(self):
        """尚未消费的字节数。持续增长说明对端在灌半包垃圾，值得断开。"""
        return len(self._buf) - self._pos
