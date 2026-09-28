/* Force-directed brain graph on a canvas. No dependencies.
   Forces follow d3-force's defaults: many-body repulsion, link springs weighted
   by degree, and a weak pull to the center so orphans stay in view. Filters,
   display and forces are tunable like Obsidian's graph view (GRAPH_DEFAULTS). */
"use strict";

const MAX_REPULSION_DISTANCE_SQUARED = 600 * 600;
const VELOCITY_KEEP = 0.6;
const ALPHA_DECAY = 0.0228;
const ALPHA_MIN = 0.004;
const GOLDEN_ANGLE = 2.399963;
const HUB_DEGREE = 6;
// The brain's entry point (CORTEX.md): a soft orange glow and a dark yellow label.
const ENTRY_GLOW = "rgba(255, 140, 40, 0.22)";
const ENTRY_LABEL = "#b8860b";

const GRAPH_DEFAULTS = Object.freeze({
  // Filters: which notes are drawn.
  search: "",
  orphans: true,
  local: false,
  depth: 1,
  hiddenColors: [],
  // Display.
  nodeSize: 0.6,
  linkWidth: 1,
  linkOpacity: 0.8,
  arrows: false,
  labels: false,
  textFade: 1.6,
  highlight: true,
  // Forces.
  center: 0.035,
  repel: 110,
  linkStrength: 1,
  linkDistance: 70,
});
const FILTER_KEYS = ["search", "orphans", "local", "depth", "hiddenColors"];
const FORCE_KEYS = ["center", "repel", "linkStrength", "linkDistance"];

class BrainGraph {
  constructor(canvas, { onSelect, settings } = {}) {
    this.canvas = canvas;
    this.context = canvas.getContext("2d");
    this.onSelect = onSelect || (() => {});
    this.settings = { ...GRAPH_DEFAULTS, ...settings };
    this.nodes = [];
    this.links = [];
    this.visibleNodes = [];
    this.visibleLinks = [];
    this.visibleIds = new Set();
    this.insetRight = 0;
    this.byId = new Map();
    this.neighbors = new Map();
    this.view = { x: 0, y: 0, scale: 1 };
    this.alpha = 0;
    this.hover = null;
    this.selected = null;
    this.dragNode = null;
    this.panning = null;
    this.moved = false;
    this.running = false;
    this.colors = {};
    this._bind();
    new ResizeObserver(() => this.resize()).observe(canvas);
  }

  readColors() {
    const style = getComputedStyle(document.documentElement);
    const token = (name) => style.getPropertyValue(name).trim();
    this.colors = { node: token("--node"), link: token("--link"), label: token("--label"), accent: token("--accent") };
  }

  setData(data) {
    const previous = this.byId;
    this.nodes = data.nodes.map((node, index) => {
      const old = previous.get(node.id);
      const radius = 12 * Math.sqrt(index + 1);
      return {
        ...node,
        x: old ? old.x : Math.cos(index * GOLDEN_ANGLE) * radius,
        y: old ? old.y : Math.sin(index * GOLDEN_ANGLE) * radius,
        vx: 0, vy: 0, degree: 0,
      };
    });
    this.byId = new Map(this.nodes.map((node) => [node.id, node]));
    this.links = data.links
      .map((link) => ({ source: this.byId.get(link.source), target: this.byId.get(link.target) }))
      .filter((link) => link.source && link.target);
    this.neighbors = new Map(this.nodes.map((node) => [node.id, new Set()]));
    for (const link of this.links) {
      link.source.degree++;
      link.target.degree++;
      this.neighbors.get(link.source.id).add(link.target.id);
      this.neighbors.get(link.target.id).add(link.source.id);
    }
    this._sizeNodes();
    this._refilter();
    this.readColors();
    const fresh = previous.size === 0;
    this.reheat(fresh ? 1 : 0.3);
    if (fresh) this._fitWhenSettled = true;
  }

  /* Change any settings (a partial object); refilters, reheats or redraws as needed. */
  setSettings(changes) {
    const changed = Object.keys(changes).filter((key) => JSON.stringify(changes[key]) !== JSON.stringify(this.settings[key]));
    if (!changed.length) return;
    this.settings = { ...this.settings, ...changes };
    if (changed.includes("nodeSize")) this._sizeNodes();
    if (changed.some((key) => FILTER_KEYS.includes(key))) {
      this._refilter();
      this.reheat(0.3);
      this._fitWhenSettled = true;
    } else if (changed.some((key) => FORCE_KEYS.includes(key))) {
      this.reheat(0.5);
    }
    this.draw();
  }

