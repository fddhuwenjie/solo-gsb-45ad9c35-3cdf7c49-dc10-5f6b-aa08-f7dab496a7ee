"""投影机布线规划（多机拼接工作台的线缆联合布线）。

前置条件
--------
2~3 台投影机的安装点已通过拼接校核（``geometry.compute_blend`` 的
``joint_ok=True`` 且每台都给出了实际安装点）：覆盖连续、逐机可行、
各机机身检修包络两两互避且不进禁装区。

问题输入（顶视图，单位米）
--------------------------
* 每台设备的安装点（镜头点）P_k、机身尺寸与检修净距（沿用拼接段定义）；
* 机柜矩形 cabinet；
* 可走线通道网络：若干**相交的轴对齐矩形** channels，每个通道有
  中心线（沿矩形长边方向）与容量 capacity（可同时容纳的线缆条数）；
* 每台设备指定通道接入口 Q_k（须落在某通道矩形内）与最长线缆 Lmax_k；
* 房间禁装区 obstacles。

布线模型
--------
每台设备的线缆分两段：

1. **接入段** P_k → Q_k：在房间内避开全部禁装区、机柜以及**其他设备**的
   机身检修包络（自身包络为出线端，豁免）的最短折线；用“障碍四角可见
   图 + Dijkstra”求欧氏最短路径（边界接触允许，与既有几何规则一致）。
2. **通道段** Q_k → 机柜：接入口沿短正交引线落到所属通道中心线，沿
   中心线网络（相交矩形的中心线交点/重叠连通边构成图）行至进入机柜的
   终端节点；中心线被禁装区/机柜覆盖的区间为硬阻断，被设备 j 检修包络
   覆盖的区间仅对 j 自身放行（其余设备不可用）。

各通道被多少条线缆使用（同一线缆对同一通道只计 1 次）不得超过容量。
联合求解：逐设备枚举接入口到机柜的简单路径（长度上界 = Lmax−接入段
长），再按设备顺序 DFS 组合，施加通道容量约束，取**总长度最短**者；
总长度相同按设备顺序取字典序最小（路径按长度/节点序确定序枚举），
结果稳定确定。任一设备不可达、线缆超长或容量无联合解时
``status="no_joint_solution"``，**不返回任何部分布线路径**，仅返回
失败设备与阻断通道（几何阻断 / 容量阻断）诊断。
"""

from geometry import EPS, service_envelope

PATH_LIMIT = 80          # 每台设备枚举的简单路径条数上限


# ==========================================================================
# 基础几何
# ==========================================================================

def _strict_inside(p, r):
    """点是否严格位于矩形内部（边界接触不算）。"""
    return (r[0] + EPS < p[0] < r[2] - EPS and
            r[1] + EPS < p[1] < r[3] - EPS)


def _inside_closed(p, r, tol=EPS):
    return (r[0] - tol <= p[0] <= r[2] + tol and
            r[1] - tol <= p[1] <= r[3] + tol)


def _rect_area(r):
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def _strict_t_interval(p0, d, r):
    """开线段 p0+t·d（t∈[0,1]）严格位于矩形 r 内部的 t 开区间。

    沿 x/y 两个方向做板条求交；与矩形仅边界接触（含沿边滑行）时返回
    None。返回 (lo, hi)（hi-lo > EPS）。
    """
    lo, hi = 0.0, 1.0
    for ax in (0, 1):
        o, dd = p0[ax], d[ax]
        l, h = r[ax], r[ax + 2]
        if abs(dd) <= EPS:
            if not (l + EPS < o < h - EPS):
                return None
            continue
        t1, t2 = (l - o) / dd, (h - o) / dd
        a, b = (t1, t2) if t1 < t2 else (t2, t1)
        lo, hi = max(lo, a), min(hi, b)
    if hi - lo <= EPS:
        return None
    return (lo, hi)


def _seg_crosses(p, q, r):
    """开线段 p→q 是否穿过矩形 r 的内部，或任一端点严格位于 r 内。

    用于引线/连接线校验：边界接触、沿边滑行均允许。
    """
    if _strict_inside(p, r) or _strict_inside(q, r):
        return True
    d = (q[0] - p[0], q[1] - p[1])
    return _strict_t_interval(p, d, r) is not None


def _seg_within_rect(p, q, r):
    """线段 p→q 是否完全位于矩形 r 内（含边界）。引线必须在通道内部。"""
    return _inside_closed(p, r) and _inside_closed(q, r)


def _dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _nearest_point_rect(p, r):
    return (min(max(p[0], r[0]), r[2]), min(max(p[1], r[1]), r[3]))


