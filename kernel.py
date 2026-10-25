# -*- coding: utf-8 -*-
"""
kernel.py — 字节层的**线格式**

键值帧的负载长什么样，就这一件事。它是**两份代码之间的合同**：

    客户端（`socket_layer.Client`）      照这儿编码请求、解码应答
    字节门（`io_layer.byte_handler`）    照这儿解请求、编应答

**门已经不在这个文件里了。** 以前这里有个 `Kernel` 类，它持一个 Arena、
把帧翻成 `arena.put/get/scan`——那是**绕过语法层的一条旁路**。现在门整个
搬到输入输出层，字节门的动作走 `Syntax.put/get/scan/flush`，由语法层调度。
留在这个文件里的是**格式**：结构体、编解码、键长上限、错误码。

为什么格式要单独站一个文件：客户端和门现在在不同的文件、不同的层，两边
各自照着同一张表写一遍就是"两处会忘记同步改"。所以表只写一次。

负载格式（全部小端，定长头 + 变长体）
--------------------------------------

  PUT    请求  [klen:2][vlen:4][dim:8][key][value]
         应答  [rid:4][block:4][slot:4]

  GET    请求  [klen:2][key]
         应答  [found:1][rid:4][block:4][slot:4][value]

  SCAN   请求  [rid:4][block:4][slot:4][limit:4]   rid=NO_CURSOR 表示从头
         应答  [rid:4][block:4][slot:4][count:4]
               + count × [klen:2][vlen:4][key][value]
               还有后续时置 Flag.MORE

  BATCH  请求  [count:4] + count × PUT 的请求段
         应答  [ok:4][fail:4] + fail × [idx:4][msglen:2][msg]

**这张表是字节级的合同，编解码分在两处**：改宽一个字段、换个字节序，两处
都得动，而漏了哪一处不会报错，只会解出错位的东西——所以往这儿加字段之前
先想清楚。

键是**不透明的字节串**。键长上限由 `klen` 字段的宽度决定（2 字节），和
`Slot.encode_record` 里那道检查是同一个数。

这一层不知道的事
----------------

**不知道盘。** 区、块、槽、榫卯，一个都不认得——它只保证"写进去的字节原样
读得回来"（这一条由 `arena` 管）。

**不知道库。** 它不 import `arena`、`syntax`、`storage`——门怎么走、走
哪个层，是 `io_layer.byte_handler` 的事。

单写者去哪儿了
--------------

以前这个文件里的 `Kernel` 有个"没给 arena 就拿 root 开一个"的分支，是全项目
**唯一能开出第二个 Arena 的口子**。那条分支先被删掉，现在连类一起搬走了：
**Arena 只在 `Syntax.__init__` 里开得出来**，字节门拿到的是一整个 `Syntax`，
它够不着 `Arena` 的构造。单写者从纪律变成了签名。
"""

import struct

__all__ = ['KernelError', 'Code', 'NO_CURSOR', 'MAX_KEY', 'DEFAULT_SCAN_LIMIT',
           'PUT_REQ', 'GET_REQ', 'ADDR', 'FOUND', 'SCAN_REQ', 'SCAN_OUT',
           'REC', 'BATCH_REQ', 'BATCH_OUT', 'BATCH_ERR',
           'encode_put', 'encode_get', 'encode_scan', 'encode_batch',
           'decode_found', 'decode_records', 'decode_batch_result']

# 键长上限由 PUT 负载里 klen 字段的宽度决定（2 字节），和
# Slot.encode_record 里那道检查是同一个数。
MAX_KEY = 0xFFFF

# SCAN 游标里"从头开始"的哨兵。区域号实际到不了这里——根数组只给到
# 20 位，见 store_format.ROOT_ENTRIES。
NO_CURSOR = 0xFFFFFFFF

DEFAULT_SCAN_LIMIT = 256


class KernelError(Exception):
    """字节层的失败。带上错误码，让对端能分辨是不是该重试。"""

    def __init__(self, message, code=0):
        Exception.__init__(self, message)
        self.code = code


class Code(object):
    """错误码。少而准：能据它做出不同动作的才配有一个码。"""
    OK = 0
    NOT_FOUND = 1
    BAD_REQUEST = 2       # 负载本身不合法——重发一万次结果都一样
    ENGINE = 3            # 存储层拒绝，如一条记录大过一个空区域
    UNSUPPORTED = 4
    # 别处拒的（玄武二代 §四·二：9099 上不是环回的那一帧）。
    #
    # 为什么不并到 `UNSUPPORTED`：那两个把排错的人领到相反的方向 ——
    # `UNSUPPORTED` 说"这个操作码我不认"，该去查协议版本；这一条说的是
    # "操作码我认、话我也听懂了，**是我不让你办**"，该去查策略和来源。
    # 并成一条的话，9099 上被拒的人会一直去翻协议文档。
    PERMISSION = 5