  _sizeNodes() {
    for (const node of this.nodes) {
      node.radius = (4 + Math.sqrt(node.degree) * 2.2) * this.settings.nodeSize;
      if (node.entry) node.radius = Math.max(node.radius * 1.5, 9 * this.settings.nodeSize); // the brain's entry point stands out
    }
  }

  /* Recompute which notes and links are drawn and simulated. */
  _refilter() {
    const settings = this.settings;
    let visible = this.nodes;
    const query = settings.search.trim().toLowerCase();
    if (query) {
      const tag = query.replace(/^#/, "");
      visible = visible.filter((node) => node.id.toLowerCase().includes(query) || node.label.toLowerCase().includes(query)
        || (node.tags || []).some((nodeTag) => nodeTag.toLowerCase().includes(tag)));
    }
    if (settings.hiddenColors.length) {
      const hidden = new Set(settings.hiddenColors);
      visible = visible.filter((node) => !hidden.has(node.color || ""));
    }
    if (settings.local && this.selected && this.byId.has(this.selected)) {
      const reach = new Set([this.selected]);
      let frontier = [this.selected];
      for (let level = 0; level < settings.depth; level++) {
        const next = [];
        for (const id of frontier) for (const other of this.neighbors.get(id)) if (!reach.has(other)) { reach.add(other); next.push(other); }
        frontier = next;
      }
      visible = visible.filter((node) => reach.has(node.id));
    }
    let ids = new Set(visible.map((node) => node.id));
    let links = this.links.filter((link) => ids.has(link.source.id) && ids.has(link.target.id));
    if (!settings.orphans) {
      const linked = new Set(links.flatMap((link) => [link.source.id, link.target.id]));
      visible = visible.filter((node) => linked.has(node.id) || node.id === this.selected);
      ids = new Set(visible.map((node) => node.id));
      links = links.filter((link) => ids.has(link.source.id) && ids.has(link.target.id));
    }
    this.visibleNodes = visible;
    this.visibleLinks = links;
    this.visibleIds = ids;
  }

  reheat(alpha = 0.5) {
    this.alpha = Math.max(this.alpha, alpha);
    if (!this.running) {
      this.running = true;
      requestAnimationFrame(() => this._frame());
    }
  }

  _frame() {
    if (this.alpha > ALPHA_MIN || this.dragNode) {
      for (let step = 0; step < 2; step++) this._tick();
      if (this._fitWhenSettled && this.alpha < 0.2) {
        this._fitWhenSettled = false;
        this.fit();
      }
      this.draw();
      requestAnimationFrame(() => this._frame());
    } else {
      this.running = false;
      this.draw();
    }
  }

  _tick() {
    const nodes = this.visibleNodes, alpha = this.alpha, count = nodes.length;
    const { repel, linkStrength, linkDistance, center } = this.settings;
    for (let i = 0; i < count; i++) {
      const first = nodes[i];
      for (let j = i + 1; j < count; j++) {
        const second = nodes[j];
        let dx = second.x - first.x, dy = second.y - first.y;
        let distanceSquared = dx * dx + dy * dy;
        if (distanceSquared > MAX_REPULSION_DISTANCE_SQUARED) continue;
        if (distanceSquared < 1) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; distanceSquared = 1; }
        const force = (-repel * alpha) / distanceSquared;
        first.vx += dx * force; first.vy += dy * force;
        second.vx -= dx * force; second.vy -= dy * force;
      }
    }
    for (const link of this.visibleLinks) {
      const source = link.source, target = link.target;
      let dx = target.x + target.vx - source.x - source.vx, dy = target.y + target.vy - source.y - source.vy;
      const distance = Math.sqrt(dx * dx + dy * dy) || 1;
      const strength = Math.min(1, linkStrength / Math.min(source.degree, target.degree));
      const pull = ((distance - linkDistance) / distance) * alpha * strength;
      dx *= pull; dy *= pull;
      const bias = source.degree / (source.degree + target.degree);
      target.vx -= dx * bias; target.vy -= dy * bias;
      source.vx += dx * (1 - bias); source.vy += dy * (1 - bias);
    }
    for (const node of nodes) {
      node.vx -= node.x * center * alpha;
      node.vy -= node.y * center * alpha;
      if (node === this.dragNode) { node.vx = node.vy = 0; continue; }
      node.vx *= VELOCITY_KEEP; node.vy *= VELOCITY_KEEP;
      node.x += node.vx; node.y += node.vy;
    }
    this.alpha += (0 - this.alpha) * ALPHA_DECAY;
  }