# ==========================================================================
# 接入段：障碍四角可见图最短路径
# ==========================================================================

def _access_route(start, end, blockers, room_rect):
    """求 start→end 避开 blockers（label, rect）且在房间内的最短折线路径。

    节点 = 起、终点 + 各障碍矩形四角；两端点不得严格位于任一障碍内
    （起点对自身检修包络豁免，由调用方从 blockers 中剔除）。边为节点间
    直线，要求不穿越任何障碍内部、不越出房间。Dijkstra 欧氏最短路，
    等距时按节点编号决胜，结果确定。

    返回 (path, length, blocked_labels)：blocked_labels 为无路时
    “移除后即可恢复通路”的障碍（用于归因）；成功时为空列表。
    """
    W1, D1, W2, D2 = room_rect

    def in_room(p):
        return (-EPS <= p[0] <= W2 + EPS and -EPS <= p[1] <= D2 + EPS)

    if not in_room(end):
        return None, None, []

    def end_blocked(bls):
        for label, r in bls:
            if _strict_inside(end, r):
                return label
        return None

    def start_blocked(bls):
        for label, r in bls:
            if _strict_inside(start, r):
                return label
        return None

    sb, eb = start_blocked(blockers), end_blocked(blockers)
    if sb or eb:
        return None, None, [x for x in (sb, eb) if x]

    nodes = [start, end]
    for _, r in blockers:
        nodes.append((r[0], r[1]))
        nodes.append((r[2], r[1]))
        nodes.append((r[2], r[3]))
        nodes.append((r[0], r[3]))

    # 落在某障碍内部（非自身角点）的节点不可用
    def node_ok(p):
        if not in_room(p):
            return False
        for _, r in blockers:
            if _strict_inside(p, r):
                return False
        return True

    valid = [node_ok(p) for p in nodes]

    def edge_ok(a, b, bls):
        for _, r in bls:
            if _seg_crosses(a, b, r):
                return False
        # 不越出房间（端点在边界允许）
        if min(a[0], b[0]) < -EPS or max(a[0], b[0]) > W2 + EPS or \
                min(a[1], b[1]) < -EPS or max(a[1], b[1]) > D2 + EPS:
            return False
        return True

    def dijkstra(bls):
        m = len(nodes)
        INF = float("inf")
        dist = [INF] * m
        prev = [-1] * m
        dist[0] = 0.0
        used = [False] * m
        for _ in range(m):
            u = -1
            for i in range(m):
                if used[i] or not valid[i]:
                    continue
                if u < 0 or dist[i] < dist[u] - EPS or \
                        (abs(dist[i] - dist[u]) <= EPS and i < u):
                    u = i
            if u < 0 or dist[u] == INF:
                break
            used[u] = True
            for v in range(m):
                if v == u or not valid[v]:
                    continue
                if not edge_ok(nodes[u], nodes[v], bls):
                    continue
                nd = dist[u] + _dist(nodes[u], nodes[v])
                if nd < dist[v] - EPS or \
                        (abs(nd - dist[v]) <= EPS and u < prev[v]):
                    dist[v], prev[v] = nd, u
        if dist[1] == INF:
            return None, None
        path, k = [], 1
        while k >= 0:
            path.append(nodes[k])
            k = prev[k]
        path.reverse()
        return path, dist[1]

    path, length = dijkstra(blockers)
    if path is not None:
        return path, length, []

    # 归因：逐一移除障碍做恢复测试
    culprits = []
    for idx, (label, _r) in enumerate(blockers):
        rest = [b for j, b in enumerate(blockers) if j != idx]
        p, _l = dijkstra(rest)
        if p is not None:
            culprits.append(label)
    return None, None, culprits


# ==========================================================================
# 通道中心线网络
# ==========================================================================

class _NodeBuilder:
    def __init__(self):
        self.ids = {}
        self.coords = []

    def get(self, x, y):
        key = (round(x, 6), round(y, 6))
        if key in self.ids:
            return self.ids[key]
        i = len(self.coords)
        self.ids[key] = i
        self.coords.append((x, y))
        return i


class _Network:
    def __init__(self, spines):
        self.nb = _NodeBuilder()
        self.spines = spines                 # i → {rect, kind, p0, d}
        self.edges = []                      # (a,b,length,tags,hard,owner,kind,label)
        self.rack_nodes = set()
        self.splits = {i: {0.0, 1.0} for i in spines}

    def point(self, i, t):
        sp = self.spines[i]
        return (sp["p0"][0] + t * sp["d"][0],
                sp["p0"][1] + t * sp["d"][1])

    def node_at(self, i, t):
        t = min(1.0, max(0.0, t))
        self.splits[i].add(t)
        x, y = self.point(i, t)
        return self.nb.get(x, y)


