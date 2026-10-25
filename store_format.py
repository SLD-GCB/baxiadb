# -*- coding: utf-8 -*-
"""
霸下存储协议 — 永久存储区的字节布局
===================================

这是**盘上格式**，和 protocol.py 是两回事：

    protocol.py    两个进程怎么对话     网络上的帧
    store_format   字节在永久存储区里怎么摆   盘上的结构

本模块只定义布局与编解码，不碰文件 I/O。这样布局能单独灌字节验证，
也方便之后换承载（文件、裸设备、内存映射）而不动协议。

布局总览
--------

    永久存储区
    ├─ 超级块        偏移 0，64 字节
    ├─ 填充          至 4096（对齐到区块）
    ├─ 根数组        4096 条 × 8 字节 = 32KB      区域号 → 一级块偏移
    ├─ 一级块区      每块 256 条区域描述符 × 48 = 12KB
    └─ 区域数据区
         区域
         ├─ 区域头     64 字节
         ├─ 块 0       块头 32 + 槽 × m（每槽 32）
         ├─ 块 1       ...
         └─ 数据尾区   ← 从区域尾部向前生长，装变长的键与值

寻址全可算，不查任何树：

    区域号 ──(根数组一次查表)──> 一级块 ──> 区域
    块号   = 键哈希 % 区域块数
    槽号   = (键哈希 // 区域块数) % 每块槽数

所以"直取"的准确含义是 **跳转次数有常数上界**：根数组常驻内存，
点查的 I/O 上界是"一级块 + 数据"两次。B+ 树的深度随数据长，这里不长。

为什么槽是定长而键值在别处
--------------------------
槽必须能被 `哈希 % 槽数` 直接算出来，所以槽的尺寸必须固定，否则槽号
到字节偏移就不是乘法而是查表。变长的键和值因此放在区域尾部的数据区，
槽里只留 (哈希, 偏移, 长度)。代价是每条记录多一跳间接，
换来的是槽区可以纯算术寻址。

关于区域切分维度
----------------
区域头里的 `dim_value` 是个 uint64，代表本区在切分维度上的取值。
时间戳、哈希前缀都能塞进去。描述符里的 (dim_low, dim_high) 是区域
在维度上的覆盖范围，用于判断"该不该碰这个区"。
本层不关心维度语义——按时间切还是按哈希切，是上层策略。
"""

import hashlib
import struct
import zlib

__all__ = [
    'MAGIC_SUPER', 'MAGIC_REGION', 'MAGIC_BLOCK', 'VERSION',
    'BLOCK_SIZE', 'SUPER_SIZE', 'ROOT_ARRAY_OFFSET', 'ROOT_ENTRIES',
    'LEVEL1_SHIFT', 'LEVEL1_ENTRIES', 'MAX_REGIONS',
    'DESC_SIZE', 'REGION_HEADER_SIZE', 'BLOCK_HEADER_SIZE', 'SLOT_SIZE',
    'RegionState', 'SlotState',
    'key_hash', 'split_region_id', 'find_slot', 'find_block', 'address_of',
    'Superblock', 'RegionDescriptor', 'RegionHeader', 'BlockHeader', 'Slot',
    'NO_BLOCK', 'read_block_joint', 'set_block_joint',
    'plan_region', 'region_layout', 'block_offset', 'slot_offset',
    'MAGIC_BLOOM', 'BLOOM_FP_RATE', 'BloomFilter', 'bloom_size_for',
    'REGION_DIR', 'BUCKET_SHIFT', 'CATALOG_NAME', 'bucket_of',
    'region_relpath', 'region_filename', 'bloom_relpath', 'bloom_filename',
    'region_id_of_file',
]

# ── 常量 ────────────────────────────────────────────────────────────

MAGIC_SUPER = b'BAXS'          # 超级块
MAGIC_REGION = 0x52474E31      # 'RGN1'
MAGIC_BLOCK = 0x424C4B31       # 'BLK1'
VERSION = 1

BLOCK_SIZE = 4096              # 最小 I/O 单位