  resize() {
    const pixelRatio = window.devicePixelRatio || 1;
    const { clientWidth: width, clientHeight: height } = this.canvas;
    if (!width || !height) return;
    const first = !this.width;
    this.canvas.width = Math.round(width * pixelRatio);
    this.canvas.height = Math.round(height * pixelRatio);
    this.width = width; this.height = height; this.pixelRatio = pixelRatio;
    if (first) this.view = { x: width / 2, y: height / 2, scale: 1 };
    this.draw();
  }

  fit(padding = 60) {
    if (!this.visibleNodes.length || !this.width) return;
    let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
    for (const node of this.visibleNodes) {
      left = Math.min(left, node.x); top = Math.min(top, node.y);
      right = Math.max(right, node.x); bottom = Math.max(bottom, node.y);
    }
    const width = Math.max(this.width - this.insetRight, this.width / 2); // leave room for an open options panel
    const scale = Math.min((width - padding * 2) / (right - left || 1), (this.height - padding * 2) / (bottom - top || 1), 2.5);
    this.view = { scale, x: width / 2 - ((left + right) / 2) * scale, y: this.height / 2 - ((top + bottom) / 2) * scale };
    this.draw();
  }

  /* Screen pixels on the right covered by an overlay; fit() centers notes in the rest. */
  setInsetRight(pixels) {
    this.insetRight = pixels;
    this.fit();
  }

  focus(id) {
    const node = this.byId.get(id);
    if (!node) return;
    this.select(id);
    if (this.settings.local) return; // the local graph refits around the new note
    const scale = Math.max(this.view.scale, 1.4);
    this.view = { scale, x: this.width / 2 - node.x * scale, y: this.height / 2 - node.y * scale };
    this.draw();
  }

  select(id) {
    const changed = id !== this.selected;
    this.selected = id;
    if (changed && (this.settings.local || !this.settings.orphans)) {
      this._refilter();
      this.reheat(0.4);
      if (this.settings.local) this._fitWhenSettled = true;
    }
    this.draw();
  }

  draw() {
    const { context, view, pixelRatio, settings } = this;
    if (!this.width) return;
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    context.clearRect(0, 0, this.width, this.height);
    context.save();
    context.translate(view.x, view.y);
    context.scale(view.scale, view.scale);

    const focusId = this.hover || this.selected;
    const near = settings.highlight && focusId && this.visibleIds.has(focusId) ? this.neighbors.get(focusId) || new Set() : null;
    const lit = (id) => !near || id === focusId || near.has(id);

    for (const link of this.visibleLinks) {
      const active = near && (link.source.id === focusId || link.target.id === focusId);
      context.strokeStyle = active ? this.colors.accent : this.colors.link;
      context.fillStyle = context.strokeStyle;
      context.globalAlpha = active ? 1 : settings.linkOpacity * (near ? 0.25 : 1);
      context.lineWidth = ((active ? 1.8 : 1) * settings.linkWidth) / view.scale;
      context.beginPath();
      context.moveTo(link.source.x, link.source.y);
      context.lineTo(link.target.x, link.target.y);
      context.stroke();
      if (settings.arrows) this._arrow(link, (active ? 1.8 : 1) * settings.linkWidth);
    }

    for (const node of this.visibleNodes) {
      context.globalAlpha = lit(node.id) ? 1 : 0.2;
      if (node.entry) { // a soft orange glow; the fill keeps the note's own color
        context.fillStyle = ENTRY_GLOW;
        context.beginPath();
        context.arc(node.x, node.y, node.radius * 1.9, 0, Math.PI * 2);
        context.fill();
      }
      context.fillStyle = node.color || this.colors.node;
      context.beginPath();
      context.arc(node.x, node.y, node.radius, 0, Math.PI * 2);
      context.fill();
      if (node.id === this.selected) {
        context.strokeStyle = this.colors.accent;
        context.lineWidth = 2.5 / view.scale;
        context.beginPath();
        context.arc(node.x, node.y, node.radius + 3.5 / view.scale, 0, Math.PI * 2);
        context.stroke();
      }
    }

    context.font = `${12 / Math.max(view.scale, 0.6)}px system-ui, sans-serif`;
    context.textAlign = "center";
    context.textBaseline = "top";
    context.fillStyle = this.colors.label;
    for (const node of this.visibleNodes) {
      const show = node.entry || settings.labels || view.scale > settings.textFade || node.id === focusId || (near && near.has(node.id))
        || (node.degree >= HUB_DEGREE && view.scale > settings.textFade / 2);
      if (!show) continue;
      context.globalAlpha = lit(node.id) ? 0.95 : 0.15;
      context.fillStyle = node.entry ? ENTRY_LABEL : this.colors.label;
      context.fillText(node.label, node.x, node.y + (node.entry ? node.radius * 1.9 : node.radius) + 3 / view.scale);
    }
    context.restore();
    context.globalAlpha = 1;
  }

