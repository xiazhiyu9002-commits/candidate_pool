import { useEffect, useRef, useState } from "react";

import {
  BUSINESS_DIRECTION_OPTIONS,
  CAREER_DIRECTION_VALUES,
  SPECIALIZATION_PARENT,
  careerDirectionLabel,
  specializationLabel,
  specializationsOfDirections,
} from "../constants/directions";

/**
 * 职业方向 / 业务方向的多选控件。
 *
 * 职业方向按「大类 -> 细分」两级展示：大类一行，其细分缩进排在同一行右侧，
 * 细分只能在其大类被选中后才可选（点细分会自动带上所属大类）。这样用户直接看到
 * 词表的层级结构，不必自己记住「哪个细分属于哪个大类」。
 *
 * 上限由调用方指定：候选人解析表传 2，JD 解析表传 3，筛选面板不传（不限）。
 */

const DEFAULT_MAX_SPECIALIZATIONS_PER_DIRECTION = 2;

export interface CareerDirectionSelection {
  directions: string[];
  specializations: string[];
}

export interface CareerDirectionPickerProps {
  directions: readonly string[];
  specializations: readonly string[];
  onChange: (next: CareerDirectionSelection) => void;
  /** 大类上限；不传表示不限。 */
  maxDirections?: number;
  /** 每个大类下的细分上限；默认 2。 */
  maxSpecializationsPerDirection?: number;
  /** 待核队列只确认大类时传 false，隐藏二级细分。 */
  showSpecializations?: boolean;
  ariaLabel?: string;
}

