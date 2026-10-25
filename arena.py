# -*- coding: utf-8 -*-
"""
霸下 arena 层 — 永久存储区的文件管理
====================================

`store_format` 定义字节怎么摆，这里负责把它们真正读写到盘上：
建区、分配、封存、崩溃恢复、批量落盘。

盘上组织（每区域独立文件，按区域号分桶）：

    <存储区>/
      catalog                     超级块 + 根数组 + 一级块区
      r/000/r00000000.region      区域文件
      r/000/r00000000.bloom       封存时生成的布隆过滤器
      r/00a/r00000a3f.region

采用的设计（每条都是定下来的，不是默认值）：

    每区域独立文件    区域 = 文件，区域号 → 文件名可算
    单活跃区、单写者  同时只有一个区可写，其余只读
    只追加            数据只追加；同一键在活跃区内原地更新槽指向
    批量 fsync        攒够一批再落盘，断电最多丢最后一批
    256MB 区域        文件数与粒度之间的折中
    单独 catalog      元数据一个文件，数据一堆文件
    每区布隆过滤器    点查先在内存筛区，不必实扫

三条关键性质
------------

**活跃区可变，封存区不可变。** 这是整个设计的支点。活跃区里槽可以
原地改写（把 rec_offset 指向新追加的数据），一旦封存就彻底只读，
地址永久有效。写新记录永远只追加数据区，不回头改任何封存区。

**点查从新到旧。** 只追加意味着同一个键可能有多个版本散在不同区域，
新的一定在区域号更大的区里，所以 `get` 按区域号降序找，第一个命中的
就是最新的。

**垮掉最多是多几个没人指向的字节。** 写入顺序固定为「先数据、后槽」。
断电时槽要么指向完整记录，要么还没写；不会出现槽指着一片垃圾。
数据区里那些没人指向的字节是纯粹的浪费，整区压实时回收。

区域的死：压实
--------------

区只生不死，久了盘上全是没人指向的旧版本。`compact()` 是唯一一处
**删文件**的代码，它的全部工作只有一句：**把「扫描现在会看到的答案」
照原样搬到新区，再丢掉老区**。所以判据也就一句——压实前后扫出来的
「按键去重、取最新」的结果必须逐项相等。

它只认字节，不认表：引擎不知道表是什么，压实也不该知道。谁比谁新完全
由区域号表达（只追加 ⇒ 新版本总在更大的区号里），键只当作不透明字节
去重。

**次序即正确性。** 存活者必须先钉盘（提交点 A），描述符才允许注销
（提交点 B），文件才允许删。反过来 —— 先注销再搬，或者搬了没钉盘就
注销 —— 就是静默丢行。这和 `seal_active` 里「布隆必须先于描述符登记」
是同一种约束，看代码时别把它们当成可以顺手调换的两行。

崩在任何一步都不改变答案：副本没钉盘时源区完好；钉盘之后新旧并存而
新区号更大、扫描先命中新的；注销之后源区在库里已经不存在，而它的存活者
早就在新区里钉好了。最后剩下孤儿文件——那是纯粹的垃圾，开库时扫掉。

**代价要付得起**：压实期间盘上同时存在「存活者的副本」和「全部源区」，
所以需要一份和活数据等量的临时空间。区域号也不回收（上限见
`sf.MAX_REGIONS`）——复用旧号会让上层按区号记的账认错区，那是静默丢行。

关于内存里的东西去哪了
----------------------
块缓存、记录缓存、脏标记、淘汰、落盘次序——**全部搬进了 `mortise.py`**。
本模块不再自己缓存任何一个字节，读写一律经过榫卯缓冲层。Arena 在这里
的角色只剩三个：算地址、管文件句柄、管区域的生老病死。

引擎不管什么
------------

对外只有三个动作：**put（写字节）、get（按键取字节）、scan（遍历）**。
键是一串不透明的字节，值也是一串不透明的字节，引擎只做哈希定位和
原样搬运，不解释里面是什么。

**这里没有"删除"。** 以前有，实现是"写一个空值墓碑"——那是一个越过
边界替上层做的决定。删掉它不只是分层好看：空值墓碑和"值本来就是空串"
在盘上完全一样，读回来分不开，扫描也会把它当正常记录扫出去。只要引擎
还认得"删除"这个词，它就必然要对值的表示做假设，这个歧义就消不掉。

要表达删除，由上层用一个它自己定义的墓碑值去 put，读的时候自己判断。
引擎这边看到的只是一次普通的写入——而这正是它该看到的。

**引擎也不管键长什么样。** 主键怎么编、维度代表什么、键前缀是不是
表名，全是上层的事。这里只保证一件事：写进去的字节原样读得回来。
"""

import errno
import os
import time

import mortise
import store_format as sf

__all__ = ['Arena', 'ArenaError', 'ArenaConfig']

REGION_CAPACITY = 256 << 20      # 256MB
AVG_RECORD = 1024                # 用于倒推槽区比例
FSYNC_BYTES = 4 << 20            # 攒够 4MB 落一次盘
FSYNC_SECONDS = 1.0              # 或最多等 1 秒
LOAD_FACTOR = 0.85               # 槽位用到这个比例就封存
SPILL_BLOCKS = 8                 # 跨块溢出的硬闸：一个键最多探这么多块
BUFFER_BYTES = 64 << 20          # 榫卯缓冲层的内存预算

NO_REGION = 0xFFFFFFFF


class ArenaError(Exception):
    pass


class ArenaConfig(object):
    """存储区参数。容量和平均记录大小会写进超级块，开库时必须一致。"""

    def __init__(self, capacity=REGION_CAPACITY, avg_record=AVG_RECORD,
                 fp_rate=sf.BLOOM_FP_RATE, fsync_bytes=FSYNC_BYTES,
                 fsync_seconds=FSYNC_SECONDS, load_factor=LOAD_FACTOR,
                 spill_blocks=SPILL_BLOCKS, buffer_bytes=BUFFER_BYTES):
        self.capacity = capacity
        self.avg_record = avg_record
        self.fp_rate = fp_rate
        self.fsync_bytes = fsync_bytes
        self.fsync_seconds = fsync_seconds
        # 一个键最多探多少个块。**这是"直取"的硬承诺**：溢出被限在这个
        # 窗口里，超了就封区换新，所以点查读盘块数的上界是个常数，
        # 和库有多大无关。没有这道闸，探测链会随装载率悄悄拉长，
        # 之前那个"写得进去读不出来"的 bug 就是链长失控的直接结果。
        self.spill_blocks = spill_blocks
        self.buffer_bytes = buffer_bytes
        # 槽位用到这个比例就封存换区。**这不是省空间的开关，是正确性
        # 和性能的取舍**：探测是线性的，槽位填得越满，探测链越长；
        # 填到 100% 时链长退化，写进去的键可能落在读的窗口之外而永久
        # 失联。留出余量才能保证"写在哪儿就一定能从哪儿读回来"。
        self.load_factor = load_factor

    @property
    def blocks_slots(self):
        return sf.plan_region(self.capacity, self.avg_record)


class _密文句柄(object):
    """
    一个区文件句柄，**读写两侧各过一道密文**。

    包在区文件句柄的出口上（见 `Arena._上锁`），所以区文件里出去的
    每一个字节都从这里过。**这一层没碰 `store_format` 一个字节** ——
    流密码长度不变，第 N 个明文字节在盘上还是第 N 个，偏移全都不用换算。

    它认得的东西很少，就 `arena` 真正用到的那几个：`read` / `write` /
    `seek` / `tell` / `truncate` / `flush` / `fileno` / `close`。
    **别的属性原样透到里面那个句柄上**（`__getattr__`），所以
    `_HandleCache` 拿它当普通句柄关、`_RegionSink` 拿它当普通句柄
    `fsync` —— 那些地方一行都不用改。

    能这么薄，是因为**写的偏移永远是"当前位置"**：`_RegionSink` 每写
    一处都先 `seek`，`_allocate_region` 是顺序写。所以 `write` 现取一次
    `tell()` 就够，不用自己维护一条位置。
    """

    __slots__ = ('_fh', '_密文')

    def __init__(self, fh, 密文):
        self._fh = fh
        self._密文 = 密文

    def read(self, size=-1):
        # ⛔ 偏移要在**读之前**取。`read` 一返回位置就跑到后面去了，
        # 取晚了就是拿"读完"的位置去解"读之前"那段密文 —— 症状是
        # 前半段对、后半段乱，而且只在跨多次 read 的时候现形。
        位 = self._fh.tell()
        数据 = self._fh.read() if (size is None or size < 0) \
            else self._fh.read(size)
        return self._密文.解(位, 数据)

    def write(self, 数据):
        位 = self._fh.tell()
        self._fh.write(self._密文.加(位, 数据))
        # 报**明文的长度**回去。报密文的长度会让上层以为多写了一段。
        return len(数据)

    def seek(self, 位, 从=None):
        return self._fh.seek(位) if 从 is None else self._fh.seek(位, 从)

    def tell(self):
        return self._fh.tell()

    def truncate(self, 大小=None):
        return self._fh.truncate(大小)

    def flush(self):
        return self._fh.flush()

    def fileno(self):
        return self._fh.fileno()

    def close(self):
        return self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()
        return False

    def __getattr__(self, 名):
        # `_fh` 还没赋上就被问到时，别去碰它 —— 那会是无限递归。
        if 名 == '_fh':
            raise AttributeError(名)
        return getattr(self._fh, 名)

    def __repr__(self):
        return '<密文句柄 %r>' % (self._fh,)


