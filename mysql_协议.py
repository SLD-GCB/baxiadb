# -*- coding: utf-8 -*-
"""
mysql_协议.py — MySQL 服务端线协议，**纯字节层**。

这一层只认字节。它不知道 SQL，不知道霸下，也不知道「库」是什么。上面那层
（`mysql_翻译`）把 MySQL 的语句翻成霸下的语句，再往上是 `mysql_门` 管连接。

**为什么要自己写一遍**
======================

爬虫（`taowu-v2.1`，约 4.5 万行）现成有一条 `out_mysql` 入库腿，走 pymysql
连 MySQL。要让**爬虫一个字不改**地把数据写进霸下，就得在霸下这头装成一个
MySQL 服务端——而 MySQL 服务端是不能靠 pip 装的（那是客户端库）。

所以这个文件是**一遍最小的 MySQL 服务端**，够 pymysql 2.2.8 用就行。
每个字节都是对着 `D:\\python13.3\\Lib\\site-packages\\pymysql\\` 的源码核过
的，核到哪一行就写在注释里。**换 pymysql 版本时要照着那些注释重核一遍。**

三条最容易错的事
================

**一、包序号（sequence id）只有一处维护。**

MySQL 的每个包头上带一个字节的序号，pymysql 会**严格校验**
（`connections.py:777`）：

    if packet_number != self._next_seq_id:
        self._force_close()
        raise err.InternalError("Packet sequence number wrong - got %d expected %d")

错一次连接当场断，而报的错和真因毫不相干。所以序号**只由 `报文流` 一处
维护**，外面一个包都不许绕过去手写。规则只有两条：

    读完对端一个包（序号 N）之后： self._序号 = (N + 1) & 0xFF
    写包：用 self._序号，写完后  self._序号 = (self._序号 + 1) & 0xFF

pymysql 那边是同一条规则，所以两处对得上：

    握手期  服务端发 Handshake(0) → 客户端回 1 → 服务端读到 1，置 2 → 发 OK(2)
    命令期  pymysql 命令包头**硬编 0**（`connections.py:880`），写完置 1
            → 服务端读到 0，置 1 → 第一个应答就是 1

**二、能力位既不能少置，也不能多置。** 见 `能力` 那一段，每一位都写了理由。

**三、≥16MB 要分包，而且发的时候要补尾包。** 见 `报文流.发` 那段——不补的话
客户端会**一直等下去**（表现为卡死，不是报错），这是最难查的一种症状。

⛔ 这个文件里没有一处读口令
==========================

客户端的口令在协议上是以 SHA1 挑战应答的形式过来的，**这一层拿不到明文**。
真正拿到明文的地方在 `mysql_门` 那一步「换插件」（见 `编换插件`）。
"""

import os
import socket
import struct

__all__ = [
    '协议错', '读超时', 'MAX_PAYLOAD', '能力', '状态', '字段类型',
    '编长度', '读长度', '报文流',
    '编OK', '编ERR', '编EOF',
    '编握手', '编换插件', '解握手响应',
    '编列定义', '编行', '写结果集',
]


class 协议错(Exception):
    """对端发来的字节不合规矩。**这个错要变成 ERR 包回过去**，不要往上抛。"""


class 读超时(协议错):
    """
    socket 上等久了，一个字节都没来。

    ⛔ **这不是"连接断了"，两件事必须分得开。** 空闲连接等超时是常态
    （爬虫采集一轮可能要几分钟），把它和"对端关了 socket"报成同一个错，
    运维看到的就全是"断线"，而真正断的那条混在里面看不出来。

    继承 `协议错` 是为了让"凡是收不到了"的兜底代码照样接得住它，
    需要区分的调用方单独接这个类。
    """


# ══ 一、常数 ═══════════════════════════════════════════════════════════

#: 一个包里最多装多少字节负载。包头是 3 字节长度，所以上限就是 2^24-1。
MAX_PAYLOAD = 0xFFFFFF


