import { useEffect, useRef, useState, type CSSProperties } from "react";

export const HOVER_PREVIEW_CHARS = 10;

/** 鼠标从单元格移动到浮层之间允许的最大间隔时间，避免中途穿越空隙时浮层提前关闭。 */
const CLOSE_DELAY_MS = 240;

// 取首行前 maxChars 个字符作为预览，超出部分用省略号代替。
export function truncateFirstLine(text: string | null | undefined, maxChars = HOVER_PREVIEW_CHARS): string {
  const firstLine = (text ?? "").split("\n")[0].trim();
  if (!firstLine) return "";
  return firstLine.length > maxChars ? `${firstLine.slice(0, maxChars)}…` : firstLine;
}

// 证据编号写法：projects[0]、experiences[1].summary、项目[0] 等。
const EVIDENCE_REF = String.raw`[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]{0,11}\s*\[\s*[0-9A-Za-z]{1,4}\s*\](?:\.[A-Za-z_][A-Za-z0-9_]*)*`;
// 命中两类：① 括号（含全角/方括号）包裹的一串证据编号；② 行尾裸露的证据编号。
const EVIDENCE_REF_BLOCK = new RegExp(
  String.raw`[（(【\[]\s*(?:${EVIDENCE_REF}\s*[、,，/]?\s*)+[）)】\]]` + "|" + String.raw`(?:[、,，；;]?\s*${EVIDENCE_REF}\s*)+$`,
  "gm",
);

// 去掉复核结论里的证据编号（如「（projects[0]、experiences[1]）」「(projects[o])」「experiences[0].summary」），只保留理由正文。
export function stripEvidenceRefs(text: string): string {
  return text.replace(EVIDENCE_REF_BLOCK, "").replace(/[ \t]{2,}/g, " ").trim();
}

interface PanelPosition {
  top?: number;
  bottom?: number;
  left: number;
  width: number;
  maxHeight: number;
}

export interface HoverTextProps {
  /** 完整内容，使用 \n 分隔多行。 */
  text: string | null | undefined;
  /** 自定义预览文案，缺省时取 text 首行前 10 字。 */
  preview?: string;
  /** 预览截断字数，默认 10。 */
  maxChars?: number;
  className?: string;
}

/**
 * 长文本单元格：默认只展示首行前若干字，鼠标悬停时弹出浮层展示完整内容。
 * 浮层使用 fixed 定位，避免被表格的 overflow 裁剪；鼠标可移入浮层继续阅读或滚动。
 */
export function HoverText({ text, preview, maxChars, className }: HoverTextProps) {
  const anchorRef = useRef<HTMLSpanElement>(null);
  const closeTimer = useRef<number | null>(null);
  const [position, setPosition] = useState<PanelPosition | null>(null);
  const limit = maxChars ?? HOVER_PREVIEW_CHARS;
  const full = (text ?? "").trim();
  const previewText = preview ?? truncateFirstLine(full, limit);
  const firstLine = full.split("\n")[0].trim();
  const hasMore = full.length > 0 && (full.split("\n").length > 1 || firstLine.length > limit);

  useEffect(() => () => {
    if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
  }, []);

  function cancelClose() {
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  }

  // 延迟关闭：给鼠标留出从单元格移动到浮层的时间。
  function scheduleClose() {
    cancelClose();
    closeTimer.current = window.setTimeout(() => {
      closeTimer.current = null;
      setPosition(null);
    }, CLOSE_DELAY_MS);
  }

  function open() {
    if (!hasMore || !anchorRef.current) return;
    cancelClose();
    const rect = anchorRef.current.getBoundingClientRect();
    const margin = 8;
    const width = Math.min(460, window.innerWidth - margin * 2);
    const left = Math.min(Math.max(rect.left, margin), Math.max(margin, window.innerWidth - width - margin));
    const spaceBelow = window.innerHeight - rect.bottom - margin;
    const spaceAbove = rect.top - margin;
    // 下方空间足够（或比上方更宽裕）时向下弹，否则向上弹。
    const placeBelow = spaceBelow >= 180 || spaceBelow >= spaceAbove;
    const maxHeight = Math.max(180, Math.min(480, placeBelow ? spaceBelow : spaceAbove));
    const offset = 2;
    if (placeBelow) {
      setPosition({ top: rect.bottom + offset, left, width, maxHeight });
    } else {
      setPosition({ bottom: window.innerHeight - rect.top + offset, left, width, maxHeight });
    }
  }

  return (
    <span
      ref={anchorRef}
      className={`hover-text ${className ?? ""}`.trim()}
      onMouseEnter={open}
      onMouseLeave={scheduleClose}
    >
      {previewText || "—"}
      {position && (
        <span
          className="hover-text__panel"
          style={position as CSSProperties}
          role="tooltip"
          onMouseEnter={cancelClose}
          onMouseLeave={scheduleClose}
        >
          {full.split("\n").map((line, index) => (
            <span key={index} className="hover-text__line">{line || "\u00a0"}</span>
          ))}
        </span>
      )}
    </span>
  );
}