class _HandleCache(object):
    """
    区域文件的句柄缓存。封存区是只读的，但点查要反复探它们，
    每次 open/close 太贵。上限一到就关掉最久未用的。

    **只管文件描述符，不管内容。** 内容归 mortise.MortiseBuffer 管，
    两层的淘汰互相独立：句柄被关掉不等于它上面的块被丢出缓存，
    块被淘汰也不等于要关句柄。分开是故意的——句柄是内核资源，
    块是内存，它们的稀缺程度和淘汰依据都不一样。
    """

    def __init__(self, limit=64):
        self._limit = limit
        self._fh = {}
        self._tick = {}

    def get(self, path, writable=False):
        # 同一个文件可能同时要只读和可写两个句柄（封存区的块要读，
        # 而封存时它自己的区域头还要最后写一次）。所以键带上模式。
        key = (path, writable)
        fh = self._fh.get(key)
        if fh is None:
            fh = open(path, 'r+b' if writable else 'rb', buffering=0)
            self._fh[key] = fh
        self._tick[key] = time.monotonic()

        while len(self._fh) > self._limit:
            oldest = min(self._tick, key=self._tick.get)
            if oldest == key:
                break               # 不淘汰刚用到的这一个
            victim = self._fh.pop(oldest, None)
            self._tick.pop(oldest, None)
            if victim is not None:
                try:
                    victim.close()
                except OSError:
                    pass
        return fh

    def close_path(self, path):
        """
        关掉某个路径上**所有模式**的句柄。删文件之前必须走这一步——
        键是 `(路径, 可写)`，同一个文件可能同时挂着只读和可写两个句柄，
        漏关一个，Windows 上 `os.remove` 就是一句 `PermissionError`。

        返回关掉了几个，给排错看。
        """
        n = 0
        for key in [k for k in self._fh if k[0] == path]:
            fh = self._fh.pop(key)
            self._tick.pop(key, None)
            try:
                fh.close()
            except OSError:
                pass
            n += 1
        return n

    def close_all(self):
        for fh in self._fh.values():
            try:
                fh.close()
            except OSError:
                pass
        self._fh.clear()
        self._tick.clear()


class _RegionSink(object):
    """
    缓冲层的落盘出口。**整个模块只有它往区域文件里写字节。**

    榫卯次序由缓冲层保证（记录 → 块 → 区域头），这里只负责把三个阶段
    各自写成正确的 write。句柄归 Arena 管，因为句柄的生命周期——活跃区
    常开、封存区 LRU——不是缓冲层该操心的事。

    偏移全部现算：块的字节偏移是 `块号 × 块字节数`，记录的偏移缓冲层
    直接给了。没有一张需要维护的偏移表。
    """

    __slots__ = ('_arena', '_touched', 'written')

    def __init__(self, arena):
        self._arena = arena
        self._touched = set()      # 这一轮碰过的区域，sync 时只刷它们
        self.written = {'records': 0, 'blocks': 0, 'headers': 0}

    def write_records(self, items):
        arena = self._arena
        for rid, offset, raw in items:
            fh = arena._region_fh(rid, writable=True)
            fh.seek(offset)
            fh.write(raw)
            self._touched.add(rid)
            self.written['records'] += 1

    def write_blocks(self, items):
        arena = self._arena
        per = arena._slots_per_block
        for rid, idx, raw in items:
            fh = arena._region_fh(rid, writable=True)
            fh.seek(sf.block_offset(idx, per))
            fh.write(raw)
            self._touched.add(rid)
            self.written['blocks'] += 1

    def write_header(self, rid, hdr):
        fh = self._arena._region_fh(rid, writable=True)
        fh.seek(0)
        fh.write(hdr.encode())
        self._touched.add(rid)
        self.written['headers'] += 1

    def sync(self):
        """真正的持久化屏障。**落盘次序和持久化次序是两件事**——
        前面那些 write 只进了页缓存，这一步才把它们钉到盘上。"""
        for rid in self._touched:
            try:
                fh = self._arena._region_fh(rid, writable=True)
            except (OSError, ArenaError):
                continue
            try:
                fh.flush()
                os.fsync(fh.fileno())
            except OSError:
                pass
        self._touched.clear()