def _spine_of(ch):
    """通道矩形的中心线：沿长边方向；等宽深时取水平。返回 spine 描述。"""
    x, y, w, h = ch["x"], ch["y"], ch["w"], ch["h"]
    if w + EPS >= h:
        return {"kind": "H", "rect": [x, y, x + w, y + h],
                "p0": (x, y + h / 2), "d": (w, 0.0), "length": w}
    return {"kind": "V", "rect": [x, y, x + w, y + h],
            "p0": (x + w / 2, y), "d": (0.0, h), "length": h}


def _t_of_point_on_spine(sp, p):
    """点 p 在中心线直线上的参数 t（不夹取）。"""
    if sp["kind"] == "H":
        return (p[0] - sp["p0"][0]) / sp["d"][0] if abs(sp["d"][0]) > EPS else 0.0
    return (p[1] - sp["p0"][1]) / sp["d"][1] if abs(sp["d"][1]) > EPS else 0.0


def _build_network(channels, cab_rect, hard_blockers, envelopes, ports):
    """构建中心线图。

    cab_rect:      机柜矩形（4 元列表）；
    hard_blockers: [(label, rect)]（禁装区 + 机柜），中心线进入其内部的
                   边为硬阻断；
    envelopes:     [(device_idx, rect)]，中心线进入设备 j 包络的边仅对 j
                   自身放行；
    ports:         [{device, channel, point}]，每台接入口及所属通道。

    返回 (net, port_info, channel_spans)：
      port_info[k] = {node, channel, lead_ok, lead_label, foot}
      channel_spans[i] = 硬阻断边明细 [{from,to,label}]
    """
    n_ch = len(channels)
    spines = {i: _spine_of(ch) for i, ch in enumerate(channels)}
    net = _Network(spines)

    all_blockers = ([(lab, r, -1) for lab, r in hard_blockers] +
                    [(f"设备 {j + 1} 的机身检修包络", r, j)
                     for j, r in envelopes])

    # ---- 1) 阻断区间端点作为中心线切分点 ----
    for i, sp in spines.items():
        for _label, r, _owner in all_blockers:
            iv = _strict_t_interval(sp["p0"], sp["d"], r)
            if iv is None:
                continue
            net.node_at(i, iv[0])
            net.node_at(i, iv[1])

    # ---- 2) 垂直相交：中心线交点为两通道共享节点 ----
    for i in range(n_ch):
        for j in range(i + 1, n_ch):
            si, sj = spines[i], spines[j]
            if si["kind"] == sj["kind"]:
                continue
            h = si if si["kind"] == "H" else sj
            v = sj if si["kind"] == "H" else si
            hi = i if si["kind"] == "H" else j
            vi = j if si["kind"] == "H" else i
            th = _t_of_point_on_spine(h, (v["p0"][0], h["p0"][1]))
            tv = _t_of_point_on_spine(v, (v["p0"][0], h["p0"][1]))
            if -EPS <= th <= 1 + EPS and -EPS <= tv <= 1 + EPS:
                thc, tvc = min(1.0, max(0.0, th)), min(1.0, max(0.0, tv))
                ph = h["p0"][0] + thc * h["d"][0], \
                    h["p0"][1] + thc * h["d"][1]
                # 同一坐标节点登记到两条 spine（共享交点）
                net.nb.get(*ph)
                net.splits[hi].add(thc)
                net.splits[vi].add(tvc)

    # ---- 3) 平行重叠通道：在重叠区中部加垂直/水平连通边 ----
    parallel_edges = []
    for i in range(n_ch):
        for j in range(i + 1, n_ch):
            si, sj = spines[i], spines[j]
            if si["kind"] != sj["kind"]:
                continue
            ri, rj = si["rect"], sj["rect"]
            if si["kind"] == "H":
                ox1, ox2 = max(ri[0], rj[0]), min(ri[2], rj[2])
                if ox2 - ox1 <= EPS:
                    continue
                # 两条中心线 y 都须严格位于重叠区内
                if not (max(ri[1], rj[1]) + EPS < si["p0"][1] <
                        min(ri[3], rj[3]) - EPS and
                        max(ri[1], rj[1]) + EPS < sj["p0"][1] <
                        min(ri[3], rj[3]) - EPS):
                    continue
                xm = (ox1 + ox2) / 2
                pa, pb = (xm, si["p0"][1]), (xm, sj["p0"][1])
            else:
                oy1, oy2 = max(ri[1], rj[1]), min(ri[3], rj[3])
                if oy2 - oy1 <= EPS:
                    continue
                if not (max(ri[0], rj[0]) + EPS < si["p0"][0] <
                        min(ri[2], rj[2]) - EPS and
                        max(ri[0], rj[0]) + EPS < sj["p0"][0] <
                        min(ri[2], rj[2]) - EPS):
                    continue
                ym = (oy1 + oy2) / 2
                pa, pb = (si["p0"][0], ym), (sj["p0"][0], ym)
            hard, owner, label = _segment_blocker(pa, pb, hard_blockers,
                                                   envelopes)
            if hard:
                continue
            # 连通端点登记到两条中心线的切分点，保证节点落在 spine 边上
            if si["kind"] == "H":
                net.node_at(i, _t_of_point_on_spine(si, pa))
                net.node_at(j, _t_of_point_on_spine(sj, pb))
            else:
                net.node_at(i, _t_of_point_on_spine(si, pa))
                net.node_at(j, _t_of_point_on_spine(sj, pb))
            parallel_edges.append((pa, pb, frozenset((i, j)), owner, label))

    # ---- 4) 机柜终端：中心线进入机柜的交点；或机柜与通道矩形相交时的引线 ----
    rack_leads = []                      # (foot_node, point, tags, length)
    for i, sp in spines.items():
        iv = _strict_t_interval(sp["p0"], sp["d"], cab_rect)
        if iv is not None:
            for t in (iv[0], iv[1]):
                net.rack_nodes.add(net.node_at(i, t))
            continue
        ri = sp["rect"]
        ox1 = max(ri[0], cab_rect[0]); oy1 = max(ri[1], cab_rect[1])
        ox2 = min(ri[2], cab_rect[2]); oy2 = min(ri[3], cab_rect[3])
        if ox2 - ox1 <= EPS or oy2 - oy1 <= EPS:
            continue                       # 通道不挨机柜，无终端
        foot_xy = _nearest_point_rect(
            ((cab_rect[0] + cab_rect[2]) / 2, (cab_rect[1] + cab_rect[3]) / 2),
            [sp["p0"][0], sp["p0"][1],
             sp["p0"][0] + sp["d"][0], sp["p0"][1] + sp["d"][1]])
        t = _t_of_point_on_spine(sp, foot_xy)
        if not (-EPS <= t <= 1 + EPS):
            continue
        t = min(1.0, max(0.0, t))
        foot_xy = net.point(i, t)
        end = _nearest_point_rect(foot_xy, cab_rect)
        # 引线须在通道矩形内且避开禁装区/他人检修包络
        blocked = False
        if not _seg_within_rect(foot_xy, end, ri):
            blocked = True
        for lab, r in hard_blockers:
            if lab == "机柜":
                continue
            if _seg_crosses(foot_xy, end, r):
                blocked = True
        for _j, r in envelopes:
            if _seg_crosses(foot_xy, end, r):
                blocked = True
        if not blocked and _dist(foot_xy, end) <= EPS:
            net.rack_nodes.add(net.node_at(i, t))
            continue
        if blocked:
            continue
        nf = net.node_at(i, t)
        ne = net.nb.get(*end)
        rack_leads.append((nf, ne, frozenset((i,)), _dist(foot_xy, end)))
        net.rack_nodes.add(ne)

    # ---- 5) 设备接入口节点与引线 ----
    port_info = {}
    for pt in ports:
        k, ci, q = pt["device"], pt["channel"], pt["point"]
        sp = spines[ci]
        t = _t_of_point_on_spine(sp, q)
        tc = min(1.0, max(0.0, t))
        foot = net.point(i=ci, t=tc)
        nf = net.node_at(ci, tc)
        nq = net.nb.get(*q)
        lead_ok, lead_label = True, None
        # 引线（接入口→中心线垂足）须在通道矩形内
        if not _seg_within_rect(q, foot, sp["rect"]):
            lead_ok, lead_label = False, "接入口引线超出通道范围"
        else:
            for lab, r in hard_blockers:
                if _seg_crosses(q, foot, r):
                    lead_ok, lead_label = False, f"接入口被{lab}阻断"
                    break
            if lead_ok:
                for j, r in envelopes:
                    if j == k:
                        continue
                    if _seg_crosses(q, foot, r):
                        lead_ok, lead_label = False, \
                            f"接入口被设备 {j + 1} 的机身检修包络阻断"
                        break
        if lead_ok and _dist(q, foot) > EPS:
            net.edges.append((nq, nf, _dist(q, foot), frozenset((ci,)),
                              False, k, "port", None))
        port_info[k] = {"node": nq, "channel": ci, "lead_ok": lead_ok,
                        "lead_label": lead_label,
                        "point": q, "foot": foot}

    # ---- 6) 生成中心线边并按中点判定阻断归属 ----
    # 机柜覆盖区间为正常终端（路径在此结束），不算几何阻断；其余硬阻断
    # （禁装区）与设备检修包络按中点归属切分后的边。
    channel_spans = {i: [] for i in spines}
    for i, sp in spines.items():
        ts = sorted(net.splits[i])
        for a_t, b_t in zip(ts[:-1], ts[1:]):
            if b_t - a_t <= EPS:
                continue
            pa = net.point(i, a_t); pb = net.point(i, b_t)
            mid = ((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2)
            na, nb = net.nb.get(*pa), net.nb.get(*pb)
            inside_cab = _inside_closed(mid, cab_rect, tol=1e-6)
            hard, owner, label = False, -1, None
            if not inside_cab:
                for hl, hr in hard_blockers:
                    if hl == "机柜":
                        continue
                    if _strict_inside(mid, hr):
                        hard, owner, label = True, -1, hl
                        break
                if not hard:
                    for j, er in envelopes:
                        if _strict_inside(mid, er):
                            owner, label = j, f"设备 {j + 1} 的机身检修包络"
                            break
            length = _dist(pa, pb)
            net.edges.append((na, nb, length, frozenset((i,)),
                              hard, owner, "spine", label))
            if hard:
                channel_spans[i].append(
                    {"from": [round(pa[0], 4), round(pa[1], 4)],
                     "to": [round(pb[0], 4), round(pb[1], 4)],
                     "blocker": label})

    # ---- 7) 平行连通边、机柜引线 ----
    for pa, pb, tags, owner, label in parallel_edges:
        na, nb = net.nb.get(*pa), net.nb.get(*pb)
        net.edges.append((na, nb, _dist(pa, pb), tags, False, owner,
                          "connect", label))
    for nf, ne, tags, length in rack_leads:
        net.edges.append((nf, ne, length, tags, False, -1, "rack", None))

    return net, port_info, channel_spans


def _segment_blocker(pa, pb, hard_blockers, envelopes):
    """连接边（通道间平行连通）阻断判定。"""
    for lab, r in hard_blockers:
        if _seg_crosses(pa, pb, r):
            return True, -1, lab
    for j, r in envelopes:
        if _seg_crosses(pa, pb, r):
            return False, j, f"设备 {j + 1} 的机身检修包络"
    return False, -1, None


# ==========================================================================
# 逐设备通道段路径枚举与联合选择
# ==========================================================================

def _adjacency(net, device_k):
    """设备 k 可用的无向邻接表：(neighbor, edge_index, length, tags)。"""
    adj = {}
    for ei, (a, b, length, tags, hard, owner, kind, _label) in enumerate(net.edges):
        if hard:
            continue
        if owner >= 0 and owner != device_k:
            continue
        adj.setdefault(a, []).append((b, ei, length, tags))
        adj.setdefault(b, []).append((a, ei, length, tags))
    for v in list(adj):
        adj[v].sort(key=lambda t: (round(t[2], 6), t[0]))
    return adj


def _dijkstra_to_rack(adj, start, goals):
    """start 到任一机柜节点的最短距离（用于枚举剪枝与诊断）。"""
    INF = float("inf")
    dist = {start: 0.0}
    seen = set()
    while True:
        u = None
        for v, d in dist.items():
            if v in seen:
                continue
            if u is None or d < dist[u] - EPS or \
                    (abs(d - dist[u]) <= EPS and v < u):
                u = v
        if u is None:
            break
        seen.add(u)
        if u in goals:
            return dist[u]
        for nb, _ei, w, _tags in adj.get(u, []):
            nd = dist[u] + w
            if nb not in dist or nd < dist[nb] - EPS:
                dist[nb] = nd
    return None


def _enumerate_paths(net, device_k, start, goals, length_cap,
                     limit=PATH_LIMIT):
    """枚举 start→任一机柜节点的简单路径（长度 ≤ length_cap），按
    (长度, 节点序列) 确定序返回。每条记录 {length, nodes, edges, used}。"""
    adj = _adjacency(net, device_k)
    goals = {g for g in goals if g == start or g in adj}
    shortest = _dijkstra_to_rack(adj, start, goals)
    if shortest is None:
        return [], None
    results = {}

    def heuristic(u):
        # 欧氏距离到最近机柜节点是任意路径的下界（可接纳剪枝）
        return min(_dist(net.nb.coords[u], net.nb.coords[g]) for g in goals)

    path_nodes = [start]
    path_edges = []
    used = set()
    visited = {start}

    def dfs(u, dist):
        if len(results) >= limit:
            return
        if u in goals:
            key = tuple(path_nodes)
            if key not in results:
                results[key] = {
                    "length": dist,
                    "nodes": list(path_nodes),
                    "edges": list(path_edges),
                    "used": frozenset(used),
                }
            return
        for nb, ei, w, tags in adj.get(u, []):
            if nb in visited:
                continue
            nd = dist + w
            if nd > length_cap + EPS or nd + heuristic(nb) > length_cap + EPS:
                continue
            added = [c for c in tags if c not in used]
            visited.add(nb); path_nodes.append(nb); path_edges.append(ei)
            used.update(added)
            dfs(nb, nd)
            for c in added:
                used.discard(c)
            path_nodes.pop(); path_edges.pop(); visited.remove(nb)

    dfs(start, 0.0)
    out = sorted(results.values(),
                 key=lambda r: (round(r["length"], 9),
                                tuple(r["nodes"])))
    return out, shortest


def _joint_select(per_device_paths, capacities, n):
    """按设备顺序 DFS 组合路径并施加通道容量约束。

    返回 (total, [path_index × n]) 或 None。决胜顺序（均确定、稳定）：
      1) 总长度最短；
      2) 总长度相同：全方案合计占用的“线缆—通道”条数最少（同距时少绕通道）；
      3) 仍相同：按设备顺序的路径索引字典序最小（路径按长度/节点序枚举）。
    """
    lower_bounds = [p[0]["length"] if p else float("inf")
                    for p in per_device_paths]
    best = None  # (total, used_count, index_tuple)

    counts = [0] * len(capacities)

    def dfs(k, total, used_count, idxs):
        nonlocal best
        if k == n:
            cand = (total, used_count, tuple(idxs))
            better = (best is None or cand[0] < best[0] - EPS or
                      (abs(cand[0] - best[0]) <= EPS and
                       (cand[1] < best[1] or
                        (cand[1] == best[1] and cand[2] < best[2]))))
            if better:
                best = cand
            return
        # 下界剪枝
        lb = total + sum(lower_bounds[k2] for k2 in range(k, n))
        if best is not None and lb > best[0] + EPS:
            return
        for pi, rec in enumerate(per_device_paths[k]):
            touched = []
            ok = True
            for c in rec["used"]:
                if counts[c] + 1 > capacities[c]:
                    ok = False
                    break
                touched.append(c)
            if not ok:
                continue
            for c in touched:
                counts[c] += 1
            idxs.append(pi)
            dfs(k + 1, total + rec["length"],
                used_count + len(touched), idxs)
            idxs.pop()
            for c in touched:
                counts[c] -= 1

    dfs(0, 0.0, 0, [])
    if best is None:
        return None
    return best[0], list(best[2])


# ==========================================================================
# 入口
# ==========================================================================

def compute_cabling(blend, segments, room, obstacles, points,
                    cabinet, channels, dev_specs):
    """投影机联合布线。

    blend:        geometry.compute_blend 的结果（须 joint_ok）。
    segments:     拼接段（取机身尺寸与检修净距）。
    obstacles:    禁装区原始字典（含 name）。
    points:       每台安装点（镜头点），不允许 None。
    cabinet:      {x,y,w,h}。
    channels:     [{name,x,y,w,h,capacity}]。
    dev_specs:    [{access:{x,y}, max_length}] × N。
    """
    n = len(segments)
    cab_rect = [cabinet["x"], cabinet["y"],
                cabinet["x"] + cabinet["w"], cabinet["y"] + cabinet["h"]]
    ch_rects = [[c["x"], c["y"], c["x"] + c["w"], c["y"] + c["h"]]
                for c in channels]
    capacities = [int(c["capacity"]) for c in channels]
    room_rect = [0.0, 0.0, float(room["width"]), float(room["depth"])]

    base = {
        "status": "no_joint_solution", "routed": False,
        "count": n, "total_length": None, "devices": None,
        "channels": [], "rack_points": [],
        "failed_devices": [], "blocked_channels": [], "diagnostics": [],
    }

    # ---- 拼接校核前置 ----
    missing = [k for k in range(n) if not points[k]]
    if not blend.get("joint_ok") or missing:
        base["status"] = "points_missing" if blend.get("joint_ok") else \
            "blend_invalid"
        base["diagnostics"].append(
            "拼接校核尚未通过或安装点未确定：布线仅在全部设备安装点均通过"
            "拼接校核（覆盖连续、逐机可行、检修包络互避）后进行")
        return base

    # ---- 设备机身 / 检修包络 ----
    envelopes = []
    for k, seg in enumerate(segments):
        sch = seg["scheme"]
        cl = float(seg.get("clearance", 0.0))
        env = service_envelope(points[k]["x"], points[k]["y"],
                               sch["body_w"], sch["body_d"], cl)
        envelopes.append((k, env))

    hard_blockers = [
        (ob.get("name") or f"禁装区 {i + 1}",
         [ob["x"], ob["y"], ob["x"] + ob["w"], ob["y"] + ob["h"]])
        for i, ob in enumerate(obstacles)]
    hard_blockers.append(("机柜", cab_rect))

    # ---- 接入口归属通道 ----
    failed = {k: [] for k in range(n)}
    ports = []
    port_channel = {}
    for k in range(n):
        q = (float(dev_specs[k]["access"]["x"]),
             float(dev_specs[k]["access"]["y"]))
        if not _inside_closed(q, room_rect):
            failed[k].append("接入口超出房间范围")
            port_channel[k] = None
            continue
        candidates = [i for i, r in enumerate(ch_rects) if _inside_closed(q, r)]
        if not candidates:
            failed[k].append("指定的通道接入口不在任何可走线通道矩形内")
            port_channel[k] = None
            continue
        # 同时落在多个重叠通道内时，取面积最小（最具体）者，同面积取小编号
        ci = min(candidates,
                 key=lambda i: (_rect_area(ch_rects[i]), i))
        port_channel[k] = ci
        ports.append({"device": k, "channel": ci, "point": q})

    # ---- 接入段可见图最短路 ----
    access = {}
    for k in range(n):
        start = (points[k]["x"], points[k]["y"])
        q = (float(dev_specs[k]["access"]["x"]),
             float(dev_specs[k]["access"]["y"]))
        # 阻断体 = 禁装区 + 机柜 + 其他设备检修包络（自身包络为出线端，豁免）
        bls = list(hard_blockers)
        for j, env in envelopes:
            if j != k:
                bls.append((f"设备 {j + 1} 的机身检修包络", env))
        path, length, culprits = _access_route(start, q, bls, room_rect)
        if path is None:
            if culprits:
                failed[k].append(
                    "接入段无法避障到达接入口（移除后可恢复："
                    + "、".join(f"「{c}」" for c in dict.fromkeys(culprits))
                    + "）")
            else:
                failed[k].append("接入段无法避障到达接入口"
                                 "（禁装区/机柜/邻机检修包络组合阻断）")
            access[k] = None
        else:
            access[k] = {"path": path, "length": length}

    # ---- 中心线网络 ----
    net, port_info, channel_spans = _build_network(
        channels, cab_rect, hard_blockers,
        [(j, env) for j, env in envelopes], ports)

    for k, info in port_info.items():
        if not info["lead_ok"]:
            failed[k].append(info["lead_label"])

    # ---- 逐设备通道段枚举（先不施加上界取最短，用于超长诊断）----
    max_len = [float(d["max_length"]) for d in dev_specs]
    per_device_paths = []
    for k in range(n):
        if access[k] is None or port_channel.get(k) is None or \
                not port_info.get(k, {}).get("lead_ok"):
            per_device_paths.append([])
            continue
        budget = max_len[k] - access[k]["length"]
        info = port_info[k]
        if budget < -EPS:
            failed[k].append(
                f"接入段长 {access[k]['length']:.2f}m 已超过最长线缆 "
                f"{max_len[k]:.2f}m")
            per_device_paths.append([])
            continue
        paths_all, shortest = _enumerate_paths(
            net, k, info["node"], net.rack_nodes, float("inf"))
        if shortest is None:
            failed[k].append("通道中心线网络无法从该接入口到达机柜"
                             "（通道未连通或被阻断）")
            per_device_paths.append([])
            continue
        if access[k]["length"] + shortest > max_len[k] + EPS:
            failed[k].append(
                f"最短布线总长 {access[k]['length'] + shortest:.2f}m "
                f"超过最长线缆 {max_len[k]:.2f}m")
            per_device_paths.append([])
            continue
        capped = [r for r in paths_all
                  if access[k]["length"] + r["length"] <= max_len[k] + EPS]
        per_device_paths.append(capped or paths_all[:1])

    # ---- 阻断通道（几何）----
    blocked_channels = []
    for i, spans in channel_spans.items():
        if spans:
            names = dict.fromkeys(s["blocker"] for s in spans)
            blocked_channels.append({
                "index": i,
                "name": channels[i].get("name") or f"通道 {i + 1}",
                "reasons": [f"中心线被「{nm}」阻断" for nm in names],
                "spans": spans,
            })

    def any_fail():
        return any(failed[k] for k in range(n))

    # ---- 联合选择（容量）----
    chosen = None
    capacity_blocked = []
    if not any_fail() and all(per_device_paths[k] for k in range(n)):
        chosen = _joint_select(per_device_paths, capacities, n)
        if chosen is None:
            # 归因：逐一放开某通道容量后恢复联合可行 → 容量瓶颈通道
            for ci, cap in enumerate(capacities):
                if cap >= n:
                    continue
                rel = list(capacities)
                rel[ci] = n + 1
                if _joint_select(per_device_paths, rel, n) is not None:
                    capacity_blocked.append(ci)

    # ---- 输出 ----
    channels_echo = []
    for i, ch in enumerate(channels):
        channels_echo.append({
            "index": i,
            "name": ch.get("name") or f"通道 {i + 1}",
            "rect": [round(ch["x"], 4), round(ch["y"], 4),
                     round(ch["x"] + ch["w"], 4),
                     round(ch["y"] + ch["h"], 4)],
            "capacity": capacities[i],
            "occupancy": 0,
            "used_by": [],
            "blocked": bool(channel_spans[i]),
            "blockers": list(dict.fromkeys(
                s["blocker"] for s in channel_spans[i])),
        })

    base["rack_points"] = [[round(net.nb.coords[g][0], 4),
                            round(net.nb.coords[g][1], 4)]
                           for g in sorted(net.rack_nodes)]
    base["channels"] = channels_echo
    base["blocked_channels"] = blocked_channels

    if any_fail() or chosen is None:
        base["status"] = "no_joint_solution"
        for k in range(n):
            if failed[k]:
                base["failed_devices"].append({
                    "index": k,
                    "name": segments[k]["scheme"].get("name") or f"设备 {k + 1}",
                    "reasons": failed[k],
                })
        if not any_fail() and chosen is None:
            if capacity_blocked:
                for ci in capacity_blocked:
                    base["blocked_channels"].append({
                        "index": ci,
                        "name": channels[ci].get("name") or f"通道 {ci + 1}",
                        "reasons": [f"通道容量 {capacities[ci]} 不足："
                                    f"所有可行走线都需要更多线缆共用该通道"],
                        "spans": [],
                    })
            base["diagnostics"].append(
                "通道容量约束下不存在让全部设备同时接入机柜的联合布线"
                + ("（容量瓶颈：" + "、".join(
                    f"「{channels[ci].get('name') or f'通道 {ci + 1}'}」"
                    for ci in capacity_blocked) + "）" if capacity_blocked
                  else ""))
        return base

    # ---- 成功：整理每台路径 / 长度 / 通道占用 ----
    total, choice = chosen
    occupancy = [set() for _ in channels]
    devices_out = []
    for k in range(n):
        rec = per_device_paths[k][choice[k]]
        acc = access[k]
        nodes = [net.nb.coords[v] for v in rec["nodes"]]
        used = sorted(rec["used"])
        for c in used:
            occupancy[c].add(k)
        total_k = acc["length"] + rec["length"]
        devices_out.append({
            "index": k,
            "name": segments[k]["scheme"].get("name") or f"设备 {k + 1}",
            "access": [round(dev_specs[k]["access"]["x"], 4),
                       round(dev_specs[k]["access"]["y"], 4)],
            "access_path": [[round(x, 4), round(y, 4)] for x, y in acc["path"]],
            "path": [[round(x, 4), round(y, 4)] for x, y in nodes],
            "access_length": round(acc["length"], 4),
            "channel_length": round(rec["length"], 4),
            "length": round(total_k, 4),
            "max_length": round(max_len[k], 4),
            "within_max_length": total_k <= max_len[k] + EPS,
            "channels": used,
        })
    for i, users in enumerate(occupancy):
        channels_echo[i]["occupancy"] = len(users)
        channels_echo[i]["used_by"] = sorted(users)

    base.update({
        "status": "ok", "routed": True,
        "total_length": round(
            sum(access[k]["length"] + per_device_paths[k][choice[k]]["length"]
                for k in range(n)), 4),
        "devices": devices_out,
    })
    return base
