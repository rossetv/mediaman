// A tiny fake DOM for running the real static scripts under node: elements,
// selectors, events and a document, no layout. Other harnesses require() it.
'use strict';

function parseCompound(text) {
  const out = { tag: null, id: null, classes: [], attrs: [], not: [] };
  text = text.replace(/:not\(([^)]*)\)/g, (_all, inner) => { out.not.push(parseCompound(inner)); return ''; });
  const re = /^([a-z]+)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/gi;
  let m;
  while ((m = re.exec(text))) {
    if (m[1]) out.tag = m[1].toUpperCase();
    else if (m[2]) out.id = m[2];
    else if (m[3]) out.classes.push(m[3]);
    else out.attrs.push([m[4], m[5]]);
  }
  return out;
}

function matchesCompound(el, c) {
  if (el.nodeType !== 1) return false;
  if (c.tag && el.tagName !== c.tag) return false;
  if (c.id && el.id !== c.id) return false;
  if (!c.classes.every((k) => el.classList.contains(k))) return false;
  if (c.not.some((n) => matchesCompound(el, n))) return false;
  return c.attrs.every(([k, v]) => el.hasAttribute(k) && (v === undefined || el.getAttribute(k) === v));
}

function matchesSelector(el, sel) {
  return sel.split(',').some((one) => {
    const parts = one.trim().split(/\s+/).map(parseCompound);
    if (!matchesCompound(el, parts[parts.length - 1])) return false;
    let i = parts.length - 2;
    for (let a = el.parentNode; a && i >= 0; a = a.parentNode) {
      if (matchesCompound(a, parts[i])) i--;
    }
    return i < 0;
  });
}

