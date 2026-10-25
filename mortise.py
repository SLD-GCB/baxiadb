# -*- coding: utf-8 -*-
"""
霸下榫卯缓冲层
==============

先把话说清楚：**主流数据库的缓冲池把页当成可以互换的独立单位**，
淘汰谁只看访问频度，页与页之间没有关系。这一层不是。它认为构件之间
是咬合的——有些东西必须一起在内存、按固定次序落盘、成组被淘汰。
这就是榫卯。

三种咬合
--------

    记录 ↔ 槽   次序上的咬合，而且是三类里**唯一承重的**。写入固定为
                「先数据、后槽」：反过来，断电时槽就会指着一片没写出来
                的字节。但两种构件在内存里的寿命完全不同——记录写完就
                再没人改它，槽却会被反复原地更新。所以缓冲层把它们分开
                攒，落盘时按 记录 → 块 → 区域头 三级提交，这对榫卯在
                落盘序上永远劈不开。

    块 ↔ 块     键的主块装不下就溢出到邻块，两个块互相承接，契口记在
                块头的 tenon/mortise 里。一起预取、一起淘汰。
                （诚实说一句：这条链在当前规则下是**确定的**——溢出就是
                往后挪一格，所以 tenon/mortise 记的是"已知的事实"而不是
                "新的信息"。它的价值在恢复和整区压实不必重扫，以及给
                缓冲层一个成组的依据，不在让点查更快。）

    块 ↔ 区域头 活跃区的块和它的区域头是一体的。区域头是**提交点**：
                它落盘等于宣告"这一批块已经写完了"，所以必须最后写。

落盘次序
--------

    1. 记录（数据尾区）  榫
    2. 块（槽区）        卯
    3. 区域头            封条

在第 1 和第 2 步之间断电，盘上是一堆没人指向的记录字节：浪费，但整区
压实时能收回来。反过来（先写槽）就是槽指着一片垃圾。这个不对称是整套
「只追加」设计的全部理由。

**次序在写入层面成立，不等于在持久化层面成立。** 真正的屏障要每一步
fsync 才守得住，那个代价没人付得起。所以这里接受极小概率的"槽落盘了、
它指向的记录还没落"——后果由读路径兜底：`_lookup_in` 拿到记录之后会
拿键去比对，比不中就当作没找到。**键比对是最后一道防线**，它把一次
可能的数据损坏降级成一次漏读。

缓冲层不做 I/O
--------------

读靠 load 回调，写靠 sink 对象。这样它能单独灌数据验证，也方便以后
换承载（裸设备、mmap）而不动这里的逻辑。单写者模型，不加锁——
和 Arena 的前提一致。
"""

import time
from collections import OrderedDict

import store_format as sf

__all__ = ['Block', 'Record', 'Header', 'MortiseBuffer',
           'DEFAULT_MAX_BYTES', 'DEFAULT_PREFETCH']

DEFAULT_MAX_BYTES = 64 << 20     # 缓冲层内存上限
DEFAULT_PREFETCH = 1             # 一次结构预取最多往后带几个块


class Block(object):
    """
    块构件。索引的最小单位，也是榫卯的构件单位。

    raw 是 bytearray 而不是 bytes——活跃区里槽要原地改写，bytes 改不了。

    tenon/mortise 是从块头里读出来的**副本**，摆在对象上是为了让预取和
    淘汰不必每次都去翻裸字节。副本必须永远是块头的镜像：只有
    `MortiseBuffer.set_joint` 一条路能改块头，也只有它负责同步副本。
    """

    __slots__ = ('rid', 'idx', 'raw', 'dirty', 'tenon', 'mortise',
                 'slots_used', 'slots_total', 'tick', 'hits', 'prefetched')

    def __init__(self, rid, idx, raw):
        self.rid = rid
        self.idx = idx
        self.raw = raw
        self.dirty = False
        self.slots_used, self.slots_total, self.tenon, self.mortise = \
            sf.read_block_joint(raw)
        self.tick = time.monotonic()
        self.hits = 0
        self.prefetched = False      # 是被预取进来的，还没被真正用过

    @property
    def size(self):
        return len(self.raw)

    @property
    def full(self):
        """槽用满了。**满了意味着链一定还在往后走**，预取的依据。"""
        return self.slots_total > 0 and self.slots_used >= self.slots_total

    def __repr__(self):
        return '<Block %d:%d %dB%s>' % (
            self.rid, self.idx, len(self.raw),
            ' dirty' if self.dirty else '')


