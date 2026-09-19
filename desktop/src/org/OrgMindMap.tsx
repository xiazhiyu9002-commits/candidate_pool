import { useEffect, useMemo, useRef, useState } from "react";
import type { OrgTreeNode } from "../App";


const NODE_MIN_W = 120;
const NODE_H = 52;
const HGAP = 24;
const VGAP = 46;
const MARGIN = 56;
const NODE_PADDING_X = 10;
const NODE_GAP = 6;

let _measureCtx: CanvasRenderingContext2D | null = null;

function measureText(text: string, weight: number, size: number): number {
  if (!_measureCtx) {
    _measureCtx = document.createElement("canvas").getContext("2d");
  }
  if (!_measureCtx) return text.length * size * 0.6;
  _measureCtx.font = `${weight} ${size}px Inter, "PingFang SC", "Microsoft YaHei", sans-serif`;
  return _measureCtx.measureText(text).width;
}

function nodeLabel(node: OrgTreeNode): { name: string; sub: string } {
  const name = node.name + (node.is_key ? " ★" : "");
  const sub =
    node.kind === "department"
      ? node.team_size != null
        ? `${node.team_size} 人`
        : ""
      : [node.title, node.job_level].filter(Boolean).join(" · ");
  return { name, sub };
}

function nodeWidth(node: OrgTreeNode): number {
  const { name, sub } = nodeLabel(node);
  const nameW = measureText(name, 700, 13);
  const subW = sub ? measureText(sub, 400, 11) : 0;
  const toggle = node.children.length > 0 ? NODE_GAP + 10 : 0;
  return Math.max(NODE_MIN_W, Math.ceil(Math.max(nameW, subW) + toggle + NODE_PADDING_X * 2));
}

function collectNodes(node: OrgTreeNode, acc: OrgTreeNode[] = []): OrgTreeNode[] {
  acc.push(node);
  for (const child of node.children) collectNodes(child, acc);
  return acc;
}


interface Position {
  x: number;
  y: number;
  depth: number;
}

interface Layout {
  positions: Map<string, Position>;
  edges: { from: Position; to: Position }[];
  nodes: OrgTreeNode[];
  parentOf: Map<string, string>;
  width: number;
  height: number;
}


function computeLayout(root: OrgTreeNode, collapsed: Set<string>, widths: Map<string, number>): Layout {
  const positions = new Map<string, Position>();
  const edges: { from: Position; to: Position }[] = [];
  const nodes: OrgTreeNode[] = [];
  const parentOf = new Map<string, string>();

  const nodeW = (node: OrgTreeNode) => widths.get(node.id) ?? NODE_MIN_W;

  const visibleChildren = (node: OrgTreeNode) =>
    collapsed.has(node.id) ? [] : node.children;

  const subtreeWidth = (node: OrgTreeNode): number => {
    const kids = visibleChildren(node);
    if (kids.length === 0) return nodeW(node);
    return Math.max(
      nodeW(node),
      kids.reduce((sum, k) => sum + subtreeWidth(k), 0) + HGAP * (kids.length - 1)
    );
  };

  let maxDepth = 0;

  const assign = (node: OrgTreeNode, depth: number, left: number, parentId: string | null) => {
    const width = subtreeWidth(node);
    const pos: Position = { x: left + width / 2, y: MARGIN + depth * (NODE_H + VGAP), depth };
    positions.set(node.id, pos);
    nodes.push(node);
    maxDepth = Math.max(maxDepth, depth);
    if (parentId) {
      parentOf.set(node.id, parentId);
      edges.push({ from: positions.get(parentId)!, to: pos });
    }

    let childLeft = left;
    for (const child of visibleChildren(node)) {
      const cw = subtreeWidth(child);
      assign(child, depth + 1, childLeft, node.id);
      childLeft += cw + HGAP;
    }
  };

  assign(root, 0, MARGIN, null);

  const width = subtreeWidth(root) + 2 * MARGIN;
  const height = MARGIN + maxDepth * (NODE_H + VGAP) + MARGIN;

  return { positions, edges, nodes, parentOf, width, height };
}


/** 只有空白画布才进入平移：节点、工具条、输入框要保留各自的点击与拖拽行为。 */
function isPanSurface(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  return target.closest(".org-node, .org-mindmap-toolbar, input, button") === null;
}


interface OrgMindMapProps {
  tree: OrgTreeNode | null;
  selectedId: string | null;
  onSelect: (node: OrgTreeNode) => void;
  onRename: (node: OrgTreeNode, name: string) => void;
  onAddChild: (node: OrgTreeNode) => void;
  onAddSibling: (node: OrgTreeNode) => void;
  onDelete: (node: OrgTreeNode) => void;
  onMove: (source: OrgTreeNode, target: OrgTreeNode) => void;
  onUndo: () => void;
  onRedo: () => void;
  pendingEditId: string | null;
  onPendingEditConsumed: () => void;
  isFullscreen: boolean;
  onToggleFullscreen: () => void;
}