class 能力(object):
    """
    服务端握手包里的能力位（capability flags）。

    **我们报的恰好是低 16 位 `0xA20D` + 高 16 位 `0x002A`，
    合起来是 32 位的 `0x002AA20D`，一个字都不多。**
    不是"多报几个也无所谓"——每一位都有后果，多置的那一位要么让客户端走
    我们没实现的路，要么直接变成一个洞：

    ⚠️ **别把这两个半字念反了。** 报文里 `cap_low`（2 字节）在前、`cap_high`
    （2 字节）在后，pymysql 那边是
    `capabilities = 低; capabilities |= 高 << 16`
    （`connections.py` 的 `_get_server_information`）——**低半字在低位**。
    所以"低 0xA20D + 高 0x002A"写成一个数就是 `0x002AA20D`，不是
    `0xA20D002A`（那个写法是拿"高在前"的顺序念出来的，数值上正好错反）。
    拿 pymysql 自己的 `CAPABILITIES = 0x3aa205` 对一下就知道方向：
    它的低半字是 `0xA205`（LONG_PASSWORD|LONG_FLAG|PROTOCOL_41|TRANSACTIONS|
    SECURE_CONNECTION），高半字是 `0x3A`——低半字确实在低位。

    下面这张表里每一位后面跟的 `1<<n` 就是**这一个数**里的位号：

    ── 置的 ────────────────────────────────────────────────────────
      LONG_PASSWORD               1<<0     老客户端的行为开关，置了无害
      LONG_FLAG                   1<<2     同上
      CONNECT_WITH_DB             1<<3     **要**：客户端才会把 database 发过来，
                                         而库名是适配层的路由键（见 mysql_门）
      PROTOCOL_41                 1<<9     **要**：41 版报文格式
      TRANSACTIONS                1<<13    **要**：`SET AUTOCOMMIT` / `COMMIT`
                                         这些语句要有名分
      SECURE_CONNECTION           1<<15    **要**：挑战应答的长度前缀格式
      MULTI_RESULTS               1<<17    置了才像真 MySQL（pymysql 自己按
                                         `server_version >= 5` 也会加）
      PLUGIN_AUTH                 1<<19    **要**：否则握手响应里没有插件名那一段，
                                         `_auth_plugin_name` 会停在 `""`
      PLUGIN_AUTH_LENENC_CLIENT_DATA  1<<21  **要**：auth 响应走 lenenc 前缀，
                                         这是 pymysql 的首选分支，解析最规整

    ── **必须不置**的 ──────────────────────────────────────────────
      COMPRESS                    1<<5     置了客户端可能发压缩包，我们没实现解压
      LOCAL_FILES                 1<<7     ⛔ 置了客户端可能走 `LOAD DATA LOCAL`。
                                         `MySQLResult.read()` 见到 0xFB 开头的包
                                         就会去读本地文件——**那是一条从服务端
                                         读客户端文件的通道**，绝不能开
      SSL                         1<<11    置了 + 客户端 ssl 参数非空会走 TLS 升级。
                                         不置 ⇒ 客户端不会升级，明文口令那条
                                         通道才归我们控制（见 `编换插件`）
      MULTI_STATEMENTS            1<<16    不置，客户端就不会一次发多条；爬虫也
                                         从不发多条
      CONNECT_ATTRS               1<<20    **不置**：握手响应里就少一段 lenenc
                                         属性块要跳，解析更死板
      DEPRECATE_EOF               1<<24    ⛔ **绝不置**：pymysql 的
                                         `_check_packet_is_eof` 写死认 EOF 包
                                         （源码里那句 `# TODO: Support
                                         CLIENT.DEPRECATE_EOF`），置了会在
                                         结果集末尾把行当 EOF 或者反过来
      SESSION_TRACK               1<<23    不置
      PS_MULTI_RESULTS            1<<18    不用预处理语句
      HANDLE_EXPIRED_PASSWORDS    1<<22    不置
    """

    低 = 0xA20D
    高 = 0x002A
    全 = 低 | (高 << 16)          # 0x002AA20D —— 低半字在**低位**


