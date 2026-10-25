#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
io_layer.py — 输入输出层

**库的进出都从这儿过。**

它是数据库对外的闸口：外面来的数据从这儿进去，库里的数据从这儿出去。

   采集端 / 界面 / 脚本
      ↓ 进                      ↑ 出
   ┌──────────────────────────────────────────┐
   │ 输入输出层   Gate（闸口）                 │
   │   进     send / send_many / fill          │
   │   出     fetch / fetch_many / page / count│
   │   整形   as_dicts                         │
   │   调度   分批、错误策略、分页、记账        │
   └──────────────────────────────────────────┘
      ↓ 两条腿（同一套口，构造时定死）          ↑
   本地腿  storage.Storage         同一个进程里直接开库
   远程腿  socket_layer.Client     连一个已经开着的服务端
      ↓                                         ↑
   语法层 → 榫卯缓冲层 → 存储引擎 → 永久存储区

**它不是界面。** 界面（将来那一层，PySide6）接在它上面；这一层自己不
画任何东西，只把数据送进送出，把结果摆成程序好拿的形状。

三条界线
--------

**它不解释语句。** 「建表」「查询」是什么意思、主键怎么编、维度代表什么，
全是语法层的事。这里把语句文本原样往下送，把结果原样往上抬。

**进出那条路上它不认得帧。** 远程腿手里那个 `socket_layer.Client` 已经把
一串帧收齐成 `Result` 了，闸口拿到的是 `Result`，不是字节。

**但"当门"那一侧它认得帧**——**两扇门都归这一层**：

    sql_handler   0x2x  帧进来翻成语句、结果编回帧
    byte_handler  0x1x  帧进来翻成键值、地址编回帧

`sql_handler` 原来长在语法层身上，`byte_handler`（字节门）原来长在
`kernel.py` 的 `Kernel` 类上、而且直接打 `arena`——**是唯一一条不经过
语法层的路**。两扇门现在都在这儿，都走语法层。

两边别混：**闸口**是给前端用的面（里面是 `Result`），**帧门**是接线用的
（里面是帧）。

**两条腿必须表现一致。** 这是这一层存在的全部理由：调用方不该知道（也不
该关心）自己是在同一个进程里开库，还是连着一个服务端。所以两腿**任何一处
行为不同都是 bug**，不是"配置不同"。已经见过的一处，**现在已经治好了**：

    同一句 `fill(..., batch=100000)` —— 本地腿闷头跑完（`execute_batch`
    不拦批的大小），远程腿报"一批太多"。根子是 `MAX_BATCH_STATEMENTS`
    只在**帧那条路**上拦，而两条腿走的是两个入口。帧门搬到这一层之后，
    上限就在这一层，`fill` 按它压批——两腿在同一个数上收口，不再靠
    "两处都记得写同样的判断"。

单写者
------

**引擎是单写者：一个永久存储区同一时刻只能有一个 Arena。** 所以：
`Gate.create/open(root)` 和 `serve(root)` **不能同时**对着同一个 root——
那是两个写者互相踩（各自的活跃区、各自的缓冲层、各自的数据尾指针），
踩出来的坏数据要到很久以后才有人发现。

服务一开，库就归**服务端的线程**独占，这个进程不能再从自己的本地腿进出。
这一条直接用形状挡掉了：`serve()` 返回的是 `Service`，**它只有 `port` 和
`close()`，没有进出的口**。要在同一个进程里既当服务又读写，就 loopback
连自己（`Gate.connect('127.0.0.1', svc.port)`），走的是同一条远程腿。

    # 同进程
    with Gate.create('网页.db') as g:
        g.send('建表 网页 (网址 文本 主键, 域名 文本)')
        g.fill('网页', [('a.com', 'a'), ('b.com', 'b')],
               columns=['网址', '域名'])
        print(g.fetch('查询 * 从 网页'))

    # 跨进程：一边开库起服务，另一边连上来
    svc = serve('网页.db')
    with Gate.connect('127.0.0.1', svc.port) as g:
        print(g.fetch('查询 计数(*) 从 网页').as_lists())
    svc.close()