class Node {
  constructor(doc, tag) {
    this.ownerDocument = doc;
    this.nodeType = tag.toLowerCase() === '#text' ? 3 : 1;
    this.tagName = tag.toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this.attrs = {};
    this.listeners = {};
    this.style = {};
    this._text = '';
    this.inert = false;
  }
  get id() { return this.attrs.id || ''; }
  set id(v) { this.attrs.id = String(v); }
  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = String(v); }
  get classList() {
    const el = this;
    const list = () => el.className.split(/\s+/).filter(Boolean);
    return {
      contains: (c) => list().includes(c),
      add: (...cs) => { el.className = [...new Set(list().concat(cs))].join(' '); },
      remove: (...cs) => { el.className = list().filter((x) => !cs.includes(x)).join(' '); },
      toggle: (c, on) => {
        const want = on === undefined ? !list().includes(c) : on;
        if (want) el.classList.add(c); else el.classList.remove(c);
        return want;
      },
    };
  }
  get tabIndex() { return this.hasAttribute('tabindex') ? Number(this.attrs.tabindex) : -1; }
  set tabIndex(v) { this.attrs.tabindex = String(v); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get textContent() { return this.nodeType === 3 ? this._text : this.childNodes.map((c) => c.textContent).join(''); }
  set textContent(t) {
    this.childNodes.forEach((c) => { c.parentNode = null; });
    this.childNodes = [];
    if (this.nodeType === 3) { this._text = String(t); return; }
    if (t !== '' && t !== null && t !== undefined) this.appendChild(this.ownerDocument.createTextNode(String(t)));
  }
  sibling(step, elementsOnly) {
    if (!this.parentNode) return null;
    const list = this.parentNode.childNodes;
    for (let i = list.indexOf(this) + step; i >= 0 && i < list.length; i += step) {
      if (!elementsOnly || list[i].nodeType === 1) return list[i];
    }
    return null;
  }
  get nextSibling() { return this.sibling(1, false); }
  get nextElementSibling() { return this.sibling(1, true); }
  get previousElementSibling() { return this.sibling(-1, true); }
  appendChild(c) { return this.insertBefore(c, null); }
  insertBefore(c, ref) {
    if (c.parentNode) c.remove();
    c.parentNode = this;
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    if (i < 0) this.childNodes.push(c); else this.childNodes.splice(i, 0, c);
    return c;
  }
  remove() {
    if (!this.parentNode) return;
    const p = this.parentNode;
    p.childNodes.splice(p.childNodes.indexOf(this), 1);
    this.parentNode = null;
  }
  get firstChild() { return this.childNodes[0] || null; }
  get firstElementChild() { return this.children[0] || null; }
  get isConnected() { return this.ownerDocument.documentElement.contains(this); }
  append(...cs) { cs.forEach((c) => this.appendChild(typeof c === 'string' ? this.ownerDocument.createTextNode(c) : c)); }
  prepend(c) { this.insertBefore(c, this.firstChild); }
  after(c) { this.parentNode.insertBefore(c, this.nextSibling); }
  toggleAttribute(k, on) { if (on) this.setAttribute(k, ''); else this.removeAttribute(k); }
  isEqualNode(n) {
    return this.tagName === n.tagName && this._text === n._text && JSON.stringify(this.attrs) === JSON.stringify(n.attrs) &&
      this.childNodes.length === n.childNodes.length && this.childNodes.every((c, i) => c.isEqualNode(n.childNodes[i]));
  }
  before(c) { this.parentNode.insertBefore(c, this); }
  replaceWith(c) { const p = this.parentNode; p.insertBefore(c, this); this.remove(); }
  contains(n) { for (let a = n; a; a = a.parentNode) if (a === this) return true; return false; }
  descendants() { return this.childNodes.flatMap((c) => (c.nodeType === 1 ? [c, ...c.descendants()] : [])); }
  matches(sel) { return matchesSelector(this, sel); }
  closest(sel) { for (let a = this; a && a.nodeType === 1; a = a.parentNode) if (a.matches(sel)) return a; return null; }
  querySelectorAll(sel) { return this.descendants().filter((e) => e.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  focus() { this.ownerDocument.activeElement = this; }
  blur() { this.ownerDocument.activeElement = this.ownerDocument.body; }
  getBoundingClientRect() { return { top: 0, bottom: 20, left: 0, right: 20, width: 20, height: 20 }; }
  getClientRects() { return this.ownerDocument.body.contains(this) ? [this.getBoundingClientRect()] : []; }
  get offsetWidth() { return 20; }
  get offsetHeight() { return 20; }
  cloneNode() {
    const copy = this.ownerDocument.createElement(this.tagName);
    copy.attrs = { ...this.attrs };
    copy._text = this._text;
    copy.nodeType = this.nodeType;
    this.childNodes.forEach((c) => copy.appendChild(c.cloneNode(true)));
    return copy;
  }
}

function makeDocument() {
  const doc = {
    listeners: {},
    createElement: (t) => new Node(doc, t),
    createElementNS: (ns, t) => new Node(doc, t),
    createTextNode: (t) => { const n = new Node(doc, '#text'); n._text = t; return n; },
    addEventListener: (t, fn) => { (doc.listeners[t] = doc.listeners[t] || []).push(fn); },
    getElementById: (id) => doc.documentElement.querySelector('#' + id),
    querySelector: (s) => doc.documentElement.querySelector(s),
    querySelectorAll: (s) => doc.documentElement.querySelectorAll(s),
  };
  doc.documentElement = new Node(doc, 'html');
  doc.body = doc.documentElement.appendChild(new Node(doc, 'body'));
  doc.activeElement = doc.body;
  doc.importNode = (n) => {
    const copy = n.cloneNode(true);
    const adopt = (x) => { x.ownerDocument = doc; x.childNodes.forEach(adopt); };
    adopt(copy);
    return copy;
  };
  return doc;
}

/* Build markup from [tag, attrs, ...children] arrays; a string is a text node. */
function build(doc, spec) {
  if (typeof spec === 'string') return doc.createTextNode(spec);
  const [tag, attrs, ...kids] = spec;
  const el = doc.createElement(tag);
  Object.keys(attrs || {}).forEach((k) => el.setAttribute(k, attrs[k]));
  kids.forEach((k) => el.appendChild(build(doc, k)));
  return el;
}

/* Bubble a click from *target* up to the document. */
function click(doc, target) {
  const e = { target, button: 0, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  for (let a = target; a; a = a.parentNode) { e.currentTarget = a; (a.listeners.click || []).forEach((fn) => fn(e)); }
  (doc.listeners.click || []).forEach((fn) => fn(e));
}

const flush = () => new Promise((r) => setImmediate(r));
async function settle() { for (let i = 0; i < 6; i++) await flush(); }

module.exports = { Node, makeDocument, build, click, flush, settle };