export function CareerDirectionPicker({
  directions,
  specializations,
  onChange,
  maxDirections,
  maxSpecializationsPerDirection = DEFAULT_MAX_SPECIALIZATIONS_PER_DIRECTION,
  showSpecializations = true,
  ariaLabel = "职业方向",
}: CareerDirectionPickerProps) {
  const selectedDirections = new Set(directions);
  // 只保留父级已被选中的细分，避免出现「细分没有对应大类」的矛盾组合。
  const selectedSpecializations = new Set(
    specializations.filter((code) => selectedDirections.has(SPECIALIZATION_PARENT[code])),
  );
  const keptSpecializations = specializations.filter((code) =>
    selectedDirections.has(SPECIALIZATION_PARENT[code]));

  function countSpecializations(direction: string): number {
    return keptSpecializations.filter((code) => SPECIALIZATION_PARENT[code] === direction).length;
  }

  function toggleDirection(direction: string) {
    if (selectedDirections.has(direction)) {
      // 取消大类时一并清掉它的细分，避免留下无父级的细分。
      onChange({
        directions: directions.filter((value) => value !== direction),
        specializations: keptSpecializations.filter(
          (code) => SPECIALIZATION_PARENT[code] !== direction),
      });
      return;
    }
    if (maxDirections !== undefined && directions.length >= maxDirections) return;
    onChange({ directions: [...directions, direction], specializations: [...keptSpecializations] });
  }

  function toggleSpecialization(code: string) {
    const parent = SPECIALIZATION_PARENT[code];
    if (!parent) return;
    if (selectedSpecializations.has(code)) {
      onChange({
        directions: [...directions],
        specializations: keptSpecializations.filter((value) => value !== code),
      });
      return;
    }
    if (countSpecializations(parent) >= maxSpecializationsPerDirection) return;
    // 细分依赖大类：点细分时先自动补上父级，再补细分本身。
    const nextDirections = selectedDirections.has(parent) ? [...directions] : [...directions, parent];
    onChange({ directions: nextDirections, specializations: [...keptSpecializations, code] });
  }

  return (
    // 只要一层时（待核队列只确认大类）平铺成一行标签，避免竖排占满列表行高。
    <div className={`dir-picker${showSpecializations ? "" : " dir-picker--flat"}`}
         role="group" aria-label={ariaLabel}>
      {CAREER_DIRECTION_VALUES.map((option) => {
        const isOn = selectedDirections.has(option.value);
        const children = showSpecializations ? specializationsOfDirections([option.value]) : [];
        const directionBlocked =
          maxDirections !== undefined && !isOn && directions.length >= maxDirections;
        return (
          <div className="dir-picker__row" key={option.value}>
            <button
              type="button"
              className={`dir-chip${isOn ? " dir-chip--on" : ""}`}
              aria-pressed={isOn}
              aria-label={option.label}
              disabled={directionBlocked}
              title={directionBlocked ? `最多只能选 ${maxDirections} 个职业大类` : undefined}
              onClick={() => toggleDirection(option.value)}
            >
              {option.label}
            </button>
            {children.length > 0 && (
              <div className="dir-picker__children">
                {children.map((child) => {
                  const childOn = selectedSpecializations.has(child.value);
                  const childBlocked = !childOn
                    && countSpecializations(option.value) >= maxSpecializationsPerDirection;
                  return (
                    <button
                      key={child.value}
                      type="button"
                      className={`dir-chip dir-chip--sub${childOn ? " dir-chip--on" : ""}`}
                      aria-pressed={childOn}
                      aria-label={`${option.label}·${child.label}`}
                      disabled={childBlocked}
                      title={childBlocked
                        ? `每个大类下最多选 ${maxSpecializationsPerDirection} 个细分`
                        : undefined}
                      onClick={() => toggleSpecialization(child.value)}
                    >
                      {child.label}
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export interface BusinessDirectionPickerProps {
  values: readonly string[];
  onChange: (next: string[]) => void;
  /** 上限；不传表示不限。 */
  maxValues?: number;
  ariaLabel?: string;
}

export function BusinessDirectionPicker({
  values,
  onChange,
  maxValues,
  ariaLabel = "业务方向",
}: BusinessDirectionPickerProps) {
  const selected = new Set(values);
  return (
    <div className="dir-picker dir-picker--flat" role="group" aria-label={ariaLabel}>
      {BUSINESS_DIRECTION_OPTIONS.map((option) => {
        const isOn = selected.has(option.value);
        const blocked = maxValues !== undefined && !isOn && values.length >= maxValues;
        return (
          <button
            key={option.value}
            type="button"
            className={`dir-chip${isOn ? " dir-chip--on" : ""}`}
            aria-pressed={isOn}
            aria-label={option.label}
            disabled={blocked}
            title={blocked ? `最多只能选 ${maxValues} 个业务方向` : undefined}
            onClick={() => onChange(
              isOn ? values.filter((value) => value !== option.value) : [...values, option.value],
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

export interface CareerDirectionFilterProps {
  /** 已选职业大类（筛选用单值，最多 1 个）。 */
  directions: readonly string[];
  /** 已选职业细分（筛选用单值，最多 1 个）。 */
  specializations: readonly string[];
  onChange: (next: CareerDirectionSelection) => void;
  ariaLabel?: string;
}

/**
 * 人才库精确筛选用的职业方向控件：一级下拉 + 二级右侧弹出（级联选择器）。
 *
 * 一级是个长得像下拉框的按钮，展开后左列列出全部职业大类；点中某个大类，
 * 它自己的细分就在**同一面板的右列**弹出（右列行首「全部」表示按大类筛、不限细分）。
 * 二级选完即收起；只选了一级则按大类筛。
 */
export function CareerDirectionFilter({
  directions,
  specializations,
  onChange,
  ariaLabel = "职业方向",
}: CareerDirectionFilterProps) {
  const [open, setOpen] = useState(false);
  const [activeDirection, setActiveDirection] = useState("");
  const rootRef = useRef<HTMLDivElement | null>(null);

  const selectedDirection =
    directions[0] ?? SPECIALIZATION_PARENT[specializations[0] ?? ""] ?? "";
  const selectedSpecialization = specializations[0] ?? "";
  const activeChildren = activeDirection ? specializationsOfDirections([activeDirection]) : [];
  const triggerText = selectedSpecialization
    ? `${careerDirectionLabel(selectedDirection)} · ${specializationLabel(selectedSpecialization)}`
    : selectedDirection ? careerDirectionLabel(selectedDirection) : "全部";

  // 展开时把左列高亮对齐到当前已选大类，避免「右列显示 A、左列高亮 B」。
  useEffect(() => {
    if (open) setActiveDirection(selectedDirection);
  }, [open, selectedDirection]);

  useEffect(() => {
    if (!open) return;
    function onPointerDown(event: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  function chooseDirection(value: string) {
    onChange({ directions: value ? [value] : [], specializations: [] });
    if (!value) {
      setOpen(false);
      return;
    }
    // 选一级不收起：右列紧接着弹出，让用户接着挑细分。
    setActiveDirection(value);
  }

  function chooseSpecialization(value: string) {
    onChange({
      directions: selectedDirection ? [selectedDirection] : [],
      specializations: value ? [value] : [],
    });
    setOpen(false);
  }

  function optionClass(isOn: boolean): string {
    return `dir-cascade__option${isOn ? " is-on" : ""}`;
  }

  return (
    <div className="dir-cascade" ref={rootRef}>
      <button
        type="button"
        className="dir-cascade__trigger"
        aria-label={ariaLabel}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <span className="dir-cascade__value">{triggerText}</span>
        <span className="dir-cascade__caret" aria-hidden="true">▾</span>
      </button>
      {open && (
        <div className="dir-cascade__panel" role="group" aria-label={`${ariaLabel}选项`}>
          <div className="dir-cascade__column">
            <button type="button" aria-label="全部职业方向" aria-pressed={selectedDirection === ""}
              className={optionClass(selectedDirection === "")}
              onClick={() => chooseDirection("")}>全部</button>
            {CAREER_DIRECTION_VALUES.map((option) => {
              const hasChildren = specializationsOfDirections([option.value]).length > 0;
              return (
                <button
                  key={option.value}
                  type="button"
                  aria-label={option.label}
                  aria-pressed={activeDirection === option.value}
                  className={optionClass(activeDirection === option.value)}
                  onClick={() => chooseDirection(option.value)}
                >
                  <span>{option.label}</span>
                  {hasChildren && <span aria-hidden="true" className="dir-cascade__more">›</span>}
                </button>
              );
            })}
          </div>
          {activeChildren.length > 0 && (
            <div className="dir-cascade__column dir-cascade__column--sub">
              <button type="button"
                aria-label={`${careerDirectionLabel(activeDirection)}（不限细分）`}
                aria-pressed={selectedDirection === activeDirection && selectedSpecialization === ""}
                className={optionClass(selectedDirection === activeDirection && selectedSpecialization === "")}
                onClick={() => chooseSpecialization("")}>全部</button>
              {activeChildren.map((child) => (
                <button
                  key={child.value}
                  type="button"
                  aria-label={child.label}
                  aria-pressed={selectedSpecialization === child.value}
                  className={optionClass(selectedSpecialization === child.value)}
                  onClick={() => chooseSpecialization(child.value)}
                >
                  {child.label}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