# ── 负载结构（对外公开：门要用它们解请求、编应答）────────────────────
# 客户端和门分在两个文件里，所以这些**不是私有的**。`__all__` 里有它们，
# 就是为了让"门照着它解包"这件事是明写的，而不是谁去 import 下划线名字。

PUT_REQ = struct.Struct('<HIQ')       # klen, vlen, dim
GET_REQ = struct.Struct('<H')         # klen
ADDR = struct.Struct('<III')          # rid, block, slot
FOUND = struct.Struct('<BIII')        # found, rid, block, slot
SCAN_REQ = struct.Struct('<IIII')     # rid, block, slot, limit
SCAN_OUT = struct.Struct('<IIII')     # rid, block, slot, count
REC = struct.Struct('<HI')            # klen, vlen
BATCH_REQ = struct.Struct('<I')       # count
BATCH_OUT = struct.Struct('<II')      # ok, fail
BATCH_ERR = struct.Struct('<IH')      # idx, msglen


# ── 客户端编码 / 应答解码 ───────────────────────────────────────────

def encode_put(key, value=b'', dim=0):
    key, value = _as_bytes(key), _as_bytes(value)
    return PUT_REQ.pack(len(key), len(value), dim) + key + value


def encode_get(key):
    key = _as_bytes(key)
    return GET_REQ.pack(len(key)) + key


def encode_scan(cursor=None, limit=DEFAULT_SCAN_LIMIT):
    rid, blk, slot = (NO_CURSOR, 0, 0) if cursor is None else cursor
    return SCAN_REQ.pack(rid, blk, slot, limit)


def encode_batch(items):
    """items 是 (key, value) 或 (key, value, dim) 的序列。"""
    out = bytearray(BATCH_REQ.pack(len(items)))
    for item in items:
        dim = item[2] if len(item) > 2 else 0
        out += encode_put(item[0], item[1], dim)
    return bytes(out)


def decode_found(payload):
    """GET 应答 -> (found, rid, block, slot, value)。"""
    if len(payload) < FOUND.size:
        raise KernelError('GET 应答太短：%d 字节' % len(payload))
    found, rid, blk, slot = FOUND.unpack_from(payload)
    return bool(found), rid, blk, slot, payload[FOUND.size:]


def decode_records(payload):
    """
    SCAN 应答 -> ((rid, block, slot) | None, [(key, value), ...])。

    长度必须逐条对齐到字节尾巴。对不上就是帧被截了或被改了，
    宁可当场报错也不要返回半截结果。
    """
    if len(payload) < SCAN_OUT.size:
        raise KernelError('SCAN 应答太短：%d 字节' % len(payload))
    rid, blk, slot, count = SCAN_OUT.unpack_from(payload)
    pos = SCAN_OUT.size
    out = []
    for i in range(count):
        if pos + REC.size > len(payload):
            raise KernelError('SCAN 应答第 %d 条缺长度头' % i)
        klen, vlen = REC.unpack_from(payload, pos)
        pos += REC.size
        if pos + klen + vlen > len(payload):
            raise KernelError('SCAN 应答第 %d 条越界' % i)
        out.append((payload[pos:pos + klen],
                    payload[pos + klen:pos + klen + vlen]))
        pos += klen + vlen
    if pos != len(payload):
        raise KernelError('SCAN 应答尾部多出 %d 字节' % (len(payload) - pos))
    return (None if rid == NO_CURSOR else (rid, blk, slot)), out


def decode_batch_result(payload):
    """BATCH 应答 -> (ok, fail, [(idx, msg), ...])。"""
    if len(payload) < BATCH_OUT.size:
        raise KernelError('BATCH 应答太短：%d 字节' % len(payload))
    ok, fail = BATCH_OUT.unpack_from(payload)
    pos = BATCH_OUT.size
    errs = []
    for i in range(fail):
        if pos + BATCH_ERR.size > len(payload):
            raise KernelError('BATCH 应答第 %d 条失败项缺头' % i)
        idx, mlen = BATCH_ERR.unpack_from(payload, pos)
        pos += BATCH_ERR.size
        errs.append((idx, payload[pos:pos + mlen].decode('utf-8', 'replace')))
        pos += mlen
    return ok, fail, errs


def _as_bytes(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v)
    return str(v).encode('utf-8')
