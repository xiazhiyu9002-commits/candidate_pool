import { useId, useRef, useState, type CSSProperties } from "react";

interface PanelPosition {
  top?: number;
  bottom?: number;
  left: number;
  width: number;
}

export interface InfoTipProps {
  /** 悬停或键盘聚焦时显示的说明文字。 */
  text: string;
}

const PANEL_MAX_WIDTH = 280;
const MARGIN = 8;

/**
 * 常显说明文字的替代品：平时只占一个 ⓘ，鼠标悬停或键盘聚焦时弹出说明。
 *
 * 为什么单独做一个而不是复用 `HoverText`：`HoverText` 是「长文本单元格」的预览模式，
 * 只在内容被截断时才弹层，并且会把正文按行渲染；而这里要的是「无论多短都弹」的静态说明。
 *
 * 注意不要把它放进 `<label>`：`<button>` 属于 labelable 元素，嵌在 label 里是非法 HTML，
 * 因此调用方应把 ⓘ 作为切换项的兄弟节点（见 `.rewrite-toggle-group`）。
 *
 * 可访问名称固定为「查看说明」，说明正文通过 `aria-describedby` 关联：把正文直接当名称
 * 会带来两类意外——正文里出现的「关键词」等界面词会被 `getByRole('button', { name: '关键词' })`
 * 误命中，正文与切换项名称互为子串时又会让 `getByLabel` 命中两个元素。
 */
export function InfoTip({ text }: InfoTipProps) {
  const anchorRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();
  const [position, setPosition] = useState<PanelPosition | null>(null);

  function open() {
    if (!anchorRef.current || !text.trim()) return;
    const rect = anchorRef.current.getBoundingClientRect();
    const width = Math.min(PANEL_MAX_WIDTH, window.innerWidth - MARGIN * 2);
    const left = Math.min(
      Math.max(rect.left - 6, MARGIN),
      Math.max(MARGIN, window.innerWidth - width - MARGIN),
    );
    const spaceBelow = window.innerHeight - rect.bottom - MARGIN;
    const spaceAbove = rect.top - MARGIN;
    const placeBelow = spaceBelow >= 80 || spaceBelow >= spaceAbove;
    setPosition(
      placeBelow
        ? { top: rect.bottom + 2, left, width }
        : { bottom: window.innerHeight - rect.top + 2, left, width },
    );
  }

  function close() {
    setPosition(null);
  }

  return (
    <button
      ref={anchorRef}
      type="button"
      className="info-tip"
      aria-label="查看说明"
      aria-describedby={position ? panelId : undefined}
      onMouseEnter={open}
      onMouseLeave={close}
      onFocus={open}
      onBlur={close}
    >
      <span aria-hidden="true">ⓘ</span>
      {position && (
        <span
          id={panelId}
          className="hover-text__panel info-tip__panel"
          style={position as CSSProperties}
          role="tooltip"
        >
          {text}
        </span>
      )}
    </button>
  );
}