class 状态(object):
    """
    OK / EOF 包里的 `server_status` 位。

    **`NO_BACKSLASH_ESCAPES`（1<<9 = 512）永远不置。** 这条是隐形的坑：
    `Connection.escape_string` 会读我们发出去的 status 决定转义风格
    （`connections.py:552`），置了它 pymysql 就会从反斜杠转义切成 `''` 双写。
    **我们只要一种方言**——反斜杠，那也是爬虫实际发出的那一种。

    `MORE_RESULTS`（1<<3 = 8）也永远不置，置了 pymysql 会去要下一批结果。
    """

    AUTOCOMMIT = 0x0002
    MORE_RESULTS = 0x0008
    NO_BACKSLASH_ESCAPES = 0x0200


class 字段类型(object):
    """
    Column Definition 里那个 `type` 字节。只列我们真会发的几个，
    **认不出的一律不发**——发一个没核过的类型码，客户端会拿一个我们
    没验证过的转换器去解。
    """

    LONG = 3           # INT  → pymysql 解成 int
    DOUBLE = 5         # 实数 → float
    DATETIME = 12      # 时间 → datetime
    BLOB = 252         # 二进制 → bytes
    STRING = 254       # 文本 → str（**前提是 charsetnr 不是 63**，见 列定义）


# ══ 二、长度前缀（length-encoded） ═════════════════════════════════════

def 编长度(n):
    """
    把一个非负整数编成 MySQL 的 length-encoded integer。

        0 .. 250        1 字节
        251..65535      0xFC + 2 字节
        65536..2^24-1   0xFD + 3 字节
        其余            0xFE + 8 字节

    **251（0xFB）不能用来表示数字**——它在行里是 NULL 的记号
    （`read_length_encoded_integer` 见到 251 直接返回 `None`，不再往下读）。
    """
    if n < 0:
        raise 协议错('长度不能是负数：%r' % (n,))
    if n < 251:
        return struct.pack('<B', n)
    if n <= 0xFFFF:
        return b'\xfc' + struct.pack('<H', n)
    if n <= 0xFFFFFF:
        return b'\xfd' + struct.pack('<I', n)[:3]
    return b'\xfe' + struct.pack('<Q', n)


def 读长度(buf, pos=0):
    """
    从 `buf[pos:]` 读一个 length-encoded integer，回 `(值, 新位置)`。

    251 → `None`（NULL）。**这是 NULL 唯一的表现形式**，调用方必须能接住
    `None`，不能当成 0。
    """
    if pos >= len(buf):
        raise 协议错('长度前缀读到末尾了')
    c = buf[pos]
    pos += 1
    if c < 251:
        return c, pos
    if c == 251:
        return None, pos
    if c == 252:
        if pos + 2 > len(buf):
            raise 协议错('2 字节长度前缀不够读')
        return struct.unpack_from('<H', buf, pos)[0], pos + 2
    if c == 253:
        if pos + 3 > len(buf):
            raise 协议错('3 字节长度前缀不够读')
        return struct.unpack('<I', buf[pos:pos + 3] + b'\x00')[0], pos + 3
    if c == 254:
        if pos + 8 > len(buf):
            raise 协议错('8 字节长度前缀不够读')
        return struct.unpack_from('<Q', buf, pos)[0], pos + 8
    raise 协议错('认不出的长度前缀第一个字节：%#x' % c)


def _编串(b):
    """长度前缀 + 原字节。"""
    return 编长度(len(b)) + b


def _读串(buf, pos):
    """读一个长度前缀串，回 `(bytes, 新位置)`。NULL 当成空串。"""
    n, pos = 读长度(buf, pos)
    if n is None:
        return b'', pos
    if pos + n > len(buf):
        raise 协议错('串长 %d，缓冲只剩 %d 字节' % (n, len(buf) - pos))
    return buf[pos:pos + n], pos + n


def _读零结尾(buf, pos):
    """
    读一个 NUL 结尾的串（用户名、库名、插件名都是这个形状），
    回 `(bytes, 新位置)`。**末尾没有 NUL 就抛**——猜一个边界出来，
    错的是身份，而身份错了从外面完全看不出来。
    """
    i = buf.find(b'\x00', pos)
    if i < 0:
        raise 协议错('这个串没有 NUL 结尾（从偏移 %d 起）' % pos)
    return buf[pos:i], i + 1


# ══ 三、报文流：**包序号只在这里维护** ═══════════════════════════════

