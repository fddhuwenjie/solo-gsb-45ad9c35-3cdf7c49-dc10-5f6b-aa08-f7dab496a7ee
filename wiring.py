"""投影机布线规划（多机拼接工作台扩展能力）。

输入（顶视图坐标，单位米）：
  devices[k] = {
      "name", "point":(x,y)  —— 已通过拼接校核的镜头安装点，
      "body_w","body_d","clearance" —— 机身尺寸与检修净距，用于构造
                                       “其他设备的机身检修包络”障碍，
      "access":(x,y) —— 用户为该台指定的通道接入口，
      "max_length"   —— 最长线缆长度，
  }
  cabinet=(x,y) 机柜位置；
  channels=[{name,x,y,w,h,capacity}] 由相交矩形组成的可走线通道网络；
  obstacles=[{name,x,y,w,h}] 房间禁装区（与单机/拼接同一约定）。

路径模型
--------
① 接入段（安装点 → 接入口）：禁装区与其他设备检修包络构成轴对齐矩形
   障碍集，以起点、终点与各障碍四角构建 visibility graph（边界接触允许，
   与全工作台一致），Dijkstra 求折线最短路。
② 通道段（接入口 → 机柜）：每个矩形通道取长边方向中心线；两通道矩形
   严格相交时，以相交区中心为接线节点、向各自中心线做投影短接（矩形为
   凸集，短接线全程位于对应通道内部）；接入口/机柜以同样的投影方式接入
   所有包含它的通道。线缆沿中心线与短接边行进。
③ 边逐设备判定可用：禁装区阻断所有设备；设备 j 的检修包络只阻断
   j 以外的设备（本机线缆沿自身包络走线不受限）。
④ 容量：每根线缆对其途经的每个通道计 1 次占用，占用数不得超过容量。

联合求解
--------
接入段障碍与安装点均固定，接入段最短路逐设备唯一确定；通道段在最长
线缆限额内枚举全部简单路径，再按设备顺序 DFS 组合容量可行方案，以
(总长度, 逐设备长度向量, 路径签名) 取确定性最优（同长按设备顺序稳定）。
任一设备无避障接入段 / 通道不可达 / 超最长线缆，或容量无共同解时，
不返回任何路径（无部分布线），仅回传失败设备、阻断通道与诊断。
"""

import heapq
from itertools import combinations

from geometry import EPS, rects_intersect, service_envelope

# 单设备通道简单路径枚举上限（通道网络规模很小，仅作安全截断）
_PATH_CAP = 1000


# ==========================================================================
# 基础几何
# ==========================================================================

def _seg_crosses_rect(p, q, r):
    """开线段 p→q 是否穿过矩形 r 的内部（仅边界接触/擦边不算，Liang-Barsky）。"""
    x1, y1, x2, y2 = r
    px, py = p
    lo, hi = 0.0, 1.0
    for v, d, a, b in ((px, q[0] - px, x1, x2),
                       (py, q[1] - py, y1, y2)):
        if abs(d) <= EPS:
            if not (a + EPS < v < b - EPS):
                return False
            continue
        t_a, t_b = (a - v) / d, (b - v) / d
        tl, th = min(t_a, t_b), max(t_a, t_b)
        lo, hi = max(lo, tl), min(hi, th)
        if lo >= hi - EPS:
            return False
    return hi - lo > EPS


def _point_in_rect(p, r, tol=EPS):
    """点是否在矩形闭包内（含边界，tol 容差）。"""
    return (r[0] - tol <= p[0] <= r[2] + tol and
            r[1] - tol <= p[1] <= r[3] + tol)


def _point_inside(p, r):
    """点是否严格位于矩形内部。"""
    return (r[0] + EPS < p[0] < r[2] - EPS and
            r[1] + EPS < p[1] < r[3] - EPS)


def _dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


# ==========================================================================
# 通道网络构图
# ==========================================================================

class _Network:
    def __init__(self):
        self.key_to_idx = {}
        self.coords = []
        self.edges = []          # {a,b,cid,length,blocked:[label_idx…]}

    def node(self, p):
        key = (round(p[0], 6), round(p[1], 6))
        if key not in self.key_to_idx:
            self.key_to_idx[key] = len(self.coords)
            self.coords.append((float(p[0]), float(p[1])))
        return self.key_to_idx[key]

    def add_edge(self, p, q, cid):
        length = _dist(p, q)
        if length <= EPS:
            return
        a, b = self.node(p), self.node(q)
        if a == b:
            return
        self.edges.append({"a": a, "b": b, "cid": cid,
                           "length": length, "blocked": []})


