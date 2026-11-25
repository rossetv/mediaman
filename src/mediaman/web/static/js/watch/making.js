/* Watch page: what the line under Recommended says while a person's picks are made, and the reel
 * that turns beside it.
 *
 *   MMWatch.making.start({ few })   the changing line: { node, end(words, then), ending(), stop() }
 *   MMWatch.making.reel()           the drawn reel
 *
 * The server tells only that picks are being made, done or failed: no step and no count. So the
 * lines are banter, and none may say how far it is, or claim work mediaman does not do.
 * Needs watch/core.js.
 */
(function () {
  'use strict';

  var core = window.MMWatch.core;
  var el = core.el;

  var BEAT_MS = 4200;
  var STILL_BEAT_MS = 12000; // under reduced motion
  var FADE_MS = 400; // past the words' fade in _watch_making.css
  var LATE_MS = 90000;
  var LATE_EVERY = 3;
  var END_MS = 2800;
  var SVG_NS = 'http://www.w3.org/2000/svg';
  var HOLES = [[12, 6.6], [17.1, 10.3], [15.2, 16.4], [8.8, 16.4], [6.9, 10.3]];

  var HOW_LONG = 'It can take a minute or two.';

  // At most 34 characters: two lines on a phone.
  var LINES = [
    // The projection room.
    'Dusting off the projector…',
    'Threading the film…',
    'Warming up the bulb…',
    'Finding the right reel…',
    'Rewinding, carefully…',
    'Splicing two good ideas…',
    'Focusing. A touch to the left…',
    'Chasing a hair off the lens…',
    'Untangling the film…',
    'Keeping the beam steady…',
    // The foyer and the seats.
    'Tearing tickets…',
    'Saving you a good seat…',
    'Sweeping the aisle…',
    'Dimming the lights…',
    'Straightening the red carpet…',
    'Changing the letters outside…',
    'Looking for row G…',
    'Plumping the seat cushions…',
    'Polishing the ticket booth…',
    'Shushing the back row…',
    'Unsticking the floor…',
    'Opening the curtains slowly…',
    'Borrowing the usher’s torch…',
    'Handing out the funny glasses…',
    // Snacks.
    'Asking the popcorn…',
    'Salting the popcorn…',
    'Choosing sweet or salty…',
    'Opening sweets very quietly…',
    'Sharing the big bucket…',
    'Losing a sweet under the seat…',
    'Blaming the nachos…',
    // The critics.
    'Arguing about the ending…',
    'Defending the sequel…',
    'Forgiving a slow first act…',
    'Sharpening the pencil…',
    'Debating the plot hole…',
    'Disagreeing politely…',
    'Giving it one more chance…',
    'Weighing laughs against tears…',
    'Writing notes in the dark…',
    'Practising the slow clap…',
    'Respecting the twist…',
    'Polishing the thick glasses…',
    'Stroking the beard…',
    // The library and the shop on the corner.
    'Reading the back of the box…',
    'Blowing dust off the top shelf…',
    'Judging a film by its poster…',
    'Alphabetising by mood…',
    'Stamping the library card…',
    'Whispering by the documentaries…',
    'Climbing the tall ladder…',
    'Finding the one misfiled film…',
    'Asking the clerk with opinions…',
    'Checking behind the counter…',
    // The set.
    'Hushing the crew…',
    'Waiting for the right light…',
    'Moving the boom out of shot…',
    'Powdering the lead’s nose…',
    'Rehearsing the big speech…',
    'Looking for the director’s chair…',
    'Hiding the wires…',
    'Snapping the clapperboard…',
    'Doing one more take…',
    'Feeding the stunt double…',
    'Rolling in the fog…',
    'Tuning the orchestra…',
    'Cueing the strings…',
    'Painting the backdrop…',
    'Combing the monster…',
    'Teaching the horse its lines…',
    'Pausing for effect…',
    'Winking at the camera…',
    'Sitting through the credits…',
    // The sofa.
    'Looking for the remote…',
    'Negotiating the sofa…',
    'Promising just one episode…',
    'Dodging spoilers…',
    'Saving the best blanket…',
    'Humming the theme tune…',
    'Asking who that actor is…'
  ];

  // For someone who has rated few titles.
  var FEW = [
    'Going by what you play…',
    'Few thumbs so far. No matter…',
    'Getting to know your taste…',
    'Taking a guess at your genre…',
    'Reading the room…'
  ];

  // Once it has taken long.
  var LATE = [
    'Still choosing. Worth it.',
    'Good taste takes its time.',
    'Still here. So is the popcorn.',
    'This one is a slow burn.',
    'Long shoot today. Still rolling.',
    'Put the kettle on. We’re on it.',
    'Taking the scenic route.'
  ];

  var firsts = {}; // the first line of each set as it was last shuffled

  /* *list* shuffled, beginning with another line than the last time, and than *not*. */
  function shuffled(name, list, not) {
    var out = list.slice();
    for (var i = out.length - 1; i > 0; i -= 1) {
      var j = Math.floor(Math.random() * (i + 1));
      var kept = out[i];
      out[i] = out[j];
      out[j] = kept;
    }
    while (out[0] === firsts[name] || out[0] === not) out.push(out.shift());
    firsts[name] = out[0];
    return out;
  }

  function shape(name, attrs) {
    var node = document.createElementNS(SVG_NS, name);
    Object.keys(attrs).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    return node;
  }

  function reel() {
    var svg = shape('svg', { class: 'making-reel', viewBox: '0 0 24 24', 'aria-hidden': 'true', focusable: 'false' });
    var turn = shape('g', { class: 'making-reel__turn' });
    turn.appendChild(shape('circle', { class: 'making-reel__rim', cx: 12, cy: 12, r: 10.6 }));
    HOLES.forEach(function (at) { turn.appendChild(shape('circle', { class: 'making-reel__hole', cx: at[0], cy: at[1], r: 2.3 })); });
    turn.appendChild(shape('circle', { class: 'making-reel__hub', cx: 12, cy: 12, r: 1.5 }));
    svg.appendChild(turn);
    return svg;
  }

  function start(opts) {
    var node = el('span', 'making');
    node.setAttribute('aria-hidden', 'true');
    var began = Date.now();
    var still = core.reduceMotion();
    var late = shuffled('late', LATE);
    var lines = shuffled('lines', LINES);
    var sinceLate = LATE_EVERY;
    var said = null; // the line shown
    var beat = null;
    var ended = false;
    var endTimer = null;
    if (opts.few) lines.unshift(shuffled('few', FEW)[0]);
    lines.splice(1, 0, HOW_LONG);

    function next() {
      if (Date.now() - began >= LATE_MS && late.length) {
        sinceLate += 1;
        if (sinceLate >= LATE_EVERY) {
          sinceLate = 0;
          return late.shift();
        }
      }
      if (!lines.length) lines = shuffled('lines', LINES, said.textContent);
      return lines.shift();
    }

    function show(words) {
      var old = said;
      if (old && still) old.remove();
      else if (old) {
        old.classList.add('is-out');
        setTimeout(function () { old.remove(); }, FADE_MS);
      }
      said = node.appendChild(el('span', 'making__words', words));
    }

    function pace() {
      clearInterval(beat);
      beat = ended || document.hidden ? null : setInterval(function () { show(next()); }, still ? STILL_BEAT_MS : BEAT_MS);
    }

    function stop() {
      ended = true;
      clearInterval(beat);
      clearTimeout(endTimer);
      endTimer = null;
      document.removeEventListener('visibilitychange', pace);
    }

    show(next());
    document.addEventListener('visibilitychange', pace);
    pace();

    return {
      node: node,
      ending: function () { return ended; },
      stop: stop,
      /* Say *words* for a moment, then call *then*; a stop before that calls nothing. */
      end: function (words, then) {
        stop();
        node.classList.add('is-end');
        show(words);
        endTimer = setTimeout(function () { if (endTimer !== null) then(); }, END_MS);
      }
    };
  }

  window.MMWatch.making = { start: start, reel: reel, sets: { lines: LINES, few: FEW, late: LATE } };
})();