class 报文流(object):
    """
    一条 MySQL 连接上的字节流。

    **它独占两件事**：socket 上的收发，和那一个包序号。外面拿到 `发`/`收`
    就行，不要去数序号——`connections.py:777` 那处校验会让数错的那一次
    以一个完全指不到点子的错断掉。
    """

    def __init__(self, sock):
        self._sock = sock
        self._序号 = 0
        self.收到字节数 = 0
        self.发出字节数 = 0

    # ── 序号 ────────────────────────────────────────────────────────

    @property
    def 序号(self):
        return self._序号

    def 重置序号(self, n=0):
        """命令期的开头。**pymysql 的命令包头硬编 0**，读到之后序号自然到 1。"""
        self._序号 = n & 0xFF

    def 收一条命令(self):
        """
        收**一条**命令，序号从 0 重新数。**命令期只该用这一个入口。**

        ⛔ **每一条命令都从 0 重新数，不是一条连到底。** 这一条是踩出来的：
        命令期如果只在一开始 `重置序号(0)` 一次、然后连着收，第二条命令就会
        以"包序号不对"断掉——因为客户端那边**每条命令都重新置 0**
        （`connections.py:880-888`，那句是

            prelude = struct.pack("<iB", packet_size, command)
            ...
            self._next_seq_id = 1

        拿 `packet_size` 当 4 字节小端整数写下去，而 `packet_size` 永远
        小于 2^24，所以第四个字节恒为 0——**那个 0 就是序号本身**，
        紧跟的第五个字节才是命令号。序号写在长度字段的高位上，
        这也是为什么"每条命令都是 0"。）

        所以把"置 0"和"收"**做成一次调用**，中间不给任何机会插进别的读。
        分成两步写（先 `重置序号` 再 `收`）能对，但下一个读很容易漏掉重置，
        而漏掉的表现是第二条命令就断线。

        收完之后序号自然停在 1，正是服务端第一个应答该用的号。
        """
        self._序号 = 0
        return self.收()

    # ── 收 ──────────────────────────────────────────────────────────

    def _收够(self, n):
        buf = bytearray()
        while len(buf) < n:
            try:
                片 = self._sock.recv(n - len(buf))
            except socket.timeout as exc:
                # **等超时 ≠ 断了。** 见 `读超时` 那段:把这两件事报成同一个错，
                # 空闲连接多的时候运维看到的就是一片"断线"，真断的那条淹在里面。
                raise 读超时('等了 %s 秒没收到字节（连接还开着）' % exc)
            except OSError as exc:
                raise 协议错('连接断了：%s' % exc)
            if not 片:
                raise 协议错('连接被对端关了')
            buf += 片
        self.收到字节数 += n
        return bytes(buf)

    def 收(self):
        """
        收一个**完整的**负载。

        **≥16MB 的分片在这里拼掉**：客户端发一条超长语句时是切成若干个
        0xFFFFFF 的满片，最后跟一个不足满片的收尾
        （`connections.py:880-899` 就是手拼这个头）。序号逐片 +1。

        读完（含收尾片）之后，序号推到"对端下一片该用的号"。
        """
        整个 = bytearray()
        while True:
            头 = self._收够(4)
            长度 = 头[0] | (头[1] << 8) | (头[2] << 16)
            号 = 头[3]
            if 号 != self._序号:
                raise 协议错('包序号不对：收到 %d，应该是 %d'
                             % (号, self._序号))
            self._序号 = (self._序号 + 1) & 0xFF
            if 长度:
                整个 += self._收够(长度)
            # 满片要接着读；不满的（含收尾那个 0 长度包）就是结尾。
            if 长度 < MAX_PAYLOAD:
                return bytes(整个)

    # ── 发 ──────────────────────────────────────────────────────────

    def 发(self, 负载=b''):
        """
        发一个负载，自动切分片。

        ⛔ **总长正好是 0xFFFFFF 的整数倍时，最后必须再补一个 0 长度包。**
        pymysql 那边读完一片是先看长度：

            if bytes_to_read < MAX_PACKET_LEN:
                break

        它靠"这一片不满"来判断结束。少补那个空包，客户端**会一直等下去**
        ——症状是卡死，不是报错，这是最难查的一种。
        """
        剩 = 负载
        while True:
            片 = 剩[:MAX_PAYLOAD]
            剩 = 剩[MAX_PAYLOAD:]
            self._写一个(片)
            if len(片) < MAX_PAYLOAD:
                return            # 这一片不满，就是结尾了
            if not 剩:
                self._写一个(b'')  # 正好整数倍，补一个空包收尾
                return

    def _写一个(self, 片):
        n = len(片)
        if n > MAX_PAYLOAD:
            raise 协议错('一个包最多 %d 字节，给了 %d' % (MAX_PAYLOAD, n))
        头 = struct.pack('<I', n)[:3] + struct.pack('<B', self._序号)
        self._序号 = (self._序号 + 1) & 0xFF
        try:
            self._sock.sendall(头 + 片)
        except socket.timeout as exc:
            # 和收那边同一个道理：发超时是**对端收得慢**（发送缓冲满了），
            # 连接还活着，别报成断了。
            raise 读超时('发了 %s 秒没发完（对端收得慢，连接还开着）' % exc)
        except OSError as exc:
            raise 协议错('发不出去：%s' % exc)
        self.发出字节数 += 4 + n

    def 关(self):
        try:
            self._sock.close()
        except OSError:
            pass


