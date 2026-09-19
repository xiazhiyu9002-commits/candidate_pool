import { useState, type MouseEvent as ReactMouseEvent } from "react";

function fallbackCopy(text: string) {
  const area = document.createElement("textarea");
  area.value = text;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    document.execCommand("copy");
  } catch {
    // ignore
  }
  document.body.removeChild(area);
}

function copyToClipboard(text: string) {
  if (!text) return;
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).catch(() => fallbackCopy(text));
  } else {
    fallbackCopy(text);
  }
}

export function EditableCell({
  value,
  onSave,
}: {
  value: string;
  onSave: (next: string) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [saving, setSaving] = useState(false);
  const [copied, setCopied] = useState(false);

  async function save() {
    if (draft === value) {
      setEditing(false);
      return;
    }
    setSaving(true);
    try {
      await onSave(draft);
      setEditing(false);
    } catch {
      // keep editing so the user can retry
    } finally {
      setSaving(false);
    }
  }

  function onContextMenu(event: ReactMouseEvent) {
    event.preventDefault();
    copyToClipboard(value);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 800);
  }

  if (editing) {
    return (
      <input
        autoFocus
        className="editable-input"
        value={draft}
        disabled={saving}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={() => void save()}
        onKeyDown={(event) => {
          if (event.key === "Enter") void save();
          if (event.key === "Escape") setEditing(false);
        }}
      />
    );
  }

  return (
    <span
      className={copied ? "editable-cell copied" : "editable-cell"}
      title="双击修改，右键复制"
      onContextMenu={onContextMenu}
      onDoubleClick={() => {
        setDraft(value);
        setEditing(true);
      }}
    >
      {value || "—"}
    </span>
  );
}
