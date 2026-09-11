/* Local draft only: no network, storage, or automatic topic writes. */
(() => {
  "use strict";

  const keyOf = (value) => value.toLowerCase().replace(/ß/g, "ss").replace(/ς/g, "σ");
  // Split before normalization, like the API. Ordinary spaces belong to phrases.
  const split = (value) => {
    const seen = new Set();
    return String(value).split(/[，,、;；\r\n]/).map((item) =>
      item.normalize("NFKC").replace(/[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/g, " ").replace(/^ +| +$/g, "")
    ).filter((item) => {
      const key = keyOf(item);
      if (!item || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  };
  function element(tag, attributes = {}, text = "") {
    const result = document.createElement(tag);
    for (const [key, value] of Object.entries(attributes)) result.setAttribute(key, value);
    result.textContent = text;
    return result;
  }

  class KeywordEditor {
    constructor({ prefix, canonicalId, label }) {
      this.prefix = prefix;
      this.label = label;
      this.root = document.getElementById(`${prefix}-editor`);
      this.canonical = document.getElementById(canonicalId);
      this.list = document.getElementById(`${prefix}-chips`);
      this.count = document.getElementById(`${prefix}-count`);
      this.values = [];
      this.editing = -1;
      this.composing = false;
      this.timer = null;
      this.helper = "点标签修改，点 × 删除；最后保存当前主题。";
      this.entryLabel = element("label", { class: "keyword-editor__label", for: `${prefix}-entry` }, `添加${label}`);
      this.entry = element("textarea", { id: `${prefix}-entry`, rows: "1", "aria-describedby": `${prefix}-message`, placeholder: label === "关键词" ? "例如：银行，利率、Bank of America" : "例如：招聘、广告" });
      this.add = element("button", { id: `${prefix}-add`, type: "button", class: "button button--secondary" }, "添加");
      this.cancel = element("button", { id: `${prefix}-cancel-edit`, type: "button", class: "button button--quiet", hidden: "" }, "取消编辑");
      const row = element("div", { class: "keyword-editor__entry-row" });
      row.append(this.entry, this.add, this.cancel);
      this.message = element("p", { id: `${prefix}-message`, class: "keyword-editor__message", role: "status", "aria-live": "polite", "aria-atomic": "true" }, this.helper);
      this.details = element("details", { id: `${prefix}-bulk-toggle`, class: "keyword-editor__bulk" });
      const summary = element("summary", {}, "批量粘贴");
      const bulkLabel = element("label", { class: "field", for: `${prefix}-bulk` });
      bulkLabel.append(element("span", {}, `批量添加${label}`));
      this.bulk = element("textarea", { id: `${prefix}-bulk`, rows: "3", "aria-describedby": `${prefix}-message`, placeholder: "支持中文逗号、英文逗号、顿号、分号和换行" });
      bulkLabel.append(this.bulk);
      this.bulkAdd = element("button", { id: `${prefix}-bulk-add`, type: "button", class: "button button--secondary" }, "添加到列表");
      this.details.append(summary, bulkLabel, this.bulkAdd);
      this.root.append(this.entryLabel, row, this.message, this.details);
      this.add.addEventListener("click", () => this.commit());
      this.cancel.addEventListener("click", () => { this.resetEntry(); this.feedback(); this.changed(); this.entry.focus(); });
      this.bulkAdd.addEventListener("click", () => this.commitAll());
      this.entry.addEventListener("compositionstart", () => { this.composing = true; });
      this.entry.addEventListener("compositionend", () => { this.composing = false; });
      this.entry.addEventListener("keydown", (event) => {
        if (event.isComposing || event.keyCode === 229 || this.composing) return;
        if (event.key === "Enter" && !event.isComposing && event.keyCode !== 229) {
          event.preventDefault();
          this.commit();
        } else if (event.key === "Escape" && this.editing >= 0) {
          event.preventDefault();
          event.stopPropagation();
          this.cancel.click();
        }
      });
      [this.entry, this.bulk].forEach((input) => input.addEventListener("input", () => this.feedback()));
      this.render();
    }

    setValues(values) {
      // Existing values are already API-normalized. Do not split stored phrases again.
      this.values = [...values];
      this.bulk.value = "";
      this.details.open = false;
      this.resetEntry();
      this.feedback();
      this.render();
    }

    snapshot() {
      return { values: this.values, entry: this.entry.value, bulk: this.bulk.value, editing: this.editing };
    }

    feedback(message = this.helper, state = "default", input = this.entry) {
      window.clearTimeout(this.timer);
      this.root.dataset.state = state;
      this.root.setAttribute("aria-busy", String(state === "loading"));
      this.message.replaceChildren(document.createTextNode(message));
      this.entry.removeAttribute("aria-invalid");
      this.bulk.removeAttribute("aria-invalid");
      if (state === "error") {
        input.setAttribute("aria-invalid", "true");
        if (input === this.bulk) this.details.open = true;
        input.focus();
      } else if (state === "success") {
        this.timer = window.setTimeout(() => this.feedback(), 5000);
      }
    }

    changed() {
      this.render();
      this.root.dispatchEvent(new Event("keyword-change", { bubbles: true }));
    }

    resetEntry() {
      this.editing = -1;
      this.entry.value = "";
      this.entryLabel.textContent = `添加${this.label}`;
      this.add.textContent = "添加";
      this.cancel.hidden = true;
    }

    commit() {
      const additions = split(this.entry.value);
      if (!additions.length && this.editing < 0) return true;
      if (this.editing >= 0 && additions.length !== 1) {
        this.feedback("一次修改一个词；不能留空，删除请点标签旁的 ×。", "error");
        return false;
      }
      const next = [...this.values];
      if (this.editing >= 0) {
        if (next.some((word, index) => index !== this.editing && keyOf(word) === keyOf(additions[0]))) {
          this.feedback("列表里已有这个词，请换一个；原词仍保留。", "error");
          return false;
        }
        next[this.editing] = additions[0];
      } else {
        for (const word of additions) if (!next.some((existing) => keyOf(existing) === keyOf(word))) next.push(word);
      }
      if (next.length > 40) {
        this.feedback(`每组最多 40 个${this.label}，本次共 ${next.length} 个；请减少后再添加。`, "error");
        return false;
      }
      const changed = JSON.stringify(next) !== JSON.stringify(this.values);
      this.values = next;
      this.resetEntry();
      this.feedback(changed ? "已更新草稿；保存当前主题后生效。" : "这些词已在列表中，没有重复添加。", "success");
      this.changed();
      this.entry.focus();
      return true;
    }

    commitAll() {
      if (!this.commit()) return false;
      if (!this.bulk.value.trim()) return true;
      const next = [...this.values];
      for (const word of split(this.bulk.value)) if (!next.some((existing) => keyOf(existing) === keyOf(word))) next.push(word);
      if (next.length > 40) {
        this.feedback(`每组最多 40 个${this.label}，本次共 ${next.length} 个；请减少批量内容后再添加。`, "error", this.bulk);
        return false;
      }
      this.values = next;
      this.bulk.value = "";
      this.feedback("批量内容已加入草稿；保存当前主题后生效。", "success");
      this.changed();
      return true;
    }

    merge(values) {
      if (!this.commitAll()) return false;
      const next = [...this.values];
      for (const word of values) if (!next.some((existing) => keyOf(existing) === keyOf(word))) next.push(word);
      if (next.length > 40) {
        this.feedback("加入建议后会超过 40 个词，请先删除一些；建议仍保留。", "error");
        return false;
      }
      this.values = next;
      this.feedback("建议已加入草稿；保存当前主题后生效。", "success");
      this.changed();
      return true;
    }

    edit(index) {
      if (!this.commit()) return;
      this.editing = index;
      this.entry.value = this.values[index];
      this.entryLabel.textContent = `修改${this.label}`;
      this.add.textContent = "更新";
      this.cancel.hidden = false;
      this.feedback("修改后点更新；取消编辑会保留原词。", "default");
      this.render();
      this.entry.focus();
      this.entry.select();
    }

    remove(index) {
      const word = this.values[index];
      this.values.splice(index, 1);
      if (this.editing === index) this.resetEntry();
      else if (this.editing > index) this.editing -= 1;
      this.feedback(`已从草稿移除“${word}”。`);
      const undo = element("button", { type: "button", class: "button button--quiet" }, "撤销");
      undo.addEventListener("click", () => {
        if (this.values.length >= 40 || this.values.some((value) => keyOf(value) === keyOf(word))) return;
        this.values.splice(Math.min(index, this.values.length), 0, word);
        if (this.editing >= index) this.editing += 1;
        this.feedback("已恢复到草稿。", "success");
        this.changed();
        this.entry.focus();
      });
      this.message.append(undo);
      this.changed();
      this.entry.focus();
    }

    render() {
      this.canonical.value = this.values.join(", ");
      this.count.textContent = `${this.values.length} / 40`;
      this.count.dataset.state = this.values.length > 40 ? "error" : "ok";
      this.list.replaceChildren(...this.values.map((word, index) => {
        const token = element("span", { class: "keyword-token", "data-editing": String(index === this.editing) });
        const edit = element("button", { class: "keyword-token__edit", type: "button", "aria-label": `编辑${this.label}：${word}`, title: word }, word);
        const remove = element("button", { class: "keyword-token__remove", type: "button", "aria-label": `删除${this.label}：${word}` }, "×");
        edit.addEventListener("click", () => this.edit(index));
        remove.addEventListener("click", () => this.remove(index));
        token.append(edit, remove);
        return token;
      }));
      if (!this.values.length) this.list.append(element("p", { class: "keyword-editor__empty" }, this.label === "关键词" ? "还没有关键词，在下方添加第一个。" : "没有排除词；需要屏蔽的内容可在下方添加。"));
    }
  }

  window.CockpitKeywordEditor = KeywordEditor;
})();
