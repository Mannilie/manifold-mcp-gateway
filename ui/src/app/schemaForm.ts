// Renders the settings_schema subset (DECISIONS.md, Phase 3 gate 2). The server validates
// again on save; this only makes the form pleasant.

import { h } from "./dom";
import type { Schema, SchemaProperty } from "./api";

export interface FormHandle {
  element: HTMLElement;
  value(): Record<string, unknown>;
  reset(values: Record<string, unknown>): void;
}

export function schemaForm(schema: Schema, initial: Record<string, unknown>, onChange: () => void): FormHandle {
  const props = schema.properties ?? {};
  const required = new Set(schema.required ?? []);
  const getters: Record<string, () => unknown> = {};
  const setters: Record<string, (v: unknown) => void> = {};
  const element = h("div", { class: "schema-form" });
  if (Object.keys(props).length === 0) {
    element.append(h("p", { class: "muted" }, "This toolset has no settings."));
  }
  for (const [name, prop] of Object.entries(props)) {
    const field = renderField(name, prop, required.has(name), initial[name], onChange);
    getters[name] = field.get;
    setters[name] = field.set;
    element.append(field.element);
  }
  return {
    element,
    value: () => {
      const out: Record<string, unknown> = {};
      for (const [k, get] of Object.entries(getters)) {
        const v = get();
        if (v !== undefined) out[k] = v;
      }
      return out;
    },
    reset: (values) => { for (const [k, set] of Object.entries(setters)) set(values[k]); },
  };
}

interface Field { element: HTMLElement; get(): unknown; set(v: unknown): void }

function label(name: string, prop: SchemaProperty, required: boolean, control: HTMLElement): HTMLElement {
  const hints = prop["x-manifold"] ?? {};
  return h("label", { class: "field" },
    h("span", {}, prop.title ?? name, required ? " *" : ""),
    control,
    prop.description ? h("small", {}, prop.description, " ", hints.help_url ? h("a", { href: hints.help_url, target: "_blank", rel: "noopener" }, "Help") : null) : null,
  );
}

function renderField(name: string, prop: SchemaProperty, required: boolean, initial: unknown, onChange: () => void): Field {
  const hints = prop["x-manifold"] ?? {};
  const start = initial === undefined ? prop.default : initial;
  if (prop.type === "boolean") {
    const input = h("input", { type: "checkbox", checked: Boolean(start), onchange: onChange });
    const el = h("label", { class: "field inline" }, input, h("span", {}, prop.title ?? name));
    return { element: h("div", {}, el, prop.description ? h("small", { class: "muted" }, prop.description) : null),
      get: () => input.checked, set: (v) => { input.checked = Boolean(v); } };
  }
  if (prop.type === "array") {
    const editor = listEditor(Array.isArray(start) ? (start as string[]) : [], hints.placeholder ?? "", onChange);
    return { element: label(name, prop, required, editor.element), get: editor.get, set: (v) => editor.set(Array.isArray(v) ? (v as string[]) : []) };
  }
  if (prop.enum) {
    const select = h("select", { onchange: onChange }, ...prop.enum.map((v) => h("option", { value: v }, v)));
    select.value = start === undefined ? (prop.enum[0] ?? "") : String(start);
    return { element: label(name, prop, required, select), get: () => select.value, set: (v) => { select.value = v === undefined ? (prop.enum?.[0] ?? "") : String(v); } };
  }
  if (prop.type === "string") {
    const control = hints.multiline
      ? h("textarea", { placeholder: hints.placeholder ?? "", oninput: onChange })
      : h("input", { type: "text", placeholder: hints.placeholder ?? "", oninput: onChange, minlength: prop.minLength, maxlength: prop.maxLength, pattern: prop.pattern });
    control.value = start === undefined ? "" : String(start);
    return { element: label(name, prop, required, control), get: () => (control.value === "" && !required ? undefined : control.value), set: (v) => { control.value = v === undefined ? "" : String(v); } };
  }
  const input = h("input", { type: "number", step: prop.type === "integer" ? "1" : "any", min: prop.minimum, max: prop.maximum, oninput: onChange });
  input.value = start === undefined ? "" : String(start);
  return { element: label(name, prop, required, input), get: () => (input.value === "" ? undefined : Number(input.value)), set: (v) => { input.value = v === undefined ? "" : String(v); } };
}

export function listEditor(initial: string[], placeholder: string, onChange: () => void): { element: HTMLElement; get(): string[]; set(v: string[]): void } {
  let items = [...initial];
  const list = h("div", { class: "list-editor" });
  const draw = () => {
    list.replaceChildren();
    items.forEach((value, i) => {
      const input = h("input", { type: "text", value, placeholder, oninput: () => { items[i] = input.value; onChange(); } });
      list.append(h("div", { class: "item" },
        input,
        h("button", { type: "button", title: "Move up", disabled: i === 0, onclick: () => { [items[i - 1], items[i]] = [items[i]!, items[i - 1]!]; draw(); onChange(); } }, "↑"),
        h("button", { type: "button", title: "Move down", disabled: i === items.length - 1, onclick: () => { [items[i + 1], items[i]] = [items[i]!, items[i + 1]!]; draw(); onChange(); } }, "↓"),
        h("button", { type: "button", title: "Remove", onclick: () => { items.splice(i, 1); draw(); onChange(); } }, "Remove"),
      ));
    });
    list.append(h("button", { type: "button", onclick: () => { items.push(""); draw(); onChange(); const last = list.querySelector<HTMLInputElement>(".item:last-of-type input"); last?.focus(); } }, "Add"));
  };
  draw();
  return { element: list, get: () => items.map((s) => s.trim()).filter(Boolean), set: (v) => { items = [...v]; draw(); } };
}