  /* An arrowhead where a link meets its target note. */
  _arrow(link, width) {
    const { context, view } = this;
    const dx = link.target.x - link.source.x, dy = link.target.y - link.source.y;
    const length = Math.hypot(dx, dy);
    if (length < link.target.radius * 2) return;
    const ux = dx / length, uy = dy / length;
    const tipX = link.target.x - ux * link.target.radius, tipY = link.target.y - uy * link.target.radius;
    const size = (4 + width * 1.5) / view.scale;
    context.beginPath();
    context.moveTo(tipX, tipY);
    context.lineTo(tipX - ux * size * 1.6 - uy * size * 0.7, tipY - uy * size * 1.6 + ux * size * 0.7);
    context.lineTo(tipX - ux * size * 1.6 + uy * size * 0.7, tipY - uy * size * 1.6 - ux * size * 0.7);
    context.closePath();
    context.fill();
  }

  _toWorld(event) {
    const bounds = this.canvas.getBoundingClientRect();
    const screenX = event.clientX - bounds.left, screenY = event.clientY - bounds.top;
    return { screenX, screenY, x: (screenX - this.view.x) / this.view.scale, y: (screenY - this.view.y) / this.view.scale };
  }

  _hit(point) {
    let best = null, bestDistance = Infinity;
    for (const node of this.visibleNodes) {
      const distance = Math.hypot(node.x - point.x, node.y - point.y);
      if (distance < node.radius + 4 / this.view.scale && distance < bestDistance) { best = node; bestDistance = distance; }
    }
    return best;
  }

  _bind() {
    const canvas = this.canvas;
    canvas.addEventListener("pointerdown", (event) => {
      canvas.setPointerCapture(event.pointerId);
      const point = this._toWorld(event);
      this.moved = false;
      this.downAt = { screenX: point.screenX, screenY: point.screenY };
      const hit = this._hit(point);
      if (hit) {
        this.dragNode = hit;
        this.reheat(0.25);
      } else {
        this.panning = { screenX: point.screenX, screenY: point.screenY, viewX: this.view.x, viewY: this.view.y };
        canvas.classList.add("dragging");
      }
    });
    canvas.addEventListener("pointermove", (event) => {
      const point = this._toWorld(event);
      if (this.downAt && Math.hypot(point.screenX - this.downAt.screenX, point.screenY - this.downAt.screenY) > 3) this.moved = true;
      if (this.dragNode) {
        this.dragNode.x = point.x; this.dragNode.y = point.y;
        this.reheat(0.15);
      } else if (this.panning) {
        this.view.x = this.panning.viewX + (point.screenX - this.panning.screenX);
        this.view.y = this.panning.viewY + (point.screenY - this.panning.screenY);
        this.draw();
      } else {
        const hit = this._hit(point);
        const id = hit ? hit.id : null;
        if (id !== this.hover) {
          this.hover = id;
          canvas.classList.toggle("pointer", !!hit);
          canvas.title = hit ? hit.id : "";
          this.draw();
        }
      }
    });
    const end = (event) => {
      if (this.dragNode && !this.moved) this.onSelect(this.dragNode.id);
      this.dragNode = null;
      this.panning = null;
      this.downAt = null;
      canvas.classList.remove("dragging");
      if (event.pointerId !== undefined && canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
    };
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", end);
    canvas.addEventListener("pointerleave", () => {
      if (this.hover && !this.dragNode) { this.hover = null; this.draw(); }
    });
    canvas.addEventListener("wheel", (event) => {
      event.preventDefault();
      const point = this._toWorld(event);
      const scale = Math.min(6, Math.max(0.15, this.view.scale * Math.exp(-event.deltaY * 0.0015)));
      this.view.x = point.screenX - point.x * scale;
      this.view.y = point.screenY - point.y * scale;
      this.view.scale = scale;
      this.draw();
    }, { passive: false });
    canvas.addEventListener("dblclick", (event) => {
      if (!this._hit(this._toWorld(event))) this.fit();
    });
    window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
      this.readColors();
      this.draw();
    });
  }
}

window.BrainGraph = BrainGraph;
window.GRAPH_DEFAULTS = GRAPH_DEFAULTS;