def _channel_geom(ch):
    x, y, w, h = ch["x"], ch["y"], ch["w"], ch["h"]
    rect = [x, y, x + w, y + h]
    horizontal = w >= h                     # 通道沿长边取中心线
    if horizontal:
        cy = (y + y + h) / 2.0
        center = ((x, cy), (x + w, cy))
        axis = 0
    else:
        cx = (x + x + w) / 2.0
        center = ((cx, y), (cx, y + h))
        axis = 1
    return rect, center, axis


def _project_to_channel(p, ch_geom):
    """点 p 到通道中心线的投影（夹在中心线端点内）。p 在通道矩形内时，
    投影点与 p 的连线整段位于该矩形内（凸性）。"""
    rect, center, axis = ch_geom
    if axis == 0:
        x = min(max(p[0], rect[0]), rect[2])
        return (x, center[0][1])
    y = min(max(p[1], rect[1]), rect[3])
    return (center[0][0], y)


def _build_network(channels, attach_points):
    """构建通道网络图。

    attach_points: 需接入网络的点名点坐标（各设备接入口 + 机柜）；所有
    落在某通道矩形内（含边界）的点均以投影短接边接入该通道中心线。
    """
    geoms = [_channel_geom(ch) for ch in channels]
    net = _Network()
    line_pts = [set() for _ in channels]
    extra = []                              # (投影点, 外部点, cid)

    # 中心线端点
    for i, (_, center, _) in enumerate(geoms):
        line_pts[i].add(center[0])
        line_pts[i].add(center[1])

    # 相交通道：相交区中心为接线节点，向各自中心线投影短接
    for i, j in combinations(range(len(channels)), 2):
        if not rects_intersect(geoms[i][0], geoms[j][0]):
            continue
        ri, rj = geoms[i][0], geoms[j][0]
        ox1, oy1 = max(ri[0], rj[0]), max(ri[1], rj[1])
        ox2, oy2 = min(ri[2], rj[2]), min(ri[3], rj[3])
        junc = ((ox1 + ox2) / 2.0, (oy1 + oy2) / 2.0)
        for cid in (i, j):
            proj = _project_to_channel(junc, geoms[cid])
            line_pts[cid].add(proj)
            extra.append((proj, junc, cid))

    # 外部点（接入口、机柜）接入包含它的通道
    for q in attach_points:
        for cid, geom in enumerate(geoms):
            if _point_in_rect(q, geom[0]):
                proj = _project_to_channel(q, geom)
                line_pts[cid].add(proj)
                extra.append((proj, q, cid))

    # 中心线按轴向排序连成链
    for cid, (_, center, axis) in enumerate(geoms):
        pts = sorted(line_pts[cid], key=lambda p: (p[axis], p[1 - axis]))
        for p, q in zip(pts[:-1], pts[1:]):
            net.add_edge(p, q, cid)
    for p, q, cid in extra:
        net.add_edge(p, q, cid)
    return net, geoms


# ==========================================================================
# 接入段：障碍角点 visibility graph 最短路
# ==========================================================================

def _visible(p, q, blocker_rects):
    return not any(_seg_crosses_rect(p, q, r) for r in blocker_rects)


