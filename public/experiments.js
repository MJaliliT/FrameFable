const axes = {
  tones: ['Humorous', 'Suspenseful', 'Mystery'],
  levels: ['Beginning', 'Developing', 'Expanding', 'Advanced'],
  lengths: [250, 450, 750]
};
const criteria = {
  tone: 'How well did the story reflect the requested tone?',
  music: 'How appropriate was the background music?',
  narration: 'How appropriate was the narration?',
  length: 'How well did the story match the target length?',
  level: 'How well did the story match the target reading level?',
  identity: 'How consistently did the story preserve subject identities?',
  subject_accuracy: 'How accurately were the subjects identified?',
  box_accuracy: 'How accurately did the boxes fit the subjects?',
  bridge_needed: 'How necessary was the connecting scene?',
  bridge_helpful: 'How helpful was the connecting scene?'
};
const $ = (id) => document.getElementById(id);
const codePattern = /^[0-9a-f]{32}$/;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function button(text, callback, primary = false) {
  const control = node('button', text, primary ? 'primary' : undefined);
  control.type = 'button';
  control.addEventListener('click', callback);
  return control;
}

function card(title) {
  const element = node('section', undefined, 'card');
  if (title) element.append(node('h2', title));
  return element;
}

function saveFile(value, name) {
  const blob = value instanceof Blob ? value : new Blob([JSON.stringify(value, null, 2)],
    { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const anchor = node('a');
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

async function api(action, fields = {}, key = '') {
  const response = await fetch('/api/experiments', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(key && { 'X-OpenAI-Key': key }) },
    body: JSON.stringify({ action, ...fields })
  });
  let data;
  try { data = await response.json(); }
  catch { throw new Error('FrameFable could not reach the experiment archive (HTTP ' + response.status + ').'); }
  if (!response.ok) throw new Error(data.error || 'The experiment request failed.');
  return data;
}

function shareField(id) {
  const label = node('label', 'Voting link');
  const field = node('input');
  field.readOnly = true;
  const url = new URL(location.href);
  url.hash = 'vote=' + id;
  field.value = url.href;
  field.addEventListener('focus', () => field.select());
  label.append(field);
  return label;
}

function remember(id) {
  try {
    const list = storedBatches().filter((item) => item !== id);
    localStorage.setItem('framefable-experiments', JSON.stringify([id, ...list].slice(0, 24)));
  } catch { /* The share link still works when storage is unavailable. */ }
}

function storedBatches() {
  try {
    const list = JSON.parse(localStorage.getItem('framefable-experiments') || '[]');
    return Array.isArray(list) ? list.filter((id) => typeof id === 'string' && codePattern.test(id)) : [];
  } catch { return []; }
}

function questionKeys(variant) {
  return Object.keys(criteria).filter((key) => (
    (key !== 'music' || variant.media.includes('music.wav')) &&
    (key !== 'narration' || variant.media.includes('narration.wav')) &&
    (!key.startsWith('bridge_') || variant.bridges.length)
  ));
}

function drawSubject(source, subject) {
  const frame = node('div', undefined, 'boxed-image review-subject-image');
  const image = node('img');
  image.src = source.src;
  image.alt = source.name;
  frame.append(image);
  const { left, top, right, bottom } = subject.box ?? {};
  if ([left, top, right, bottom].every((x) => Number.isInteger(x) && x >= 0 && x <= 1000) && right > left && bottom > top) {
    const box = node('span', undefined, 'subject-outline');
    Object.assign(box.style, { left: left / 10 + '%', top: top / 10 + '%',
      width: (right - left) / 10 + '%', height: (bottom - top) / 10 + '%' });
    box.setAttribute('aria-hidden', 'true');
    frame.append(box);
  }
  return frame;
}

export function setupExperiments({ startSetup, showPanel, payload, credential }) {
  const panel = $('experiments');
  const selections = {};
  const options = $('experiment-options');
  options.append(node('h2', 'Experiment combinations'));
  const groups = node('div', undefined, 'experiment-axes');
  for (const [axis, label] of [['tones', 'Tones'], ['levels', 'Reading levels'], ['lengths', 'Target word counts']]) {
    const group = node('fieldset');
    group.append(node('legend', label));
    selections[axis] = axes[axis].map((value) => {
      const item = node('label', undefined, 'checkbox');
      const input = node('input');
      input.type = 'checkbox';
      input.checked = true;
      input.value = value;
      input.addEventListener('change', updateCount);
      item.append(input, node('span', String(value)));
      group.append(item);
      return input;
    });
    groups.append(group);
  }
  const count = node('p');
  options.append(groups, count, node('p', 'Analyze the image sequence once, then enter any names before running the batch. Experimental media and votes stay in the server archive.'));
  updateCount();

  function selected() {
    return Object.fromEntries(Object.entries(selections).map(([axis, inputs]) => [
      axis, inputs.filter(({ checked }) => checked).map(({ value }) => axis === 'lengths' ? Number(value) : value)
    ]));
  }

  function updateCount() {
    const total = Object.values(selected()).reduce((product, values) => product * values.length, 1);
    count.textContent = total + (total === 1 ? ' variant' : ' variants');
  }

  function home() {
    showPanel('Run Experiments', 'Generate controlled story variants or rate a completed batch.');
    const menu = card();
    const actions = node('div', undefined, 'experiment-menu');
    actions.append(button('New experiments', startSetup, true), button('Vote previous one', voteLanding));
    menu.append(actions);
    panel.replaceChildren(menu);
  }

  function voteLanding() {
    showPanel('Vote previous one', 'Open a completed batch from your browser or enter a shared voting link.');
    const contents = card();
    const form = node('form', undefined, 'fields');
    const label = node('label', 'Batch code or voting link');
    const input = node('input');
    input.required = true;
    label.append(input);
    const submit = node('button', 'Open batch', 'primary');
    submit.type = 'submit';
    form.append(label, submit);
    const feedback = node('p');
    feedback.setAttribute('role', 'status');
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const value = input.value.trim();
      const id = value.split('#vote=').at(-1);
      await openBatch(id, feedback);
    });
    contents.append(form, feedback);
    for (const id of storedBatches()) {
      const item = button('Open batch ' + id.slice(0, 8), () => openBatch(id, feedback));
      contents.append(item);
      void api('status', { id }).then((batch) => {
        item.textContent = new Date(batch.createdAt).toLocaleString() + ' · ' + batch.completed + '/' + batch.total + ' · ' + batch.status;
      }).catch(() => {});
    }
    panel.replaceChildren(contents);
  }

  async function openBatch(id, feedback) {
    if (!codePattern.test(id)) {
      feedback.textContent = 'Enter a valid batch code or voting link.';
      return;
    }
    feedback.textContent = 'Opening the batch…';
    try {
      const info = await api('status', { id });
      remember(id);
      if (info.status === 'running') return monitor(info);
      if (!info.completed) throw new Error(info.error || 'This batch has no generated variants.');
      review(await api('review', { id }));
    } catch (error) { feedback.textContent = error.message; }
  }

  async function monitor(info) {
    const progress = card('Generating variants');
    const status = node('p');
    status.setAttribute('role', 'status');
    const meter = node('progress');
    meter.max = info.total;
    progress.append(status, meter, shareField(info.id));
    showPanel('Experiment batch', 'The server keeps generating and saving variants if you close this page.');
    panel.replaceChildren(progress);
    while (progress.isConnected && !panel.hidden) {
      meter.value = info.completed || 0;
      status.textContent = (info.completed || 0) + ' of ' + info.total + ' variants processed';
      if (info.status && info.status !== 'running') {
        if (info.completed) {
          progress.append(button('Review and vote', () => openBatch(info.id, status), true));
        } else status.textContent = info.error || 'No variants were generated.';
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 3000));
      try { info = await api('status', { id: info.id }); }
      catch (error) { status.textContent = error.message; break; }
    }
  }

  function review(batch) {
    const variants = batch.variants.filter((variant) => !variant.error);
    if (!variants.length) {
      showPanel('Experiment results', 'No variants are available to rate.');
      const errors = card();
      for (const variant of batch.variants) errors.append(node('p', 'Variant ' + variant.index + ' · ' + variant.error));
      panel.replaceChildren(errors);
      return;
    }
    const ratings = {};
    let position = 0;
    let voter = '';
    showPanel('Vote on experiment variants', 'Rate each applicable criterion from 1 (poor) to 5 (excellent).');
    const notice = node('p');
    if (variants.length !== batch.configurations.length) {
      notice.textContent = 'Available variants: ' + variants.length + ' of ' + batch.configurations.length + '.';
    }
    const utility = card();
    utility.append(shareField(batch.id), button('Download experiment data', () => saveFile(batch, 'framefable-experiment-' + batch.id.slice(0, 8) + '.json')));
    panel.replaceChildren(utility, notice);
    const ballot = node('form', undefined, 'card experiment-ballot');
    const details = node('div');
    panel.append(ballot, details);
    renderVariant();

    function renderVariant() {
      const variant = variants[position];
      const fields = node('div', undefined, 'rating-fields');
      ballot.replaceChildren(node('h2', 'Experiment ' + (position + 1) + ' of ' + variants.length));
      const nameLabel = node('label', 'Your name');
      const name = node('input');
      name.required = true;
      name.maxLength = 100;
      name.value = voter;
      name.addEventListener('input', () => { voter = name.value; });
      nameLabel.append(name);
      ballot.append(nameLabel, fields);
      for (const key of questionKeys(variant)) {
        const group = node('fieldset');
        group.append(node('legend', criteria[key]));
        const stars = node('div', undefined, 'rating-stars');
        const answers = ratings[variant.index] ??= {};
        for (let n = 1; n <= 5; n++) {
          const label = node('label');
          const radio = node('input');
          radio.type = 'radio';
          radio.name = key;
          radio.value = n;
          radio.required = true;
          radio.checked = answers[key] === n;
          radio.addEventListener('change', () => { answers[key] = n; });
          label.append(radio, node('span', '★ ' + n));
          stars.append(label);
        }
        group.append(stars);
        fields.append(group);
      }
      const actions = node('div', undefined, 'actions');
      if (position > 0) actions.append(button('Previous', () => { position--; renderVariant(); }));
      const submit = node('button', position === variants.length - 1 ? 'Finish' : 'Next', 'primary');
      submit.type = 'submit';
      const feedback = node('p');
      feedback.setAttribute('role', 'status');
      actions.append(submit);
      ballot.append(feedback, actions);
      ballot.onsubmit = async (event) => {
        event.preventDefault();
        if (position < variants.length - 1) {
          position++;
          renderVariant();
          ballot.scrollIntoView({ block: 'start' });
          return;
        }
        submit.disabled = true;
        try {
          const report = await api('vote', { id: batch.id, voter, ratings });
          showReport(report);
        } catch (error) { feedback.textContent = error.message; submit.disabled = false; }
      };
      details.replaceChildren(variantDetails(batch, variant));
    }
  }

  function variantDetails(batch, variant) {
    const content = card(variant.title);
    const config = variant.configuration;
    content.append(node('p', [config.tone, config.readingLevel, 'Target ' + config.approximateWords + ' words',
      'Actual ' + variant.wordCount + ' words', new Date(variant.createdAt).toLocaleString()].join(' · ')));
    const modelCall = variant.calls.find(({ action }) => action === 'story');
    const tokens = modelCall?.usage;
    content.append(node('p', batch.settings.provider + ' · ' + (modelCall?.model || batch.settings.model)
      + (tokens ? ' · ' + tokens.input_tokens + ' input tokens · ' + tokens.output_tokens + ' output tokens' : ' · Token counts unavailable')));
    const root = '/api/experiments/' + batch.id + '/' + variant.index + '/';
    for (const [filename, label] of [['music.wav', 'Background music'], ['narration.wav', 'Narration']]) {
      if (!variant.media.includes(filename)) continue;
      const audio = node('audio');
      audio.controls = true;
      audio.preload = 'none';
      audio.src = root + filename;
      audio.setAttribute('aria-label', label);
      content.append(node('h3', label), audio);
    }
    const issues = Object.entries(variant.errors);
    for (const [stage, error] of issues) content.append(node('p', stage.replaceAll('_', ' ') + ' · ' + error));
    if (variant.bridges.length) {
      content.append(node('h3', 'Connecting scenes'));
      const scenes = node('ol', undefined, 'review-bridges');
      for (const [index, text] of variant.bridges.entries()) {
        const item = node('li');
        const filename = 'bridge-' + (index + 1) + '.png';
        if (variant.media.includes(filename)) {
          const image = node('img');
          image.src = root + filename;
          image.alt = text;
          image.loading = 'lazy';
          item.append(image);
        }
        item.append(node('p', text));
        scenes.append(item);
      }
      content.append(scenes);
    }
    content.append(node('h3', 'Identified subjects'));
    const grid = node('div', undefined, 'review-subjects');
    for (const subject of batch.subjects) {
      const image = batch.images.find(({ id }) => id === subject.imageId);
      if (!image) continue;
      const figure = node('figure');
      figure.append(drawSubject(image, subject), node('figcaption', subject.name + ' · ' + subject.label
        + ' · ' + (subject.nameSource === 'entered' ? 'User-entered' : 'Automatic')));
      grid.append(figure);
    }
    content.append(grid);
    if (!grid.childElementCount) content.append(node('p', 'No subjects were identified.'));
    if (variant.media.includes('story.pdf')) {
      const pdf = node('iframe', undefined, 'experiment-pdf');
      pdf.title = 'Illustrated story PDF';
      pdf.src = root + 'story.pdf';
      pdf.loading = 'lazy';
      const link = node('a', 'Open PDF separately');
      link.href = pdf.src;
      link.target = '_blank';
      link.rel = 'noopener';
      content.append(node('h3', 'Story PDF'), link, pdf);
    }
    return content;
  }

  function showReport({ reportPdf, ...report }) {
    const variantsLabel = report.variantsRated === 1 ? 'variant' : 'variants';
    showPanel('Votes saved', report.variantsRated + ' ' + variantsLabel + ' rated · ' + report.ratingCount + ' ratings · Overall mean ' + report.meanScore + '/5');
    const result = card('Average rating by criterion');
    for (const [key, summary] of Object.entries(report.criteria)) {
      const label = node('label', criteria[key]);
      const bar = node('meter');
      bar.min = 0; bar.max = 5; bar.value = summary.mean;
      label.append(bar, node('span', summary.mean + '/5 · ' + summary.count + ' ratings'));
      result.append(label);
    }
    result.append(button('Download results PDF', () => {
      const bytes = Uint8Array.from(atob(reportPdf), (character) => character.charCodeAt(0));
      saveFile(new Blob([bytes], { type: 'application/pdf' }), 'framefable-voting-results.pdf');
    }, true), button('Download ratings JSON', () => saveFile(report, 'framefable-votes.json')));
    panel.replaceChildren(result);
  }

  $('experiments-button').addEventListener('click', home);
  $('begin-experiment').addEventListener('click', async (event) => {
    const control = event.currentTarget;
    control.disabled = true;
    const status = $('status');
    status.hidden = false;
    status.textContent = 'Saving the ordered experiment inputs…';
    try {
      const batch = await api('create', { data: { ...payload(), ...selected() } }, credential());
      remember(batch.id);
      void monitor(batch);
    } catch (error) { status.textContent = error.message; }
    finally { control.disabled = false; }
  });
  const linkedId = location.hash.startsWith('#vote=') ? location.hash.slice(6) : '';
  if (codePattern.test(linkedId)) {
    const status = node('p', 'Opening the shared batch…');
    showPanel('Vote previous one', 'Opening a shared experiment batch.');
    panel.replaceChildren(status);
    void openBatch(linkedId, status);
  }
}