# ══ 四、三个通用报文 ═══════════════════════════════════════════════════

def 编OK(affected=0, insert_id=0, status=状态.AUTOCOMMIT, warnings=0, 文案=b''):
    """
    OK 报文。

    ⛔ **必须 ≥7 字节**：pymysql 是这么判的（`protocol.py:246`）

        return self._data[0] == 0 and len(self._data) >= 7

    短一个字节它就不认，然后往下当别的东西解。所以 status 和 warnings
    那两个 uint16 一个都不能省，哪怕都是 0。
    """
    出 = b'\x00' + 编长度(affected) + 编长度(insert_id)
    出 += struct.pack('<HH', status, warnings)
    return 出 + 文案


def 编ERR(码, 文案, sqlstate='HY000', status=状态.AUTOCOMMIT):
    """
    ERR 报文。

    ⛔ **第 4 个字节那个 `#`（0x23）不能省**：pymysql 的
    `err.raise_mysql_exception` 靠它认出 sqlstate 那一段（`err.py:146`）。

    霸下那些中文报错**原样塞进去**——pymysql 用 `decode("utf-8","replace")`
    解，爬虫的 `db_operate` 会把它写进日志，人看得懂。
    """
    if not isinstance(文案, bytes):
        文案 = str(文案).encode('utf-8', 'replace')
    if len(sqlstate) != 5:
        sqlstate = 'HY000'
    出 = b'\xff' + struct.pack('<H', int(码)) + b'\x23'
    出 += sqlstate.encode('ascii') + 文案
    return 出


def 编EOF(status=状态.AUTOCOMMIT, warnings=0):
    """
    EOF 报文。

    ⛔ **必须 <9 字节**：pymysql 是这么判的（`protocol.py:252`）

        return self._data[0] == 0xFE and len(self._data) < 9

    这一条和"0xFE 也可能是 length-encoded 的开头"是同一个判断的两面，
    所以**绝不能往里塞别的东西**。固定 5 字节，一个不多一个不少。
    """
    return b'\xfe' + struct.pack('<HH', warnings, status)


# ══ 五、握手 ═══════════════════════════════════════════════════════════

