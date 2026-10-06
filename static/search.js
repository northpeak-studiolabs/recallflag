// Client-side recall search over a compact JSON index. No framework.
(function () {
  var base = window.RW_BASE || '';
  var form = document.getElementById('sf'), input = document.getElementById('q');
  var out = document.getElementById('results'), status = document.getElementById('status');
  var SRC = {F: 'FDA', D: 'FDA', M: 'FDA', C: 'CPSC', V: 'NHTSA'};
  var data = null, loading = null;
  function load() {
    if (!loading) loading = fetch(base + '/assets/search-index.json').then(function (r) { return r.json(); })
      .then(function (rows) { data = rows.map(function (r) { return {r: r, h: (r[2] + ' ' + r[3] + ' ' + r[5]).toLowerCase()}; }); return data; });
    return loading;
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]; }); }
  function run() {
    var q = input.value.trim().toLowerCase();
    if (q.length < 2) { out.innerHTML = ''; status.textContent = 'Type at least 2 characters.'; return; }
    status.textContent = 'Searching…';
    load().then(function () {
      var terms = q.split(/\s+/), hits = [];
      for (var i = 0; i < data.length && hits.length < 100; i++) {
        var h = data[i].h, ok = true;
        for (var t = 0; t < terms.length; t++) if (h.indexOf(terms[t]) < 0) { ok = false; break; }
        if (ok) hits.push(data[i].r);
      }
      status.textContent = hits.length ? (hits.length >= 100 ? 'Showing the 100 newest matches.' : hits.length + ' match' + (hits.length > 1 ? 'es' : '') + '.') : 'No recalls found. Try fewer or different words.';
      out.innerHTML = hits.map(function (r) {
        return '<li class="item"><a class="item-link" href="' + base + '/recall/' + r[0] + '/">' + esc(r[2]) + '</a><div class="item-meta"><span class="badge b-' + r[4] + '">' + SRC[r[4]] + '</span> ' + r[1] + ' · ' + esc(r[3]) + '</div></li>';
      }).join('');
    }).catch(function () { status.textContent = 'Search index failed to load.'; });
  }
  var timer;
  input.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(function () {
    history.replaceState(null, '', '?q=' + encodeURIComponent(input.value)); run(); }, 150); });
  form.addEventListener('submit', function (e) { e.preventDefault(); run(); });
  var q0 = new URLSearchParams(location.search).get('q');
  if (q0) { input.value = q0; run(); }
})();