class Arena(object):
    """
    永久存储区。

    单写者模型：本类不做任何加锁。调用方要么单线程使用，要么自己在
    外层串行化写入。读可以并发（封存区只读），但读写同一活跃区时
    仍需外部协调。
    """

    def __init__(self, root, config=None, 密文=None):
        self.root = root
        self.cfg = config or ArenaConfig()
        # 盘上那层锁。**`None` 就是明文库** —— 也就是今天的样子。
        #
        # 它不是 `玄武_钥` 的引用，是**鸭子类型**：只要有两个方法
        #     加(偏移, 明文) -> 密文
        #     解(偏移, 密文) -> 明文
        # 谁组装谁接线（跟 `socket_layer.make_handler` 那三扇门一个规矩）。
        # Arena 不 import 任何加密模块，理由也跟套接层不 import 那三扇门
        # 一模一样：**盘怎么摆，跟盘上的字节要不要打乱，是两件事。**
        self._密文 = 密文
        self.sb = None
        self._root_array = None          # 4096 条 uint64，常驻内存
        self._level1_dirty = set()       # 待落盘的一级块
        self._catalog = None
        self._active = None              # 活跃区的 RegionHeader
        self._last_sealed = None         # 最近一次封存的区域号（软榫交接棒）
        self._active_fh = None
        self._active_bloom = None        # 活跃区的布隆，内存里维护
        self._descs = {}                 # region_id -> RegionDescriptor
        self._bloom_cache = {}
        self._handles = _HandleCache()   # 封存区句柄缓存
        self.buf = None                  # 榫卯缓冲层，建库/开库时装配
        self._sink = None
        self._last_fsync = time.monotonic()
        self._blocks_per_region = 0
        self._slots_per_block = 0
        self._stats = {'puts': 0, 'gets': 0, 'bloom_skips': 0,
                       'sealed': 0, 'compacted': 0,
                       'spill_seals': 0, 'joints': 0, 'spill_full': 0,
                       # 压实与孤儿清理的计数。`dropped_regions` 是压实真正
                       # 注销掉的区数，`orphans_swept` 是开库时扫掉的孤儿
                       # 文件数。两个分开记，是因为它们来自**两条不同的
                       # 路**：一条是本次压实删干净了，一条是上次崩在
                       # 「描述符已注销、文件还没删」之间留下的。
                       'dropped_regions': 0, 'orphans_swept': 0,
                       'orphan_bytes': 0, 'unlink_failed': 0,
                       # 描述符解不开的条数。**这个数非零是有后果的**：
                       # 说明盘上有一页描述符坏掉，那些区在库里已经看不见
                       # 了，它们的文件绝不能被当成孤儿删掉——那就把
                       # 「还能取证恢复」降级成「永久丢失」。见 _sweep_orphans。
                       'bad_descriptors': 0}

    # ── 榫卯缓冲层 ──────────────────────────────────────────────────

    def _mount_buffer(self):
        """
        装配缓冲层。必须在 `_blocks_per_region` 定下来之后调用——
        预取要在块的环形边界上绕回，而缓冲层不该反过来问 Arena。
        """
        self.buf = mortise.MortiseBuffer(
            max_bytes=self.cfg.buffer_bytes,
            blocks_per_region=self._blocks_per_region)
        self._sink = _RegionSink(self)
        return self.buf

    # ── 生命周期 ────────────────────────────────────────────────────

    def create(self):
        """初始化一个全新的存储区。目录必须为空或不存在。"""
        if os.path.exists(os.path.join(self.root, sf.CATALOG_NAME)):
            raise ArenaError('存储区已存在: %s' % self.root)
        os.makedirs(os.path.join(self.root, sf.REGION_DIR), exist_ok=True)

        self._blocks_per_region, self._slots_per_block = \
            self.cfg.blocks_slots
        self._mount_buffer()
        self.sb = sf.Superblock(self.cfg.capacity)
        self._root_array = bytearray(sf.ROOT_ENTRIES * 8)

        self._catalog = open(self._catalog_path(), 'w+b', buffering=0)
        # 先撑到根数组末尾，随后按需追加一级块
        self._catalog.write(b'\0' * sf.LEVEL1_OFFSET)
        self._write_superblock()
        self._write_root_array()
        self._last_fsync = time.monotonic()
        self._fsync_catalog()
        return self

    def open(self):
        """打开已有存储区，并从 catalog 恢复全部运行时状态。"""
        path = self._catalog_path()
        if not os.path.exists(path):
            raise ArenaError('存储区不存在: %s' % path)
        self._catalog = open(path, 'r+b', buffering=0)

        # ⛔ **从这一行往下抛了，也得把已经拿到手的句柄放掉。**
        #
        # 这是踩过的坑：`Arena(root, cfg, 密文).open()` 装到一半抛异常时，
        # 半成品的 Arena 对象被丢掉，而 `self._catalog` 和 `_handles` 里
        # 那几个 `open()` 结果没人接盘 —— CPython 迟早会 GC 掉它们，但
        # **GC 的时机不由这里说了算**。在 Windows 上，被独占的文件
        # `os.remove` / `rmtree` 就是一句 `PermissionError`，而调用方
        # （`备份.做完验一遍`）用的正是 `ignore_errors=True` —— 于是
        # **删不掉也不吭声**，用户听到"已经删掉了"，盘上留着半个空壳。
        #
        # 最常撞上这条的就是**钥匙不对**：拿一个明文路径去开加密库，或者
        # 密码给错了，异常在读到第一个区域头时就抛出来，而 catalog 早开了。
        #
        # 收尾走 `_关掉没开成的`，**不是 `close()`** —— 那个会
        # `flush(force=True)`，而此刻 `sb` / `_descs` / 榫卯全是半截的，
        # 让它按"完整的一次关机"去落盘，等于拿半截状态去写盘。
        try:
            raw = self._read_at(self._catalog, 0, sf.SUPER_SIZE)
            self.sb = sf.Superblock.decode(raw)
            if self.sb.region_capacity != self.cfg.capacity:
                raise ArenaError(
                    '区域容量不一致：库是 %d，配置是 %d'
                    % (self.sb.region_capacity, self.cfg.capacity))
            self._blocks_per_region, self._slots_per_block = \
                self.cfg.blocks_slots
            self._mount_buffer()

            self._root_array = bytearray(
                self._read_at(self._catalog, sf.ROOT_ARRAY_OFFSET,
                              sf.ROOT_ENTRIES * 8))
            self._load_descriptors()
            self._bump_allocator()
            self._recover()
            # 上次没回收干净的区。**必须在描述符装完、发号器顶上去之后**——
            # 它认的是描述符里的 DEAD 标记，而"这个号还能不能发给新"靠的
            # 是发号器，两件事都得先定下来。
            self._sweep_orphans()
        except BaseException:
            self._关掉没开成的()
            raise
        return self

    def _关掉没开成的(self):
        """
        **开库没开成时的收尾：只放句柄，一个字节都不落盘。**

        和 `close()` 的分工：`close()` 是"这次会话结束了"，它要
        `flush(force=True, sync=True)` 走完整的榫卯次序；这一条是"这次
        会话压根没开起来"，`sb` / `_descs` / 榫卯可能是半截的，**任何
        写入都是在拿半截状态覆盖一个可能是好的库**。

        所以这里只做一件事：把 `_catalog` / `_active_fh` / `_handles`
        里那几个真句柄关掉，把路径原样还给操作系统。幂等——已经在
        `close()` 里关过的属性会被重新置 `None`，第二次进来什么都不做。
        """
        if self._active_fh is not None:
            try:
                self._active_fh.close()
            except OSError:
                pass
            self._active_fh = None
        self._handles.close_all()
        if self._catalog is not None:
            try:
                self._catalog.close()
            except OSError:
                pass
            self._catalog = None

    def _bump_allocator(self):
        """
        **发号器只许往前顶，绝不许退。** 区号要是被重用，会坏两件事。

        一，`_allocate_region` 建新区的第一句是 `open(path, 'w+b')`——它会
        **清空**同名文件。重用一个还活着的区号，就是当场抹掉那个区。而
        `sb.next_region` 和它对应的描述符虽然写在同一个 catalog 里，却不在
        同一页上；一次没等到 fsync 的崩溃完全可能让描述符页落了盘、超级
        块那页没落，于是重启之后发号器退回去，指向刚建好的那个区。

        二，整套判活死都建立在「**区号大 = 版本新**」上，扫描正是靠它降序
        走。发号器一退，同一个区号底下就会出现两批年龄说不清的数据。

        顶到 `max(已登记区号) + 1` 就够了，也最省：不需要额外记账，描述符
        本身就是那份记账。此刻 `_descs` 里还含着标了 DEAD 的区——它们的
        文件也许还没删干净，所以这个上界宁可保守一点。
        """
        if self.sb.next_region > max(self._descs, default=-1):
            return
        self.sb.next_region = max(self._descs) + 1
        self._write_superblock()
        self._fsync_catalog()

    def close(self):
        if self._catalog is None:
            return
        try:
            # 关闭是最后一次落盘机会。**必须走完整的榫卯次序**——
            # 记录先于槽、槽先于区域头，不能因为要关机了就图快。
            self.flush(force=True, sync=True)
        finally:
            if self._active_fh is not None:
                try:
                    self._active_fh.close()
                except OSError:
                    pass
                self._active_fh = None
            self._handles.close_all()
            self._catalog.close()
            self._catalog = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── 磁盘路径 ────────────────────────────────────────────────────

    def _catalog_path(self):
        return os.path.join(self.root, sf.CATALOG_NAME)

    def _region_path(self, rid):
        return os.path.join(self.root, *sf.region_relpath(rid).split('/'))

    def _bloom_path(self, rid):
        return os.path.join(self.root, *sf.bloom_relpath(rid).split('/'))

    # ── 区域注销与回收 ──────────────────────────────────────────────

    def _mark_dead(self, rid):
        """
        给一个区打上**废弃**的阳性标记。**这就是压实的提交点。**

        `capacity` 故意**保持非零**：`_load_descriptors` 判活看的是
        `capacity == 0`，所以一个非零的 DEAD 描述符会被读进 `_descs`——
        重启之后"这个区是被压实回收掉的"这件事还看得见，回收能接着做完。

        **反过来写 `capacity = 0` 是个陷阱。** 那样一来，三种情况在盘上
        完全同形：「压实把它回收了」「这一格从来没分配过」「这一页描述符
        被写零了」。而第三种意味着那里躺着一个**活区**——一旦靠"没有登记"
        来判死，删文件就会删到活区头上，还是静默的。

        所以回收只认阳性标记这条不对称，是整套回收能安全存在的前提。
        """
        desc = sf.RegionDescriptor(
            rid, sf.RegionState.DEAD, bloom_bytes=0,
            capacity=self.cfg.capacity,
            record_count=self._descs[rid].record_count)
        self._catalog.seek(self._desc_offset(rid))
        self._catalog.write(desc.encode())
        self._descs[rid] = desc

    def _blank_descriptor(self, rid):
        """
        把一个区的描述符槽整段清零，并从 `_descs` 里摘掉。

        **只在文件确认删干净之后调用。** 清零之后这里就真的"没有登记"
        了——而"没有登记"和"这一页被写零了"在盘上同形，只有文件也已经
        不在的时候，这两者才真的等价。

        清零是为了不让废弃区的清单一直长下去：`regions()` 每次查询都要
        走一遍全部描述符，留一堆只增不减的墓碑是白花钱。
        """
        self._catalog.seek(self._desc_offset(rid))
        self._catalog.write(b'\0' * sf.DESC_SIZE)
        self._descs.pop(rid, None)

    def _dead_rids(self):
        """catalog 里**明确标着**废弃的区号，升序。回收只认这个阳性标记。"""
        return sorted(r for r, d in self._descs.items()
                      if d.state == sf.RegionState.DEAD)

    def _unregistered_files(self):
        """
        盘上有文件、catalog 里没有它描述符的那些区域文件。

        **只报不改。** 理由见 `_mark_dead`：没有登记 ≠ 这个区不存在。
        一页描述符被写零之后，那些区在这里全显得像孤儿，而它们其实躺着
        真数据。所以 `verify()` 把它们报出来让人看，回收那边一个都不碰。
        """
        base = os.path.join(self.root, sf.REGION_DIR)
        out = []
        if not os.path.isdir(base):
            return out
        for dirpath, _dirs, names in os.walk(base):
            for name in names:
                rid = sf.region_id_of_file(name)
                if rid is None or rid in self._descs:
                    continue
                path = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                out.append((path, size))
        return out

    def _unlink(self, path):
        """删一个文件，返回删没删掉。**本来就不在，也算删掉了**——这是幂等。"""
        try:
            os.remove(path)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def _reclaim_dead(self):
        """
        把标着 DEAD 的区真正回收掉：删掉两个文件，删干净了才把描述符清零。

        **先标记、后删、最后清零，三步分开是有意的**，理由见 `_mark_dead`
        和 `_blank_descriptor`。崩在任何一步之间都能接着做完：DEAD 标记
        是持久的，文件删一半也不影响，`_unlink` 是幂等的。

        **删不掉不算错，不抛异常。** 走到这儿那个文件已经是垃圾了，删不掉
        只是多占点空间，下回开库还会再试一次。为它抛 `OSError` 反而坏事：
        那个异常不在语句层的兜底名单里，会一路穿过整个上层。

        返回 `(回收了几个区, 释放了多少字节, 有几个没删干净)`。
        """
        rids = self._dead_rids()
        if not rids:
            return 0, 0, 0
        done = freed = failed = 0
        for rid in rids:
            paths = (self._region_path(rid), self._bloom_path(rid))
            sizes = []
            for path in paths:
                try:
                    sizes.append(os.path.getsize(path))
                except OSError:
                    sizes.append(0)
            # 句柄必须先关净：键是 (路径, 可写)，同一个文件可能同时挂着
            # 两个句柄，漏一个，Windows 上 os.remove 就是 PermissionError。
            self._handles.close_path(paths[0])
            self.buf.drop_region(rid)
            self._bloom_cache.pop(rid, None)
            if all(self._unlink(path) for path in paths):
                freed += sum(sizes)
                self._blank_descriptor(rid)
                done += 1
            else:
                failed += 1     # 留着 DEAD 标记，下回开库再来
        if done:
            self._fsync_catalog()
        return done, freed, failed

    def _sweep_orphans(self):
        """
        开库时把上次没回收干净的区收尾。

        来源只有一个：压实崩在「描述符已标 DEAD、文件还没删」之间，或者
        那次 `os.remove` 没删成。两种情况下那些区在库里**都已经不存在
        了**——DEAD 标记就是提交点——文件是纯粹的垃圾。

        不扫的话那批空间永远回不来：`verify` 只按描述符遍历，看不见没有
        登记的文件。

        **`bad_descriptors` 非零时一个都不碰。** 描述符有一页读不出来，
        说明 catalog 本身已经不可信，"哪些区是废的"这个判断也就不可信。
        这时候安静地收手，比按一份读歪的清单删文件强得多。
        """
        if self._stats['bad_descriptors']:
            return 0
        done, freed, failed = self._reclaim_dead()
        self._stats['orphans_swept'] += done
        self._stats['orphan_bytes'] += freed
        self._stats['unlink_failed'] += failed
        return done

    @staticmethod
    def _read_at(fh, offset, size):
        fh.seek(offset)
        data = fh.read(size)
        if len(data) < size:
            raise ArenaError('读取越界: 偏移 %d 需要 %d 字节，实得 %d'
                             % (offset, size, len(data)))
        return data

    # ── catalog 读写 ────────────────────────────────────────────────

    def _write_superblock(self):
        self._catalog.seek(0)
        self._catalog.write(self.sb.encode())

    def _write_root_array(self):
        self._catalog.seek(sf.ROOT_ARRAY_OFFSET)
        self._catalog.write(bytes(self._root_array))

    def _level1_offset(self, group, allocate=True):
        """区域组 → 一级块在 catalog 里的偏移。未分配时按需追加。"""
        pos = sf.ROOT_ARRAY_OFFSET + group * 8
        offset = int.from_bytes(self._root_array[pos:pos + 8], 'little')
        if offset or not allocate:
            return offset
        self._catalog.seek(0, os.SEEK_END)
        offset = self._catalog.tell()
        if offset < sf.LEVEL1_OFFSET:
            offset = sf.LEVEL1_OFFSET
        self._catalog.write(b'\0' * (sf.LEVEL1_ENTRIES * sf.DESC_SIZE))
        self._root_array[pos:pos + 8] = offset.to_bytes(8, 'little')
        self._write_root_array()
        return offset

    def _desc_offset(self, rid):
        group, idx = sf.split_region_id(rid)
        base = self._level1_offset(group)
        return base + idx * sf.DESC_SIZE

    def _load_descriptors(self):
        self._descs.clear()
        self._stats['bad_descriptors'] = 0
        for group in range(sf.ROOT_ENTRIES):
            offset = self._level1_offset(group, allocate=False)
            if not offset:
                continue
            raw = self._read_at(self._catalog, offset,
                                sf.LEVEL1_ENTRIES * sf.DESC_SIZE)
            for idx in range(sf.LEVEL1_ENTRIES):
                rid = (group << sf.LEVEL1_SHIFT) | idx
                # **先分出"从来没分配过"，再谈 CRC。** 一级块一页有 256
                # 个槽，用到几个就是几个，剩下的整段零。而零值槽**过不了
                # CRC**——`crc32` 只有对空输入才是 0，对 48 个零字节不是。
                # 不先分这一步，一页里没用的槽全会被记成"描述符坏了"，
                # 于是 `bad_descriptors` 常年在几百上下，`_sweep_orphans`
                # 那个安全闸就永远焊死——回收一次都跑不成。
                hi = (idx + 1) * sf.DESC_SIZE
                if not any(raw[idx * sf.DESC_SIZE:hi]):
                    continue
                try:
                    desc = sf.RegionDescriptor.decode(
                        raw, idx * sf.DESC_SIZE)
                except ValueError:
                    # 非零、又过不了 CRC：这一格是真的坏了。记下来——
                    # `_sweep_orphans` 靠这个数决定敢不敢碰盘上的文件。
                    self._stats['bad_descriptors'] += 1
                    continue
                if desc.capacity == 0 or desc.region_id != rid:
                    continue
                self._descs[rid] = desc

    def _put_descriptor(self, desc):
        self._catalog.seek(self._desc_offset(desc.region_id))
        self._catalog.write(desc.encode())
        self._descs[desc.region_id] = desc

    # ── 启动恢复 ────────────────────────────────────────────────────

    def _recover(self):
        """
        打开时重建运行时状态。

        需要重建两样东西：活跃区是谁，以及它的布隆过滤器。
        布隆在封存时才落盘，所以活跃区的布隆只能扫它的槽位重建。
        扫描范围被限制在一个区域内——这是选择细粒度的直接好处，
        恢复代价不随库的总大小增长。
        """
        active_rid = None
        for rid, desc in self._descs.items():
            if desc.state != sf.RegionState.ACTIVE:
                continue
            # 描述符存在但文件不见了，说明上次封存到一半就崩了。
            # 不能把它当成活跃区，否则一开库就抛异常。
            if not os.path.exists(self._region_path(rid)):
                continue
            if active_rid is None or rid > active_rid:
                active_rid = rid

        # 软榫的交接棒也要从盘上捡回来：重开库之后第一次换区，
        # 新区的 prev_region 得指向真正的前驱，而不是凭空断掉。
        sealed = [rid for rid, d in self._descs.items()
                  if d.state == sf.RegionState.SEALED]
        if sealed:
            self._last_sealed = max(sealed)

        if active_rid is not None:
            self._active, self._active_fh = \
                self._open_region(active_rid, writable=True)
            self._rebuild_active_bloom()
        # 有描述符但没有活跃区（正常封存后的状态）就不建新区，
        # 等第一次 put 时再说——只读的库不该凭空多出一个区。

    def _rebuild_active_bloom(self):
        """
        扫活跃区的全部槽位，重建布隆过滤器，并**重算数据尾指针**。

        数据尾指针不信任磁盘上的头部，而是取所有在用槽里最小的记录
        偏移——那才是真正被引用过的最高水位。这样即使崩溃时头部没
        落盘，重开后也绝不会把新记录写到已有记录还指着的地方。
        头部里那些比它大的残留字节就此被回收。
        """
        hdr = self._active
        bloom = sf.BloomFilter(max(1, self.capacity_records),
                               region_id=hdr.region_id,
                               fp_rate=self.cfg.fp_rate)
        used = 0
        frontier = self.cfg.capacity        # 没有在用槽时正好等于"空区"
        for block_idx in range(hdr.blocks):
            raw = self._read_block(hdr.region_id, block_idx)
            base = sf.BLOCK_HEADER_SIZE
            for i in range(self._slots_per_block):
                off = base + i * sf.SLOT_SIZE
                if raw[off] != sf.SlotState.LIVE:
                    continue
                slot = sf.Slot.decode(raw, off)
                if slot.state != sf.SlotState.LIVE:
                    continue
                bloom.add(slot.key_hash)
                used += 1
                if slot.rec_offset < frontier:
                    frontier = slot.rec_offset
        self._active_bloom = bloom
        if used != hdr.record_count or frontier != hdr.data_tail:
            hdr.record_count = used
            hdr.data_tail = frontier
        # **无论如何都刷一次元数据**。目录里那份描述符是封存那一刻的
        # 快照，活跃区写了多少条只有这里才知道：上次崩溃时目录没来得及
        # fsync，描述符里的记录数就还是 0。开着库的状态必须自洽，
        # 不能一路带着一个说谎的描述符跑。
        self._flush_active_meta()

    # ── 区域打开 / 分配 / 封存 ──────────────────────────────────────

    def _open_region(self, rid, writable=False):
        """读区域头。返回 (区域头, 句柄)，**句柄由调用方负责关闭**。"""
        path = self._region_path(rid)
        if not os.path.exists(path):
            raise ArenaError('区域文件缺失: %s' % path)
        # ⛔ **先上锁再读头。** 上了锁的库里，区域头在盘上也是密文 ——
        # 拿裸句柄去读，解出来是一串乱码，而 `RegionHeader.decode` 会在
        # 一堆看着像"文件坏了"的地方炸掉：magic 不对、区域号对不上。
        # 报出来的全是盘的事，真因只有一个字：**没上锁**。
        fh = self._上锁(open(path, 'r+b' if writable else 'rb', buffering=0))
        try:
            raw = self._read_at(fh, 0, sf.REGION_HEADER_SIZE)
            hdr = sf.RegionHeader.decode(raw)
            if hdr.region_id != rid:
                raise ArenaError('区域文件与区域号不符: 文件里是 %d，期望 %d'
                                 % (hdr.region_id, rid))
        except Exception:
            fh.close()
            raise
        return hdr, fh

    def _allocate_region(self, dim=None):
        """开一个新区，用软榫挂到链上。"""
        if self.sb.next_region >= sf.MAX_REGIONS:
            raise ArenaError('区域号用尽（上限 %d）' % sf.MAX_REGIONS)

        rid = self.sb.next_region
        # **最后一根保险丝。** 下面那句 `open(path, 'w+b')` 会清空同名文件，
        # 所以一个已经登记过的区号在这儿被发出去，就等于当场抹掉那个区。
        # `_bump_allocator` 已经在开库时把这条路堵上了，这里再挡一道——
        # 代价是一次字典查，挡不住的那次代价是一整区数据。
        if rid in self._descs:
            raise ArenaError(
                '区域号 %d 已经登记过了，不能重用（这会清空那个区）。'
                '发号器是 sb.next_region=%d，它退回去了。'
                % (rid, self.sb.next_region))
        path = self._region_path(rid)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        hdr = sf.RegionHeader(rid, self._blocks_per_region,
                              self._slots_per_block, self.cfg.capacity,
                              dim_value=0 if dim is None else dim)
        # 软榫：新区记住前驱，**绝不回头改前驱的 next**——前驱可能
        # 已经封存只读。链条因此是单向的，只能从新往旧回溯。区域的
        # 正序本来就由区域号表达，不需要双向链。
        #
        # 前驱要从 _last_sealed 取，不能从 _active 取：换区的正常次序是
        # 「先 seal_active（它把 _active 置空）再 _allocate_region」，
        # 这时 _active 已经是 None，链条会整条断掉。
        prev = self._active.region_id if self._active is not None \
            else self._last_sealed
        if prev is not None:
            hdr.prev_region = prev

        with self._上锁(open(path, 'w+b', buffering=0)) as fh:
            fh.write(hdr.encode())
            # **块 0 的块头也要写全**，不能图省事写一片零。零的块头里
            # magic 不对、tenon/mortise 是 0（0 是个合法的块号，不是
            # NO_BLOCK）——现在榫卯契口要靠它，零值会被读成一个假的链接。
            bh0 = sf.BlockHeader(rid, 0, self._slots_per_block)
            fh.write(bh0.encode())
            fh.write(b'\0' * (self._slots_per_block * sf.SLOT_SIZE))
            # 先把全部块的块头写好，块内槽留空
            for block_idx in range(1, self._blocks_per_region):
                bh = sf.BlockHeader(rid, block_idx, self._slots_per_block)
                fh.write(bh.encode())
                fh.write(b'\0' * (self._slots_per_block * sf.SLOT_SIZE))
            # 撑到区域容量，让后续追加不必反复扩文件。
            #
            # ⛔ **多出来的那一截要显式写零、不能靠 `truncate` 扩。**
            # `truncate` 扩出来的尾巴是**盘给的裸零**，不是密文 ——
            # 而上了锁之后盘上不该有裸零：读回来会被解成乱码。
            # （现在这段差通常是 0，写这个循环是为了"通常是"三个字
            # 变不成"万一"。）
            已写 = fh.tell()
            差 = self.cfg.capacity - 已写
            while 差 > 0:
                步 = min(差, 1 << 20)
                fh.write(b'\0' * 步)
                差 -= 步
            fh.truncate(self.cfg.capacity)
            fh.flush()
            os.fsync(fh.fileno())

        # 上一任活跃区必须先把元数据落盘再关句柄，否则它最后写入的
        # 记录数/数据尾指针留在内存里，重启就丢了
        if self._active is not None:
            self._flush_active_meta()
            if self._active_fh is not None:
                self._active_fh.flush()
                try:
                    os.fsync(self._active_fh.fileno())
                except OSError:
                    pass
                self._active_fh.close()
                self._active_fh = None

        self._active, self._active_fh = self._open_region(rid, writable=True)
        self._active_bloom = sf.BloomFilter(
            max(1, self.capacity_records), region_id=rid,
            fp_rate=self.cfg.fp_rate)
        self.sb.next_region = rid + 1
        self.sb.region_count += 1

        desc = sf.RegionDescriptor(
            rid, sf.RegionState.ACTIVE, bloom_bytes=0,
            capacity=self.cfg.capacity, record_count=0,
            dim_low=0 if dim is None else dim,
            dim_high=0 if dim is None else dim)
        self._put_descriptor(desc)
        self._write_superblock()
        self._fsync_catalog()
        return rid

    @property
    def active_region(self):
        """
        当前活跃区的区域号，没有就是 None。

        语法层要靠它认"区换了"——每次写完比一比，变了就说明刚那个区
        已经封存，该在它身上记的账得赶紧记。不去偷看 `_active` 是为了
        让"活跃区会变"这件事有个明确的出口，而不是散在各处。
        """
        return self._active.region_id if self._active is not None else None

    @property
    def capacity_records(self):
        """一个区域的槽位总数，用于给布隆定尺寸。"""
        blocks, spb = self.cfg.blocks_slots
        return blocks * spb

    @property
    def slot_budget(self):
        """槽位用到多少条就该封存。见 ArenaConfig.load_factor 的说明。"""
        return max(1, int(self.capacity_records * self.cfg.load_factor))

    def seal_active(self):
        """
        封存当前活跃区。

        封存 = 定格。做完这三件事这个区就彻底只读了：
          1. 生成布隆过滤器并落盘，描述符登记大小
          2. 区域头写全（记录数、数据尾指针）并置 SEALED
          3. 描述符改 SEALED
        顺序很重要：布隆必须先落盘，否则描述符登记了一个不存在的
        过滤器，重启后点查会以为"这个区不可信"而实扫。
        """
        if self._active is None:
            return None
        hdr = self._active
        rid = hdr.region_id

        bloom_path = self._bloom_path(rid)
        os.makedirs(os.path.dirname(bloom_path), exist_ok=True)
        blob = self._active_bloom.encode()
        # **布隆也必须上锁。** 它里面是 `key_hash(键)` —— 明文放盘上，
        # 拿到文件的人可以拿一份字典离线对着它试：试中了就知道**你库里
        # 有那个键**。区文件全是乱码而布隆明文，等于把钥匙孔留在门上。
        # 它跟区文件一样是整文件读写、偏移从 0 起，所以同一条密钥流直接能用。
        with self._上锁(open(bloom_path, 'wb', buffering=0)) as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())

        hdr.state = sf.RegionState.SEALED
        self._flush_active_meta()

        # **封存必须先把这一区彻底落盘，再关句柄。** 封存的语义是"从此
        # 只读"，一个还没写完就被关掉的区不叫封存，叫半成品。走的是完整
        # 的榫卯次序，而且真的 fsync——之后这一区的地址就该永远有效了。
        self.flush(force=True, sync=True)

        desc = self._descs[rid]
        desc.state = sf.RegionState.SEALED
        desc.bloom_bytes = len(blob)
        desc.record_count = hdr.record_count
        self._put_descriptor(desc)
        self._write_superblock()
        self._fsync_catalog()

        if self._active_fh is not None:
            try:
                self._active_fh.close()
            except OSError:
                pass
            self._active_fh = None
        self._last_sealed = rid      # 软榫的交接棒，见 _allocate_region
        self._active = None
        self._active_bloom = None
        self._stats['sealed'] += 1
        return rid

    # ── 压实 ────────────────────────────────────────────────────────

    COMPACT_SCAN_LIMIT = 4096   # 扫描分批大小：别把一个区整段拉进内存

    def compact(self):
        """
        整库压实。**全库唯一一处删区域文件的地方。**

        做的事说清楚就一句：**把「扫描现在会看到的答案」原样搬到新区，
        再丢掉所有封存区。** 所以判据也就一句——压实前后按「区号降序、
        谁先出现谁赢」扫出来的 (键 → 值) 必须**逐项相等**。

        判活死走的是现成的 `scan`，搬运走的是现成的 `put`——两条路都被
        反复走过。这里新写的只有三样：**什么时候钉盘、什么时候注销、
        什么时候删**，而它们之间的次序就是全部的正确性所在（见模块
        docstring 里那张表）。

        活跃区不动：它里面的键一定是所有版本里最新的，而且它随时可写。

        返回计数字典。其中 `copied_regions` 是收到副本的区号集合——
        **上层要拿它去认账**：副本绕过了上层的记账路径，那些区里从此混着
        "上层没记过的行"。见 `syntax.Syntax._compact`。
        """
        stats = {'dropped_regions': 0, 'marked_dead': 0, 'moved': 0,
                 'moved_bytes': 0, 'reclaimed_bytes': 0, 'copied_regions': set(),
                 'unlink_failed': 0}

        # 手上不能有脏构件。后面要 drop_region，而它对脏构件是**静默丢弃**
        # （mortise：文件都要没了，写它没有意义）——所以先把攒着的按榫卯
        # 次序钉到盘上，再谈丢谁。
        self._durable_barrier()

        active = self.active_region
        # **源区在这句话之后就是一份定格的名册，第 ④⑤ 步一路用它。**
        # 绝不在后面重新求值：搬运过程中活跃区随时可能被填满而换区，那就
        # 会多出一个刚刚封存、**不在这份名册里**的区——它装着某批键的唯一
        # 一份副本，一旦按"重新求值"的结果去删，删的就是存活者本身。
        sources = tuple(sorted(rid for rid, d in self._descs.items()
                               if d.state == sf.RegionState.SEALED))
        if not sources:
            return stats

        # 「边扫边写」的全部前提就在这一句：活跃区区号最大，而扫描按区号
        # 降序——所以它必然整段排在最前面，**在它被完整扫完之前不会发生
        # 任何一次 put**。搬运改的是活跃区，扫的是源区，两者不重叠。
        # 这个前提没了，"扫到一半被人改了"就是静默丢行，所以宁可在这儿
        # 响亮地停下来，也不赌。
        if active is not None and active < sources[-1]:
            raise ArenaError(
                '活跃区 %d 比封存区 %d 还旧——压实"边扫边写"的前提不成立，'
                '继续下去会漏行' % (active, sources[-1]))

        # ① 先播种活跃区。它的键最新，源区里同名的**全是旧版本**。
        # 这一步不能省、也不能和 ② 合并：省掉的话，一个新版本在活跃区、
        # 旧版本在源区里的键会被当成存活者 put 回去，而 put 对活跃区已有
        # 的键是**原地改写槽**——旧版本把新版本盖掉，答案错得无声无息。
        seen = set()
        try:
            if active is not None:
                self._collect_live(active, seen, False, stats)
            # ② 源区，区号降序：每个键的第一次出现就是它最新的那一版。
            for rid in reversed(sources):
                self._collect_live(rid, seen, True, stats)
        except MemoryError:
            # `seen` 和"活键个数"成正比，这是整库压实唯一一处随库变大的
            # 内存开销，装不下就停在这儿。此刻一个源区都还没动过，库和
            # 没跑压实之前一模一样——让 MemoryError 原样冲出去也安全，
            # 但它穿过语句层时没人认得出这是怎么回事。
            raise ArenaError(
                '压实要用一份和"活键个数"成正比的内存记住见过哪些键，'
                '这台机器上装不下。库没有变化——源区一个字节都没动。')

        # ③ 提交点 A：副本钉盘。**在它返回之前，一个源区都不许动。**
        # 这里必须用严格屏障：普通 flush 会吞掉 fsync 的 OSError，而接下来
        # 就是要注销并删掉源区——那批唯一副本此刻可能只躺在页缓存里。
        self._durable_barrier()

        # ④ 提交点 B：注销源区。**标记是阳性的**（state=DEAD 且 capacity
        # 非零），理由见 `_mark_dead`。崩在它之前，源区完好、答案不变；
        # 崩在它之后，那些区的存活者早就在新区里钉好了。**次序不可换。**
        for rid in sources:
            self._mark_dead(rid)
            self.sb.region_count -= 1
        self._write_superblock()
        self._fsync_catalog()

        # ⑤ 收尾：关句柄、清内存、删文件、清零描述符。此刻答案已经不依赖
        # 它们了——删不掉只是留一堆垃圾，下回开库 `_sweep_orphans` 再收一次。
        done, freed, failed = self._reclaim_dead()
        stats['reclaimed_bytes'] = freed
        stats['unlink_failed'] = failed
        stats['dropped_regions'] = done
        stats['marked_dead'] = len(sources)

        # 软榫的交接棒别指着一个已经没了的区：新区挂前驱时会用它。
        left = [r for r, d in self._descs.items()
                if d.state == sf.RegionState.SEALED]
        self._last_sealed = max(left) if left else None

        self._stats['dropped_regions'] += done
        self._stats['unlink_failed'] += failed
        self._stats['compacted'] += 1
        return stats

    def _durable_barrier(self):
        """
        严格持久化屏障：把脏构件按榫卯次序写下去，然后 fsync，**失败就抛**。

        和 `flush(force=True, sync=True)` 只差一点：失败会响。平时那条路
        故意吞掉 fsync 的 OSError——"最多丢最后一批"是这个引擎允许的活法。
        但压实不能这么办：它紧接着要**注销并删掉源区**，而那些唯一副本此
        刻可能只躺在页缓存里。ENOSPC 或者 EIO 之下若无其事地走完第 ④⑤
        步，就是无声无息地丢一整批行。

        抛 `ArenaError` 而不是 `OSError`：`Syntax._run` 的兜底只接
        `StatementError / SqlError / SchemaError / ArenaError`，一个裸
        `OSError` 会穿过整个语句层。此刻源区一个都没动，库是干净的。
        """
        self.buf.flush(self._sink, sync=False)
        try:
            for rid in list(self._sink._touched):
                fh = self._region_fh(rid, writable=True)
                fh.flush()
                os.fsync(fh.fileno())
            self._catalog.flush()
            os.fsync(self._catalog.fileno())
        except OSError as exc:
            raise ArenaError(
                '压实走到一半没能把数据钉到盘上（%s）。源区一个都没动，'
                '库还是压实之前的样子，这是安全的。' % exc)
        self._sink._touched.clear()

    def _collect_live(self, rid, seen, copy, stats):
        """
        扫一个区，把**第一次出现**的键收进 `seen`；`copy` 为真时顺手把
        它写回活跃区。

        `seen` 是「比这个区更新的地方已经出现过哪些键」，由调用方一路传
        下来——所以**调用次序必须是区号降序**。次序换了，就是拿旧版本当
        新版本使。

        **认键用完整字节，不用哈希。** 这里判错的后果是把一个还活着的
        版本当成旧的丢掉，那是静默丢行——拿 8 字节哈希去赌不值得。

        `seen` 的大小是"全库活键的个数"。这是整库压实唯一一处随库变大
        的内存开销，明写在这里，好过让人去猜。
        """
        cursor = None
        while True:
            cursor, items, more = self.scan(
                cursor, self.COMPACT_SCAN_LIMIT, regions=(rid,))
            for _r, _b, _s, key, value in items:
                if key in seen:
                    continue
                seen.add(key)
                if not copy:
                    continue
                new_rid, _blk, _slot = self.put(key, value)
                stats['copied_regions'].add(new_rid)
                stats['moved'] += 1
                stats['moved_bytes'] += len(key) + len(value)
            if not more:
                return

    # ── 块的读写 ────────────────────────────────────────────────────

    def _block_bytes(self):
        return sf.BLOCK_HEADER_SIZE + self._slots_per_block * sf.SLOT_SIZE

    def _上锁(self, fh):
        """
        给一个区文件句柄包上密文那一层。**没锁就原样返回。**

        **幂等**：已经包过的原样放行。这一条是必须的，因为句柄有四个
        出口（`_open_region`、`_region_fh` 两条分支、`_allocate_region`），
        而 `_active_fh` 这一条**可能从任何一个口子来** —— `_open_region`
        给它赋过值（`:819`），`_region_fh` 自己也赋（见下）。不幂等的话
        同一个句柄会被包两层，**写下去加密两次、读上来只解一次** ——
        症状是数据看着像坏了，而根因在包的次数。

        包在**出口**上而不是包在 `open` 上，是因为 `_HandleCache` 缓存的是
        **裸句柄**：它只管文件描述符这一种内核资源，加密不是它的活。

        出口一共**五个**，缺一个就是那一个文件明文躺在盘上：

            区文件  `_open_region` / `_region_fh` 两条分支 / `_allocate_region`
            布隆    `_seal_bloom` 写 / `_load_bloom` 读   ← **不是区文件，
                                                            不走句柄缓存**

        `catalog` **不在里面**，这是有意的：它装的是超级块、根数组、
        区域描述符，**没有键也没有值**，泄露的只有"有多少个区、装了几成"。
        要不要为这一点把开库的诊断能力也一起锁掉，得单独定（见开发计划 §十一）。
        """
        if fh is None or self._密文 is None:
            return fh
        if isinstance(fh, _密文句柄):
            return fh
        return _密文句柄(fh, self._密文)

    def _region_fh(self, rid, writable=False):
        if self._active is not None and self._active.region_id == rid:
            if self._active_fh is None:
                self._active_fh = open(self._region_path(rid), 'r+b',
                                       buffering=0)
            return self._上锁(self._active_fh)
        return self._上锁(self._handles.get(self._region_path(rid), writable))

    def _load_block(self, rid, block_idx):
        """缓冲层未命中时的读盘回调。**只在这里读块。**"""
        offset = sf.block_offset(block_idx, self._slots_per_block)
        return self._read_at(self._region_fh(rid), offset,
                             self._block_bytes())

    def _read_block(self, rid, block_idx):
        """取块。返回的就是缓存里那块 bytearray，**改动即生效**。"""
        return self.buf.block(rid, block_idx, self._load_block).raw

    def _block_of(self, rid, block_idx):
        """取块构件本身——要标脏或写榫卯契口时用这个。"""
        return self.buf.block(rid, block_idx, self._load_block)

    def _dirty(self, blk):
        self.buf.touch_block(blk)

    def _read_slots(self, hdr, block_idx):
        raw = self._read_block(hdr.region_id, block_idx)
        out = []
        base = sf.BLOCK_HEADER_SIZE
        for i in range(self._slots_per_block):
            try:
                out.append(sf.Slot.decode(raw, base + i * sf.SLOT_SIZE))
            except ValueError:
                out.append(sf.Slot(state=sf.SlotState.EMPTY))
        return out

    def _read_record(self, rid, slot):
        """取记录。走的也是缓冲层——块命中之后，这一跳才是剩下的 I/O。"""
        raw = self.buf.record(
            rid, slot.rec_offset, slot.rec_len,
            lambda: self._read_at(self._region_fh(rid), slot.rec_offset,
                                  slot.rec_len))
        return sf.Slot.decode_record(raw, 0, slot.key_len, slot.val_len)

    def _write_record(self, key, value):
        """
        把记录追加到活跃区的数据尾区，返回偏移。

        **不写盘**——字节交给缓冲层攒着，等下次 flush 时按榫卯次序
        （记录先于槽）统一提交。尾指针在内存里立刻前进，所以同一个
        偏移不会被分配两次。
        """
        hdr = self._active
        blob = key + value
        new_tail = hdr.data_tail - len(blob)
        if new_tail < hdr.data_floor:
            # 调用方必须先用 _needs_rotation 判过。**这里不再返回 None**：
            # 静默失败会让调用方拿着旧区选出来的槽号继续往下走，那才是
            # 真正难查的那种错。
            raise ArenaError('活跃区数据尾区放不下：尾指针 %d，地板 %d，'
                             '需要 %d' % (hdr.data_tail, hdr.data_floor,
                                          len(blob)))
        hdr.data_tail = new_tail
        self.buf.stage_record(hdr.region_id, new_tail, blob)
        return new_tail

    def _flush_active_meta(self):
        """
        把活跃区的区域头标脏，等下一轮 flush 提交。

        **活跃区的权威记录数在区域头里，不在描述符里。** 描述符是封存
        那一刻的快照——只有封存区才不可变，才配得上一份不会过期的
        元数据。这里顺手把内存里的描述符也同步过来，纯粹是为了让
        stats()/verify() 在当前进程里看到一致的数字；目录文件本身不
        跟着每次写入 fsync，崩了也由 _recover 从区域文件重算回来。

        **和以前最大的不同是不再写盘。** 区域头是整体重写的 64 字节，
        每写一条记录就 seek 一次写一遍，等于把一次顺序写拆成几万次
        随机写。现在它只在 flush 的最后一步、作为整批的封条出现。
        """
        if self._active is None:
            return
        desc = self._descs.get(self._active.region_id)
        if desc is not None:
            desc.record_count = self._active.record_count
        member = self.buf.header(self._active.region_id, self._active)
        self.buf.touch_header(self._active.region_id)

    # ── 槽位探测 ────────────────────────────────────────────────────

    def _primary(self, h):
        return sf.find_block(h, self._blocks_per_region), \
            sf.find_slot(h, self._slots_per_block)

    # 槽内固定偏移：状态 1 字节，键哈希 8 字节跟在 4 字节之后。
    # 探测时先直接比原始字节，只有键哈希撞上了才做完整解码（含 CRC32）。
    # 每写一条就解码整块 127 个槽是 4.7ms/条 的元凶——那是全表扫描
    # 级别的无用功，而正常情况第一个槽就是空的。
    _SLOT_STATE_OFF = 0
    _SLOT_HASH_OFF = 4

    def _find_slot(self, rid, h, key):
        """
        找一个键所在的槽。返回 (block, slot_idx, version) 或 None。

        探测顺序：从主位置所在块的起始槽开始，槽绕回则该块走完，
        再沿块的次序走到下一块，如此环形一圈。**这条环形就是榫卯的
        用武之地**——块与块靠次序咬合，把局部的溢出摊到邻块上，
        而不是让某个块单独撑爆。

        **遇到第一个空槽就停**：空槽是探测链的终点，键不可能落在它
        后面。这条性质依赖 `_free_slot` 也插在同一个"第一个空槽"上，
        两边顺序必须保持一致，否则会写进去读不回来。
        """
        limit = min(self.cfg.spill_blocks, self._blocks_per_region)
        blocks = self._blocks_per_region
        spb = self._slots_per_block
        start_block = sf.find_block(h, blocks)
        start_slot = sf.find_slot(h, spb)
        want = h.to_bytes(8, 'little')
        live = sf.SlotState.LIVE
        empty = sf.SlotState.EMPTY
        for step in range(limit):
            block_idx = (start_block + step) % blocks
            raw = self._read_block(rid, block_idx)
            for i in range(spb):
                idx = (start_slot + i) % spb
                off = sf.BLOCK_HEADER_SIZE + idx * sf.SLOT_SIZE
                state = raw[off + self._SLOT_STATE_OFF]
                if state == empty:
                    return None            # 链到头了，后面不会有
                if state != live:
                    continue
                if raw[off + self._SLOT_HASH_OFF:
                       off + self._SLOT_HASH_OFF + 8] != want:
                    continue
                slot = sf.Slot.decode(raw, off)
                stored_key, _ = self._read_record(rid, slot)
                if stored_key == key:
                    return block_idx, idx, slot.version
        return None

    def _free_slot(self, h):
        """
        找一个能写的空槽。返回 (block, slot_idx, version) 或 None。

        探测窗口和 `_find_slot` 一样被 `spill_blocks` 限死——**写入的
        活动范围和读取的搜索范围必须逐字相同**，否则就是写进去读不出来。
        超窗返回 None，由调用方封区换新，这就是那个常数的来处。

        越过块边界时顺手封契口（见 `_seal_joint`）。
        """
        limit = min(self.cfg.spill_blocks, self._blocks_per_region)
        blocks = self._blocks_per_region
        spb = self._slots_per_block
        start_block = sf.find_block(h, blocks)
        start_slot = sf.find_slot(h, spb)
        empty = sf.SlotState.EMPTY
        for step in range(limit):
            block_idx = (start_block + step) % blocks
            raw = self._read_block(self._active.region_id, block_idx)
            for i in range(spb):
                idx = (start_slot + i) % spb
                off = sf.BLOCK_HEADER_SIZE + idx * sf.SLOT_SIZE
                if raw[off + self._SLOT_STATE_OFF] == empty:
                    if step:
                        # 走到这一步说明前面 step 个块**整块走完都没空位**。
                        # 探测顺序一绕回块内起点就出块，所以"越过某块"和
                        # "某块全满"是同一件事——契口记的就是这件事。
                        self._seal_joint(start_block, step)
                    return block_idx, idx, 0
        return None

    def _needs_rotation(self, hdr, need):
        """
        活跃区还装得下这条记录吗。两个区都要看：

          数据尾区   装不下这条记录的字节了
          槽位       用到装载上限了

        只判数据区是错的——槽位先满时数据区还空着一截，但探测链已经
        拉长到快要找不到东西了。
        """
        return (hdr.data_tail - need < hdr.data_floor
                or hdr.record_count >= self.slot_budget)

    def _rotate_region(self, dim, need):
        """
        封存活跃区、开一个新区，并确认这条记录在新区里放得下。

        **必须在选块号槽号之前调用**，否则选出来的地址属于旧区，
        写下去就是改别人的槽。
        """
        self.seal_active()
        self._allocate_region(dim)
        hdr = self._active
        if hdr.data_tail - need < hdr.data_floor:
            raise ArenaError('这条记录放不进一个空区域'
                             '（记录 %d 字节，区域容量 %d 字节）'
                             % (need, self.cfg.capacity))
        return hdr

    def _seal_joint(self, home, crossed):
        """
        在活跃区里封一串契口：块 `home+i` 满溢，链交给 `home+i+1`。

        两次独立的调用，块 A 记下后继、块 B 记下前驱，所以一个块最多
        一个 tenon 和一个 mortise。**必须在槽写下去之前或之后都无所谓**
        ——契口不是寻址依据，探测规则不用它，它只是把"哪儿溢到哪儿"
        这件事从隐含约定变成盘上可查的结构。丢了它最多是链不完整，
        点查照旧（槽是自描述的，读路径靠键比对确认）。
        """
        blocks = self._blocks_per_region
        rid = self._active.region_id
        for i in range(crossed):
            a = (home + i) % blocks
            b = (home + i + 1) % blocks
            ablk = self._block_of(rid, a)
            # 越过它 = 它整块走完都没空位 = 它的槽满了，顺手把账记准
            self.buf.set_joint(ablk, mortise=b,
                               slots_used=ablk.slots_total)
            self.buf.set_joint(self._block_of(rid, b), tenon=a)
        self._stats['joints'] += crossed

    # ── 读写接口 ────────────────────────────────────────────────────

    def put(self, key, value, dim=None):
        """
        写入一条记录。返回 (区域号, 块号, 槽号)。

        同一个键在活跃区里重复写入不会堆积槽——找到已有槽就原地
        改写它指向新追加的数据。**改的只是槽，不是封存区**：
        活跃区本来就是可变的。
        """
        if not isinstance(key, (bytes, bytearray)):
            key = str(key).encode('utf-8')
        key = bytes(key)
        if not isinstance(value, (bytes, bytearray)):
            value = bytes(value)

        if self._active is None:
            self._allocate_region(dim)

        hdr = self._active
        h = sf.key_hash(key)
        need = len(key) + len(value)

        # **换区判定集中在最前面。** 以前是在"写记录失败"之后才换区，
        # 那时块号和槽号已经按旧区选好了，接着却写进新区——等于在新区
        # 里随机改一个别人的槽。数据区先满、槽区还没满的时候就会踩到。
        # 现在先把区定下来，后面选出来的地址一定属于最终那个区。
        if self._needs_rotation(hdr, need):
            hdr = self._rotate_region(dim, need)

        hit = self._find_slot(hdr.region_id, h, key)
        if hit is not None:
            block_idx, slot_idx, version = hit
        else:
            free = self._free_slot(h)
            if free is None:
                # 溢出窗口走完了还没空位——**这正是那道硬闸在起作用**。
                # 封区换新，探测链的长度上界就此被钉死在 spill_blocks。
                self._stats['spill_full'] += 1
                hdr = self._rotate_region(dim, need)
                free = self._free_slot(h)
                if free is None:
                    raise ArenaError('新区也找不到空槽，区域规划有问题')
            block_idx, slot_idx, version = free

        offset = self._write_record(key, value)

        slot = sf.Slot(h, len(key), offset, len(value),
                       version=version + 1, state=sf.SlotState.LIVE)
        blk = self._block_of(hdr.region_id, block_idx)
        blk.raw[sf.BLOCK_HEADER_SIZE + slot_idx * sf.SLOT_SIZE:
                sf.BLOCK_HEADER_SIZE + (slot_idx + 1) * sf.SLOT_SIZE] = \
            slot.encode()
        # 槽数记账：只在真正占了一个空槽时加，覆盖写不算
        if hit is None:
            self.buf.set_joint(blk, slots_used=min(blk.slots_total,
                                                   blk.slots_used + 1))
        self._dirty(blk)

        if hit is None:
            hdr.record_count += 1
        if dim is not None:
            if hdr.dim_value == 0:
                hdr.dim_value = dim
            hdr.dim_value = max(hdr.dim_value, dim)

        self._active_bloom.add(h)
        self._flush_active_meta()
        self._stats['puts'] += 1
        self._maybe_flush()
        return hdr.region_id, block_idx, slot_idx

    def get(self, key):
        """
        点查。**按区域号降序找**——只追加意味着新版本总在更大的区域号里，
        第一个命中的就是最新的。

        活跃区先查（它没有布隆，只有一个，实扫也无所谓），然后依次
        过封存区：布隆说不在这就一定不在，直接跳过。
        """
        if not isinstance(key, (bytes, bytearray)):
            key = str(key).encode('utf-8')
        key = bytes(key)
        h = sf.key_hash(key)
        self._stats['gets'] += 1
        # 时间闸也要在只读路径上走一遍。**"最多等 1 秒"是个时间承诺，
        # 而时间在只读的时候照样流逝**——如果只在写路径检查，一旦停写，
        # 最后那批脏数据就永远等不到落盘，崩溃窗口从 1 秒变成"直到
        # 下次写入为止"。代价是一次单调时钟比较。
        self._maybe_flush()

        if self._active is not None:
            # 布隆为 None 时不能跳过——那会漏掉整个活跃区
            if self._active_bloom is None or \
                    self._active_bloom.might_contain(h):
                value = self._lookup_in(self._active.region_id, h, key)
                if value is not None:
                    return value

        for rid in sorted(self._descs, reverse=True):
            desc = self._descs[rid]
            if desc.state not in (sf.RegionState.SEALED,
                                  sf.RegionState.ACTIVE):
                continue
            if self._active is not None and rid == self._active.region_id:
                continue
            if desc.record_count == 0:
                continue
            bloom = self._load_bloom(desc)
            if bloom is not None and not bloom.might_contain(h):
                self._stats['bloom_skips'] += 1
                continue

            value = self._lookup_in(rid, h, key)
            if value is not None:
                return value
        return None

    def _lookup_in(self, rid, h, key):
        """
        在指定区域里找一个键。

        走法和 `_find_slot` 必须逐字一致：环形走完全部块，遇到第一个
        空槽就收工。**这里的窗口大小不是性能参数，是正确性参数**——
        曾经写成只走 16 个块，结果区域槽位接近写满、探测链拉长到 16 个
        块之外时，写进去的键就永远读不回来了。

        窗口现在由 `spill_blocks` 定死，而写入路径用的是同一个窗口。
        **两边必须是同一个数**——这是硬闸，不是调优参数。
        """
        limit = min(self.cfg.spill_blocks, self._blocks_per_region)
        blocks = self._blocks_per_region
        spb = self._slots_per_block
        start_block = sf.find_block(h, blocks)
        start_slot = sf.find_slot(h, spb)
        want = h.to_bytes(8, 'little')
        live = sf.SlotState.LIVE
        empty = sf.SlotState.EMPTY
        for step in range(limit):
            block_idx = (start_block + step) % blocks
            raw = self._read_block(rid, block_idx)
            for i in range(spb):
                idx = (start_slot + i) % spb
                off = sf.BLOCK_HEADER_SIZE + idx * sf.SLOT_SIZE
                state = raw[off + self._SLOT_STATE_OFF]
                if state == empty:
                    return None            # 链到头了，后面不会有
                if state != live:
                    continue
                if raw[off + self._SLOT_HASH_OFF:
                       off + self._SLOT_HASH_OFF + 8] != want:
                    continue
                try:
                    slot = sf.Slot.decode(raw, off)
                except ValueError:
                    continue
                stored, value = self._read_record(rid, slot)
                if stored == key:
                    return value
        return None

    def scan(self, cursor=None, limit=256, regions=None):
        """
        按区域号降序遍历，返回 `(next_cursor, items, more)`。

        `regions` 给一个区域号集合时只扫这些区，其余**连块都不读**。
        这是语法层做维度裁剪的落点：裁剪要真的省下 I/O，就必须在这里
        跳过，而不是扫完再按行过滤——那样省的是内存，不是盘。
        传 None（默认）就是全扫，行为和以前一模一样。

        次序是三级：**区域号降序 → 块号升序 → 槽号升序**。区域号降序
        不是随便定的——只追加意味着同一个键的新版本总在更大的区域号里，
        降序扫第一个撞上的就是最新的那个版本。

        cursor 是 (区域号, 块号, 槽号)，从这个位置**含**着往后扫；None
        表示从头开始。返回的 next_cursor 可以直接喂给下一次调用接着扫，
        more 为假表示扫到头了。

        **不去重。** 一个键的旧版本还留在封存区里，所以同一个键可能出现
        多次。要做到"每个键只出一条"就得记住这次扫描已经见过哪些键——
        那是跨帧状态，而内核的立身之本就是无状态（见 protocol.py）。
        去重是上层的事：拿到降序结果，谁先出现谁最新。
        """
        if limit <= 0:
            return cursor, [], False
        if not self._descs:
            return None, [], False

        # 区域号降序排一次，后面找"下一个更小的区"就只在这个表里走
        if regions is None:
            order = sorted(self._descs, reverse=True)
        else:
            order = sorted((r for r in self._descs if r in regions),
                           reverse=True)
        if not order:
            # 裁剪把所有区都排除了。**这是个正常结果，不是错误**：
            # "这个维度区间里没有数据"和"这个表是空的"在这里长得一样。
            return None, [], False
        live_states = (sf.RegionState.SEALED, sf.RegionState.ACTIVE)

        if cursor is None:
            rid, block_idx, slot_idx = order[0], 0, 0
            pos = 0
        else:
            rid, block_idx, slot_idx = cursor
            # 游标可能指向一个已经被压实掉的区，退到第一个不比它大的区
            pos = 0
            while pos < len(order) and order[pos] > rid:
                pos += 1
            if pos >= len(order):
                return None, [], False
            if order[pos] != rid:
                rid, block_idx, slot_idx = order[pos], 0, 0

        blocks = self._blocks_per_region
        spb = self._slots_per_block
        items = []
        empty = sf.SlotState.EMPTY
        live = sf.SlotState.LIVE

        while pos < len(order):
            rid = order[pos]
            desc = self._descs[rid]
            if desc.state in live_states:
                while block_idx < blocks:
                    raw = self._read_block(rid, block_idx)
                    while slot_idx < spb:
                        off = sf.BLOCK_HEADER_SIZE + slot_idx * sf.SLOT_SIZE
                        state = raw[off + self._SLOT_STATE_OFF]
                        here = slot_idx
                        slot_idx += 1
                        if state != live:
                            continue
                        try:
                            slot = sf.Slot.decode(raw, off)
                            key, value = self._read_record(rid, slot)
                        except (ValueError, OSError, ArenaError):
                            # 半写下去的槽、越界的数据区——跳过。
                            # 扫描是尽力而为的遍历，不该被一条坏记录打断。
                            continue
                        items.append((rid, block_idx, here, key, value))
                        if len(items) >= limit:
                            return (rid, block_idx, slot_idx), items, True
                    block_idx += 1
                    slot_idx = 0
            pos += 1
            block_idx = 0
            slot_idx = 0

        return None, items, False

    def _load_bloom(self, desc):
        rid = desc.region_id
        if rid in self._bloom_cache:
            return self._bloom_cache[rid]
        if not desc.bloom_bytes:
            return None                    # 没有布隆，只能实扫
        path = self._bloom_path(rid)
        try:
            # 写的时候上了锁，这里就得解（`_上锁` 幂等，明文库原样返回）。
            with self._上锁(open(path, 'rb')) as fh:
                blob = fh.read()
            bloom = sf.BloomFilter.decode(blob)
        except (OSError, ValueError):
            # 布隆坏了不该让查询出错，只是退化成实扫。**永远不要因为
            # 一个加速结构损坏就报错**——它只是慢一点，不是错。
            return None
        self._bloom_cache[rid] = bloom
        return bloom

    # ── 落盘 ────────────────────────────────────────────────────────

    def _maybe_flush(self):
        """
        攒够一批就落盘。**阈值看的是缓冲层的脏字节数**，不是写过多少——
        同一条记录被反复覆盖，只有最后那一次值得写出去。

        缓冲层自己也有一条底线：内存预算被脏构件压满时它淘汰不动了，
        这时候必须刷，否则内存会一直涨。所以这里也看它一眼。
        """
        now = time.monotonic()
        if self.buf is None:
            return
        if self.buf.dirty_bytes >= self.cfg.fsync_bytes or \
                now - self._last_fsync >= self.cfg.fsync_seconds or \
                self.buf.bytes > self.cfg.buffer_bytes:
            self.flush()

    def flush(self, force=False, sync=False):
        """
        把缓冲层里的脏构件按榫卯次序提交：记录 → 块 → 区域头。

        `sync=True` 才真的 fsync。**落盘次序和持久化次序是两件事**：
        前者保证崩溃时最坏结果是浪费字节而不是坏数据，后者决定数据
        什么时候真的钉在盘上。平时只做前者，代价才付得起。
        """
        if self.buf is None:
            return
        if not force and self.buf.dirty_bytes == 0:
            if sync:
                self._sink.sync()
                self._fsync_catalog()
            return
        self.buf.flush(self._sink, sync=sync)
        if sync:
            self._fsync_catalog()
        self._last_fsync = time.monotonic()

    def _fsync_catalog(self):
        if self._catalog is not None:
            self._catalog.flush()
            try:
                os.fsync(self._catalog.fileno())
            except OSError:
                pass

    # ── 诊断 ────────────────────────────────────────────────────────

    def regions(self):
        """
        区域清单，按区域号升序。供 `SHOW REGIONS` 和诊断使用。

        **这是一份只读快照，不是调度依据。** 拿它去决定下一个该写哪儿
        是错的——活跃区随时可能封存换新，等你照着这份清单动手的时候，
        它已经过期了。要写就走 `put()`，由它自己决定。

        `dim_low/dim_high` 是**建区那一刻**记下的值，之后不再更新，
        所以它不等于"这个区里实际有哪些维度"。真正可用的维度区间在
        语法层手里（syntax.py）——它知道每条记录写进来时带的 dim，
        引擎不知道。
        """
        active = self._active.region_id if self._active is not None else None
        out = []
        for rid in sorted(self._descs):
            d = self._descs[rid]
            out.append({
                'region_id': rid,
                'state': d.state,
                'records': d.record_count,
                'capacity': d.capacity,
                'bloom_bytes': d.bloom_bytes,
                'dim_low': d.dim_low,
                'dim_high': d.dim_high,
                'active': rid == active,
            })
        return out

    def stats(self):
        out = dict(self._stats)
        # **"几个区"报的是还活着的区。** 标了 DEAD 的描述符会短暂留在
        # `_descs` 里（回收没删干净时留得更久），把它算进去，人看到的就是
        # "区数没降下来"，而压实其实已经生效了。废弃的那些单列一行。
        live = [d for d in self._descs.values()
                if d.state in (sf.RegionState.SEALED, sf.RegionState.ACTIVE)]
        out['regions'] = len(live)
        out['dead_regions'] = len(self._descs) - len(live)
        out['active'] = self._active.region_id if self._active else None
        out['next_region'] = self.sb.next_region if self.sb else None
        out['records'] = sum(d.record_count for d in live)
        out['blocks_per_region'] = self._blocks_per_region
        out['slots_per_block'] = self._slots_per_block
        out['blooms_resident'] = len(self._bloom_cache)
        out['bloom_mem_kb'] = sum(b.bytes_used for b in
                                  self._bloom_cache.values()) // 1024
        if self.buf is not None:
            out['buffer'] = self.buf.stats_report()
        return out

    def verify(self):
        """
        巡检：逐个区域核对文件在不在、区域头对不对、布隆可不可信。
        返回问题列表，空列表表示健康。
        """
        problems = []
        # 先报那两件"会连累后面所有判断"的事。**描述符解不开必须报**：
        # 那时它的区在库里已经看不见了，而 `_sweep_orphans` 靠
        # `bad_descriptors` 非零来拒绝碰任何文件——不报出来，人只会看到
        # "巡检干净"，然后奇怪为什么有个区的数据查不到。
        bad = self._stats['bad_descriptors']
        if bad:
            problems.append(
                '有 %d 条区域描述符解不开（CRC 不过）：那些区在库里已经'
                '看不见了，但文件还躺在盘上。先修 catalog，在那之前回收'
                '一律不碰任何文件' % bad)
        for path, size in self._unregistered_files():
            problems.append(
                '盘上有区域文件、catalog 里没有它的描述符: %s（%d 字节）。'
                '**它可能是活的**——描述符那一页被写零了也会长这样。'
                '这里只报不动，别删' % (path, size))

        active_rid = self._active.region_id if self._active else None
        for rid in sorted(self._descs):
            desc = self._descs[rid]
            path = self._region_path(rid)
            if desc.state == sf.RegionState.DEAD:
                # 已标废弃：文件正常应该已经被回收掉了。**不拿它的区域头
                # 去比状态**——头里还写着 SEALED，那是对的，不是故障。
                if os.path.exists(path):
                    problems.append(
                        '区域 %d 已标废弃但文件还在（上次回收没删干净，'
                        '下次开库会再试一次）' % rid)
                continue
            if not os.path.exists(path):
                problems.append('区域 %d 文件缺失: %s' % (rid, path))
                continue
            try:
                hdr, fh = self._open_region(rid)
            except (ArenaError, ValueError) as exc:
                problems.append('区域 %d 区域头损坏: %s' % (rid, exc))
                continue
            try:
                if rid == active_rid:
                    # **活跃区的权威在内存里，不在盘上。** 区域头是攒批
                    # 落盘的，盘上那份天生滞后，拿它去跟描述符比相等是
                    # 拿错了参照物——那会把正常行为报成故障。
                    # 该查的是它有没有"跑到前面去"：盘上比内存里还多，
                    # 说明有我们不知道的东西写过这个文件。
                    if hdr.record_count > self._active.record_count:
                        problems.append(
                            '区域 %d 盘上记录数 %d 超过了内存里的 %d'
                            % (rid, hdr.record_count,
                               self._active.record_count))
                    if hdr.state != sf.RegionState.ACTIVE:
                        problems.append('区域 %d 是活跃区，盘上状态却是 %d'
                                        % (rid, hdr.state))
                else:
                    if hdr.state != desc.state:
                        problems.append(
                            '区域 %d 状态不一致: 文件 %d vs 描述符 %d'
                            % (rid, hdr.state, desc.state))
                    if hdr.record_count != desc.record_count:
                        problems.append(
                            '区域 %d 记录数不一致: 文件 %d vs 描述符 %d'
                            % (rid, hdr.record_count, desc.record_count))
            finally:
                fh.close()
            if desc.state == sf.RegionState.SEALED:
                if not desc.bloom_bytes:
                    problems.append('区域 %d 已封存但没有布隆，点查会实扫'
                                    % rid)
                elif not os.path.exists(self._bloom_path(rid)):
                    problems.append('区域 %d 布隆文件缺失' % rid)
            problems.extend(self._verify_joints(rid))
        return problems

    def _verify_joints(self, rid):
        """
        查榫卯链完不完整：**每一处"我溢出去"都必须有一处"我承接了"**。

        契口不是寻址依据，缺了它点查照旧能读到数据（槽是自描述的，读
        路径靠键比对确认）。但半截的链意味着一次没写完的封契口，
        正是崩溃窗口留下的指纹，值得报出来而不是咽下去。
        """
        problems = []
        blocks = self._blocks_per_region
        for idx in range(blocks):
            try:
                raw = self._read_block(rid, idx)
                _, _, tenon, mortise = sf.read_block_joint(raw)
            except (ArenaError, ValueError):
                continue
            if mortise != sf.NO_BLOCK:
                if mortise >= blocks:
                    problems.append('区域 %d 块 %d 的卯眼越界: %d'
                                    % (rid, idx, mortise))
                    continue
                back = self._read_block(rid, mortise)
                if sf.read_block_joint(back)[2] != idx:
                    problems.append(
                        '区域 %d 块 %d 指向 %d，但那里没记着它（榫卯断了）'
                        % (rid, idx, mortise))
            if tenon != sf.NO_BLOCK and tenon >= blocks:
                problems.append('区域 %d 块 %d 的榫头越界: %d'
                                % (rid, idx, tenon))
        return problems