def 编握手(连接号, salt=None):
    """
    Handshake V10。**服务端先发，序号 0。**

    字段次序**必须**和 pymysql 的 `_get_server_information` 逐字节对齐——
    那边是按偏移硬读的，不是按名字找的：

        data[i]                     协议版本 0x0A
        NUL 结尾                    server_version   ← **必须以数字开头**
        4 字节                      thread_id
        8 字节                      salt 前半
        1 字节 0x00                 filler（那边注明 `i += 9  # 8 + 1(filler)`）
        2 字节                      能力位低 16 位
        1 字节                      charset
        2 字节                      status
        2 字节                      能力位高 16 位
        1 字节                      auth_plugin_data_len
        10 字节 全 0                reserved（那边注明 `i += 10`）
        12 字节                     salt 后半
        1 字节 0x00                 （那边注明 `i += 1`）
        NUL 结尾                    插件名

    ⛔ **`server_version` 必须以数字开头。** `connections.py:904` 那句是

        if int(self.server_version.split(".", 1)[0]) >= 5:

    写个 `"霸下-1.0"` 上来就 `ValueError`，而且是在客户端里炸的。

    salt 一共 20 字节（8+12），正好是 MySQL 挑战应答的长度：
    那边算的是 `salt_len = max(12, auth_plugin_data_len - 9)`。
    """
    if salt is None:
        salt = os.urandom(20)
    salt = (salt + b'\x00' * 20)[:20]

    出 = b'\x0a'
    出 += b'8.0.32-baxia\x00'
    出 += struct.pack('<I', int(连接号) & 0xFFFFFFFF)
    出 += salt[:8]
    出 += b'\x00'
    出 += struct.pack('<H', 能力.低)
    出 += struct.pack('<B', 45)          # utf8mb4_general_ci（charset_by_name 实测 45）
    出 += struct.pack('<H', 状态.AUTOCOMMIT)
    出 += struct.pack('<H', 能力.高)
    出 += struct.pack('<B', 21)          # auth_plugin_data_len
    出 += b'\x00' * 10
    出 += salt[8:]
    出 += b'\x00'
    出 += b'mysql_native_password\x00'
    return 出


#: 我们真正想用的那个插件。**它能把客户端输入的明文口令要过来**，
#: 详见 `编换插件`。
明文插件名 = b'mysql_clear_password'


def 编换插件(salt=None):
    """
    AuthSwitchRequest —— **本设计最要紧的一个包**。

    握手时我们报的是 `mysql_native_password`（报这个最省事，也是最像真
    MySQL 的），客户端于是回一段 SHA1 挑战应答——**那段东西我们直接丢掉**，
    因为从它反推不出口令。

    紧接着发这个包，把插件换成 `mysql_clear_password`。客户端下一包发来的
    就是**它填的那个明文口令**。

    依据在 pymysql 的 `_process_auth`（`connections.py`）里，那一支是：

        elif plugin_name == b"mysql_clear_password":
            data = self.password + b"\\0"

    **前后没有任何 `_secure` 检查**——只有 `sha256_password` 和
    `caching_sha2_password` 那两条路才要求 TLS。所以这条路在明文连接上走得通。

    拿到明文之后，`mysql_门` 会把它交给**霸下自己的 pbkdf2** 去判
    （`闸口.登录`）。于是：

        **账号映射表里一个口令都不用存。**

    ⚠️ 明文口令是要过网的。所以默认只绑 `127.0.0.1`，另有「允许来源」白名单
    再收一道（见 `mysql_配置`）。**把监听地址改成 `0.0.0.0` 就等于把口令
    发到网上**，这条写在 `mysql_配置` 那条说明里。

    ⛔⛔ **解码必须用 `latin-1`，不能用 UTF-8。**（这一条是实测撞出来的。）

    pymysql 在 `Connection.__init__` 里（`connections.py:311-312`）是这么存口令的：

        self.password = password or b""
        if isinstance(self.password, str):
            self.password = self.password.encode("latin1")

    **它按 latin-1 编，不是 UTF-8。** 所以线上那串字节是 latin-1 字节。
    收下来按 UTF-8 解会出两件坏事，而且都不响：

    * `'café'` 这类口令——字节 `0xE9` 不是合法 UTF-8 开头，解出来是乱码，
      **口令明明对却被判成错**；
    * 更坏的一种：UTF-8 有 `errors='replace'` 兜底的话，好几种不同口令会
      **解成同一个字符串**——那就是拿别人的口令也能进。

    `latin-1` 是**双射**的（0..255 每个字节恰好对一个码位），所以

        明文 = 后一个包.decode('latin-1')

    拿回来的就是用户在客户端里敲的那个 str，一个字符不差。**这是这一层唯一
    正确的一把尺子。**

    另有一条边界，是同一个起因的另一面：**口令里不能有中文**。latin-1 编不了
    汉字，`'口令'.encode('latin1')` 在**客户端**当场
    `UnicodeEncodeError`——连不到我们这儿来，报错也指在 pymysql 里。
    所以配 MySQL 侧的霸下账号时，**口令要用 ASCII 字符**。
    （这条写进 `mysql_配置` 的说明，也写进报告。）

    包体：`0xFE` + 插件名 + NUL + 挑战串（明文插件不用挑战，但要给足长度，
    不然 pymysql 那边 `read_all()` 拿到空串，某些路径会不舒服）。
    """
    if salt is None:
        salt = os.urandom(20)
    return b'\xfe' + 明文插件名 + b'\x00' + (salt + b'\x00' * 20)[:20]