SUPER_SIZE = 64
ROOT_ARRAY_OFFSET = BLOCK_SIZE
ROOT_ENTRIES = 4096            # 根数组条目数 → 常驻内存 32KB
LEVEL1_SHIFT = 8
LEVEL1_ENTRIES = 1 << LEVEL1_SHIFT          # 256 条描述符 / 一级块
MAX_REGIONS = ROOT_ENTRIES * LEVEL1_ENTRIES  # 1,048,576 个区域

DESC_SIZE = 48
REGION_HEADER_SIZE = 64
BLOCK_HEADER_SIZE = 32
SLOT_SIZE = 32

LEVEL1_OFFSET = ROOT_ARRAY_OFFSET + ROOT_ENTRIES * 8   # 36864


class RegionState(object):
    ACTIVE = 0     # 活跃：唯一可写的区
    SEALED = 1     # 封存：只读，榫卯已重整紧实，地址永久有效
    DEAD = 2       # 废弃：整区可回收
    BROKEN = 3     # 损坏：巡检发现校验不过，待修复


class SlotState(object):
    EMPTY = 0
    LIVE = 1
    DEAD = 2       # 已失效但未回收——区域封存前不会真的抹掉


def _crc(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def key_hash(key):
    """
    键哈希：直取的定位依据，也是去重的身份。

    必须是**跨进程、跨机器稳定**的。所以不能用 Python 内置 hash()——
    它受 PYTHONHASHSEED 影响，重启就变，写进盘里的槽号会全部失效。
    blake2b 截 64 位，稳定且碰撞率对 64 位空间足够。
    """
    if not isinstance(key, (bytes, bytearray, memoryview)):
        key = str(key).encode('utf-8')
    digest = hashlib.blake2b(bytes(key), digest_size=8).digest()
    return int.from_bytes(digest, 'little')


# ── 地址计算 ────────────────────────────────────────────────────────

def split_region_id(region_id):
    """区域号 → (根数组下标, 一级块内下标)。"""
    if not 0 <= region_id < MAX_REGIONS:
        raise ValueError('区域号越界: %d (上限 %d)' % (region_id, MAX_REGIONS))
    return region_id >> LEVEL1_SHIFT, region_id & (LEVEL1_ENTRIES - 1)


def find_slot(h, slots_per_block):
    return (h // slots_per_block) % slots_per_block if slots_per_block else 0


def find_block(h, blocks):
    return h % blocks


def address_of(key, blocks, slots_per_block):
    """
    键 → (块号, 槽号)，纯算术。

    两个维度用哈希的不同位段计算，避免块号和槽号强相关——否则
    哈希低位有规律时，冲突会全部挤进同一个块。
    """
    h = key_hash(key)
    return find_block(h, blocks), find_slot(h, slots_per_block)


class _Struct(object):
    """带 CRC32 自校验的定长结构基类。"""

    STRUCT = None
    SIZE = 0
    CRC_OFFSET = 0

    def _seal(self, raw):
        """填充末尾的 CRC 字段。raw 是 bytearray，CRC 覆盖 [0:CRC_OFFSET]。"""
        struct.pack_into('<I', raw, self.CRC_OFFSET,
                         _crc(bytes(raw[:self.CRC_OFFSET])))
        return bytes(raw)

    @classmethod
    def _verify(cls, raw):
        if raw[:cls.CRC_OFFSET] is None:
            return
        stored = struct.unpack_from('<I', raw, cls.CRC_OFFSET)[0]
        if _crc(bytes(raw[:cls.CRC_OFFSET])) != stored:
            raise ValueError('%s CRC32 校验失败' % cls.__name__)

    @classmethod
    def size(cls):
        return cls.SIZE


class Superblock(_Struct):
    """
    超级块。永久存储区的入口，固定落在偏移 0。

    它是整条寻址链的封底：根数组常驻内存靠的就是这里写死的
    root_offset / root_entries——**这个封底不能随数据增长**，
    否则递归就没有底，最终会长回一棵 B+ 树。
    """

    STRUCT = struct.Struct('<4sHHIIQIIIIQIIII')
    SIZE = SUPER_SIZE
    CRC_OFFSET = 60

    __slots__ = ('magic', 'version', 'state', 'block_size', 'region_capacity',
                 'root_offset', 'root_entries', 'level1_entries',
                 'next_region', 'total_records', 'region_count')

    def __init__(self, region_capacity, block_size=BLOCK_SIZE,
                 root_offset=ROOT_ARRAY_OFFSET, root_entries=ROOT_ENTRIES,
                 level1_entries=LEVEL1_ENTRIES):
        self.magic = MAGIC_SUPER
        self.version = VERSION
        self.state = 0
        self.block_size = block_size
        self.region_capacity = region_capacity
        self.root_offset = root_offset
        self.root_entries = root_entries
        self.level1_entries = level1_entries
        self.next_region = 0        # 下一个待分配的区域号
        self.total_records = 0
        self.region_count = 0

    def encode(self):
        raw = bytearray(self.STRUCT.pack(
            self.magic, self.version, self.state, self.block_size,
            self.region_capacity, self.root_offset, self.root_entries,
            self.level1_entries, self.next_region, 0,
            self.total_records, self.region_count, 0, 0, 0))
        return self._seal(raw)

    @classmethod
    def decode(cls, raw):
        if len(raw) < cls.SIZE:
            raise ValueError('超级块不足 %d 字节' % cls.SIZE)
        if raw[:4] != MAGIC_SUPER:
            raise ValueError('不是霸下存储区：魔术字 %r' % bytes(raw[:4]))
        cls._verify(raw)
        (magic, version, state, block_size, region_capacity, root_offset,
         root_entries, level1_entries, next_region, _,
         total_records, region_count, _, _, _) = cls.STRUCT.unpack_from(raw)
        if version != VERSION:
            raise ValueError('存储协议版本不符: %d (本程序支持 %d)'
                             % (version, VERSION))
        obj = cls.__new__(cls)
        obj.magic, obj.version, obj.state = magic, version, state
        obj.block_size, obj.region_capacity = block_size, region_capacity
        obj.root_offset, obj.root_entries = root_offset, root_entries
        obj.level1_entries = level1_entries
        obj.next_region, obj.total_records = next_region, total_records
        obj.region_count = region_count
        return obj


class RegionDescriptor(_Struct):
    """
    一级块里的一条区域描述符。查找时读它——**点查的第一道关卡**。

    bloom_bytes 是布隆过滤器文件的字节数；为 0 表示本区没有可用布隆
    （还在活跃、或生成失败），点查必须实扫进去。

    原设计里这个字段叫 phys_offset（区域在单一大文件里的偏移）。
    改成每区域独立文件之后，区域号到文件名是可算的，偏移没有意义了，
    于是腾出来给布隆大小用——它恰好是点查时最先要问的东西。
    """

    STRUCT = struct.Struct('<IBBHQIIQQII')
    SIZE = DESC_SIZE
    CRC_OFFSET = 44

    __slots__ = ('region_id', 'state', 'tier', 'bloom_bytes', 'capacity',
                 'record_count', 'dim_low', 'dim_high')

    def __init__(self, region_id, state=RegionState.ACTIVE, bloom_bytes=0,
                 capacity=0, record_count=0, dim_low=0, dim_high=0, tier=0):
        self.region_id = region_id
        self.state = state
        self.tier = tier          # 预留：将来细分层用
        self.bloom_bytes = bloom_bytes
        self.capacity = capacity
        self.record_count = record_count
        self.dim_low = dim_low    # 本区在切分维度上的下界
        self.dim_high = dim_high

    def encode(self):
        raw = bytearray(self.STRUCT.pack(
            self.region_id, self.state, self.tier, 0, self.bloom_bytes,
            self.capacity, self.record_count, self.dim_low, self.dim_high,
            0, 0))
        return self._seal(raw)

    @classmethod
    def decode(cls, raw, offset=0):
        cls._verify(raw[offset:offset + cls.SIZE])
        (rid, state, tier, _, bloom, cap, count, low, high, _, _) = \
            cls.STRUCT.unpack_from(raw, offset)
        return cls(rid, state, bloom, cap, count, low, high, tier)

    def covers(self, dim):
        """本区在切分维度上是否覆盖 dim。用于跳过无关区域。"""
        return self.dim_low <= dim <= self.dim_high


class RegionHeader(_Struct):
    """
    区域头。每个区域的第一个字节就是它。

    prev_region / next_region 就是**榫头和卯眼**：区域在切分维度上的
    相邻关系，写死在头部字节里，不依赖任何外部索引。

    活跃区的榫是"软"的——新区域只写自己的 prev 指向老区，绝不回头
    改老区的 next（老区因此永不重写）。封存的那一刻才做一次榫卯重整，
    把链条双向补全、变成"硬榫"，之后整区只读、地址永久有效。
    """

    STRUCT = struct.Struct('<IIBBHIIIQIIQQII')
    SIZE = REGION_HEADER_SIZE
    CRC_OFFSET = 60

    __slots__ = ('magic', 'region_id', 'state', 'slot_shift', 'blocks',
                 'slots_per_block', 'data_tail', 'data_floor', 'dim_value',
                 'prev_region', 'next_region', 'record_count')

    def __init__(self, region_id, blocks, slots_per_block, capacity,
                 dim_value=0, state=RegionState.ACTIVE, slot_shift=5):
        self.magic = MAGIC_REGION
        self.region_id = region_id
        self.state = state
        self.slot_shift = slot_shift          # 槽大小 = 1 << slot_shift
        self.blocks = blocks
        self.slots_per_block = slots_per_block
        # 数据尾区从区域尾部向头部生长，所以 tail 递减、
        # 撞上 data_floor 就是区域满了，该封存了
        self.data_tail = capacity
        self.data_floor = region_layout(blocks, slots_per_block)[1]
        self.dim_value = dim_value
        self.prev_region = 0xFFFFFFFF         # 0xFFFFFFFF = 无
        self.next_region = 0xFFFFFFFF
        self.record_count = 0

    @property
    def slot_size(self):
        return 1 << self.slot_shift

    @property
    def total_slots(self):
        return self.blocks * self.slots_per_block

    def data_free(self):
        return self.data_tail - self.data_floor

    def full(self, need, slots_used):
        """
        区域该封存了吗。

        **两个区都要看**：数据尾区撑爆，或者槽位用尽。只判其中一个
        是常见的错——槽区先满时数据区还空着一半，区域却已经写不进去了。
        """
        return (self.data_tail - need < self.data_floor
                or slots_used >= self.total_slots)

    def encode(self):
        raw = bytearray(self.STRUCT.pack(
            self.magic, self.region_id, self.state, self.slot_shift,
            self.blocks, self.slots_per_block, self.data_tail,
            self.data_floor, self.dim_value, self.prev_region,
            self.next_region, self.record_count, 0, 0, 0))
        return self._seal(raw)

    @classmethod
    def decode(cls, raw, offset=0):
        chunk = raw[offset:offset + cls.SIZE]
        if len(chunk) < cls.SIZE:
            raise ValueError('区域头不足 %d 字节' % cls.SIZE)
        if struct.unpack_from('<I', chunk, 0)[0] != MAGIC_REGION:
            raise ValueError('区域头魔术字不符')
        cls._verify(chunk)
        (magic, rid, state, shift, blocks, spb, tail, floor, dim,
         prev_r, next_r, count, _, _, _) = cls.STRUCT.unpack_from(chunk)
        obj = cls.__new__(cls)
        obj.magic, obj.region_id, obj.state = magic, rid, state
        obj.slot_shift, obj.blocks, obj.slots_per_block = shift, blocks, spb
        obj.data_tail, obj.data_floor, obj.dim_value = tail, floor, dim
        obj.prev_region, obj.next_region = prev_r, next_r
        obj.record_count = count
        return obj


class BlockHeader(_Struct):
    """
    块头。块是榫卯的**构件单位**——槽在块内定长排布，
    块与块之间靠 tenon/mortise 咬合。
    """

    STRUCT = struct.Struct('<IIIHHIIII')
    SIZE = BLOCK_HEADER_SIZE
    CRC_OFFSET = 28

    __slots__ = ('magic', 'region_id', 'block_idx', 'slots_used',
                 'slots_total', 'tenon', 'mortise')

    def __init__(self, region_id, block_idx, slots_total, slots_used=0,
                 tenon=0xFFFFFFFF, mortise=0xFFFFFFFF):
        self.magic = MAGIC_BLOCK
        self.region_id = region_id
        self.block_idx = block_idx
        self.slots_used = slots_used
        self.slots_total = slots_total
        self.tenon = tenon        # 榫头：前驱块号
        self.mortise = mortise    # 卯眼：后继块号

    def encode(self):
        raw = bytearray(self.STRUCT.pack(
            self.magic, self.region_id, self.block_idx, self.slots_used,
            self.slots_total, self.tenon, self.mortise, 0, 0))
        return self._seal(raw)

    @classmethod
    def decode(cls, raw, offset=0):
        chunk = raw[offset:offset + cls.SIZE]
        cls._verify(chunk)
        (magic, rid, bidx, used, total, tenon, mortise, _, _) = \
            cls.STRUCT.unpack_from(chunk)
        if magic != MAGIC_BLOCK:
            raise ValueError('块头魔术字不符')
        return cls(rid, bidx, total, used, tenon, mortise)


# 块头字段的裸字节偏移。写入路径就地改 slots_used / tenon / mortise 时
# 用它，不走 decode+encode。
NO_BLOCK = 0xFFFFFFFF
_BH_SLOTS_USED_OFF = 12
_BH_SLOTS_TOTAL_OFF = 14
_BH_TENON_OFF = 16
_BH_MORTISE_OFF = 20
_BH_CRC_OFF = BlockHeader.CRC_OFFSET


def read_block_joint(raw, offset=0):
    """读块头的 (槽已用, 槽总数, tenon, mortise)，不做 CRC 校验。"""
    return (struct.unpack_from('<H', raw, offset + _BH_SLOTS_USED_OFF)[0],
            struct.unpack_from('<H', raw, offset + _BH_SLOTS_TOTAL_OFF)[0],
            struct.unpack_from('<I', raw, offset + _BH_TENON_OFF)[0],
            struct.unpack_from('<I', raw, offset + _BH_MORTISE_OFF)[0])


def set_block_joint(raw, offset=0, slots_used=None, tenon=None, mortise=None):
    """
    就地改块头的槽数与榫卯字段，重算 CRC32。传 None 的字段不动。

    **故意不走 BlockHeader.decode + encode**：那条路要校验 CRC、构造
    对象、再编码回来，而这里每次槽溢出都会走到。裸字节改写只碰三个
    字段。分开传 None 是因为"块 A 记下后继"和"块 B 记下前驱"是两次
    独立的调用，各自只该改一边。

    返回是否真的动了字节——没动就不必把块标脏。
    """
    changed = False
    if slots_used is not None:
        struct.pack_into('<H', raw, offset + _BH_SLOTS_USED_OFF, slots_used)
        changed = True
    if tenon is not None:
        struct.pack_into('<I', raw, offset + _BH_TENON_OFF, tenon)
        changed = True
    if mortise is not None:
        struct.pack_into('<I', raw, offset + _BH_MORTISE_OFF, mortise)
        changed = True
    if not changed:
        return False
    crc = _crc(bytes(raw[offset:offset + _BH_CRC_OFF]))
    struct.pack_into('<I', raw, offset + _BH_CRC_OFF, crc)
    return True


class Slot(_Struct):
    """
    槽。定长 32 字节，可被 `哈希 % 槽数` 直接算出偏移。

    键和值不在槽里——它们在区域尾部的数据区，槽只存
    (哈希, 偏移, 长度)。代价是一跳间接，换来槽区可以纯算术寻址。

    写入顺序必须是**先数据、后槽**：数据区记录先落盘，槽最后写。
    这样断电时最坏情况是数据区多了几个没人指向的字节（浪费，
    可回收），而不是槽指着一片垃圾（读到坏数据）。
    """

    STRUCT = struct.Struct('<BBHQIIIII')
    SIZE = SLOT_SIZE
    CRC_OFFSET = 28

    __slots__ = ('state', 'key_len', 'key_hash', 'rec_offset', 'val_len',
                 'version')

    def __init__(self, key_hash_value=0, key_len=0, rec_offset=0, val_len=0,
                 version=0, state=SlotState.EMPTY):
        self.state = state
        self.key_len = key_len
        self.key_hash = key_hash_value
        self.rec_offset = rec_offset
        self.val_len = val_len
        self.version = version

    @property
    def rec_len(self):
        return self.key_len + self.val_len

    def encode(self):
        raw = bytearray(self.STRUCT.pack(
            self.state, 0, self.key_len, self.key_hash, self.rec_offset,
            self.val_len, self.version, 0, 0))
        return self._seal(raw)

    @classmethod
    def decode(cls, raw, offset=0):
        chunk = raw[offset:offset + cls.SIZE]
        if len(chunk) < cls.SIZE:
            raise ValueError('槽不足 %d 字节' % cls.SIZE)
        cls._verify(chunk)
        (state, _, klen, h, off, vlen, ver, _, _) = cls.STRUCT.unpack_from(chunk)
        return cls(h, klen, off, vlen, ver, state)

    def encode_record(self, key, value):
        """数据区记录：[键][值]。长度存在槽里，但这里也做一次自洽校验。"""
        if not isinstance(key, (bytes, bytearray)):
            key = str(key).encode('utf-8')
        if not isinstance(value, (bytes, bytearray)):
            value = bytes(value)
        if len(key) > 0xFFFF:
            raise ValueError('键长 %d 超过 65535' % len(key))
        return bytes(key) + bytes(value)

    @staticmethod
    def decode_record(raw, offset, key_len, val_len):
        need = key_len + val_len
        chunk = raw[offset:offset + need]
        if len(chunk) < need:
            raise ValueError('数据区记录越界：需要 %d，实得 %d'
                             % (need, len(chunk)))
        return bytes(chunk[:key_len]), bytes(chunk[key_len:])


# ── 区域布局规划 ────────────────────────────────────────────────────

def region_layout(blocks, slots_per_block):
    """
    (区域头 + 全部块) 占用的字节数，以及数据尾区的下界。
    返回 (块区结束偏移, 数据区起点)。两者相同——数据区紧接着块区，
    从区域尾部向前生长。
    """
    block_bytes = BLOCK_HEADER_SIZE + slots_per_block * SLOT_SIZE
    end = REGION_HEADER_SIZE + blocks * block_bytes
    return end, end


def plan_region(capacity, avg_record_size=1024,
                target_block_bytes=BLOCK_SIZE):
    """
    给定区域容量和**平均记录大小**，规划出 (块数, 每块槽数)。

    块大小落在区块（4KB）以内，这样哈希定位到块之后，一次 I/O 就能把
    整块连同它的槽全部读进来，块内冲突的探测代价是内存里的常数，
    不再产生额外 I/O。

    槽区和数据区的比例**必须由平均记录大小倒推，不能拍脑袋**。
    每个槽固定 32 字节，每条记录占 (键长+值长) 字节，所以要让
    槽数和记录容量同时用尽：

        块数 × 每块槽数 × 平均记录 = 容量 - 块数 × 每块字节

    解得 块数 = 容量 / (每块槽数 × 平均记录 + 每块字节)

    记录越大，槽区占比越小——1KB 的记录下槽区只占约 3%，
    100 字节的记录下则要占到 24%。用小记录的参数去规划大记录的库，
    数据区会先撑爆；反过来则是槽区撑爆。
    """
    slots = max(1, (target_block_bytes - BLOCK_HEADER_SIZE) // SLOT_SIZE)
    per_block = BLOCK_HEADER_SIZE + slots * SLOT_SIZE

    denom = slots * max(1, avg_record_size) + per_block
    blocks = (capacity - REGION_HEADER_SIZE) // denom
    if blocks < 1:
        raise ValueError(
            '区域容量 %d 太小：平均记录 %d 字节时连一个块都排不下'
            % (capacity, avg_record_size))

    floor, _ = region_layout(blocks, slots)
    if floor >= capacity:
        raise ValueError('区域容量 %d 太小：槽区就吃掉了 %d 字节'
                         % (capacity, floor))
    return blocks, slots


def block_offset(block_idx, slots_per_block):
    return REGION_HEADER_SIZE + block_idx * \
        (BLOCK_HEADER_SIZE + slots_per_block * SLOT_SIZE)


def slot_offset(block_idx, slot_idx, slots_per_block):
    return block_offset(block_idx, slots_per_block) + BLOCK_HEADER_SIZE + \
        slot_idx * SLOT_SIZE


# ── 布隆过滤器 ──────────────────────────────────────────────────────
#
# 单活跃区 + 只追加推出"区域按写入顺序切"，于是点查没法从键推出该去
# 哪个区。每区配一个布隆过滤器、常驻内存，就是让点查先在内存里筛掉
# 绝大多数区，只进真正可能命中的那一两个。
#
# 内存账要算清楚：256MB 区域按 1KB 记录约 25 万条，1% 误判率需要
# 约 300KB。几百个区（几十 TB）是几十 MB，可以接受；区域数再往上
# 走就要分层了。所以描述符里记 bloom_bytes，为 0 表示本区没有可用
# 布隆、点查必须实扫——缺布隆只是变慢，不会出错。

MAGIC_BLOOM = b'BAXB'
BLOOM_FP_RATE = 0.01           # 目标误判率


class BloomFilter(object):
    """
    布隆过滤器。**只会误判"有"，绝不误判"没有"**——这个性质是它能
    安全参与点查的原因：说没有就一定没有，可以直接跳过该区。

    位数组按 Kirsch-Mitzenmacher 用一个键哈希派生出 k 个位置，
    不必做 k 次独立哈希。
    """

    HEADER = struct.Struct('<4sHHIIIBBHII')
    HEADER_SIZE = 32
    CRC_OFFSET = 28

    __slots__ = ('region_id', 'n_items', 'm_bits', 'k', '_bits')

    def __init__(self, n_items, region_id=0, fp_rate=BLOOM_FP_RATE):
        n_items = max(1, int(n_items))
        ln2 = 0.6931471805599453
        m = int(-n_items * _log(fp_rate) / (ln2 * ln2)) + 1
        k = max(1, int(round(m / float(n_items) * ln2)))
        self.region_id = region_id
        self.n_items = n_items
        self.m_bits = m
        self.k = min(k, 64)
        self._bits = bytearray((m + 7) // 8)

    def _positions(self, kh):
        """键哈希 → k 个位位置。两个半字做步进，避免再做 k 次哈希。"""
        h1 = kh & 0xFFFFFFFF
        h2 = ((kh >> 32) | 1) & 0xFFFFFFFF
        m = self.m_bits
        for i in range(self.k):
            yield (h1 + i * h2) % m

    def add(self, kh):
        bits = self._bits
        for pos in self._positions(kh):
            bits[pos >> 3] |= (1 << (pos & 7))

    def might_contain(self, kh):
        bits = self._bits
        for pos in self._positions(kh):
            if not (bits[pos >> 3] & (1 << (pos & 7))):
                return False       # 一位为 0 即确定不在，这是硬保证
        return True

    def error_rate(self):
        """当前填充率下的实际误判率，用于巡检布隆是否已经饱和。"""
        ones = sum(bin(b).count('1') for b in self._bits)
        filled = ones / float(self.m_bits) if self.m_bits else 0.0
        return filled ** self.k

    def encode(self):
        bits = bytes(self._bits)
        head = self.HEADER.pack(
            MAGIC_BLOOM, VERSION, 0, self.region_id, self.n_items,
            self.m_bits, self.k, 0, 0, len(bits), 0)
        crc = zlib.crc32(head[:self.CRC_OFFSET])
        crc = zlib.crc32(bits, crc)
        return head[:self.CRC_OFFSET] + struct.pack('<I', crc) + bits

    @classmethod
    def decode(cls, raw):
        if len(raw) < cls.HEADER_SIZE:
            raise ValueError('布隆文件不足 %d 字节' % cls.HEADER_SIZE)
        (magic, ver, _, rid, n, m, k, _, _, blen, crc) = \
            cls.HEADER.unpack_from(raw, 0)
        if magic != MAGIC_BLOOM:
            raise ValueError('布隆魔术字不符: %r' % bytes(magic))
        if len(raw) < cls.HEADER_SIZE + blen:
            raise ValueError('布隆位数组被截断: 需要 %d，实得 %d'
                             % (blen, len(raw) - cls.HEADER_SIZE))
        calc = zlib.crc32(raw[:cls.CRC_OFFSET])
        calc = zlib.crc32(raw[cls.HEADER_SIZE:cls.HEADER_SIZE + blen], calc)
        if calc != crc:
            raise ValueError('布隆 CRC32 校验失败')
        obj = cls.__new__(cls)
        obj.region_id, obj.n_items, obj.m_bits, obj.k = rid, n, m, k
        obj._bits = bytearray(raw[cls.HEADER_SIZE:cls.HEADER_SIZE + blen])
        return obj

    @property
    def bytes_used(self):
        return self.HEADER_SIZE + len(self._bits)


def bloom_size_for(n_items, fp_rate=BLOOM_FP_RATE):
    """预先算出布隆文件会有多大，供描述符在不生成过滤器时就记录大小。"""
    ln2 = 0.6931471805599453
    m = int(-max(1, n_items) * _log(fp_rate) / (ln2 * ln2)) + 1
    return BloomFilter.HEADER_SIZE + (m + 7) // 8


def _log(x):
    import math
    return math.log(x)


# ── 区域文件的盘上组织 ──────────────────────────────────────────────
#
# 每区域独立文件，按区域号高位分桶进子目录，任何单个目录下不超过
# 256 个文件——大规模部署时 ls / 备份 / rsync / fsck 才不会卡死。
#
#     <存储区>/r/00a/r00000a3f.region
#     <存储区>/r/00a/r00000a3f.bloom

REGION_DIR = 'r'
BUCKET_SHIFT = 8               # 每桶 256 个区域
CATALOG_NAME = 'catalog'       # 超级块 + 根数组 + 一级块区都在这一个文件里


def bucket_of(region_id):
    return region_id >> BUCKET_SHIFT


def region_relpath(region_id):
    return '%s/%03x/%s' % (REGION_DIR, bucket_of(region_id),
                           region_filename(region_id))


def region_filename(region_id):
    return 'r%08x.region' % region_id


def bloom_relpath(region_id):
    return '%s/%03x/%s' % (REGION_DIR, bucket_of(region_id),
                           bloom_filename(region_id))


def bloom_filename(region_id):
    return 'r%08x.bloom' % region_id


def region_id_of_file(name):
    """
    文件名 -> 区域号，认不出来就是 None。

    **反过来解名是有用的**：开库时要找出"盘上有文件、catalog 里没有它
    描述符"的孤儿（压实崩在"已注销、还没删"之间留下的就是这种）。那件
    事不能靠猜文件名——所以这个解析和上面两个生成函数放在一起，一处
    定义、一处解析，改了名字不会只改一半。

    只认 `r<8 位十六进制>.region` 和 `.bloom` 两种形状，别的一律 None：
    删文件这件事上，"认不出来就不碰"是唯一安全的默认。
    """
    if not name.startswith('r'):
        return None
    stem, dot, ext = name[1:].partition('.')
    if dot != '.' or ext not in ('region', 'bloom') or len(stem) != 8:
        return None
    try:
        return int(stem, 16)
    except ValueError:
        return None
