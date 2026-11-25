// The parts of a form that takes a new password, as _password.html draws them, for build() of fake_dom.js.
// `old`, `fresh` and `user` are the ids of the fields it has.
'use strict';

const why = (id) => ['div', { class: 'pwd-why', 'data-room': 'This password is on the common-password list.' }, ['div', { id: id + '-why' }]];
const field = (key, id, eye) => ['div', { class: 'pwd-fld pwd-fld--' + key },
  ['input', { id }], ...(eye ? [['button', { class: 'pwd-eye', 'aria-pressed': 'false' }]] : []), why(id)];
const rule = (key, rest) => ['li', { class: 'pwd-rule', 'data-rule': key },
  ['span', { class: 'pwd-rule-w', 'data-rest': rest }, rest], ['span', { class: 'pwd-rule-n' }], ['span', { class: 'sr-only' }]];

function parts({ user, old, fresh, result, submit, cancel, words = 'Save', same = Boolean(old) }) {
  return [
    ...(user ? [field('user', user)] : []), ...(old ? [field('old', old, true)] : []), field('new', fresh, true),
    rule('length', '12 characters or more'),
    ['li', { class: 'pwd-rule', 'data-rule': 'mix' }, ['span', { class: 'pwd-rule-w' }, ['span', { class: 'pwd-kinds' },
      ...['lower', 'upper', 'digit', 'symbol', 'phrase'].map((kind) => ['i', { 'data-kind': kind }])]], ['span', { class: 'sr-only' }]],
    rule('guess', 'No username'), ...(same ? [rule('same', 'Not the one you have now')] : []), rule('common', 'Not a common one'),
    ['span', { class: 'pwd-said' }],
    ['div', { class: 'pwd-aside' }, ['div', { class: 'pwd-line' }, ['span', {}], ['span', {}, 'Signs out your other devices and your TVs.']], ['div', { class: 'pwd-msg', id: result }]],
    ...(cancel ? [['button', { type: 'button', id: cancel, 'data-cancel': '' }, 'Cancel']] : []),
    ['button', { type: 'submit', id: submit }, words],
  ];
}

// What a browser gives elements that fake_dom.js does not.
function prepare(Node) {
  if (!('lastElementChild' in Node.prototype)) {
    Object.defineProperty(Node.prototype, 'lastElementChild', { get() { return this.children[this.children.length - 1] || null; } });
  }
  Node.prototype.select = function () {};
  Node.prototype.setSelectionRange = function (from, to, direction) { this.selectionStart = from; this.selectionEnd = to; this.selectionDirection = direction || 'none'; };
}

module.exports = { parts, prepare };