export function OrgMindMap({
  tree,
  selectedId,
  onSelect,
  onRename,
  onAddChild,
  onAddSibling,
  onDelete,
  onMove,
  onUndo,
  onRedo,
  pendingEditId,
  onPendingEditConsumed,
  isFullscreen,
  onToggleFullscreen,
}: OrgMindMapProps) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [zoom, setZoom] = useState(1);
  const [panning, setPanning] = useState(false);
  const consumedPendingEditRef = useRef<string | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const panStartRef = useRef<{ x: number; y: number; left: number; top: number } | null>(null);

  function panStart(event: React.MouseEvent<HTMLDivElement>) {
    const el = scrollRef.current;
    if (event.button !== 0 || !el || !isPanSurface(event.target)) return;
    event.preventDefault();
    panStartRef.current = { x: event.clientX, y: event.clientY, left: el.scrollLeft, top: el.scrollTop };
    setPanning(true);
  }

  // 拖动期间指针大概率会移出容器，监听挂在 document 上，松手才结束。
  useEffect(() => {
    if (!panning) return;
    function onMove(event: MouseEvent) {
      const el = scrollRef.current;
      const start = panStartRef.current;
      if (!el || !start) return;
      el.scrollLeft = start.left - (event.clientX - start.x);
      el.scrollTop = start.top - (event.clientY - start.y);
    }
    function onUp() {
      panStartRef.current = null;
      setPanning(false);
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    return () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
  }, [panning]);

  function zoomBy(delta: number) {
    setZoom((z) => Math.min(2, Math.max(0.4, Math.round((z + delta) * 10) / 10)));
  }

  function onWheel(event: React.WheelEvent) {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    zoomBy(event.deltaY < 0 ? 0.1 : -0.1);
  }

  const { layout, widths } = useMemo(() => {
    if (!tree) return { layout: null, widths: new Map<string, number>() };
    const widths = new Map<string, number>();
    for (const n of collectNodes(tree)) widths.set(n.id, nodeWidth(n));
    return { layout: computeLayout(tree, collapsed, widths), widths };
  }, [tree, collapsed]);

  function selectedNode(): OrgTreeNode | null {
    if (!layout) return null;
    return layout.nodes.find((n) => n.id === selectedId) ?? null;
  }

  function toggle(node: OrgTreeNode) {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(node.id)) next.delete(node.id);
      else next.add(node.id);
      return next;
    });
  }

  function startRename(node: OrgTreeNode) {
    setEditingId(node.id);
    setDraft(node.name);
  }

  function commitRename(node: OrgTreeNode) {
    const name = draft.trim();
    setEditingId(null);
    if (name && name !== node.name) onRename(node, name);
  }

  function onKeyDown(event: React.KeyboardEvent) {
    const meta = event.metaKey || event.ctrlKey;
    if (meta && (event.key === "z" || event.key === "Z")) {
      event.preventDefault();
      if (event.shiftKey) onRedo();
      else onUndo();
      return;
    }
    if (meta && (event.key === "y" || event.key === "Y")) {
      event.preventDefault();
      onRedo();
      return;
    }

    const node = selectedNode();
    if (!node || !layout) return;
    if (editingId) {
      if (event.key === "Enter") {
        event.preventDefault();
        commitRename(node);
      } else if (event.key === "Escape") {
        setEditingId(null);
      }
      return;
    }
    if (event.key === "Shift") {
      event.preventDefault();
      const parentId = layout.parentOf.get(node.id);
      if (parentId) {
        const parent = layout.nodes.find((n) => n.id === parentId);
        if (parent) onSelect(parent);
      }
      return;
    }
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      const parentId = layout.parentOf.get(node.id);
      if (parentId) {
        const parent = layout.nodes.find((n) => n.id === parentId);
        const siblings = parent ? parent.children : [];
        const idx = siblings.findIndex((s) => s.id === node.id);
        if (idx >= 0) {
          const target = event.key === "ArrowLeft" ? siblings[idx - 1] : siblings[idx + 1];
          if (target) onSelect(target);
        }
      }
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      onAddSibling(node);
    } else if (event.key === "Tab") {
      event.preventDefault();
      onAddChild(node);
    } else if (event.key === "F2") {
      event.preventDefault();
      startRename(node);
    } else if (event.key === " ") {
      event.preventDefault();
      toggle(node);
    } else if (event.key === "Delete" || event.key === "Backspace") {
      event.preventDefault();
      onDelete(node);
    }
  }

  useEffect(() => {
    if (!tree) return;
    setCollapsed(new Set());
    setEditingId(null);
  }, [tree?.id]);

  useEffect(() => {
    if (!pendingEditId || !layout) return;
    if (consumedPendingEditRef.current === pendingEditId) return;
    consumedPendingEditRef.current = pendingEditId;
    const node = layout.nodes.find((n) => n.id === pendingEditId);
    if (node) {
      setEditingId(node.id);
      setDraft(node.name);
    }
    onPendingEditConsumed();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingEditId, layout]);

  if (!tree || !layout) {
    return <div className="org-mindmap org-mindmap--empty">选择一家公司查看组织架构</div>;
  }

  return (
    <div
      ref={scrollRef}
      className={panning ? "org-mindmap is-panning" : "org-mindmap"}
      tabIndex={0}
      onKeyDown={onKeyDown}
      onWheel={onWheel}
      onMouseDown={panStart}
    >
      <div className="org-mindmap-toolbar">
        <button type="button" onClick={() => zoomBy(-0.2)} aria-label="缩小">−</button>
        <span>{Math.round(zoom * 100)}%</span>
        <button type="button" onClick={() => zoomBy(0.2)} aria-label="放大">+</button>
        <button type="button" onClick={() => setZoom(1)} aria-label="重置缩放" title="重置缩放">↺</button>
        <button
          type="button"
          onClick={onToggleFullscreen}
          aria-label={isFullscreen ? "退出全屏" : "全屏"}
          title={isFullscreen ? "退出全屏（Esc）" : "全屏查看架构图"}
        >
          {isFullscreen ? "⤡" : "⛶"}
        </button>
      </div>
      <div className="org-mindmap-zoom" style={{ width: layout.width * zoom, height: layout.height * zoom }}>
        <div className="org-mindmap-scroll" style={{ width: layout.width, height: layout.height, transform: `scale(${zoom})`, transformOrigin: "0 0" }}>
          <svg width={layout.width} height={layout.height} className="org-edges">
            {layout.edges.map((edge, i) => (
              <path
                key={i}
                d={elbowPath(edge.from, edge.to)}
                fill="none"
                stroke="#c8d4cd"
                strokeWidth={1.5}
              />
          ))}
        </svg>

        {layout.nodes.map((node) => {
          const pos = layout.positions.get(node.id)!;
          const w = widths.get(node.id) ?? NODE_MIN_W;
          const hasChildren = node.children.length > 0;
          const isCollapsed = collapsed.has(node.id);
          const isEditing = editingId === node.id;
          const isSelected = selectedId === node.id;
          const isRoot = node.id === tree.id;

          return (
            <div
              key={node.id}
              className={[
                "org-node",
                `org-node--${node.kind}`,
                isSelected ? "is-selected" : "",
                isRoot ? "is-root" : "",
              ].join(" ")}
              style={{ left: pos.x - w / 2, top: pos.y - NODE_H / 2, width: w, height: NODE_H }}
              onClick={(e) => { e.stopPropagation(); onSelect(node); }}
              onDoubleClick={() => startRename(node)}
              draggable={node.kind !== "company"}
              onDragStart={(e) => { e.dataTransfer.setData("text/plain", node.id); e.stopPropagation(); }}
              // 公司节点也接受拖放：把部门拖到公司上＝提升为顶层部门。
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                e.stopPropagation();
                const sourceId = e.dataTransfer.getData("text/plain");
                if (!sourceId || sourceId === node.id) return;
                const source = layout.nodes.find((n) => n.id === sourceId);
                if (source && source.kind !== "company") onMove(source, node);
              }}
              title={[node.title, node.job_level].filter(Boolean).join(" · ")}
            >
              {hasChildren && (
                <button
                  className="org-node-toggle"
                  onClick={(e) => { e.stopPropagation(); toggle(node); }}
                >
                  {isCollapsed ? "▸" : "▾"}
                </button>
              )}

              {isEditing ? (
                <input
                  className="org-node-input"
                  autoFocus
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onBlur={() => commitRename(node)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") commitRename(node);
                    if (e.key === "Escape") setEditingId(null);
                  }}
                />
              ) : (
                <>
                  <span className="org-node-name">{node.name}{node.is_key ? " ★" : ""}</span>
                  <span className="org-node-sub">
                    {node.kind === "department"
                      ? (node.team_size != null ? `${node.team_size} 人` : "")
                      : [node.title, node.job_level].filter(Boolean).join(" · ")}
                  </span>
                </>
              )}

              {hasChildren && isCollapsed && (
                <span className="org-node-badge">+{node.children.length}</span>
              )}
            </div>
          );
        })}
        </div>
      </div>
    </div>
  );
}


function elbowPath(from: Position, to: Position): string {
  const midY = (from.y + NODE_H / 2 + to.y - NODE_H / 2) / 2;
  return [
    `M ${from.x} ${from.y + NODE_H / 2}`,
    `L ${from.x} ${midY}`,
    `L ${to.x} ${midY}`,
    `L ${to.x} ${to.y - NODE_H / 2}`,
  ].join(" ");
}
