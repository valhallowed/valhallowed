// Shared interaction for the Valhallowed planning pages.
// details(): any element with data-k becomes a keyboard-reachable control that
// fills a detail panel. stepper(): a scenario list that lights diagram parts in order.
// Panel content is static text authored in each page, never user input.
(function () {
  function renderDetail(panel, item) {
    let html = '';
    if (item.kind) html += '<p class="eyebrow">' + item.kind + '</p>';
    html += '<h3>' + item.title + '</h3>';
    if (item.text) html += '<p>' + item.text + '</p>';
    if (item.rows) {
      html += '<dl>' + item.rows.map(function (r) {
        return '<dt>' + r[0] + '</dt><dd>' + r[1] + '</dd>';
      }).join('') + '</dl>';
    }
    panel.innerHTML = html;
  }

  function light(svg, keys) {
    svg.querySelectorAll('.lit').forEach(function (el) { el.classList.remove('lit'); });
    if (!keys || !keys.length) { svg.classList.remove('dim'); return; }
    svg.classList.add('dim');
    keys.forEach(function (k) {
      svg.querySelectorAll('[data-k="' + k + '"]').forEach(function (el) { el.classList.add('lit'); });
    });
  }

  function details(scope, data, panel, opts) {
    opts = opts || {};
    function select(k) {
      const item = data[k];
      if (!item) return;
      scope.querySelectorAll('.is-selected').forEach(function (el) { el.classList.remove('is-selected'); });
      scope.querySelectorAll('[data-k="' + k + '"]').forEach(function (el) { el.classList.add('is-selected'); });
      renderDetail(panel, item);
      if (opts.onSelect) opts.onSelect(k, item);
    }
    scope.querySelectorAll('[data-k]').forEach(function (el) {
      const k = el.getAttribute('data-k');
      if (!data[k] || el.hasAttribute('data-bound')) return;
      el.setAttribute('data-bound', '');
      el.setAttribute('tabindex', '0');
      el.setAttribute('role', 'button');
      el.setAttribute('aria-label', data[k].title.replace(/<[^>]+>/g, ''));
      el.addEventListener('click', function () { select(k); });
      el.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(k); }
      });
    });
    if (opts.first) select(opts.first);
    return select;
  }

  // cfg: { svg, seg, list, prev, next, scenarios: {key: {label, intro, steps: [{t, d, on: [keys]}]}}, onScenario }
  function stepper(cfg) {
    let current = null;
    let index = -1;
    const keys = Object.keys(cfg.scenarios);

    function paint() {
      const sc = cfg.scenarios[current];
      cfg.list.querySelectorAll('button').forEach(function (b, i) {
        if (i === index) b.setAttribute('aria-current', 'step');
        else b.removeAttribute('aria-current');
      });
      cfg.prev.disabled = index <= 0;
      cfg.next.disabled = index >= sc.steps.length - 1;
      cfg.next.textContent = index < 0 ? 'Start' : 'Next';
      if (cfg.svg) light(cfg.svg, index < 0 ? [] : sc.steps[index].on);
      if (cfg.onStep) cfg.onStep(current, index);
    }

    function choose(k) {
      current = k;
      index = -1;
      cfg.seg.querySelectorAll('button').forEach(function (b) {
        b.setAttribute('aria-pressed', String(b.getAttribute('data-sc') === k));
      });
      const sc = cfg.scenarios[k];
      cfg.list.innerHTML = '';
      sc.steps.forEach(function (st, i) {
        const li = document.createElement('li');
        const b = document.createElement('button');
        b.type = 'button';
        b.innerHTML = '<span><strong>' + st.t + '</strong><span class="d">' + st.d + '</span></span>';
        b.addEventListener('click', function () { index = i; paint(); });
        li.appendChild(b);
        cfg.list.appendChild(li);
      });
      if (cfg.onScenario) cfg.onScenario(k);
      paint();
    }

    keys.forEach(function (k) {
      const b = document.createElement('button');
      b.type = 'button';
      b.setAttribute('data-sc', k);
      b.textContent = cfg.scenarios[k].label;
      b.addEventListener('click', function () { choose(k); });
      cfg.seg.appendChild(b);
    });
    cfg.prev.addEventListener('click', function () { if (index > 0) { index -= 1; paint(); } });
    cfg.next.addEventListener('click', function () {
      const sc = cfg.scenarios[current];
      if (index < sc.steps.length - 1) { index += 1; paint(); }
    });
    choose(cfg.start || keys[0]);
  }

  window.VHD = { details: details, stepper: stepper, light: light };
})();