def 解握手响应(负载):
    """
    解 HandshakeResponse41。**只取两个字段：用户名和库名。**

    布局（对着 `connections.py:936-999` 那段"拼这个包"的代码倒着读）：

        4 字节      client_flags       —— 只记录，不据此分支
        4 字节      max_packet_size    —— 忽略
        1 字节      charset            —— 忽略（我们固定 UTF-8）
        23 字节     filler             —— 跳过
        NUL 结尾    用户名              ← **要这个**
        lenenc+     auth 响应           —— 跳过（见 `编换插件`，反正没用）
        NUL 结尾    库名                ← **要这个**（我们报了 CONNECT_WITH_DB）
        NUL 结尾    插件名              —— 跳过

    **为什么"只取两个"反而是对的**：客户端在握手响应里追加哪些字段，是由
    **服务端**报出去的能力位决定的，不是由它自己的 client_flags 决定的。
    （实测：pymysql 自己的 `client_flag` 里**没有** `CONNECT_WITH_DB`，
    但 `connections.py:987` 那句判的是 `self.db and self.server_capabilities
    & CLIENT.CONNECT_WITH_DB`——所以只要**我们**报了，库名就会发过来。）
    我们没报 `CONNECT_ATTRS`，所以末尾那段属性块不存在。**这条链上每一格
    都是死的，一个字节都不用猜。**

    ⛔ **用户名读错一个字母，身份就落到另一个霸下账号上，而从外面完全看不
    出来。** 所以长度不够、或者该有 NUL 的地方没有 NUL，一律抛，不猜。
    """
    if len(负载) < 32:
        raise 协议错('握手响应只有 %d 字节，连固定头都不够' % len(负载))

    client_flags = struct.unpack_from('<I', 负载, 0)[0]
    pos = 32

    用户名, pos = _读零结尾(负载, pos)

    # auth 响应：我们报了 PLUGIN_AUTH_LENENC_CLIENT_DATA，所以是 lenenc 前缀。
    # 拿不拿得到都无所谓（那段是 SHA1 挑战应答，反推不出口令），但**长度
    # 必须吃掉**，不然下面的库名就错位了。
    _认证, pos = _读串(负载, pos)

    库名 = b''
    if pos < len(负载):
        库名, pos = _读零结尾(负载, pos)

    插件名 = b''
    if pos < len(负载):
        try:
            插件名, pos = _读零结尾(负载, pos)
        except 协议错:
            插件名 = 负载[pos:]

    return {
        '用户': 用户名.decode('utf-8', 'surrogateescape'),
        '库': 库名.decode('utf-8', 'surrogateescape'),
        '插件': 插件名.decode('ascii', 'replace'),
        '能力': client_flags,
    }


# ══ 六、结果集 ═════════════════════════════════════════════════════════

