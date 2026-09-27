/* Force-directed brain graph on a canvas. No dependencies.
   Forces follow d3-force's defaults: many-body repulsion, link springs weighted
   by degree, and a weak pull to the center so orphans stay in view. */
"use strict";

const CHARGE = -110;
const MAX_REPULSION_DISTANCE_SQUARED = 600 * 600;
const LINK_DISTANCE = 70;
const CENTER_PULL = 0.035;
const VELOCITY_KEEP = 0.6;
const ALPHA_DECAY = 0.0228;
const ALPHA_MIN = 0.004;
const GOLDEN_ANGLE = 2.399963;

class BrainGraph {
  constructor(canvas, { onSelect } = {}) {
    this.canvas = canvas;
    this.context = canvas.getContext("2d");
    this.onSelect = onSelect || (() => {});
    this.nodes = [];
    this.links = [];
    this.byId = new Map();
    this.neighbors = new Map();
    this.view = { x: 0, y: 0, scale: 1 };
    this.alpha = 0;
    this.hover = null;
    this.selected = null;
    this.showLabels = false;
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
    for (const node of this.nodes) node.radius = 4 + Math.sqrt(node.degree) * 2.2;
    this.readColors();
    const fresh = previous.size === 0;
    this.reheat(fresh ? 1 : 0.3);
    if (fresh) this._fitWhenSettled = true;
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
    const nodes = this.nodes, alpha = this.alpha, count = nodes.length;
    for (let i = 0; i < count; i++) {
      const first = nodes[i];
      for (let j = i + 1; j < count; j++) {
        const second = nodes[j];
        let dx = second.x - first.x, dy = second.y - first.y;
        let distanceSquared = dx * dx + dy * dy;
        if (distanceSquared > MAX_REPULSION_DISTANCE_SQUARED) continue;
        if (distanceSquared < 1) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; distanceSquared = 1; }
        const force = (CHARGE * alpha) / distanceSquared;
        first.vx += dx * force; first.vy += dy * force;
        second.vx -= dx * force; second.vy -= dy * force;
      }
    }
    for (const link of this.links) {
      const source = link.source, target = link.target;
      let dx = target.x + target.vx - source.x - source.vx, dy = target.y + target.vy - source.y - source.vy;
      const distance = Math.sqrt(dx * dx + dy * dy) || 1;
      const strength = 1 / Math.min(source.degree, target.degree);
      const pull = ((distance - LINK_DISTANCE) / distance) * alpha * strength;
      dx *= pull; dy *= pull;
      const bias = source.degree / (source.degree + target.degree);
      target.vx -= dx * bias; target.vy -= dy * bias;
      source.vx += dx * (1 - bias); source.vy += dy * (1 - bias);
    }
    for (const node of nodes) {
      node.vx -= node.x * CENTER_PULL * alpha;
      node.vy -= node.y * CENTER_PULL * alpha;
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
    if (!this.nodes.length || !this.width) return;
    let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
    for (const node of this.nodes) {
      left = Math.min(left, node.x); top = Math.min(top, node.y);
      right = Math.max(right, node.x); bottom = Math.max(bottom, node.y);
    }
    const scale = Math.min((this.width - padding * 2) / (right - left || 1), (this.height - padding * 2) / (bottom - top || 1), 2.5);
    this.view = { scale, x: this.width / 2 - ((left + right) / 2) * scale, y: this.height / 2 - ((top + bottom) / 2) * scale };
    this.draw();
  }

  focus(id) {
    const node = this.byId.get(id);
    if (!node) return;
    this.selected = id;
    const scale = Math.max(this.view.scale, 1.4);
    this.view = { scale, x: this.width / 2 - node.x * scale, y: this.height / 2 - node.y * scale };
    this.draw();
  }

  select(id) {
    this.selected = id;
    this.draw();
  }

  draw() {
    const { context, view, pixelRatio } = this;
    if (!this.width) return;
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    context.clearRect(0, 0, this.width, this.height);
    context.save();
    context.translate(view.x, view.y);
    context.scale(view.scale, view.scale);

    const focusId = this.hover || this.selected;
    const near = focusId ? this.neighbors.get(focusId) || new Set() : null;
    const lit = (id) => !near || id === focusId || near.has(id);

    for (const link of this.links) {
      const active = near && (link.source.id === focusId || link.target.id === focusId);
      context.strokeStyle = active ? this.colors.accent : this.colors.link;
      context.globalAlpha = near && !active ? 0.25 : 1;
      context.lineWidth = (active ? 1.8 : 1) / view.scale;
      context.beginPath();
      context.moveTo(link.source.x, link.source.y);
      context.lineTo(link.target.x, link.target.y);
      context.stroke();
    }

    for (const node of this.nodes) {
      context.globalAlpha = lit(node.id) ? 1 : 0.2;
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
    for (const node of this.nodes) {
      const show = this.showLabels || view.scale > 1.6 || node.id === focusId || (near && near.has(node.id)) || node.degree >= 6;
      if (!show) continue;
      context.globalAlpha = lit(node.id) ? 0.95 : 0.15;
      context.fillText(node.label, node.x, node.y + node.radius + 3 / view.scale);
    }
    context.restore();
    context.globalAlpha = 1;
  }

  _toWorld(event) {
    const bounds = this.canvas.getBoundingClientRect();
    const screenX = event.clientX - bounds.left, screenY = event.clientY - bounds.top;
    return { screenX, screenY, x: (screenX - this.view.x) / this.view.scale, y: (screenY - this.view.y) / this.view.scale };
  }

  _hit(point) {
    let best = null, bestDistance = Infinity;
    for (const node of this.nodes) {
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