class Record(object):
    """
    记录构件。键和值的字节，躺在区域尾部的数据区里。

    **写完就不再变**——只追加的设计里，一条记录要么完整存在、要么不存在，
    没有"改一半"这回事。所以它只在写入的那一刻短暂地是脏的，之后就是
    一份纯只读缓存。
    """

    __slots__ = ('rid', 'offset', 'raw', 'dirty', 'tick', 'hits')

    def __init__(self, rid, offset, raw, dirty=False):
        self.rid = rid
        self.offset = offset
        self.raw = raw
        self.dirty = dirty
        self.tick = time.monotonic()
        self.hits = 0

    @property
    def size(self):
        return len(self.raw)

    def __repr__(self):
        return '<Record %d@%d %dB%s>' % (
            self.rid, self.offset, len(self.raw),
            ' dirty' if self.dirty else '')


class Header(object):
    """
    区域头构件。它只有 64 字节，从不参与淘汰——**它只是提交点的载体**。

    活跃区的每一次改动都不再单独写盘：区域头是整体重写的，攒到最后
    一次写掉才是对的。原先每写一条记录就 seek 一次写 64 字节，纯属
    把一次顺序写拆成了成千上万次随机写。
    """

    __slots__ = ('rid', 'hdr', 'dirty')

    def __init__(self, rid, hdr):
        self.rid = rid
        self.hdr = hdr
        self.dirty = False

    @property
    def size(self):
        return sf.REGION_HEADER_SIZE