def 编列定义(名, 类型码, charsetnr, 长度, 小数位=0, 标志=0,
             库名='', 表名=''):
    """
    Column Definition 41。

    字段次序**必须**和 pymysql 的 `_parse_field_descriptor`
    （`protocol.py:241-253`）逐字段对齐：六个长度前缀串，然后
    `read_struct("<xHIBHBxx")`。那个格式串拆开是

        x     1 字节，被吃掉（定长段的长度，固定 0x0C）
        H     charsetnr
        I     column_length
        B     type
        H     flags
        B     decimals
        xx    2 字节，被吃掉

    ⛔ **`charsetnr` 决定客户端把这一列解成什么**（`connections.py:1425-1433`）：

        charsetnr == 63  →  `encoding = None`  →  **保持 bytes**
        charsetnr != 63  →  按连接编码解码    →  **str**

    所以**文本列绝不能填 63**。填了爬虫拿到的是一堆 `bytes`，它那边的
    `TextCleaner` 会当场炸——而报出来的错会指到清洗代码上去，离真因很远。

    反过来，非文本类型 pymysql 给的是 `encoding='ascii'` 再走 converter，
    所以**数值列发 ASCII 文本就行，不要发二进制**
    （`int(b'123')` 在 Python 里是不成立的）。
    """
    出 = _编串(b'def')                 # catalog
    出 += _编串(库名.encode('utf-8', 'surrogateescape'))
    出 += _编串(表名.encode('utf-8', 'surrogateescape'))
    出 += _编串(表名.encode('utf-8', 'surrogateescape'))     # org_table
    出 += _编串(名.encode('utf-8', 'surrogateescape'))
    出 += _编串(名.encode('utf-8', 'surrogateescape'))       # org_name
    出 += b'\x0c'
    出 += struct.pack('<H', charsetnr)
    出 += struct.pack('<I', 长度)
    出 += struct.pack('<B', 类型码)
    出 += struct.pack('<H', 标志)
    出 += struct.pack('<B', 小数位)
    出 += b'\x00\x00'
    return 出


def 编行(格子):
    """
    一行。**每格一个长度前缀串，NULL 是单字节 0xFB。**

    ⛔ NULL 那一格最容易写错成"长度 0 的空串"。pymysql 那边
    `read_length_encoded_integer` 见到 251 直接返回 `None`、**不再往下读**；
    而长度 0 返回的是 `0`，后面跟着 0 个字节。两者在字节流上差一个字节的
    长度前缀，写错了整行的列数就全错位。

    爬虫那边收一个 `''` 和一个 `None` 是两件事——它的 `TextCleaner`
    会把空串当数据。
    """
    出 = []
    for 值 in 格子:
        if 值 is None:
            出.append(b'\xfb')
        elif isinstance(值, bytes):
            出.append(_编串(值))
        elif isinstance(值, str):
            出.append(_编串(值.encode('utf-8', 'surrogateescape')))
        else:
            # int / float / 别的标量都走这一支：**ASCII 文本**，
            # 因为客户端那边是用 ascii 解码之后再走 converter 的。
            出.append(_编串(str(值).encode('ascii', 'replace')))
    return b''.join(出)


#: 结果集里最多几列。`is_resultset_packet` 判的是 `0 < n < 251`。
最多列数 = 250


def 写结果集(流, 列, 行, status=状态.AUTOCOMMIT, 库名='', 表名=''):
    """
    把一个结果集整个写出去。

    `列` 是 `[(列名, 类型码, charsetnr, 长度, 小数位, 标志), …]`，
    `行` 是 `[[值, …], …]`——**值必须已经渲染成 str / bytes / None 了**
    （那是 `mysql_翻译` 的活，这一层不认识霸下的类型号）。

    形状：

        lenenc 列数（**1..250**）
        N × Column Definition
        EOF                        ← pymysql `_get_descriptions` 里
                                      `assert` 了这一句，少一个它当场断言失败
        行 × M
        EOF

    ⛔ **列数不能是 0**——`is_ok_packet` 判的是 `data[0] == 0`，而列数 0
    编出来正好是 `b'\\x00'`，客户端会把整个结果集当成一个 OK 报文。
    所以这里是**抛**，不是"回一个空结果集"：真要有 0 列的结果，
    那也该由上层决定回什么，不该在这一层悄悄变成一个别的东西。
    """
    if not 列:
        raise 协议错('结果集一列都没有——0 列会被客户端当成 OK 报文')
    if len(列) > 最多列数:
        raise 协议错('结果集有 %d 列，最多 %d' % (len(列), 最多列数))

    流.发(编长度(len(列)))
    for 定义 in 列:
        流.发(编列定义(*定义, 库名=库名, 表名=表名))
    流.发(编EOF(status))

    for 一格行 in 行:
        if len(一格行) != len(列):
            # 对不上就是错位：客户端会拿下一行的字节当这一行的列读，
            # 然后报一个跟真因毫不相干的 `AssertionError` 或者乱码字段名。
            raise 协议错('这一行有 %d 格，列数却是 %d'
                         % (len(一格行), len(列)))
        流.发(编行(一格行))

    流.发(编EOF(status))
