/**
 * "Where did this depth come from?" - answered on the pin.
 *
 * The record travels with the chart (web_charts/<survey>/chart.json) and is
 * written by pipeline/survey_source.py. This only presents it, in the same
 * field order and with the same labels the tool uses, so what someone types is
 * what they later read on the water.
 *
 * A chart with no record says so plainly. An empty panel would suggest the
 * provenance had been checked and found to be nothing, which is not the same
 * as nobody having written it down.
 */
const SurveySource = (() => {
  const FIELDS = [
    ['surveyedBy', 'Surveyed by'],
    ['surveyedOn', 'Surveyed'],
    ['vessel', 'Vessel'],
    ['equipment', 'Equipment'],
    ['method', 'Method'],
    ['processing', 'Processing'],
    ['verticalDatum', 'Depths reduced to'],
    ['horizontalDatum', 'Positions'],
    ['soundings', 'Soundings'],
    ['accuracy', 'Accuracy'],
    ['licence', 'Licence'],
    ['attribution', 'Attribution'],
    ['url', 'More'],
    ['notes', 'Notes'],
  ];

  const NOTHING_RECORDED =
    'No survey record was written for this chart. That does not make the '
    + 'depths wrong, but nothing here says who sounded them, when, or how they '
    + 'were reduced. Treat them with the caution you would give any '
    + 'unattributed chart.';

  /** One line to credit the survey, matching survey_source.py. */
  function citation(loc) {
    const source = loc.source || {};
    const parts = [source.surveyedBy || 'Unknown surveyor'];
    if (source.surveyedOn) parts.push(`(${source.surveyedOn})`);
    parts.push(`${loc.name} bathymetric survey.`);
    if (source.processing) parts.push(`Processed with ${source.processing}.`);
    if (source.licence) parts.push(`Licence: ${source.licence}.`);
    if (source.url) parts.push(source.url);
    return parts.join(' ');
  }

  function render(loc) {
    const source = loc.source || {};
    const body = document.getElementById('sourceBody');
    body.innerHTML = '';

    if (!Object.keys(source).length) {
      const p = document.createElement('p');
      p.className = 'hint';
      p.textContent = NOTHING_RECORDED;
      body.appendChild(p);
      return false;
    }

    const shown = new Set();
    const add = (label, value) => {
      const row = document.createElement('div');
      row.className = 'sourceRow';
      const dt = document.createElement('div');
      dt.className = 'sourceLabel';
      dt.textContent = label;
      const dd = document.createElement('div');
      dd.className = 'sourceValue';
      // A link is worth following; everything else is plain text, and stays
      // text - this content comes from a chart folder, not from the app.
      if (/^https?:\/\//i.test(value)) {
        const a = document.createElement('a');
        a.href = value;
        a.textContent = value;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        dd.appendChild(a);
      } else {
        dd.textContent = value;
      }
      row.append(dt, dd);
      body.appendChild(row);
    };

    for (const [key, label] of FIELDS) {
      if (source[key]) { add(label, String(source[key])); shown.add(key); }
    }
    // Fields the tool gained after this build still get shown, unlabelled
    // rather than dropped.
    for (const key of Object.keys(source)) {
      if (!shown.has(key) && source[key]) add(key, String(source[key]));
    }
    return true;
  }

  return {
    citation,

    show(loc) {
      const dialog = document.getElementById('sourceDialog');
      document.getElementById('sourceTitle').textContent = loc.name;
      const hasRecord = render(loc);
      const copy = document.getElementById('btnCopyCitation');
      copy.hidden = !hasRecord;
      copy.onclick = async () => {
        try {
          await navigator.clipboard.writeText(citation(loc));
          copy.textContent = 'Copied';
          setTimeout(() => { copy.textContent = 'Copy citation'; }, 1500);
        } catch (e) {
          // Clipboard needs a secure context and permission; say so rather
          // than leaving the button looking broken.
          copy.textContent = 'Copy blocked';
          setTimeout(() => { copy.textContent = 'Copy citation'; }, 1500);
        }
      };
      dialog.showModal();
    },
  };
})();