class MortiseBuffer(object):
    """榫卯缓冲层。纯内存，不做任何 I/O。"""

    def __init__(self, max_bytes=DEFAULT_MAX_BYTES,
                 prefetch=DEFAULT_PREFETCH, blocks_per_region=0):
        self._blocks = OrderedDict()    # (rid, idx) -> Block，按最近使用排序
        self._records = OrderedDict()   # (rid, offset) -> Record
        self._headers = {}              # rid -> Header
        self._bytes = 0
        self._dirty_bytes = 0
        self._max_bytes = max_bytes
        self._prefetch = prefetch
        # 块的环形边界。预取遇到块尾要知道绕回哪儿，而缓冲层本身不该
        # 去问 Arena——区域规划是启动时就定死的常数。
        self._blocks_per_region = blocks_per_region
        self.stats = {
            'block_hits': 0, 'block_misses': 0,
            'record_hits': 0, 'record_misses': 0,
            'evicted': 0, 'evicted_mate': 0,
            'prefetched': 0, 'prefetch_useful': 0,
            'flushes': 0, 'written_blocks': 0, 'written_records': 0,
        }

    # ── 账目 ────────────────────────────────────────────────────────

    @property
    def bytes(self):
        return self._bytes

    @property
    def dirty_bytes(self):
        return self._dirty_bytes

    def _charge(self, member, delta):
        self._bytes += delta
        if member.dirty:
            self._dirty_bytes += delta

    # ── 取构件 ──────────────────────────────────────────────────────

    def block(self, rid, idx, load):
        """
        取块。未命中时调用 `load(rid, idx) -> bytes` 读盘。

        命中之后顺手看一眼这个块的榫卯后继：**块满 = 链一定还在往后走**，
        所以这不是投机预取，是确定会发生的那一次读提前做掉。
        """
        key = (rid, idx)
        self._trim()          # 淘汰只在构件没出手之前做
        blk = self._blocks.get(key)
        if blk is not None:
            blk.hits += 1
            blk.tick = time.monotonic()
            if blk.prefetched:
                blk.prefetched = False
                self.stats['prefetch_useful'] += 1
            self._blocks.move_to_end(key)
            self.stats['block_hits'] += 1
            return blk

        self.stats['block_misses'] += 1
        raw = load(rid, idx)
        if not isinstance(raw, bytearray):
            raw = bytearray(raw)
        blk = Block(rid, idx, raw)
        self._install(self._blocks, key, blk)

        if self._prefetch:
            if blk.mortise != sf.NO_BLOCK:
                # 确定会发生：这个块的链已经在往后走了
                self._follow(blk.mortise, rid, load)
            elif blk.full and self._blocks_per_region:
                # 迫近：块已经满了但还没溢出过，下一次插入必然往后走
                self._follow((idx + 1) % self._blocks_per_region, rid, load)
        return blk

    def _follow(self, idx, rid, load, depth=None):
        """沿榫卯链预取。**不递归**——预取进来的块不再触发下一轮预取，
        否则一条长链会一次拽进整个区域。"""
        if (rid, idx) in self._blocks:
            return None
        raw = load(rid, idx)
        if not isinstance(raw, bytearray):
            raw = bytearray(raw)
        blk = Block(rid, idx, raw)
        blk.prefetched = True
        self._install(self._blocks, (rid, idx), blk)
        self.stats['prefetched'] += 1
        return blk

    def record(self, rid, offset, length, load):
        """取记录。未命中时调用 `load() -> bytes` 读盘。"""
        key = (rid, offset)
        self._trim()          # 同上：读进来的记录也可能把预算顶爆
        rec = self._records.get(key)
        if rec is not None:
            rec.hits += 1
            rec.tick = time.monotonic()
            self._records.move_to_end(key)
            self.stats['record_hits'] += 1
            return rec.raw
        self.stats['record_misses'] += 1
        raw = load()
        rec = Record(rid, offset, raw)
        self._install(self._records, key, rec)
        return raw

    def header(self, rid, hdr=None):
        """取区域头。传 hdr 时是登记（首次见到这个区）。"""
        member = self._headers.get(rid)
        if member is None:
            member = Header(rid, hdr)
            self._headers[rid] = member
            # **区域头也要记账。** 它只有 64 字节，但不记账的话 _bytes
            # 会低于实际占用，而 touch_header 又把它加进 _dirty_bytes，
            # 两个数就对不上——曾经出现过 dirty_bytes > bytes 这种不可能
            # 的状态，内存压力判定因此整体失灵（该刷的时候不刷）。
            self._charge(member, member.size)
        return member

    def _install(self, table, key, member):
        table[key] = member
        self._charge(member, member.size)

    def _trim(self):
        """
        预算超了就按 LRU 淘汰干净构件。

        **只在"没有在飞的构件"时调用**——也就是各个取用入口的开头。
        放进 `_install` 里会出人命：新构件刚装进去那一刻还是干净的，
        预算一紧，淘汰会一路走到表尾把它自己删掉；调用方拿到的对象
        已经不在缓存里，写在它上面的字节没有任何人看得见。
        内存紧张时成批丢记录就是这么来的——而且丢得毫无声响，
        直到读的时候才发现键找不着了。
        """
        if self._bytes > self._max_bytes:
            self.evict()

    # ── 改构件 ──────────────────────────────────────────────────────

    def touch_block(self, blk):
        """
        标脏。**顺手确认它还在缓存里**：不在就说明它是出手之后被淘汰掉的，
        那么写在它上面的字节没有任何人看得见，而且丢得无声无息——直到
        有人来读那个键才发现它没了。这种时候宁可当场炸掉。

        代价是一次 dict 查找，写路径上本来就要做的量级。
        """
        if self._blocks.get((blk.rid, blk.idx)) is not blk:
            raise RuntimeError(
                '块 %d:%d 已经不在缓存里了，写在它上面的字节会丢' % (blk.rid, blk.idx))
        if not blk.dirty:
            blk.dirty = True
            self._dirty_bytes += blk.size

    def touch_header(self, rid):
        member = self._headers.get(rid)
        if member is not None and not member.dirty:
            member.dirty = True
            self._dirty_bytes += member.size

    def stage_record(self, rid, offset, raw):
        """
        刚写下的记录进缓冲并标脏。

        这条记录此刻**只存在于内存里**——落盘要等下一次 flush，而且必须
        排在它对应的槽前面。脏构件不会被淘汰（见 evict），所以不存在
        "还没落盘就被挤出去"的可能。
        """
        key = (rid, offset)
        if not isinstance(raw, bytearray):
            raw = bytearray(raw)
        self._trim()          # 先腾地方：这一步之后进来的记录是脏的，谁也动不了它
        old = self._records.get(key)
        if old is not None:
            self._charge(old, -old.size)
        rec = Record(rid, offset, raw, dirty=True)
        self._records[key] = rec
        self._records.move_to_end(key)
        self._charge(rec, rec.size)

    def set_joint(self, blk, tenon=None, mortise=None, slots_used=None):
        """
        写榫卯契口：就地改块头的 tenon/mortise 并重算 CRC，同步副本，标脏。

        **块与块的承接是两次独立的调用**——块 A 记下"我溢出去哪儿"，
        块 B 记下"我承接了谁"——所以两个参数各自可以为 None。
        调用方（Arena）负责成对写；只写一边不会让盘上数据读不回来
        （槽是自描述的，键比对兜底），只是让链不完整，verify() 会报出来。
        """
        if not sf.set_block_joint(blk.raw, 0, slots_used=slots_used,
                                  tenon=tenon, mortise=mortise):
            return False
        blk.slots_used, blk.slots_total, blk.tenon, blk.mortise = \
            sf.read_block_joint(blk.raw)
        self.touch_block(blk)
        return True

    # ── 淘汰 ────────────────────────────────────────────────────────

    def evict(self, target=None):
        """
        按 LRU 淘汰**干净**构件，降回预算以内。

        **脏构件一律不动**：它们的内容盘上还没有，丢一个就是丢一条
        数据或一整块索引。淘汰不动就返回 False——那是在说"该刷了"，
        由调用方决定什么时候刷。

        成组淘汰：淘汰一个块时，如果它的榫卯伙伴也在缓存里且干净，
        两个一起走。伙伴脏或不在缓存就只淘汰它自己——**不为了成组而
        扣着该走的不放**，那会让淘汰卡死。
        （诚实说：当前链是线性的，伙伴就是相邻块，LRU 本来也快把它们
        挨着淘汰了，所以这一步眼下接近白做。留着是因为链的规则可能变。）
        """
        target = self._max_bytes if target is None else target
        for table in (self._records, self._blocks):
            if self._bytes <= target:
                return True
            for key in list(table):
                if self._bytes <= target:
                    return True
                member = table[key]
                if member.dirty:
                    continue
                del table[key]
                self._charge(member, -member.size)
                self.stats['evicted'] += 1
                if isinstance(member, Block):
                    self._evict_mate(member)
        return self._bytes <= target

    def _evict_mate(self, blk):
        """榫卯伙伴一起走——前提是它也在缓存里，而且干净。"""
        for idx in (blk.mortise, blk.tenon):
            if idx == sf.NO_BLOCK or idx == blk.idx:
                continue
            mate = self._blocks.get((blk.rid, idx))
            if mate is None or mate.dirty:
                continue
            del self._blocks[(blk.rid, idx)]
            self._charge(mate, -mate.size)
            self.stats['evicted'] += 1
            self.stats['evicted_mate'] += 1

    # ── 落盘 ────────────────────────────────────────────────────────

    def flush(self, sink, sync=False):
        """
        按榫卯次序落盘：**记录 → 块 → 区域头**。三次提交，一级比一级晚。

        sink 要实现：
            write_records([(rid, offset, bytes), ...])
            write_blocks([(rid, idx, bytearray), ...])
            write_header(rid, region_header)
            sync()                      # 可选，sync=True 时调用

        次序不能乱。第 1、2 步之间断电的后果是"几个没人指向的字节"，
        反过来就是"槽指着一片垃圾"——前者浪费空间，后者损坏数据。
        """
        wrote = False

        # 阶段一：记录。数据区从区域尾部向前生长，所以按偏移**从大到小**
        # 写正好等于按写入先后写，文件游标一路向前不回头。
        recs = [r for r in self._records.values() if r.dirty]
        if recs:
            recs.sort(key=lambda r: (r.rid, -r.offset))
            sink.write_records([(r.rid, r.offset, r.raw) for r in recs])
            for r in recs:
                r.dirty = False
                self._dirty_bytes -= r.size
            self.stats['written_records'] += len(recs)
            wrote = True

        # 阶段二：块。按榫卯组排，互相承接的块相邻落盘。
        blks = [b for b in self._blocks.values() if b.dirty]
        if blks:
            ordered = self._order_blocks(blks)
            sink.write_blocks([(b.rid, b.idx, b.raw) for b in ordered])
            for b in ordered:
                b.dirty = False
                self._dirty_bytes -= b.size
            self.stats['written_blocks'] += len(ordered)
            wrote = True

        # 阶段三：区域头。**最后写**——它是"前面那些块都已经交出去了"的
        # 声明，先写就等于提了一个还没兑现的承诺。
        for member in self._headers.values():
            if member.dirty:
                sink.write_header(member.rid, member.hdr)
                member.dirty = False
                self._dirty_bytes -= member.size
                wrote = True

        if sync and hasattr(sink, 'sync'):
            sink.sync()
        if wrote:
            self.stats['flushes'] += 1
        return wrote

    def _order_blocks(self, items):
        """
        把脏块排成榫卯组，互相承接的相邻落盘。

        当前规则下链是线性的（溢出就往后挪一格），所以结果就是按
        (区域, 块号) 排序——**它现在接近恒等变换**。留着它是因为链的
        规则是可能变的：一旦改成真正的溢出链，承接关系就不再等于相邻，
        这个函数是那时唯一需要保证次序的地方。顺带它也让跨区域的块
        聚在一起，减少文件之间的来回跳。
        """
        items.sort(key=lambda b: (b.rid, b.idx))
        return items

    # ── 区域维护 ────────────────────────────────────────────────────

    def drop_region(self, rid):
        """丢掉一个区域的全部构件。整区压实或删区时用。"""
        for table, pick in ((self._blocks, lambda m: m.rid),
                            (self._records, lambda m: m.rid)):
            for key in [k for k, m in table.items() if pick(m) == rid]:
                member = table.pop(key)
                self._charge(member, -member.size)
        member = self._headers.pop(rid, None)
        if member is not None:
            # 脏也不写了——文件都要没了，写它没有意义
            self._charge(member, -member.size)

    def stats_report(self):
        out = dict(self.stats)
        out['bytes'] = self._bytes
        out['dirty_bytes'] = self._dirty_bytes
        out['max_bytes'] = self._max_bytes
        out['blocks_resident'] = len(self._blocks)
        out['records_resident'] = len(self._records)
        return out