def _access_shortest(start, target, blocker_rects):
    """visibility graph 上求 start→target 最短折线。

    返回 (路径点列表, 长度)；不可达返回 (None, None)。
    """
    pts = [start, target]
    for r in blocker_rects:
        pts.append((r[0], r[1]))
        pts.append((r[2], r[1]))
        pts.append((r[2], r[3]))
        pts.append((r[0], r[3]))
    n = len(pts)
    adj = [[] for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if _visible(pts[i], pts[j], blocker_rects):
                d = _dist(pts[i], pts[j])
                adj[i].append((j, d))
                adj[j].append((i, d))
    dist = [float("inf")] * n
    prev = [-1] * n
    dist[0] = 0.0
    heap = [(0.0, 0)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist[u] + EPS:
            continue
        if u == 1:
            break
        for v, w in adj[u]:
            nd = d + w
            if nd + EPS < dist[v]:
                dist[v] = nd
                prev[v] = u
                heapq.heappush(heap, (nd, v))
    if not dist[1] < float("inf"):
        return None, None
    path, k = [], 1
    while k >= 0:
        path.append(pts[k])
        k = prev[k]
    path.reverse()
    return path, dist[1]


# ==========================================================================
# 通道段：限额内枚举简单路径
# ==========================================================================

def _enum_channel_paths(net, src, dst, usable, length_cap):
    """枚举 src→dst 所有长度 ≤ length_cap 的简单路径，返回边序列集合。"""
    adj = [[] for _ in net.coords]
    for ei, e in enumerate(net.edges):
        if not usable[ei]:
            continue
        a, b = e["a"], e["b"]
        adj[a].append((b, ei, e["length"]))
        adj[b].append((a, ei, e["length"]))
    for lst in adj:
        lst.sort(key=lambda t: (t[0], t[2]))

    sigs = []
    seen = set()
    visited = {src}

    def dfs(u, length, edge_path):
        if u == dst:
            sig = tuple(edge_path)
            if sig not in seen:
                seen.add(sig)
                sigs.append(sig)
            return
        if len(sigs) >= _PATH_CAP:
            return
        for v, ei, w in adj[u]:
            if v in visited or length + w > length_cap + EPS:
                continue
            visited.add(v)
            edge_path.append(ei)
            dfs(v, length + w, edge_path)
            edge_path.pop()
            visited.remove(v)

    dfs(src, 0.0, [])
    return sigs


def _route_options(net, src, dst, usable, length_cap):
    """简单路径枚举并整理为候选项（按长度/节点签名确定性排序）。"""
    options = []
    for sig in _enum_channel_paths(net, src, dst, usable, length_cap):
        length = sum(net.edges[ei]["length"] for ei in sig)
        used = {net.edges[ei]["cid"] for ei in sig}
        nodes = [src]
        for ei in sig:
            e = net.edges[ei]
            nodes.append(e["b"] if e["a"] == nodes[-1] else e["a"])
        options.append({"edges": sig, "nodes": nodes,
                        "length": length, "used": used})
    options.sort(key=lambda o: (o["length"], tuple(o["nodes"]),
                                tuple(sorted(o["used"]))))
    return options


# ==========================================================================
# 入口
# ==========================================================================

def _as_pos_float(v, label, allow_zero=False):
    f = float(v)
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError(f"{label} 必须是有限数值")
    if f < -EPS or (not allow_zero and f <= EPS):
        raise ValueError(f"{label} 必须为正数（当前 {f:g}）")
    return max(0.0, f) if allow_zero else f


def compute_wiring(devices, cabinet, channels, obstacles):
    """投影机布线联合规划。devices/cabinet/channels/obstacles 结构见模块文档。

    参数级错误抛 ValueError（路由层转 HTTP 400）；规划级无解通过返回值
    status="no_joint_solution" 表达，routes 为 None（不返回部分布线）。
    """
    n = len(devices)
    cab = (float(cabinet["x"]), float(cabinet["y"]))
    if cab[0] != cab[0] or cab[1] != cab[1]:
        raise ValueError("机柜坐标必须是有限数值")

    ch_in, ch_rects = [], []
    for i, ch in enumerate(channels):
        x, y = float(ch["x"]), float(ch["y"])
        w, h = float(ch["w"]), float(ch["h"])
        vals = [x, y, w, h,
                float(ch.get("capacity", 1))]
        if any(v != v or v in (float("inf"), float("-inf")) for v in vals):
            raise ValueError(f"通道 {i + 1} 的参数必须是有限数值")
        if w <= EPS or h <= EPS:
            raise ValueError(f"通道 {i + 1} 的宽、高必须为正")
        cap_raw = ch.get("capacity", 1)
        cap = int(cap_raw)
        if cap != float(cap_raw) or cap < 1:
            raise ValueError(f"通道 {i + 1} 容量必须为 ≥1 的整数")
        ch_in.append({"name": ch.get("name") or f"通道 {i + 1}",
                      "x": x, "y": y, "w": w, "h": h, "capacity": cap})
        ch_rects.append([x, y, x + w, y + h])

    obs_in = []
    for ob in obstacles:
        x, y = float(ob["x"]), float(ob["y"])
        w, h = float(ob["w"]), float(ob["h"])
        if any(v != v or v in (float("inf"), float("-inf"))
               for v in (x, y, w, h)):
            raise ValueError("禁装区坐标/尺寸必须是有限数值")
        if w <= EPS or h <= EPS:
            raise ValueError("禁装区宽、高必须为正")
        obs_in.append({"name": ob.get("name") or "禁装区",
                       "rect": [x, y, x + w, y + h]})

    devs = []
    for k, d in enumerate(devices):
        pt = (float(d["point"]["x"]), float(d["point"]["y"]))
        ac = (float(d["access"]["x"]), float(d["access"]["y"]))
        if any(v != v for v in pt + ac):
            raise ValueError(f"设备 {k + 1} 的安装点/接入口坐标必须是有限数值")
        bw = _as_pos_float(d["body_w"], f"设备 {k + 1} 机身宽")
        bd = _as_pos_float(d["body_d"], f"设备 {k + 1} 机深")
        cl = _as_pos_float(d.get("clearance", 0.0),
                           f"设备 {k + 1} 检修净距", allow_zero=True)
        ml = _as_pos_float(d["max_length"], f"设备 {k + 1} 最长线缆长度")
        devs.append({"name": d.get("name") or f"设备 {k + 1}",
                     "point": pt, "access": ac, "bw": bw, "bd": bd,
                     "clearance": cl, "max_length": ml})

    # ---- 障碍标签：禁装区对所有设备生效；检修包络只阻断其他设备 ----
    labels = []
    for ob in obs_in:
        labels.append({"scope": "all", "name": ob["name"], "rect": ob["rect"]})
    for j, d in enumerate(devs):
        labels.append({
            "scope": "other", "dev": j,
            "name": f"设备 {j + 1}（{d['name']}）的检修包络",
            "rect": service_envelope(d["point"][0], d["point"][1],
                                     d["bw"], d["bd"], d["clearance"])})

    # ---- 构图（接入口 + 机柜统一作为外部点接入）----
    attach = [d["access"] for d in devs] + [cab]
    net, geoms = _build_network(ch_in, attach)

    def node_key(p):
        return (round(p[0], 6), round(p[1], 6))

    port_nodes = [net.key_to_idx.get(node_key(d["access"])) for d in devs]
    cab_node = net.key_to_idx.get(node_key(cab))

    # ---- 边阻断标签（几何一次性算好，逐设备按 scope 过滤）----
    for e in net.edges:
        p, q = net.coords[e["a"]], net.coords[e["b"]]
        for li, lab in enumerate(labels):
            if _seg_crosses_rect(p, q, lab["rect"]):
                e["blocked"].append(li)

    def edge_usable(ei, k):
        """边对设备 k 可用：无禁装区阻断，且无其他设备的检修包络阻断。"""
        for li in net.edges[ei]["blocked"]:
            lab = labels[li]
            if lab["scope"] == "all" or lab["dev"] != k:
                return False
        return True

    usable = [[edge_usable(ei, k) for ei in range(len(net.edges))]
              for k in range(n)]

    # ---- 阻断通道明细（按 设备×通道×阻断源 去重）----
    blocked_rows = {}

    def add_blocked(cid, for_dev, by_type, by_name):
        key = (cid, for_dev, by_type, by_name)
        blocked_rows.setdefault(key, {"channel": cid, "for_device": for_dev,
                                      "by_type": by_type, "by_name": by_name})

    for ei, e in enumerate(net.edges):
        for li in e["blocked"]:
            lab = labels[li]
            if lab["scope"] == "all":
                affected = range(n)
                by_type = "obstacle"
            else:
                affected = (k for k in range(n) if k != lab["dev"])
                by_type = "device"
            for k in affected:
                add_blocked(e["cid"], k, by_type, lab["name"])

    diagnostics = []
    failed = [None] * n

    def fail(k, reason):
        if failed[k] is None:
            failed[k] = {"index": k, "name": devs[k]["name"], "reasons": []}
        failed[k]["reasons"].append(reason)

    cabinet_ok = cab_node is not None
    if not cabinet_ok:
        msg = f"机柜位置 ({cab[0]:.2f}, {cab[1]:.2f}) 不在任何通道矩形内，未接入通道网络"
        diagnostics.append(msg)

    # ---- 逐设备：接入段最短路 + 通道候选路径 ----
    access_paths = [None] * n
    access_lens = [0.0] * n
    options = [None] * n

    for k, d in enumerate(devs):
        start, target = d["point"], d["access"]
        bl_rects = [lab["rect"] for li, lab in enumerate(labels)
                    if lab["scope"] == "all" or lab["dev"] != k]

        # 接入口本身落在障碍/他人包络内
        inside = [labels[li]["name"] for li, lab in enumerate(labels)
                  if _point_inside(target, lab["rect"]) and
                  (lab["scope"] == "all" or lab["dev"] != k)]
        if inside:
            fail(k, "接入口位于" + "、".join(f"「{nm}」" for nm in inside) + "内")
        if port_nodes[k] is None:
            fail(k, f"接入口 ({target[0]:.2f}, {target[1]:.2f}) 不在任何通道矩形内，无法接入通道网络")

        # 接入段 visibility 最短路
        if not inside:
            path, length = _access_shortest(start, target, bl_rects)
            if path is None:
                hit = [lab["name"] for lab in labels
                       if (lab["scope"] == "all" or lab["dev"] != k) and
                       _seg_crosses_rect(start, target, lab["rect"])]
                tail = ("（直达段被 " + "、".join(f"「{nm}」" for nm in hit) + " 阻断）"
                        if hit else "（障碍布局封闭了全部绕行角点）")
                fail(k, "安装点到接入口不存在避障接入段" + tail)
            else:
                access_paths[k], access_lens[k] = path, length

        # 通道候选路径：先求不限长度的最短路判定连通性，再按最长线缆限额枚举
        if (not inside and port_nodes[k] is not None and cabinet_ok
                and access_paths[k] is not None):
            uncap = _route_options(net, port_nodes[k], cab_node,
                                   usable[k], float("inf"))
            if not uncap:
                fail(k, "通道网络中不存在到机柜的可行路径（通道中心线/接入口"
                        "被禁装区或其他设备检修包络阻断）")
            else:
                need = access_lens[k] + uncap[0]["length"]
                if need > d["max_length"] + EPS:
                    fail(k, f"最短线缆需 {need:.2f}m，超过最长线缆 "
                            f"{d['max_length']:.2f}m（接入段 {access_lens[k]:.2f}m + "
                            f"通道段 {uncap[0]['length']:.2f}m）")
                else:
                    cap_len = d["max_length"] - access_lens[k]
                    options[k] = _route_options(
                        net, port_nodes[k], cab_node, usable[k], cap_len)

    # ---- 容量预检：各台独立最短路径汇聚超载的通道（仅诊断提示，
    #      联合组合仍会尝试绕行；联合 DFS 才是最终判据）----
    demand = {}
    for k in range(n):
        if options[k]:
            for cid in options[k][0]["used"]:
                demand.setdefault(cid, set()).add(k)
    for cid, users in demand.items():
        if len(users) > ch_in[cid]["capacity"]:
            add_blocked(cid, None, "capacity",
                        f"各台最短路径在「{ch_in[cid]['name']}」汇聚 "
                        f"{len(users)} 根，超过容量 {ch_in[cid]['capacity']}")
            diagnostics.append(
                f"通道容量不足：「{ch_in[cid]['name']}」容量 "
                f"{ch_in[cid]['capacity']}，各台最短路径需有 {len(users)} "
                f"台（{'、'.join(str(i + 1) for i in sorted(users))}）同时经过")

    # ---- 联合组合：按设备顺序 DFS，(总长, 逐段长度, 签名) 确定性最优 ----
    current = [None] * n
    best_sol = None
    indiv_ok = all(failed[k] is None and options[k] for k in range(n))
    best_key = None

    if indiv_ok and cabinet_ok:
        mins = [options[k][0]["length"] + access_lens[k] for k in range(n)]
        usage = {}

        def dfs(k, partial):
            nonlocal best_key, best_sol
            if k == n:
                lengths = tuple(access_lens[i] + current[i]["length"]
                                for i in range(n))
                key = (sum(lengths), lengths,
                       tuple(tuple(o["nodes"]) for o in current))
                if best_key is None or key < best_key:
                    best_key = key
                    best_sol = list(current)
                return
            if partial + sum(mins[k:]) > \
                    (best_key[0] if best_key else float("inf")) + EPS:
                return
            for o in options[k]:
                if all(usage.get(c, 0) + 1 <= ch_in[c]["capacity"]
                       for c in o["used"]):
                    for c in o["used"]:
                        usage[c] = usage.get(c, 0) + 1
                    current[k] = o
                    dfs(k + 1, partial + access_lens[k] + o["length"])
                    for c in o["used"]:
                        usage[c] -= 1
            current[k] = None

        dfs(0, 0.0)

    if best_key is None:
        if indiv_ok and cabinet_ok:
            diagnostics.append(
                "通道容量无共同解：各台单独均可走线，但不存在一组使每根通道"
                "占用数均不超过容量的联合方案，未返回部分布线")
        routes = None
        total = None
        status = "no_joint_solution"
        for k in range(n):
            if failed[k] is None and (not options[k] or not cabinet_ok):
                if not cabinet_ok:
                    fail(k, "机柜未接入通道网络，线缆无法到达机柜")
                else:
                    fail(k, "受通道阻断/容量限制，无法在最长线缆内到达机柜")
        occupancy = [{"index": c, "name": ch_in[c]["name"],
                      "capacity": ch_in[c]["capacity"],
                      "count": 0, "occupied_by": []}
                     for c in range(len(ch_in))]
    else:
        status = "ok"
        routes = []
        occupancy_map = {c: set() for c in range(len(ch_in))}
        total = 0.0
        for k, d in enumerate(devs):
            o = best_sol[k]
            access_pts = [{"x": p[0], "y": p[1]} for p in access_paths[k]]
            chan_nodes = [net.coords[i] for i in o["nodes"]]
            chan_pts = [{"x": p[0], "y": p[1]} for p in chan_nodes]
            # 完整路径：接入折线 + 通道折线（接入口为公共端点，去重）
            full = access_pts + chan_pts[1:]
            length = access_lens[k] + o["length"]
            total += length
            for c in o["used"]:
                occupancy_map[c].add(k)
            routes.append({
                "device": k, "name": d["name"],
                "access_path": access_pts,
                "channel_path": chan_pts,
                "path": full,
                "access_length": round(access_lens[k], 4),
                "channel_length": round(o["length"], 4),
                "length": round(length, 4),
                "max_length": round(d["max_length"], 4),
                "within_max_length": length <= d["max_length"] + EPS,
                "used_channels": [{"index": c, "name": ch_in[c]["name"]}
                                  for c in sorted(o["used"])],
            })
        occupancy = []
        for c in range(len(ch_in)):
            users = sorted(occupancy_map[c])
            occupancy.append({"index": c, "name": ch_in[c]["name"],
                              "capacity": ch_in[c]["capacity"],
                              "count": len(users), "occupied_by": users})

    blocked = sorted(blocked_rows.values(),
                     key=lambda r: (r["channel"],
                                   -1 if r["for_device"] is None else r["for_device"],
                                   r["by_type"], r["by_name"]))
    if best_key is None and not any(f["reasons"] for f in failed if f) and not diagnostics:
        diagnostics.append("无联合布线方案")
    failed_out = [f for f in failed if f is not None]

    return {
        "status": status,
        "routed": status == "ok",
        "count": n,
        "cabinet": {"x": round(cab[0], 4), "y": round(cab[1], 4)},
        "ports": [{"x": round(d["access"][0], 4), "y": round(d["access"][1], 4)}
                  for d in devs],
        "channels": [{
            "index": c, "name": ch_in[c]["name"],
            "rect": [round(v, 4) for v in ch_rects[c]],
            "centerline": [[round(geoms[c][1][0][0], 4), round(geoms[c][1][0][1], 4)],
                           [round(geoms[c][1][1][0], 4), round(geoms[c][1][1][1], 4)]],
            "capacity": ch_in[c]["capacity"],
        } for c in range(len(ch_in))],
        "routes": routes,
        "total_length": round(total, 4) if total is not None else None,
        "channel_occupancy": occupancy,
        "failed": failed_out,
        "blocked_channels": blocked,
        "diagnostics": diagnostics,
    }