"""

import kernel as kernel_mod
import protocol
import socket_layer as socket_mod
import storage as storage_mod
import syntax as syntax_mod
import transport
import user as user_mod
import wire

__all__ = ['Gate', 'Service', 'serve', 'serve_storage', 'sql_handler',
           'byte_handler', 'login_handler', 'MAX_BATCH_STATEMENTS']

# 攒多少条错误就够看了。一个坏批次能产生几千条一模一样的错，全留着的
# 结果是"错误列表本身顶爆内存"，而人只会看头几条。
_MAX_ERRORS = 20

# 一个帧里最多几条语句。**这是帧的规矩，所以它在输入输出层**——
# 帧是要整批收下来的，不设上限的话一个客户端能用一个帧让我们排几百万
# 条语句的结果。以前它在语法层（`syntax.MAX_BATCH_STATEMENTS`），跟着
# 帧入口一起搬过来了。
#
# 搬过来顺带治好了一个不一致：册子里那句"本地腿闷头跑完、远程腿报一批
# 太多"，根子就是**只有帧那条路上有这个卡口**。现在`fill` 按它压批，
# 两条腿在同一个数上收口。
MAX_BATCH_STATEMENTS = 4096


def _pairs(statements):
    """`[(文本, 参数), …]` 或 `[文本, …]` -> 一律是前者。

    两条腿的批入口形状不一样（存储层认两种写法，套接层要两个平行的列表），
    所以在这里**归一化一次**。写在调用点各转一遍就是两处会忘。
    """
    return [(it, ()) if not isinstance(it, tuple) else (it[0], it[1] or ())
            for it in statements]


# ── 帧门（服务端那一侧） ────────────────────────────────────────────
# 这一层对外其实是**两样东西**，别混：
#
#   闸口 Gate     给人用的进出面。里面是 `Result`，不是字节。
#   帧门 sql_handler  给套接层用的。里面是帧——解帧、调语法层、编帧回去。
#
# 两样都不是"界面"：帧门是**接线用的**，由组装的那一头（`龙龟之心.py`）
# 挂到套接层上；闸口才是前端认的那个口。
#
# 帧门搬过来之前长在语法层身上（`Syntax.handle`），而它和语法层的
# `execute_batch` 有二十来行是**逐行重复**的。搬过来之后那边只剩一个
# 入口，重复自己就没了——顺带治好的还有上面说的那个批上限不一致。


def sql_handler(syntax):
    """
    给 `socket_layer.make_handler(statements=…)` 挂的帧门。

    收帧、回帧，中间调 `syntax.execute_batch`。**只做帧这一侧的规矩**：

      * 解负载（`wire`），解不开就回错误帧——不是抛
      * `SQL` 一次只许一条文本（多了客户端拿结果帧对不上号）
      * 一批不许超过 `MAX_BATCH_STATEMENTS`
      * 批里的一条只许是一句（`one_per_text`，理由同上）
      * 结果编回帧（`wire`），末帧盖 `Flag.FINAL`

    一句话对不对、表在不在、参数够不够——一概不在这儿判，全推给语法层。
    这里报的错只有"帧这一侧坏了"。

    **身份只从 `conn.session.身份` 上取，帧里一个字节都不看。** 这是整条
    权限链上唯一一处身份来源，而它必须由服务端自己掌握：帧是请求方写的，
    会话是服务端发的（见 `protocol.Op.LOGIN` 那段）。`conn` 是 `None`
    （没入会 / 没连上）时不查表——`身份` 取不到就是 `None`，语法层据此
    一句都放不过（见 `syntax._无权文案`）。
    """
    def handle(frame, conn=None):
        op = frame.opcode
        if op not in (protocol.Op.SQL, protocol.Op.SQL_BATCH):
            # 走到这儿说明套接层的路由把别的操作码发过来了。**明着报，
            # 别默默按 SQL 解** —— 解出来的会是一堆跟真实原因无关的
            # "负载太短"。
            return [transport.err_frame(
                frame, '语句门收到不该给它的操作码 %s'
                % protocol.Op.name(op))]
        try:
            texts, params = wire.decode_sql_request(frame.payload)
        except protocol.ProtocolError as exc:
            return [transport.err_frame(frame, str(exc))]
        if op == protocol.Op.SQL and len(texts) > 1:
            return [transport.err_frame(
                frame, '一次只收一条语句，这里收到 %d 条。'
                       '要一次跑多条请用批量/替换那种批接口' % len(texts))]
        if len(texts) > MAX_BATCH_STATEMENTS:
            return [transport.err_frame(
                frame, '一批 %d 条，超过上限 %d'
                       % (len(texts), MAX_BATCH_STATEMENTS))]

        # `one_per_text` 只对 `SQL_BATCH` 开。**这一条是照搬搬走之前的行为**:
        # 那时候卡的是 `op == SQL_BATCH and len(stmts) > 1`，`SQL` 那条路上
        # 一段文本里带 `;` 是放行的（于是"文本数"和"结果帧数"在那一条路上
        # 本来就不相等）。这个不一致是既有的，**不在这次搬家里顺手改**——
        # 改它等于改协议语义，那是另一件事。本仓里没有任何地方发 `Op.SQL`，
        # 所以这条路目前谁也走不到。
        one_each = (op == protocol.Op.SQL_BATCH)
        results = syntax.execute_batch(list(zip(texts, params)),
                                       one_per_text=one_each,
                                       身份=_身份_of(conn))
        return wire.encode_result_frames(results, session=frame.session,
                                         seq=frame.seq)

    return handle


def _身份_of(conn):
    """
    这条连接当前的语句身份（没有就是 `None`）。

    **取值路径只有这一条，写两遍就会有两处能改错。** `conn` 可能是
    `None`（测试里直接调 handler、或者连接还没入会），`session` 也可能
    是 `None` —— 两种都当"没身份"，语法层那边一律拒。
    """
    if conn is None:
        return None
    sess = getattr(conn, 'session', None)
    if sess is None:
        return None
    return getattr(sess, '身份', None)


def login_handler(syntax):
    """
    给 `socket_layer.make_handler(login=…)` 挂的登录门。

    收一帧 `Op.LOGIN`（用户名 + 口令），核对，**把结果挂到会话上**：

        成了   `conn.session.身份 = 身份(用户, 会话号)`
        没成   `ERR` 帧，码 `BAD_LOGIN`

    **身份挂会话，不挂连接。** 会话活在连接之外（见 `transport.Session`），
    `resume()` 换一条连接挂回同一个会话时身份跟着走——断了线不必重新
    报一遍身份。**反过来那条边界要记住**：会话真的过期了，`resume()`
    会抛 `ConnectionError`，那条路上**必须重新登录**，不能把旧身份接着用。

    **挂上之前先核对，挂上之后不复查。** 口令只在这一次比；之后的每个帧
    只认这个已经挂在会话上的身份。所以 `改密` 不会把已经在用的人踢出去，
    `删用户` 也不会——**那是要给已有会话收回权限时才需要想的事**，
    现在没做，界面上也不该假装做了。

    **用户名和口令不对回同一个码 `BAD_LOGIN`**，报的也是同一句话：分开
    的话，那句报错就成了一个"这个用户名存在"的探针（见 `protocol.py` 里
    `BAD_LOGIN` 那段）。
    """
    def handle(frame, conn=None):
        if frame.opcode != protocol.Op.LOGIN:
            # 和另外两扇门同一个规矩：路由把别的操作码发过来了就明着报，
            # 别默默按 LOGIN 解——解出来会是一堆跟真实原因无关的越界。
            return transport.err_frame(
                frame, '登录门收到不该给它的操作码 %s'
                % protocol.Op.name(frame.opcode))
        sess = getattr(conn, 'session', None) if conn is not None else None
        if sess is None:
            # **没入会就没有地方挂身份**，所以不能认这一帧。放过去的话
            # 身份挂不上，客户端却以为报过了——它接着发的每条语句都会被
            # 拒，而拒的理由（"先登录"）跟它刚做完的事正好相反。
            return transport.err_frame(
                frame, '还没入会。先发一个 HELLO 拿会话号，再报身份',
                protocol.SESSION_UNKNOWN)
        try:
            名字, 口令 = wire.decode_login(frame.payload)
        except protocol.ProtocolError as exc:
            return transport.err_frame(frame, str(exc))
        u = syntax.核对身份(名字, 口令)
        if u is None:
            return transport.err_frame(
                frame, '用户名或口令不对', protocol.BAD_LOGIN)
        sess.身份 = user_mod.身份(u, sess.sid)
        return transport.ok_frame(frame, wire.encode_login_ok(u.name))

    return handle


# ── 字节门（服务端那一侧） ──────────────────────────────────────────
# 和语句门并排的另一扇门，两扇现在都归这一层。
#
# **它原来长在 `kernel.py` 的 `Kernel` 类上**：那个类持着一个 `Arena`，
# PUT / GET / SCAN / BATCH 四个动作**直接打 `arena.put/get/scan`**——是全
# 项目唯一一条不经过语法层的路。搬过来之后门手里是一整个 `Syntax`：连
# `Arena` 的构造都够不着，调度归语法层。
#
# 帧的形状（哪个字节是什么）**没有跟着搬**，还在 `kernel.py`：那份表是
# 客户端和这边共用的合同，挪到哪一层名下都会变成"两边各抄一份，改一处
# 忘一处"。
#
# 搬过来顺带接上的一笔账：以前门绕过 `Syntax._put`，而 `_put` 是**全项目
# 唯一会注意到"区换号了"的地方**，于是它写满一个区、换区那一刻没人记账。
# 现在这条通了。**但还有一条接不上**：`arena.put` 没有表号参数，门不知道
# 自己写的是哪张表的行，所以维度账本（`_live`）它照样喂不进去。**所以它
# 只能当协议兼容通道，不能拿它往有维度裁剪的表里写**——那会静默丢行。

BYTE_OPS = frozenset((protocol.Op.PUT, protocol.Op.GET,
                      protocol.Op.SCAN, protocol.Op.BATCH))


class _ByteDoor(object):
    """
    字节门。帧进、帧出，中间调语法层的裸口。

    **是个可调用物件，不是函数**——因为它带账（`stats`），组装那一头和
    排错都看得见。

    **一句话也不解释**：键是不透明字节、值是不透明字节，这里不判断值
    是不是墓碑、是不是空串——那是上层的事。协议里没有 DEL 也是这个
    道理（见 `protocol.Op` 里 0x13 那段）。

    **它只对管理员开放**（`__call__` 头上那道关口）。理由不是"小心一点
    更好"：到这一层已经**看不出这是哪张表**了（裸键、不带表号），表级
    权限在这儿没有落脚的地方。不堵的话，"精细权限"就只精细到语句层，
    任何人换一扇门就绕过去了。
    """

    def __init__(self, syntax):
        self.syntax = syntax
        self.stats = {
            'put': 0, 'get': 0, 'get_hit': 0,
            'scan': 0, 'scan_records': 0,
            'batch': 0, 'batch_items': 0,
            'bad_request': 0, 'engine_error': 0, 'unsupported': 0,
            'bytes_in': 0, 'bytes_out': 0,
        }

    # ---- 派发 --------------------------------------------------------

    def __call__(self, frame, conn=None):
        """
        一个帧进，一个帧出。

        **只做翻译不做兜底**：真正抛出去的异常由 transport 统一转成 ERR 帧，
        连接不会因为一个坏请求断掉。语法层自己的拒绝（`StatementError`）在
        下面就地转成带码的 ERR——那种错对端能分辨得出，知道重发没用。
        """
        self.stats['bytes_in'] += len(frame.payload)
        op = frame.opcode
        # ⛔ **管理员以外一律拒绝，没有例外。**
        #
        # 这扇门收的是**裸键值**：它不知道表号，也不会替调用方把行键的
        # 前缀拼对——于是任何连上端口的人拿它能直读直写任意一行，还能
        # 伪造别表的行键前缀往里塞。权限（表级 × 操作）在这一层之下没有
        # 任何意义，因为到这儿已经看不出"这是哪张表"了。
        #
        # 爬虫灌数据**不经过这儿**：`填`（`Gate.fill`）走的是 SQL
        # （`insert_text` + `sql_batch`），照常受权限管。这条堵的是"绕过
        # 语句层直接摸存储"那条路，不是灌数据那条路。
        #
        # **`conn is None` 也拒。** 没有连接就没有会话，没有会话就没有身份
        # ——写成"没 conn 就放行"等于给所有本地/内部调用开了个后门，而
        # 那条路恰恰是最容易被人从别处够到的。
        身份 = _身份_of(conn)
        if 身份 is None or not 身份.user.superuser:
            self.stats['unsupported'] += 1
            # 码用 `UNSUPPORTED`（"这条路对你不开放"），不用"没权限"那种
            # 需要先解释表结构的码。**不回含糊的 OK** —— 客户端拿到 OK
            # 会以为它写进去了。
            return transport.err_frame(
                frame, '字节层只对管理员开放。要按表读写请走语句层'
                       '（插入/查询/更新/删除），那条路上权限才算得清',
                kernel_mod.Code.UNSUPPORTED)
        route = _BYTE_ROUTES.get(op)
        if route is None:
            # 走到这儿说明套接层的路由把别的操作码发过来了。**明着报，
            # 别默默按 PUT 解** —— 解出来的会是一堆跟真实原因无关的
            # "负载太短"。
            self.stats['unsupported'] += 1
            return transport.err_frame(
                frame, '字节门收到不该给它的操作码 %s'
                % protocol.Op.name(op), kernel_mod.Code.UNSUPPORTED)
        out = route(self, frame)
        self.stats['bytes_out'] += len(out.payload)
        return out

    # ---- 各操作 ------------------------------------------------------

    def _put_op(self, frame):
        parsed = self._parse_put(frame)
        if isinstance(parsed, protocol.Frame):
            return parsed
        key, value, dim = parsed
        try:
            rid, blk, slot = self.syntax.put(key, value, dim)
        except syntax_mod.StatementError as exc:
            return self._engine_err(frame, exc)
        self.stats['put'] += 1
        return transport.ok_frame(frame, kernel_mod.ADDR.pack(rid, blk, slot))

    def _get_op(self, frame):
        key = self._parse_key(frame, kernel_mod.GET_REQ)
        if isinstance(key, protocol.Frame):
            return key
        value = self.syntax.get(key)
        self.stats['get'] += 1
        if value is None:
            return protocol.Frame(protocol.Op.DATA,
                                  kernel_mod.FOUND.pack(0, 0, 0, 0),
                                  protocol.Flag.RESPONSE,
                                  frame.session, frame.seq)
        self.stats['get_hit'] += 1
        # 取到什么就是什么，包括零长度。**这里不解释这个值**——它是不是
        # 墓碑、是不是空串，是上层要判断的事。
        return protocol.Frame(protocol.Op.DATA,
                              kernel_mod.FOUND.pack(1, 0, 0, 0) + value,
                              protocol.Flag.RESPONSE,
                              frame.session, frame.seq)

    def _scan_op(self, frame):
        p = frame.payload
        if len(p) != kernel_mod.SCAN_REQ.size:
            return self._bad(frame, 'SCAN 负载应为 %d 字节，实得 %d'
                             % (kernel_mod.SCAN_REQ.size, len(p)))
        rid, blk, slot, limit = kernel_mod.SCAN_REQ.unpack_from(p)
        cursor = (None if rid == kernel_mod.NO_CURSOR else (rid, blk, slot))
        if limit <= 0:
            limit = kernel_mod.DEFAULT_SCAN_LIMIT
        next_cursor, items, more = self.syntax.scan(cursor, limit)

        out = bytearray(kernel_mod.SCAN_OUT.pack(
            *(next_cursor if next_cursor else (kernel_mod.NO_CURSOR, 0, 0)),
            len(items)))
        for _r, _b, _s, key, value in items:
            out += kernel_mod.REC.pack(len(key), len(value)) + key + value
        self.stats['scan'] += 1
        self.stats['scan_records'] += len(items)
        return protocol.Frame(protocol.Op.DATA, bytes(out),
                              protocol.Flag.RESPONSE
                              | (protocol.Flag.MORE if more else 0),
                              frame.session, frame.seq)

    def _batch_op(self, frame):
        """
        一整批直存，最后**只提交一次**。

        单条失败不牵连其他条：坏的那条记进失败表，其余照写。整批回滚
        做不到——只追加的设计里没有"撤销"这回事，写下去的字节就是写
        下去了。所以这里给的是"尽力而为 + 如实报告"，不是事务。名字
        叫批量，不叫事务，就是为了不让人误会。
        """
        p = frame.payload
        if len(p) < kernel_mod.BATCH_REQ.size:
            return self._bad(frame, 'BATCH 负载太短：%d 字节' % len(p))
        count = kernel_mod.BATCH_REQ.unpack_from(p)[0]
        pos = kernel_mod.BATCH_REQ.size
        ok = 0
        errs = []
        truncated = None
        for i in range(count):
            if pos + kernel_mod.PUT_REQ.size > len(p):
                truncated = i
                break
            klen, vlen, dim = kernel_mod.PUT_REQ.unpack_from(p, pos)
            base = pos + kernel_mod.PUT_REQ.size
            if base + klen + vlen > len(p):
                truncated = i
                break
            key = p[base:base + klen]
            value = p[base + klen:base + klen + vlen]
            try:
                self.syntax.put(key, value, dim)
            except syntax_mod.StatementError as exc:
                errs.append((i, str(exc)))
            else:
                ok += 1
            self.stats['batch_items'] += 1
            pos = base + klen + vlen
        if truncated is not None:
            errs.append((truncated, '第 %d 条起负载被截断' % truncated))

        # 一次提交。这是 BATCH 相对"连发多条 PUT"的全部价值所在：
        # 这一批记录作为整体落盘，榫卯次序在批内不被切开。
        #
        # `force=False` 是**照搬搬走之前那一版**（原来写的是 `arena.flush()`，
        # 缺省就是 force=False）。它意味着"攒够一批（4MB 或 1 秒）才真写"，
        # 所以"只提交一次"这句话在引擎那一层其实**不是硬承诺**。这个是既有
        # 的行为，搬家没顺手改——要改成 `force=True` 是另一件事。
        self.syntax.flush(force=False)

        out = bytearray(kernel_mod.BATCH_OUT.pack(ok, len(errs)))
        for idx, msg in errs:
            raw = msg.encode('utf-8')[:0xFFFF]
            out += kernel_mod.BATCH_ERR.pack(idx, len(raw)) + raw
        self.stats['batch'] += 1
        return transport.ok_frame(frame, bytes(out))

    # ---- 解析与报错 --------------------------------------------------

    def _parse_put(self, frame):
        """成功返回 (key, value, dim)，失败返回一个 ERR 帧。"""
        p = frame.payload
        if len(p) < kernel_mod.PUT_REQ.size:
            return self._bad(frame, 'PUT 负载太短：%d 字节' % len(p))
        klen, vlen, dim = kernel_mod.PUT_REQ.unpack_from(p)
        end = kernel_mod.PUT_REQ.size + klen + vlen
        key = self._check_key(frame, p, klen, kernel_mod.PUT_REQ.size, end)
        if isinstance(key, protocol.Frame):
            return key
        return key, p[kernel_mod.PUT_REQ.size + klen:end], dim

    def _parse_key(self, frame, head):
        """成功返回键字节，失败返回一个 ERR 帧。"""
        p = frame.payload
        if len(p) < head.size:
            return self._bad(frame, '负载太短：%d 字节' % len(p))
        klen = head.unpack_from(p)[0]
        return self._check_key(frame, p, klen, head.size, head.size + klen)

    def _check_key(self, frame, payload, klen, key_off, end):
        """
        键必须非空，而且负载必须**正好**在该结束的地方结束。

        长度对不上意味着对端和我们对这个帧的理解不一样——这种时候猜
        是没有意义的，直接判坏请求。

        注意键和值在 PUT 里的次序是 `[头][key][value]`，所以键的偏移
        就是头的长度，**不是**头的长度加值长。这里写错过一次。
        """
        if klen == 0:
            return self._bad(frame, '键不能为空')
        if end != len(payload):
            return self._bad(frame, '负载长度对不上：应为 %d，实得 %d'
                             % (end, len(payload)))
        return payload[key_off:key_off + klen]

    def _bad(self, frame, message):
        self.stats['bad_request'] += 1
        return transport.err_frame(frame, message, kernel_mod.Code.BAD_REQUEST)

    def _engine_err(self, frame, exc):
        self.stats['engine_error'] += 1
        return transport.err_frame(frame, str(exc), kernel_mod.Code.ENGINE)


_BYTE_ROUTES = {
    protocol.Op.PUT: _ByteDoor._put_op,
    protocol.Op.GET: _ByteDoor._get_op,
    protocol.Op.SCAN: _ByteDoor._scan_op,
    protocol.Op.BATCH: _ByteDoor._batch_op,
}


def byte_handler(syntax):
    """给 `socket_layer.make_handler(bytes_handler=…)` 挂的字节门。"""
    return _ByteDoor(syntax)


# ── 两条腿 ──────────────────────────────────────────────────────────
# 腿只有四个动作：一条、一批、登录、关。**两条腿的这四个动作必须同名同形**，
# 闸口上面才写得出一套代码。差别（有没有 socket、有没有会话）全封在这里。

class _LocalLeg(object):
    """本地腿：一个进程里的 `Storage`。库就在手边，没有网络。"""

    name = '本地'
    # 本地腿只有一个会话（整个进程就这一个闸口在用库），给它一个固定的
    # 哨兵号。**它必须和远程腿的会话号不可能撞上**：远程那些是服务端发的
    # 随机 64 位非负值，所以本地这个用 -1（见 `user.本地会话号`）。
    #
    # 这个号唯一的下场是当 `syntax._批会话` 的键（第 9 条：批状态按会话
    # 记）。**要不是为了那个，本地腿压根不需要会话号。**
    会话号 = user_mod.本地会话号

    def __init__(self, storage):
        self.storage = storage

    def one(self, text, params=(), 身份=None):
        return self.storage.execute(text, params, 身份=身份)

    def many(self, pairs, 身份=None):
        return self.storage.execute_batch(pairs, 身份=身份)

    def 登录(self, 用户名, 口令):
        """
        本地腿的"登录"**就是核对**：核对过了返回 `User`，没有就 `None`。

        **它不替调用方把身份存下来** —— 存哪儿是闸口的事（`Gate._身份`）。
        腿只收参数、只管够不够得着库，这是这一节开头那条分工。
        """
        return self.storage.核对身份(用户名, 口令)

    def close(self):
        self.storage.close()


class _RemoteLeg(object):
    """远程腿：一条 `socket_layer.Client` 连接。帧、会话、重传都在它下面。"""

    name = '远程'

    def __init__(self, client):
        self.client = client

    @property
    def 会话号(self):
        """服务端在握手时发的那个号。批状态按它记（同 `_LocalLeg.会话号`）。"""
        return self.client.session

    # ⚠ **下面两个 `身份` 参数是收下就扔的，这是故意的，不是漏写。**
    #
    # 远程这条路上，身份由**服务端从会话里取**（`sql_handler` 读的是
    # `conn.session.身份`）。把客户端自称的身份编进帧里发出去，等于让任何
    # 能连上端口的人自称管理员——表级权限在远程那条路上当场归零，而且
    # 从外面看不出来。网关认得这个参数，只因为**两条腿必须同名同形**，
    # 闸口上面才写得出一套代码。
    #
    # **谁"顺手"把它填进 `sql()`/`sql_batch()`，就是把这个洞重新挖开。**
    # 要传身份，传的必须是服务端查出来的那个，而不是这儿收到的这个。

    def one(self, text, params=(), 身份=None):
        return self.client.sql(text, params)

    def many(self, pairs, 身份=None):
        return self.client.sql_batch([t for t, _ in pairs],
                                     [p for _, p in pairs])

    def 登录(self, 用户名, 口令):
        """
        远程腿的"登录"**就是报一句身份**，报了之后身份在服务端那个会话上。

        返回 `User` 或 `None`——`None` 时**闸口必须把 `_身份` 清掉**，不然
        一次报错口令之后，闸口还拿着上一个人的身份继续进出，而界面显示的
        是失败。这条在 `Gate.登录` 里收口。
        """
        if not self.client.login(用户名, 口令):
            return None
        # 服务端认了。**本地这一侧的 `User` 物件拼不出来**（摘要、盐、
        # 权限全在服务端），所以回一个只有名字的壳——闸口只拿它记"当前
        # 是谁"给界面看，权限判定不靠它（远程那条路上判在服务端）。
        return user_mod.User(用户名, b'', b'', 0)

    def close(self):
        self.client.close()


# ── 闸口 ────────────────────────────────────────────────────────────

class Gate(object):
    """
    库的进出闸口。**一条腿，一套口，构造时定死。**

    进出两条路分开写（`send` / `fetch`）是因为这一层的名字就是「输入
    输出」——读的人该一眼看出哪边进哪边出。底下是同一次调用，**记账按
    结果自己的形状分**：带结果集的算「出」，只有影响行数的算「进」。
    """

    def __init__(self, leg):
        self._leg = leg
        # 这条闸口当前是谁在说话（`user.身份`，没登录就是 `None`）。
        #
        # **放闸口，不放 `Syntax`，也不放腿。** 理由各一条：
        #
        #   `Syntax` 是**进程级**的——`龙龟之心` 的 `attach` 和 `serve_storage`
        #   用的是同一份（见 `Gate.attach`）。身份是**连接级**的，挂到
        #   进程上就成了"谁最后登录谁说了算"，多连接就串号。
        #
        #   腿是 `create/open/attach/connect` 内部造的，外面拿不到句柄；
        #   而且远程那条腿上身份根本不在本地（它在服务端的会话里），放腿上
        #   会出现两个来源。**腿只收参数，闸口是唯一持有者。**
        self._身份 = None
        self._n = {'statements': 0, 'batches': 0, 'rows_in': 0,
                   'rows_out': 0, 'failed': 0}

    # ── 四种开法 ────────────────────────────────────────────────────
    #
    # 前三种是"闸口自己开库"（root -> Storage -> 腿）。第四种 `attach`
    # 是"库已经开好了，闸口接管它的进出"——**组装内核必须走这条**：
    # 内核手上已经有一份 `Storage`，再 `create/open` 一次就是第二个
    # Arena、第二个写者（见模块头「单写者」）。

    @classmethod
    def create(cls, root, config=None, **limits):
        """建一个新库，同进程直接开。已存在时由引擎决定报错还是接着用。"""
        return cls(_LocalLeg(storage_mod.Storage(root, config, **limits)
                             .create()))

    @classmethod
    def open(cls, root, config=None, **limits):
        """打开已有的库，同进程直接开。"""
        return cls(_LocalLeg(storage_mod.Storage(root, config, **limits)
                             .open()))

    @classmethod
    def attach(cls, storage):
        """
        把一个**已经开着的** `Storage` 接上闸口。

        **不碰盘、不开库**，只把进出接管过来。`龙龟之心` 用它：那边
        已经持有唯一的 `Storage` 了，闸口再从 root 开一次就是第二个
        写者——两个写者互相踩，踩出来的坏数据要到很久以后才有人发现。
        """
        return cls(_LocalLeg(storage))

    @classmethod
    def connect(cls, host, port, timeout=5.0):
        """连一个已经开着的服务端。走套接层，**不碰盘**。"""
        return cls(_RemoteLeg(socket_mod.Client(host, port, timeout)))

    # ── 生命周期 ────────────────────────────────────────────────────

    def close(self):
        self._leg.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def leg(self):
        """这条闸口站在哪条腿上（'本地' / '远程'）。给排错和界面看。"""
        return self._leg.name

    # ── 身份 ────────────────────────────────────────────────────────

    def 登录(self, 用户名, 口令):
        """
        报一句身份。成了返回用户名，没成返回 `None`。

        **两条腿干的事不一样，出来的样子一样**：本地腿当场核对（`user.核对`
        的那一套就在手边），远程腿是把身份报给服务端由它核对。闸口上面
        看不出区别，这正是两条腿存在的理由。

        **没成的时候必须把 `_身份` 清掉。** 不清的话，用错口令那次登录之后
        闸口还留着上一个人的身份，接着进出照跑——而界面刚告诉用户"口令
        不对"。**这条是静默的**：一次失败的登录反而把人留在了登录态里。
        """
        u = self._leg.登录(用户名, 口令)
        self._身份 = None if u is None else user_mod.身份(
            u, self._leg.会话号)
        return None if u is None else u.name

    def 当前用户(self):
        """
        当前这条闸口是谁在说话（用户对象），没登录就是 `None`。

        **只给界面和排错看**：显示"当前登录的是谁"、决定哪个按钮该置灰。
        别拿它当权限判据——**权限判定在语法层**（`syntax._run`），在这里
        再判一次就是两处会不一致的规矩。
        """
        身份 = self._身份
        return None if 身份 is None else 身份.user

    def 退出登录(self):
        """
        把本闸口的身份摘掉。**本地腿摘掉就是真摘掉了**（之后谁都进不来）；
        远程腿这边**摘不干净**——服务端那个会话上的身份还在，它还认你。
        要真的退出，得让服务端那条路也能清（`Op.LOGIN` 没做"登出"这个号）。
        """
        self._身份 = None

    # ── 进 ──────────────────────────────────────────────────────────
    # 下面这些方法**一行都不拼语句文本**——除了 `fill`，它拼，但拼的是
    # `存储层.insert_text` 那一份，和 `Storage.insert` 同源。

    def send(self, text, params=()):
        """送一条语句进去。返回 `Result`（错了也是 Result，带 `.error`）。"""
        return self._run(text, params)

    # ⚠ **进出的每一条路都要把 `self._身份` 传下去，一条都不能漏。**
    # 漏一条的症状不是报错，是**那条路上的权限判定拿到 `None` 之后照拒**
    # ——对没登录的调用方是对的行为，于是漏了也看不出来，直到有人登录
    # 之后发现某一类调用怎么都被拒。转口都在 `_run` / `_count` 上面
    # （`send`/`fetch` 汇进 `_run`，`send_many`/`fetch_many` 各一句），
    # 所以新增进出方法时照着这几行抄，别自己拼一句 `self._leg.one(...)`。
    #
    # `page`/`count` 不用另写：它们自己拼语句文本，然后**汇进 `fetch`**，
    # 身份跟着走。`fill` 拼的是 `insert_text`，它自己那句 `self._leg.many`
    # 上面已经带上了。

    def send_many(self, statements):
        """
        `[(文本, 参数), …]` 或 `[文本, …]`，一批进去**只落一次盘**。

        返回与入参等长的结果列表。**批里没有回滚**——前面几条成了就是成了。
        要的正是这个：批量插入的耗时几乎全在落盘次数上。
        """
        results = self._leg.many(_pairs(statements), 身份=self._身份)
        self._count(results)
        return results

    def fill(self, table, rows, columns=None, batch=0, dimension=None,
             on_error='stop'):
        """
        灌数据 —— 这一层最要紧的一条进路。

        `rows` 一行一个序列，按位置对应 `columns`（不给就按建表列序）。
        每 `batch` 行打成一批，一批一次落盘。`batch=0`（默认）表示按上限
        能塞多少塞多少。

        **批的大小会被压到 `MAX_BATCH_STATEMENTS` 以内。** 那个上限现在
        就在本模块（原来是语法层的、且只在帧那条路上拦），压不住的话同一
        句调用在两条腿上就是两种结果（见模块头「两条腿必须表现一致」）。

        `on_error`：
          `'stop'`    一批里出现失败就**不再发后面的批**。**当前这批已经
                      执行过了——批里没有回滚**，所以失败那条之后、同一批
                      里的语句也可能已经进去了。要精确停在哪一行，就把
                      `batch` 设成 1，代价是每行一次落盘。
          `'collect'` 全部跑完，失败逐条记在返回值里。

        返回 `{'rows', 'ok', 'failed', 'affected', 'batches', 'errors'}`。
        **`ok` 是"真进去了几行"**，它和 `rows` 对不上就是没灌完——这两个
        数必须摆在返回值里，而不是让人从"没报错"去推。

        `errors` **只留头 20 条**（见 `_MAX_ERRORS`；坏批次能产生几千条
        一模一样的错，全留着就是错误列表自己顶爆内存）。**要看失败总数
        就看 `failed`，别拿 `len(errors)` 当**——这两个数不相等是正常的，
        不是丢了。
        """
        rows = [list(r) for r in rows]
        if not rows:
            return {'rows': 0, 'ok': 0, 'failed': 0, 'affected': 0,
                    'batches': 0, 'errors': []}

        width = len(rows[0])
        for i, r in enumerate(rows):
            if len(r) != width:
                raise ValueError(
                    '第 %d 行有 %d 个值，第一行有 %d 个——一行一个形状，'
                    '不然插进去的列对不上' % (i + 1, len(r), width))

        # 拼一次，用一千次。这条语句对每一行都成立（值全走 `?`）。
        text = storage_mod.insert_text(table, width, columns, dimension)
        tail = (dimension,) if dimension is not None else ()

        step = int(batch) if batch and batch > 0 else MAX_BATCH_STATEMENTS
        step = min(step, MAX_BATCH_STATEMENTS)

        ok = failed = affected = batches = 0
        errors = []
        for start in range(0, len(rows), step):
            chunk = rows[start:start + step]
            results = self._leg.many([(text, tuple(r) + tail) for r in chunk],
                                     身份=self._身份)
            batches += 1
            self._n['batches'] += 1
            self._n['statements'] += len(results)
            stop = False
            for i, r in enumerate(results):
                if r.ok:
                    ok += 1
                    affected += r.affected
                    # **只数真进去的。** 账要记"进去几行"，不是"发了几条
                    # 语句"——批量灌数据时后者永远等于前者，前者才是能丢的。
                    self._n['rows_in'] += 1
                else:
                    failed += 1
                    self._n['failed'] += 1
                    stop = stop or on_error == 'stop'
                    if len(errors) < _MAX_ERRORS:
                        errors.append({'row': start + i, 'error': r.error})
            if stop:
                break

        return {'rows': len(rows), 'ok': ok, 'failed': failed,
                'affected': affected, 'batches': batches, 'errors': errors}

    # ── 出 ──────────────────────────────────────────────────────────

    def fetch(self, text, params=()):
        """取一条语句的结果。返回 `Result`。"""
        return self._run(text, params)

    def fetch_many(self, statements):
        """一批语句，一次往返。返回与入参等长的结果列表。"""
        results = self._leg.many(_pairs(statements), 身份=self._身份)
        self._count(results)
        return results

    def page(self, table, columns='*', where=None, params=(), order=None,
             size=256, offset=0):
        """
        一页一页地出。

        **必须给 `order`，不给就报错。** 页与页之间没有定序，中途任何一次
        `更新` 都会把行挪到新区去——上一页刚看过的行可能在下一页再来一次，
        而另一些行两页都不出现。**每一页看上去都完全正常**，只有把整串拼
        起来跟一次全拉比对，才看得出重了、漏了。所以这一层不给自己留这个
        口子：定序是明写的，重和漏才不可能。
        """
        if not order:
            raise ValueError(
                '翻页必须给排序——页与页之间没有定序就会重、会漏，而重和漏'
                '在每一页上看起来都完全正常。建议按主键排')
        text = '查询 %s 从 %s' % (columns, table)
        if where:
            text += ' 其中 %s' % where
        text += ' 排序 按 %s 限制 %d' % (order, int(size))
        if offset:
            text += ' 偏移 %d' % int(offset)
        return self.fetch(text, params)

    def count(self, table, where=None, params=()):
        """数一数。翻页的界面要这个数才知道有多少页。"""
        text = '查询 计数(*) 从 %s' % table
        if where:
            text += ' 其中 %s' % where
        r = self.fetch(text, params)
        if not r.ok:
            raise syntax_mod.StatementError('计数失败：%s' % r.error)
        return int(r.as_lists()[0][0])

    # ── 整形 ────────────────────────────────────────────────────────

    @staticmethod
    def as_dicts(result):
        """
        一行一个字典——给程序和界面用。**这是本层唯一一处"出"的整形**：
        表格文本 `str(result)` 语法层已经有了，不再抄一份。

        **列名重样的当场报错，不覆盖。** `查询 a, a 从 网页` 两张嘴说同名
        的列，字典只装得下一个——留谁都是静默少一列。给它们起个别名再来。
        """
        names, seen, dup = [], set(), []
        for n, _t in result.columns:
            if n in seen:
                dup.append(n)
            seen.add(n)
            names.append(n)
        if dup:
            raise ValueError('列名重样，字典装不下（%s）——给它们起别名'
                             % '、'.join(sorted(set(dup))))
        return [dict(zip(names, row)) for row in result.as_lists()]

    # ── 这一层自己的账 ──────────────────────────────────────────────

    def stats(self):
        """
        从闸口过了多少条语句、灌进去多少行、出去多少行、错了几条。

        **`failed` 单独记一列是故意的**：出错在别处是异常，在这儿是一条
        计数——批量灌数据时"有几条没进去"比"哪一条进去了"重要得多。
        """
        out = dict(self._n)
        out['leg'] = self._leg.name
        return out

    # ── 内里 ────────────────────────────────────────────────────────

    def _run(self, text, params):
        r = self._leg.one(text, params, 身份=self._身份)
        self._n['statements'] += 1
        if not r.ok:
            self._n['failed'] += 1
        elif r.columns:
            self._n['rows_out'] += len(r.rows)
        else:
            self._n['rows_in'] += r.affected
        return r

    def _count(self, results):
        for r in results:
            self._n['statements'] += 1
            if not r.ok:
                self._n['failed'] += 1
            elif r.columns:
                self._n['rows_out'] += len(r.rows)
            else:
                self._n['rows_in'] += r.affected


# ── 服务 ────────────────────────────────────────────────────────────

class Service(object):
    """
    一个开着服务的库。

    **它不给人进出，这是故意的。** 服务一开，库就归服务端的线程独占
    （引擎是单写者），这个进程再从自己的本地腿进出就是两个线程同踩一个
    Arena。所以这个类只有 `port` 和 `close()`，没有 `send` / `fetch`——
    想在同一个进程里读写，就 loopback 连自己：

        svc = serve('网页.db')
        with Gate.connect('127.0.0.1', svc.port) as g:
            g.send('查询 * 从 网页')
    """

    def __init__(self, storage, server, thread, port):
        self._storage = storage
        self._server = server
        self._thread = thread
        self.port = port

    def close(self):
        """**服务端先停，库后关。** 反过来的话，正连着的客户端会在库已经
        关掉之后还往里发，那些请求会以各种看不懂的样子失败。"""
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None
        self._storage.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def serve_storage(st, host='127.0.0.1', port=0, 门皮=None):
    """
    把一个**已经开着的** `Storage` 开成服务。返回 `Service`。

    **三扇门都在这一句里挂上。** 谁组装谁接线：本层有门，`socket_layer`
    有服务，两边都不 import 对方，由这一句把几个回调递过去——

        语句门  sql_handler(st.syntax)   帧 → 语句 → 语法层
        字节门  byte_handler(st.syntax)  帧 → 键值 → 语法层
        登录门  login_handler(st.syntax) 帧 → 用户名口令 → 会话上挂身份

    **三扇门拿的是同一份 `Syntax`**，也就是同一个库、同一个 Arena。
    套接层从此**谁都不认得**：它只按操作码段决定把帧交给哪个回调。

    **库从此归服务端线程独占**：调用方不能再从本地腿进出，要读写就
    loopback 连回来（见 `Service` 的说明）。

    `门皮=None` 时一切都和以前一样。给了就是一个可调用，收**三扇门那个
    字典**、回**三扇门那个字典**（键 `语句层` / `字节层` / `登录层`）。

    ⚠ **这一格是给玄武留的**（`玄武_门.皮9099`），也是这一期唯一一处
    "为了接上玄武而动的一行"——`开发计划-玄武二代.md` §四·二 原以为
    包住 `socket_layer.make_handler` 就够了，**那个假设不成立**：三扇门
    是在**这一句里面**组好的，外面够不着。要么在组装处留一格，要么玄武
    自己重写一遍 `serve_storage`（那就得认得 `Storage`、`Service`、
    `socket_mod` 三样），留一格显然更小。

    ⛔ **`门皮` 不许换掉 `st`。** 库还是那一个，出去的还是那几份 `Syntax`
    ——这一格只管"帧进来之后先给谁看"，不管库里是谁。
    """
    门 = {'语句层': sql_handler(st.syntax),
          '字节层': byte_handler(st.syntax),
          '登录层': login_handler(st.syntax)}
    if 门皮 is not None:
        门 = 门皮(门)
    server, port, th = socket_mod.serve_in_thread(
        host, port, statements=门['语句层'],
        bytes_handler=门['字节层'],
        login=门['登录层'])
    return Service(st, server, th, port)


def serve(root, host='127.0.0.1', port=0, config=None, create=False,
          门皮=None, **limits):
    """
    开库 + 把它开成一个服务。返回 `Service`（拿 `svc.port` 连它）。

    `port=0` 让系统挑一个空闲的，挑到的在 `svc.port` 上。
    `create=True` 建新库，否则开已有的。

    手上已经有 `Storage` 的话用 `serve_storage`——**别在这儿再开一次**。

    `门皮` 原样递给 `serve_storage`（见那一段）。
    """
    st = storage_mod.Storage(root, config, **limits)
    if create:
        st.create()
    else:
        st.open()
    return serve_storage(st, host, port, 门皮=门皮)
