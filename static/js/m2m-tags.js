(function () {
  const PILL_COLORS = ["#6b7280", "#0e8ce4", "#82b440", "#7c3aed", "#d97706"];

  class M2MTagWidget {
    constructor(root) {
      this.root = root;
      this.fieldName = root.dataset.name;
      this.placeholder = root.dataset.placeholder || "Search tags...";
      this.allTags = [];
      try {
        this.allTags = JSON.parse(root.dataset.tags || "[]");
      } catch (_err) {
        this.allTags = [];
      }
      this.selected = new Map();
      this.activeIndex = -1;
      this.build();
      this.bindEvents();
    }

    build() {
      this.root.innerHTML = `
        <div class="affeeso-m2m-tags__box" tabindex="0">
          <div class="affeeso-m2m-tags__pills"></div>
          <input type="text" class="affeeso-m2m-tags__search" autocomplete="off" />
        </div>
        <div class="affeeso-m2m-tags__dropdown" role="listbox"></div>
        <div class="affeeso-m2m-tags__hidden"></div>
      `;
      this.box = this.root.querySelector(".affeeso-m2m-tags__box");
      this.pillsEl = this.root.querySelector(".affeeso-m2m-tags__pills");
      this.searchInput = this.root.querySelector(".affeeso-m2m-tags__search");
      this.dropdown = this.root.querySelector(".affeeso-m2m-tags__dropdown");
      this.hiddenEl = this.root.querySelector(".affeeso-m2m-tags__hidden");
      this.searchInput.placeholder = this.placeholder;
      this.renderPills();
      this.syncHiddenInputs();
    }

    bindEvents() {
      this.box.addEventListener("click", () => this.searchInput.focus());
      this.searchInput.addEventListener("input", () => {
        this.activeIndex = -1;
        this.renderDropdown();
      });
      this.searchInput.addEventListener("focus", () => this.renderDropdown());
      this.searchInput.addEventListener("keydown", (e) => this.onSearchKeydown(e));
      document.addEventListener("click", (e) => {
        if (!this.root.contains(e.target)) {
          this.closeDropdown();
        }
      });
    }

    getAvailableTags() {
      return this.allTags.filter((tag) => !this.selected.has(String(tag.id)));
    }

    getFilteredTags() {
      const query = this.searchInput.value.trim().toLowerCase();
      return this.getAvailableTags().filter((tag) =>
        !query || tag.name.toLowerCase().includes(query)
      );
    }

    renderPills() {
      this.pillsEl.innerHTML = "";
      let index = 0;
      this.selected.forEach((name, id) => {
        const pill = document.createElement("span");
        pill.className = "affeeso-m2m-tags__pill";
        pill.style.background = PILL_COLORS[index % PILL_COLORS.length];
        pill.innerHTML = `
          <span class="affeeso-m2m-tags__pill-label"></span>
          <button type="button" class="affeeso-m2m-tags__pill-remove" aria-label="Remove ${name}">&times;</button>
        `;
        pill.querySelector(".affeeso-m2m-tags__pill-label").textContent = name;
        pill.querySelector(".affeeso-m2m-tags__pill-remove").addEventListener("click", (e) => {
          e.stopPropagation();
          this.removeTag(id);
        });
        this.pillsEl.appendChild(pill);
        index += 1;
      });
    }

    syncHiddenInputs() {
      this.hiddenEl.innerHTML = "";
      this.selected.forEach((_name, id) => {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = this.fieldName;
        input.value = id;
        this.hiddenEl.appendChild(input);
      });
    }

    addTag(tag) {
      this.selected.set(String(tag.id), tag.name);
      this.searchInput.value = "";
      this.activeIndex = -1;
      this.renderPills();
      this.syncHiddenInputs();
      this.renderDropdown();
      this.searchInput.focus();
    }

    removeTag(id) {
      this.selected.delete(String(id));
      this.renderPills();
      this.syncHiddenInputs();
      this.renderDropdown();
      this.searchInput.focus();
    }

    renderDropdown() {
      const filtered = this.getFilteredTags();
      this.dropdown.innerHTML = "";

      if (!this.allTags.length) {
        this.dropdown.innerHTML = `<div class="affeeso-m2m-tags__empty">No tags configured. Ask admin to add tags first.</div>`;
        this.openDropdown();
        return;
      }

      if (!filtered.length) {
        this.dropdown.innerHTML = `<div class="affeeso-m2m-tags__empty">No matching tags found.</div>`;
        this.openDropdown();
        return;
      }

      filtered.forEach((tag, index) => {
        const option = document.createElement("button");
        option.type = "button";
        option.className = "affeeso-m2m-tags__option";
        option.textContent = tag.name;
        option.dataset.index = String(index);
        if (index === this.activeIndex) {
          option.classList.add("is-active");
        }
        option.addEventListener("mousedown", (e) => e.preventDefault());
        option.addEventListener("click", () => this.addTag(tag));
        this.dropdown.appendChild(option);
      });
      this.openDropdown();
    }

    openDropdown() {
      this.dropdown.classList.add("is-open");
    }

    closeDropdown() {
      this.dropdown.classList.remove("is-open");
      this.activeIndex = -1;
    }

    onSearchKeydown(e) {
      const options = Array.from(this.dropdown.querySelectorAll(".affeeso-m2m-tags__option"));
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (!options.length) return;
        this.activeIndex = Math.min(this.activeIndex + 1, options.length - 1);
        this.highlightActiveOption(options);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        if (!options.length) return;
        this.activeIndex = Math.max(this.activeIndex - 1, 0);
        this.highlightActiveOption(options);
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        if (this.activeIndex >= 0 && options[this.activeIndex]) {
          options[this.activeIndex].click();
        } else if (options.length === 1) {
          options[0].click();
        }
        return;
      }
      if (e.key === "Backspace" && !this.searchInput.value) {
        const ids = Array.from(this.selected.keys());
        if (ids.length) {
          this.removeTag(ids[ids.length - 1]);
        }
      }
      if (e.key === "Escape") {
        this.closeDropdown();
      }
    }

    highlightActiveOption(options) {
      options.forEach((option, index) => {
        option.classList.toggle("is-active", index === this.activeIndex);
      });
      if (this.activeIndex >= 0 && options[this.activeIndex]) {
        options[this.activeIndex].scrollIntoView({ block: "nearest" });
      }
    }

    getSelectedCount() {
      return this.selected.size;
    }
  }

  function initM2MTagWidgets() {
    document.querySelectorAll(".affeeso-m2m-tags").forEach((root) => {
      if (!root._m2mWidget) {
        root._m2mWidget = new M2MTagWidget(root);
      }
    });
  }

  window.AffeeSoM2MTags = {
    init: initM2MTagWidgets,
    validateForm(form) {
      const widgets = form.querySelectorAll(".affeeso-m2m-tags");
      for (const widget of widgets) {
        const instance = widget._m2mWidget;
        const fieldName = widget.dataset.name || "";
        const label =
          fieldName === "promotion_tag_ids"
            ? "promotion tag"
            : fieldName === "target_profile_tag_ids"
              ? "target profile tag"
              : "tag";
        if (!instance || instance.getSelectedCount() === 0) {
          alert(`Please select at least one ${label}.`);
          widget.querySelector(".affeeso-m2m-tags__search")?.focus();
          return false;
        }
      }
      return true;
    },
  };

  document.addEventListener("DOMContentLoaded", initM2MTagWidgets);
})();
