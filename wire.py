#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wire.py — 语句层的帧负载编解码

**只管一件事：一段语句文本 + 参数 <-> 一串字节。** 不碰盘、不碰 socket、
不碰语句怎么执行。

为什么单独一个文件（而不是挂在某一层名下）
----------------------------------------

这套编解码**两边都要**：

  * 服务端那扇门（`io_layer`）：帧进来，翻成语句交给语法层
  * 客户端那条腿（`socket_layer.Client`）：语句翻成帧，发出去

挂在 `io_layer` 名下会撞上"输入输出层 import 套接层"的环（`io_layer`
要用 `socket_layer` 的客户端）。挂在 `socket_layer` 名下，又变成套接层
认得 SQL——而套接层只该认帧，不该知道帧里装的是不是 SQL。

所以它**谁的下属都不是**，是一块两边共用的料。层次上它在 `io_layer`
和 `socket_layer` 底下，和 `protocol` 并排。

    req = encode_sql_request(['查询 1'], [(,)])
    texts, params = decode_sql_request(req)

一件事说清楚：这里的编解码**只做搬字节**。语句对不对、表在不在、参数
给够没有，全是语法层的事——这一层报的错永远只有"字节坏了"。
"""

import struct

import protocol
import schema
# `_param_type` 是**借**语法层的，不是抄一份：类型号那张表必须只有一处
# （`_encode_param` 里说了为什么）。
from syntax import (BLOB, FLOAT, INT, TEXT, Result, StatementError,
                    _param_type)

__all__ = ['encode_sql_request', 'decode_sql_request',
           'encode_result_frames', 'decode_result_frames',
           'encode_login', 'decode_login', 'encode_login_ok', 'decode_login_ok',
           'FRAME_TARGET']

# ── 帧编解码 ────────────────────────────────────────────────────────
#
# 请求（Op.SQL / Op.SQL_BATCH 的负载）：
#
#   [语句数:2] 然后每条 [文本长:4][文本][参数个数:2]，再跟各个参数的
#   [类型:1][长度:4][值字节]
#
#   参数的值字节用 `schema.encode_value` —— 盘上怎么存，网上就怎么传。
#   多加一层序列化，就多一处会跟存储对不上的地方。
#
# 应答（Op.ROWS 的负载），每帧：
#
#   [片号:1][错误长:2][错误文本]        片 0 之外错误长恒为 0
#   片 0 还有：[列数:2] + 列数 × [类型:1][名字长:2][名字]，再跟 [影响行数:8]
#   每帧都有：[本片行数:4] + 本片行数 × [格数:2] + 格数 × [空:1][长:4][字节]
#
#   行分片只做**传输切片，不是游标**：服务端一次把整条查询做完再切，
#   客户端按序收齐就行。跨帧游标要服务端记住"查到哪了"，那就是状态，
#   而无状态是这套协议立身的根本（见 protocol.py）。

_REQ_HEAD = struct.Struct('<H')
_REQ_ONE = struct.Struct('<IH')
_PVAL = struct.Struct('<BI')
_RESP_HEAD = struct.Struct('<BH')
_COLS = struct.Struct('<H')
_COL = struct.Struct('<BH')
_AFFECTED = struct.Struct('<q')
_NROWS = struct.Struct('<I')
_CELL = struct.Struct('<BI')
_NCELLS = struct.Struct('<H')

# 一帧装多少行。切太小，帧头开销占比上去了；切太大，一帧的收发会把
# 延迟顶起来，而且客户端得先攒够一整帧才动得了。1MB 上下比较稳。
FRAME_TARGET = 1 << 20


def encode_sql_request(statements, params=None):
    """statements 是文本列表，params 是与之等长的参数元组列表。"""
    out = bytearray(_REQ_HEAD.pack(len(statements)))
    for i, text in enumerate(statements):
        blob = text.encode('utf-8')
        plist = (params[i] if params else ()) or ()
        out += _REQ_ONE.pack(len(blob), len(plist))
        out += blob
        for p in plist:
            out += _encode_param(p)
    return bytes(out)


def _encode_param(p):
    if p is None:
        return _PVAL.pack(0, 0)
    if isinstance(p, bool):
        raise StatementError('参数不收真假值，请给 1 或 0')
    if isinstance(p, int):
        raw = schema.encode_value(INT, p)
    elif isinstance(p, float):
        raw = schema.encode_value(FLOAT, p)
    elif isinstance(p, str):
        raw = p.encode('utf-8')
        return _PVAL.pack(TEXT, len(raw)) + raw
    elif isinstance(p, (bytes, bytearray, memoryview)):
        raw = bytes(p)
        return _PVAL.pack(BLOB, len(raw)) + raw
    else:
        raise StatementError('参数认不出这个类型：%r' % (p,))
    # `_param_type` 留在语法层：**类型号只有一份**。这一层要用它，是因为
    # 值字节的编码方式由类型决定（`schema.encode_value` 那头也是同一张表）；
    # 在语法层再抄一份，将来加了类型就会有一边认不出。
    return _PVAL.pack(_param_type(p), len(raw)) + raw


def decode_sql_request(payload):
    """负载 -> (语句文本列表, 参数列表)。畸形负载抛 ProtocolError。"""
    if len(payload) < _REQ_HEAD.size:
        raise protocol.ProtocolError('SQL 负载太短')
    n, = _REQ_HEAD.unpack_from(payload, 0)
    pos = _REQ_HEAD.size
    texts, params = [], []
    for _ in range(n):
        if pos + _REQ_ONE.size > len(payload):
            raise protocol.ProtocolError('SQL 负载在第 %d 条断掉'
                                         % (len(texts) + 1))
        tlen, plen = _REQ_ONE.unpack_from(payload, pos)
        pos += _REQ_ONE.size
        if pos + tlen > len(payload):
            raise protocol.ProtocolError('SQL 语句文本越界')
        texts.append(payload[pos:pos + tlen].decode('utf-8', 'replace'))
        pos += tlen
        vals = []
        for _ in range(plen):
            if pos + _PVAL.size > len(payload):
                raise protocol.ProtocolError('SQL 参数越界')
            t, ln = _PVAL.unpack_from(payload, pos)
            pos += _PVAL.size
            if pos + ln > len(payload):
                raise protocol.ProtocolError('SQL 参数内容越界')
            chunk = payload[pos:pos + ln]
            pos += ln
            vals.append(_decode_param(t, chunk))
        params.append(tuple(vals))
    return texts, params


def _decode_param(t, chunk):
    if t == 0:
        return None
    if t == TEXT:
        return chunk.decode('utf-8', 'replace')
    if t == BLOB:
        return bytes(chunk)
    if not chunk:
        return None
    v, _ = schema.decode_value(t, chunk, 0)
    return v


# ── 身份（Op.LOGIN）──────────────────────────────────────────────────
#
# **和 SQL 负载完全分开编。** 合在一起编的话，两件事就绑在同一个字节
# 布局上：以后想改身份的表示（加个令牌、换个字段），动的却是 SQL 那条
# 每天都在跑的路。分开了，各改各的。
#
# 请求： [用户名长:2][用户名][口令长:2][口令]     两段都是 UTF-8
# 应答： 成功 = `Op.OK` 帧，负载 [用户名长:2][用户名]；失败 = `Op.ERR` 帧
#
# 应答里把**用户名回一遍**，是为了让客户端不必再问一句"我是谁"就能在
# 界面上写清楚当前登录的是哪个账号。对非管理员来说这一句问不出来（`显示
# 用户` 要 `管用户` 权限），所以这一趟不是冗余的。

_U16 = struct.Struct('<H')


def encode_login(用户名, 口令):
    """`(用户名, 口令)` -> LOGIN 请求负载。"""
    u = (用户名 or '').encode('utf-8')
    p = (口令 or '').encode('utf-8')
    if len(u) > 0xFFFF or len(p) > 0xFFFF:
        raise StatementError('用户名或口令太长了')
    return _U16.pack(len(u)) + u + _U16.pack(len(p)) + p


def decode_login(payload):
    """LOGIN 请求负载 -> `(用户名, 口令)`。畸形负载抛 ProtocolError。"""
    if len(payload) < _U16.size:
        raise protocol.ProtocolError('LOGIN 负载太短')
    ulen, = _U16.unpack_from(payload, 0)
    pos = _U16.size
    if pos + ulen + _U16.size > len(payload):
        raise protocol.ProtocolError('LOGIN 负载里的用户名越界')
    u = payload[pos:pos + ulen].decode('utf-8', 'replace')
    pos += ulen
    plen, = _U16.unpack_from(payload, pos)
    pos += _U16.size
    if pos + plen > len(payload):
        raise protocol.ProtocolError('LOGIN 负载里的口令越界')
    return u, payload[pos:pos + plen].decode('utf-8', 'replace')


def encode_login_ok(用户名):
    """登录成功的应答负载：就一个用户名。"""
    u = (用户名 or '').encode('utf-8')
    return _U16.pack(len(u)) + u


def decode_login_ok(payload):
    if len(payload) < _U16.size:
        raise protocol.ProtocolError('LOGIN 应答太短')
    ulen, = _U16.unpack_from(payload, 0)
    if _U16.size + ulen > len(payload):
        raise protocol.ProtocolError('LOGIN 应答里的用户名越界')
    return payload[_U16.size:_U16.size + ulen].decode('utf-8', 'replace')


def encode_result_frames(results, session=0, seq=0, flags=0):
    """
    结果列表 -> Op.ROWS 帧列表。

    一个结果可以摊成好几帧，片号在帧里带着，客户端按序收齐就行。
    最后一个结果的末片之外，都带 `Flag.MORE`。
    """
    frames = []
    for r in results:
        # **片号是每个结果各自从 0 数的，不是全局的。** 全局累加的话，
        # 第二个结果的首片会带上 part=1，而收方认的是 part==0 才起一个
        # 新结果——于是第二个结果被当成第一个的续片，两句话的数据接在
        # 一起，谁也对不上。批处理一次发好几条，这个错一撞就中。
        part = 0
        rows = r.rows
        i = 0
        first = True
        while True:
            chunk, i, more = _take_rows(rows, i)
            body = bytearray()
            err = r.error.encode('utf-8') if r.error else b''
            body += _RESP_HEAD.pack(part, len(err)) + err
            if first:
                body += _COLS.pack(len(r.columns))
                for name, t in r.columns:
                    nb = name.encode('utf-8')
                    body += _COL.pack(t, len(nb)) + nb
                body += _AFFECTED.pack(r.affected)
            body += _NROWS.pack(len(chunk))
            for row in chunk:
                body += _encode_row(row)
            f = flags | protocol.Flag.RESPONSE
            if more:
                f |= protocol.Flag.MORE
            frames.append(protocol.Frame(protocol.Op.ROWS, bytes(body),
                                         f, session, seq))
            part += 1
            first = False
            if not more:
                break
    # **末片必须标出来。** 一个请求的应答是一串帧（一批语句就是一串
    # 结果，每个结果自己还可能分片），而 `MORE` 是**结果内**的标记 ——
    # 每个结果的末片都不带它，于是"整个应答的末片"和"第 3 个结果的
    # 末片"长得一模一样，收方没法知道该读到哪儿。
    #
    # 少了这个标记，收方只有两条路，两条都是静默出错：读到第一个不带
    # MORE 的帧就停 → 结果集被**截断**；读满它以为的条数 → 多读的那
    # 几帧把**下一个请求的应答**吃掉，两个请求的答案就此错位，而错位
    # 后的样子完全正常。`Flag.FINAL` 就是为这件事留的号，一直在那儿
    # 没人用。
    if frames:
        frames[-1].flags |= protocol.Flag.FINAL
    return frames


def _take_rows(rows, i):
    """从第 i 行起，凑够一帧的量。返回 (这一段, 下一段的起点, 还有没有)。"""
    if i >= len(rows):
        return [], i, False
    total = 0
    j = i
    while j < len(rows):
        total += _NCELLS.size + sum(
            _CELL.size + (0 if v is None else len(_cell_raw(t, v)))
            for t, v in rows[j])
        j += 1
        if total >= FRAME_TARGET:
            break
    return rows[i:j], j, j < len(rows)


def _encode_row(row):
    out = bytearray(_NCELLS.pack(len(row)))
    for t, v in row:
        if v is None:
            out += _CELL.pack(1, 0)
            continue
        raw = _cell_raw(t, v)
        out += _CELL.pack(0, len(raw)) + raw
    return bytes(out)


def _cell_raw(t, v):
    """
    一格上线时的字节。

    数字和时间转成**文本**，不是二进制。二进制更省几个字节，但客户端
    拿到还得自己解；而这里省下的那点，在一条记录的网络往返里根本看不见。
    """
    if t == BLOB:
        return v
    return schema.render(t, v).encode('utf-8')


def decode_result_frames(frames):
    """帧列表 -> [Result]。给客户端用。"""
    out = []
    cur = None
    for f in frames:
        payload = f.payload
        if len(payload) < _RESP_HEAD.size:
            raise protocol.ProtocolError('ROWS 负载太短')
        part, elen = _RESP_HEAD.unpack_from(payload, 0)
        pos = _RESP_HEAD.size
        err = None
        if elen:
            if pos + elen > len(payload):
                raise protocol.ProtocolError('ROWS 错误文本越界')
            err = payload[pos:pos + elen].decode('utf-8', 'replace')
            pos += elen
        if part == 0:
            cur = Result(error=err)
            out.append(cur)
            if pos + _COLS.size > len(payload):
                raise protocol.ProtocolError('ROWS 列数越界')
            ncols, = _COLS.unpack_from(payload, pos)
            pos += _COLS.size
            for _ in range(ncols):
                t, ln = _COL.unpack_from(payload, pos)
                pos += _COL.size
                cur.columns.append(
                    (payload[pos:pos + ln].decode('utf-8', 'replace'), t))
                pos += ln
            cur.affected, = _AFFECTED.unpack_from(payload, pos)
            pos += _AFFECTED.size
        elif cur is None:
            raise protocol.ProtocolError('收到片 %d 却没收到第 0 片' % part)
        if pos + _NROWS.size > len(payload):
            raise protocol.ProtocolError('ROWS 行数越界')
        nrows, = _NROWS.unpack_from(payload, pos)
        pos += _NROWS.size
        for _ in range(nrows):
            ncell, = _NCELLS.unpack_from(payload, pos)
            pos += _NCELLS.size
            row = []
            for _ in range(ncell):
                isnull, ln = _CELL.unpack_from(payload, pos)
                pos += _CELL.size
                if isnull:
                    row.append((0, None))
                    continue
                row.append((0, payload[pos:pos + ln].decode('utf-8', 'replace')))
                pos += ln
            cur.rows.append(row)
    return out
