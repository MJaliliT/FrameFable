const $ = (id) => document.getElementById(id);
const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
let savedTheme;
try { savedTheme = localStorage.getItem('framefable-theme'); } catch { /* Storage can be disabled. */ }
if (!['light', 'dark'].includes(savedTheme)) savedTheme = undefined;

function applyTheme() {
  const theme = savedTheme ?? (systemTheme.matches ? 'dark' : 'light');
  document.documentElement.dataset.theme = theme;
  const next = theme === 'dark' ? 'Light mode' : 'Dark mode';
  $('theme-toggle').textContent = next;
  $('theme-toggle').setAttribute('aria-label', 'Switch to ' + next.toLowerCase());
  document.querySelector('meta[name="theme-color"]').content = theme === 'dark' ? '#142034' : '#eef3fc';
}

$('theme-toggle').addEventListener('click', () => {
  savedTheme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem('framefable-theme', savedTheme); } catch { /* Storage can be disabled. */ }
  applyTheme();
});
systemTheme.addEventListener('change', applyTheme);
applyTheme();

const storyTimestamp = new Intl.DateTimeFormat('en-US', {
  year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short'
});
const stages = ['setup', 'characters', 'bridges', 'results'];
const headings = [
  ['Set up your story', 'Start with a sequence of images and decide how the story should sound.'],
  ['Review characters & objects', 'Recurring subjects are matched automatically. Give them story names if you like.'],
  ['Review connecting scenes', 'Choose which suggested events to use between your source images.'],
  ['Read your story', 'Read the story alongside its images or download an illustrated PDF.']
];
const readingGuides = {
  Beginning: 'Familiar words, concrete ideas and short, simple sentences.',
  Developing: 'Mostly familiar vocabulary, clear transitions and varied short sentences.',
  Expanding: 'More varied vocabulary and sentences, with context for unfamiliar words.',
  Advanced: 'Richer vocabulary, more nuanced plot and varied sentence structures.'
};
let settings = { provider: 'openai', model: 'gpt-6.1-terra', endpoint: 'http://127.0.0.1:11434', bridgeImages: false };
let apiKey = '';
let settingsBusy = false;
let step = 0;
let busy = false;
let highest = 0;
let images = [];
let nextImageId = 1;
let subjects = [];
let bridges = [];
let story;
let narrationUrl;
let narrationRequest;
const storyAudio = new Audio();
let musicContext;
let musicGain;
let musicSource;
let musicBuffer;
let musicRequest;
const MUSIC_WAIT_BEFORE_READING = 40_000;
let musicMuted = false;
let ledger = [];
let analysisCacheHits = 0;
let matchCacheHit = false;
let subjectSignature = '';
let runs = [];
try { runs = JSON.parse(localStorage.getItem('story-sequence-runs') || '[]'); } catch { /* Storage can be disabled. */ }

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(text, activate, { disabled = false, label, pressed } = {}) {
  const node = element('button', text);
  node.type = 'button';
  node.disabled = disabled;
  if (label) node.setAttribute('aria-label', label);
  if (pressed !== undefined) node.setAttribute('aria-pressed', String(pressed));
  node.addEventListener('click', activate);
  return node;
}

function imageNode(src, alt) {
  const node = element('img');
  node.src = src;
  node.alt = alt;
  return node;
}

function notify(message = '', error = false, target = 'status') {
  const node = $(target);
  node.textContent = message;
  node.hidden = !message;
  node.classList.toggle('error', error);
  if (target === 'status' && !$('analysis-overlay').hidden) {
    $('analysis-progress-text').textContent = message || 'Analyzing images…';
  }
}

function renderNavigation() {
  document.querySelectorAll('[data-step]').forEach((node) => {
    const index = Number(node.dataset.step);
    node.disabled = busy || index > highest;
    if (index === step) node.setAttribute('aria-current', 'step');
    else node.removeAttribute('aria-current');
  });
  $('analyze').disabled = !images.length;
  $('settings-button').disabled = busy;
  $('provider-badge').textContent = settings.provider === 'openai' ? 'OpenAI' : 'Ollama';
}

function show(index, focus = true) {
  if (index !== 3) {
    storyAudio.pause();
    stopSoundtrack();
  }
  step = index;
  stages.forEach((id, number) => { $(id).hidden = number !== index; });
  $('step-label').textContent = 'Step ' + (index + 1) + ' of ' + stages.length;
  [$('page-title').textContent, $('page-description').textContent] = headings[index];
  renderNavigation();
  if (focus) {
    $('page-title').tabIndex = -1;
    $('page-title').focus();
    window.scrollTo(0, 0);
  }
  if (index === 3) startSoundtrack();
}

async function work(task, { coverPanel = false } = {}) {
  if (busy) return;
  busy = true;
  if (coverPanel) {
    $('analysis-overlay').hidden = false;
    $('exports').disabled = true;
    $('theme-toggle').disabled = true;
  }
  stages.forEach((id) => { $(id).inert = true; });
  renderNavigation();
  notify();
  try { await task(); } catch (error) { notify(error.message, true); }
  finally {
    busy = false;
    if (coverPanel) {
      $('analysis-overlay').hidden = true;
      $('exports').disabled = false;
      $('theme-toggle').disabled = false;
    }
    stages.forEach((id) => { $(id).inert = false; });
    renderNavigation();
  }
}

async function request(action, data, config = settings, credential = apiKey, signal) {
  const started = performance.now();
  const tracked = ['analyze', 'localize', 'refine', 'match', 'plan', 'story', 'image'].includes(action);
  try {
    const response = await fetch('/api/run', {
      method: 'POST', signal,
      headers: { 'Content-Type': 'application/json', ...(config.provider === 'openai' && credential ? { 'X-OpenAI-Key': credential } : {}) },
      body: JSON.stringify({ action, settings: config, data })
    });
    const contentType = response.headers.get('Content-Type') || '';
    if (response.ok && contentType.startsWith('audio/') && ['narrate', 'soundtrack-result'].includes(action)) {
      return await response.blob();
    }
    if (!contentType.includes('application/json')) {
      if ([502, 503, 504, 524].includes(response.status)) {
        throw new Error('The server or reverse proxy is temporarily unavailable (HTTP ' + response.status + ').');
      }
      throw new Error('FrameFable returned an unexpected response (HTTP ' + response.status + ').');
    }
    let answer;
    try { answer = await response.json(); }
    catch { throw new Error('FrameFable returned an incomplete or invalid response (HTTP ' + response.status + ').'); }
    if (!response.ok) throw new Error(answer.error || 'FrameFable could not complete the request.');
    if (tracked) ledger.push({ ...answer.metric, status: 'completed', wall_ms: Math.round(performance.now() - started) });
    return answer.result;
  } catch (error) {
    if (tracked) ledger.push({ action, model: config.model, status: 'failed', usage: null, usd: null,
      wall_ms: Math.round(performance.now() - started) });
    if (error instanceof TypeError) throw new Error('Could not reach the local studio server. Check that python3 main.py is running.');
    throw error;
  }
}

function invalidateAfter(index) {
  highest = Math.min(highest, index);
  if (index <= 1) bridges = [];
  story = undefined;
  clearNarration();
  clearSoundtrack();
  renderNavigation();
}

function reorder(from, to) {
  if (from < 0 || to < 0 || to >= images.length || from === to || busy) return;
  images.splice(to, 0, images.splice(from, 1)[0]);
  invalidateAfter(0);
  renderImages();
}

const sequenceGrid = $('sequence');
let imageDrag;

sequenceGrid.addEventListener('pointerdown', (event) => {
  const thumbnail = event.target.closest('.tile img');
  if (!thumbnail || !event.isPrimary || event.button !== 0 || busy || images.length < 2) return;
  const tile = thumbnail.closest('.tile');
  const bounds = thumbnail.getBoundingClientRect();
  imageDrag = {
    pointer: event.pointerId, x: event.clientX, y: event.clientY, tile, thumbnail, bounds,
    target: null, preview: null
  };
  sequenceGrid.setPointerCapture(event.pointerId);
});

function updateImageDrag(event) {
  if (!imageDrag || imageDrag.pointer !== event.pointerId) return;
  if (!imageDrag.preview) {
    if (Math.hypot(event.clientX - imageDrag.x, event.clientY - imageDrag.y) < 6) return;
    const preview = imageNode(imageDrag.thumbnail.src, '');
    preview.className = 'drag-preview';
    preview.style.width = imageDrag.bounds.width + 'px';
    preview.style.height = imageDrag.bounds.height + 'px';
    imageDrag.preview = preview;
    imageDrag.tile.classList.add('is-dragging');
    document.body.append(preview);
  }
  const left = imageDrag.bounds.left + event.clientX - imageDrag.x;
  const top = imageDrag.bounds.top + event.clientY - imageDrag.y;
  imageDrag.preview.style.transform = 'translate(' + left + 'px, ' + top + 'px)';
  const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('.tile');
  const next = target?.parentElement === sequenceGrid && target !== imageDrag.tile ? target : null;
  if (next === imageDrag.target) return;
  imageDrag.target?.classList.remove('drop-target');
  imageDrag.target = next;
  next?.classList.add('drop-target');
}

function finishImageDrag(event) {
  if (!imageDrag || imageDrag.pointer !== event.pointerId) return;
  if (event.type === 'pointerup' && imageDrag.preview) updateImageDrag(event);
  const { tile, target, preview } = imageDrag;
  imageDrag = undefined;
  preview?.remove();
  tile.classList.remove('is-dragging');
  target?.classList.remove('drop-target');
  if (event.type === 'pointerup' && target) {
    const tiles = Array.from(sequenceGrid.children);
    reorder(tiles.indexOf(tile), tiles.indexOf(target));
  }
}

sequenceGrid.addEventListener('pointermove', updateImageDrag);
for (const event of ['pointerup', 'pointercancel', 'lostpointercapture']) {
  sequenceGrid.addEventListener(event, finishImageDrag);
}

function renderImages() {
  $('image-count').textContent = images.length + ' / 20';
  $('upload').disabled = images.length === 20;
  $('sequence').replaceChildren(...images.map(({ src, name }, index) => {
    const tile = element('li', undefined, 'tile');
    const caption = element('p', (index + 1) + '. ' + name);
    caption.title = name;
    const controls = element('div', undefined, 'tile-buttons');
    controls.append(button('×', () => {
      images.splice(index, 1);
      invalidateAfter(0);
      renderImages();
      ($('sequence').children[Math.min(index, images.length - 1)]?.querySelector('button') || $('upload')).focus();
    }, { label: 'Remove ' + name }));
    const thumbnail = imageNode(src, name);
    thumbnail.draggable = false;
    tile.append(thumbnail, caption, controls);
    return tile;
  }));
  renderNavigation();
}

async function prepareImage(file) {
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) throw new Error('Unsupported image type.');
  const bitmap = await createImageBitmap(file);
  try {
    const scale = Math.min(1, 1200 / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    const context = canvas.getContext('2d', { alpha: false });
    context.fillStyle = '#fff';
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    return { id: 'image-' + nextImageId++, name: file.name, src: canvas.toDataURL('image/jpeg', 0.87), analysis: new Map() };
  } finally { bitmap.close(); }
}

async function addFiles(files) {
  await work(async () => {
    const available = 20 - images.length;
    let added = 0;
    for (const file of files) {
      if (added === available) break;
      try { images.push(await prepareImage(file)); added++; }
      catch { /* Report unreadable files with the final count. */ }
    }
    if (added) invalidateAfter(0);
    renderImages();
    if (added < files.length) notify('Added ' + added + ' image(s). Some files could not be read as JPG, PNG or WebP, or exceeded the 20-image limit.', true);
  });
}

const dropzone = $('dropzone');
$('upload').addEventListener('change', (event) => {
  const files = Array.from(event.target.files);
  event.target.value = '';
  void addFiles(files);
});
dropzone.addEventListener('dragover', (event) => { event.preventDefault(); dropzone.classList.add('dragging'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragging'));
dropzone.addEventListener('drop', (event) => {
  event.preventDefault();
  dropzone.classList.remove('dragging');
  void addFiles(Array.from(event.dataTransfer.files));
});

const modelKey = () => JSON.stringify([settings.provider, settings.model, settings.provider === 'ollama' ? settings.endpoint : '']);
const preferences = () => ({
  tone: $('tone').value, readingLevel: $('level').value,
  readingGuidance: readingGuides[$('level').value], approximateWords: Number($('length').value)
});

function buildSubjects(observations, matches) {
  const previous = new Map(subjects.map((subject) => [subject.id, subject]));
  const next = observations.flatMap((observation) => observation.subjects).map((subject) => ({
    ...subject, group: subject.id, name: ''
  }));
  const byId = new Map(next.map((subject) => [subject.id, subject]));
  const used = new Set();
  for (const match of matches) {
    const members = [...new Set(match.occurrences)].map((id) => byId.get(id)).filter(Boolean);
    if (members.length < 2 || new Set(members.map(({ imageId }) => imageId)).size !== members.length
        || members.some(({ id }) => used.has(id))) continue;
    for (const subject of members) {
      subject.group = members[0].id;
      used.add(subject.id);
    }
  }
  for (const subject of next) {
    const prior = previous.get(subject.id);
    if (prior?.name) subject.name = prior.name;
  }
  // A changed suggestion can temporarily leave a group with different stored names.
  const names = new Map();
  for (const subject of next) {
    if (subject.name && !names.has(subject.group)) names.set(subject.group, subject.name);
  }
  for (const subject of next) subject.name = names.get(subject.group) || '';
  return next;
}

function observations() {
  return images.map((image, index) => {
    const { setting, action, subjects: visualSubjects } = image.analysis.get(modelKey());
    return { scene: index + 1, setting, action,
      subjects: visualSubjects.map((subject, subjectIndex) => ({
        ...subject, id: image.id + ':' + subjectIndex, imageId: image.id
      }))
    };
  });
}

function usableBox(box) {
  if (!box) return false;
  const { left, top, right, bottom } = box;
  return [left, top, right, bottom].every((value) => Number.isInteger(value) && value >= 0 && value <= 1000)
    && right > left && bottom > top;
}

const BOX_TILE_SIZE = 400;

function refinedImageBox(box, crop, imageWidth, imageHeight) {
  if (!usableBox(box)) return null;
  const factor = BOX_TILE_SIZE / 1000;
  const edges = {
    left: (box.left * factor - crop.offsetX) / crop.scale + crop.x,
    top: (box.top * factor - crop.offsetY) / crop.scale + crop.y,
    right: (box.right * factor - crop.offsetX) / crop.scale + crop.x,
    bottom: (box.bottom * factor - crop.offsetY) / crop.scale + crop.y
  };
  // Reject boxes that include tile padding or touch a crop edge inside the source image.
  const margin = 3 / crop.scale;
  if (edges.left < crop.x - margin || edges.top < crop.y - margin
      || edges.right > crop.x + crop.width + margin || edges.bottom > crop.y + crop.height + margin
      || (crop.x > 0 && edges.left <= crop.x + margin)
      || (crop.y > 0 && edges.top <= crop.y + margin)
      || (crop.x + crop.width < imageWidth && edges.right >= crop.x + crop.width - margin)
      || (crop.y + crop.height < imageHeight && edges.bottom >= crop.y + crop.height - margin)) return null;

  const normalize = (value, extent) => Math.max(0, Math.min(1000, Math.round(value / extent * 1000)));
  return {
    left: normalize(edges.left, imageWidth), top: normalize(edges.top, imageHeight),
    right: normalize(edges.right, imageWidth), bottom: normalize(edges.bottom, imageHeight)
  };
}

async function requestWithDeadline(action, data, timeLimit, timeoutMessage) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeLimit);
  try {
    return await request(action, data, settings, apiKey, controller.signal);
  } catch (error) {
    if (controller.signal.aborted) throw new Error(timeoutMessage);
    throw error;
  } finally { clearTimeout(timer); }
}

async function locateSubjectBoxes(src, subjects) {
  const candidates = subjects.filter(({ label }) => label.trim()).slice(0, 6);
  if (!candidates.length) return '';
  const result = await requestWithDeadline('localize', {
    image: src,
    candidates: candidates.map(({ label, box }) => ({ label: label.trim().slice(0, 200), hint: box }))
  }, 35_000, 'Grounding DINO did not respond within 35 seconds.');
  if (!result.available) return result.reason || 'Grounding DINO was unavailable.';
  subjects.forEach((subject) => {
    subject.box = null;
    subject.boxSource = 'detector';
  });
  candidates.forEach((subject, index) => {
    const detection = result.detections[index];
    if (usableBox(detection?.box)) subject.box = detection.box;
  });
  return '';
}

async function refineSubjectBoxes(src, subjects, timeLimit) {
  const candidates = subjects.filter(({ box }) => usableBox(box)).slice(0, 6);
  if (!candidates.length) return;
  const source = imageNode(src, '');
  await source.decode();
  const { naturalWidth: imageWidth, naturalHeight: imageHeight } = source;
  const size = BOX_TILE_SIZE;
  const columns = Math.min(candidates.length, 2);
  const canvas = document.createElement('canvas');
  canvas.width = columns * size;
  canvas.height = Math.ceil(candidates.length / columns) * size;
  const context = canvas.getContext('2d');
  context.fillStyle = '#111';
  context.fillRect(0, 0, canvas.width, canvas.height);
  const crops = candidates.map(({ box }, index) => {
    const paddingX = Math.max(45, (box.right - box.left) * 0.5);
    const paddingY = Math.max(45, (box.bottom - box.top) * 0.5);
    const x = Math.floor(Math.max(0, box.left - paddingX) * imageWidth / 1000);
    const y = Math.floor(Math.max(0, box.top - paddingY) * imageHeight / 1000);
    const width = Math.ceil(Math.min(1000, box.right + paddingX) * imageWidth / 1000) - x;
    const height = Math.ceil(Math.min(1000, box.bottom + paddingY) * imageHeight / 1000) - y;
    const scale = Math.min(size / width, size / height);
    const offsetX = (size - width * scale) / 2;
    const offsetY = (size - height * scale) / 2;
    const tileX = index % columns * size;
    const tileY = Math.floor(index / columns) * size;
    context.drawImage(source, x, y, width, height, tileX + offsetX, tileY + offsetY, width * scale, height * scale);
    context.strokeStyle = '#fff';
    context.strokeRect(tileX + 0.5, tileY + 0.5, size - 1, size - 1);
    context.fillStyle = '#111';
    context.fillRect(tileX + 2, tileY + 2, 29, 29);
    context.fillStyle = '#fff';
    context.font = 'bold 22px sans-serif';
    context.fillText(String(index + 1), tileX + 9, tileY + 24);
    return { x, y, width, height, scale, offsetX, offsetY };
  });
  const result = await requestWithDeadline('refine', {
    image: canvas.toDataURL('image/jpeg', 0.9),
    candidates: candidates.map(({ label, detail }, index) => ({ tile: index + 1, label, detail }))
  }, timeLimit, 'The fallback refinement took too long.');
  for (const [index, subject] of candidates.entries()) {
    const matches = result.boxes.filter(({ tile }) => tile === index + 1);
    if (matches.length !== 1) continue;
    const box = refinedImageBox(matches[0].box, crops[index], imageWidth, imageHeight);
    if (!usableBox(box)) continue;
    const previous = subject.box;
    const overlapX = Math.min(box.right, previous.right) - Math.max(box.left, previous.left);
    const overlapY = Math.min(box.bottom, previous.bottom) - Math.max(box.top, previous.top);
    if (overlapX > 0 && overlapY > 0) subject.box = box;
  }
}

function boxedSubjectImage(image, subject) {
  const frame = element('span', undefined, 'boxed-image');
  frame.append(imageNode(image.src, image.name));
  const { left, top, right, bottom } = subject.box ?? {};
  if (usableBox(subject.box)) {
    const outline = element('span', undefined, 'subject-outline');
    outline.setAttribute('aria-hidden', 'true');
    outline.style.left = left / 10 + '%';
    outline.style.top = top / 10 + '%';
    outline.style.width = (right - left) / 10 + '%';
    outline.style.height = (bottom - top) / 10 + '%';
    frame.append(outline);
  }
  return frame;
}

function subjectPreviewButton(image, subject) {
  const preview = button('', () => {
    preview.querySelector('.subject-hover-preview')?.remove();
    $('subject-preview-title').textContent = subject.label;
    const frame = boxedSubjectImage(image, subject);
    $('subject-preview-image').replaceChildren(frame);
    $('subject-preview-note').textContent = frame.querySelector('.subject-outline')
      ? (subject.boxSource === 'detector' ? 'Located with Grounding DINO.' : 'Estimated location from the vision model.')
      : (subject.boxSource === 'detector' ? 'Grounding DINO did not find a confident match for this subject.'
        : 'The vision model did not return a usable bounding box for this subject.');
    $('subject-preview').showModal();
  }, { label: 'View ' + subject.label + ' in ' + image.name });
  preview.className = 'subject-preview-button';
  const thumbnail = boxedSubjectImage(image, subject);
  preview.append(thumbnail);
  preview.addEventListener('pointerenter', (event) => {
    if (event.pointerType !== 'mouse') return;
    const { naturalWidth, naturalHeight } = thumbnail.querySelector('img');
    if (!naturalWidth || !naturalHeight) return;

    const viewportWidth = document.documentElement.clientWidth;
    const viewportHeight = document.documentElement.clientHeight;
    const anchor = preview.getBoundingClientRect();
    const scale = Math.min(420 / naturalWidth, 300 / naturalHeight,
      (viewportWidth - 32) / naturalWidth, (viewportHeight - 32) / naturalHeight);
    const width = Math.round(naturalWidth * scale);
    const height = Math.round(naturalHeight * scale);
    const side = anchor.right + width + 22 <= viewportWidth ? anchor.right + 10 : anchor.left - width - 10;
    const left = Math.max(8, Math.min(side, viewportWidth - width - 8));
    const top = Math.max(8, Math.min(anchor.top, viewportHeight - height - 8));

    const hover = boxedSubjectImage(image, subject);
    hover.classList.add('subject-hover-preview');
    hover.setAttribute('aria-hidden', 'true');
    hover.style.width = width + 'px';
    hover.style.left = left + 'px';
    hover.style.top = top + 'px';
    preview.append(hover);
  });
  preview.addEventListener('pointerleave', () => preview.querySelector('.subject-hover-preview')?.remove());
  return preview;
}

function renderSubjects() {
  const card = element('section', undefined, 'card');
  card.append(element('h2', 'Identified subjects'));
  if (!subjects.length) card.append(element('p', 'No prominent subjects were identified. You can continue with the scene descriptions.'));
  const inputs = new Map();
  for (const subject of subjects) {
    const row = element('div', undefined, 'subject');
    const imageIndex = images.findIndex(({ id }) => id === subject.imageId);
    const description = element('div');
    description.append(element('h3', subject.label), element('p', 'Image ' + (imageIndex + 1) + ' · ' + subject.detail));
    const nameLabel = element('label', 'Story name');
    const name = element('input');
    name.value = subject.name;
    name.placeholder = 'Automatic';
    name.maxLength = 60;
    inputs.set(subject.id, name);
    name.addEventListener('input', () => {
      for (const member of subjects.filter(({ group }) => group === subject.group)) {
        member.name = name.value;
        if (member !== subject) inputs.get(member.id).value = name.value;
      }
      invalidateAfter(1);
    });
    nameLabel.append(name);
    row.append(subjectPreviewButton(images[imageIndex], subject), description, nameLabel);
    card.append(row);
  }
  $('subject-list').replaceChildren(card);
}

function namedScenes() {
  const membership = new Map(subjects.map(({ id, group, name }) => [id, { subjectId: group, name: name.trim() || null }]));
  return observations().map((observation) => ({
    ...observation, subjects: observation.subjects.map(({ id, imageId, box, boxSource, ...description }) => ({ ...description, ...membership.get(id) }))
  }));
}

$('analyze').addEventListener('click', () => work(async () => {
  const key = modelKey();
  const signature = JSON.stringify([key, images.map(({ id }) => id)]);
  analysisCacheHits = images.filter((image) => image.analysis.has(key)).length;
  matchCacheHit = subjectSignature === signature;
  let detectorIssue = '';
  let refinementBudget = 15_000;
  for (const [index, image] of images.entries()) {
    if (image.analysis.has(key)) continue;
    notify('Analyzing image ' + (index + 1) + ' of ' + images.length + '…');
    const result = await request('analyze', { image: image.src });
    if (!detectorIssue && result.subjects.length) {
      notify('Locating subjects with Grounding DINO in image ' + (index + 1) + ' of ' + images.length + '…');
      const started = performance.now();
      try { detectorIssue = await locateSubjectBoxes(image.src, result.subjects); }
      catch (error) { detectorIssue = error.message; }
      if (detectorIssue && performance.now() - started >= 15_000) refinementBudget = 0;
    }
    if (detectorIssue && refinementBudget > 0 && result.subjects.some(({ box }) => usableBox(box))) {
      notify('Refining bounding boxes for image ' + (index + 1) + ' of ' + images.length + '…');
      const started = performance.now();
      try { await refineSubjectBoxes(image.src, result.subjects, refinementBudget); }
      catch { /* Keep initial boxes if the optional refinement fails. */ }
      finally { refinementBudget -= performance.now() - started; }
    }
    image.analysis.set(key, result);
  }
  if (!matchCacheHit) {
    const source = observations();
    notify('Checking for recurring characters and objects…');
    const matchInput = source.map((scene) => ({
      ...scene, subjects: scene.subjects.map(({ box, boxSource, ...subject }) => subject)
    }));
    const matches = images.length > 1 ? (await request('match', matchInput)).groups : [];
    subjects = buildSubjects(source, matches);
    subjectSignature = signature;
  }
  highest = Math.max(highest, 1);
  renderSubjects();
  notify(detectorIssue ? detectorIssue + ' The affected boxes are estimates from the selected vision model.' : '');
  show(1);
}, { coverPanel: true }));

$('plan').addEventListener('click', () => work(async () => {
  notify('Looking for gaps between scenes…');
  const scenes = namedScenes();
  const proposals = scenes.length > 1 ? (await request('plan', { scenes, ...preferences() })).bridges : [];
  const positions = new Set();
  bridges = proposals.filter(({ after, description }) => {
    if (after < 1 || after >= scenes.length || !description.trim() || positions.has(after)) return false;
    positions.add(after);
    return true;
  }).slice(0, 3).sort((a, b) => a.after - b.after).map((proposal) => ({ ...proposal, accepted: false, preview: '', inserted: false }));
  highest = 2;
  renderBridges();
  notify();
  show(2);
}));

function renderBridgeSequence() {
  $('bridge-sequence-card').hidden = !bridges.length;
  const illustrations = new Map(bridges
    .filter(({ accepted, inserted, preview }) => accepted && inserted && preview)
    .map(({ after, preview }) => [after, preview]));
  const tile = (src, alt, caption, illustration = false) => {
    const item = element('li', undefined, illustration ? 'bridge-sequence-illustration' : undefined);
    item.append(imageNode(src, alt), element('span', caption));
    return item;
  };
  $('bridge-sequence').replaceChildren(...images.flatMap((source, index) => {
    const position = index + 1;
    const items = [tile(source.src, source.name, 'Image ' + position)];
    const illustration = illustrations.get(position);
    if (illustration) {
      const caption = 'Illustration between images ' + position + ' and ' + (position + 1);
      items.push(tile(illustration, caption, 'Bridge illustration', true));
    }
    return items;
  }));
}

function renderBridges() {
  renderBridgeSequence();
  const cards = bridges.map((bridge) => {
    const card = element('section', undefined, 'card');
    card.append(element('h2', 'Between images ' + bridge.after + ' and ' + (bridge.after + 1)),
      element('p', bridge.description));
    const controls = element('div', undefined, 'bridge-controls');
    const accept = button('Use bridge', () => {
      bridge.accepted = true;
      invalidateAfter(2);
      renderBridges();
    }, { pressed: bridge.accepted });
    const skip = button('Skip', () => {
      bridge.accepted = false;
      bridge.inserted = false;
      invalidateAfter(2);
      renderBridges();
    }, { pressed: !bridge.accepted });
    controls.append(accept, skip);
    if (bridge.accepted && settings.provider === 'openai' && settings.bridgeImages && !bridge.preview) {
      controls.append(button('Generate illustration', () => work(async () => {
        notify('Generating the bridge illustration…');
        bridge.preview = await request('image', { event: bridge.description,
          adjacentScenes: namedScenes().slice(bridge.after - 1, bridge.after + 1) });
        bridge.inserted = true;
        invalidateAfter(2);
        notify('Illustration added to the image sequence and will appear in the story.');
        renderBridges();
      })));
    }
    if (bridge.accepted && bridge.preview) {
      if (!bridge.inserted) {
        const preview = imageNode(bridge.preview, 'Illustration preview for the connecting scene');
        preview.className = 'bridge-preview';
        card.append(preview);
      }
      const insert = button(bridge.inserted ? 'Remove illustration' : 'Insert illustration', () => {
        bridge.inserted = !bridge.inserted;
        invalidateAfter(2);
        renderBridges();
        $('bridge-illustration-' + bridge.after).focus();
        notify(bridge.inserted ? 'Illustration added between images ' + bridge.after + ' and ' + (bridge.after + 1) + '.'
          : 'Illustration removed from the image sequence.');
      }, { pressed: bridge.inserted });
      insert.id = 'bridge-illustration-' + bridge.after;
      controls.append(insert);
    }
    card.append(controls);
    return card;
  });
  if (!cards.length) {
    const empty = element('section', undefined, 'card');
    empty.append(element('h2', 'No extra scenes suggested'), element('p', 'You can continue to the story using your current sequence.'));
    cards.push(empty);
  }
  $('bridge-list').replaceChildren(...cards);
}

function clearNarration() {
  narrationRequest?.abort();
  narrationRequest = undefined;
  storyAudio.pause();
  if (narrationUrl) {
    storyAudio.removeAttribute('src');
    storyAudio.load();
    URL.revokeObjectURL(narrationUrl);
    narrationUrl = undefined;
  }
}

function syncNarrationButton() {
  const control = $('story-play');
  if (!control) return;
  control.disabled = Boolean(narrationRequest);
  control.textContent = narrationRequest ? 'Preparing audio…' : storyAudio.paused || storyAudio.ended ? 'Play' : 'Pause';
}

for (const event of ['play', 'pause', 'ended']) storyAudio.addEventListener(event, syncNarrationButton);

function storyText(currentStory) {
  return [currentStory.title, ...currentStory.scenes.map(({ text }) => text)].join('\n\n');
}

async function toggleNarration() {
  if (!story || story.provider !== 'openai' || narrationRequest) return;
  if (!storyAudio.paused && !storyAudio.ended) {
    storyAudio.pause();
    return;
  }
  const current = story;
  const status = $('narration-status');
  status.textContent = '';
  try {
    if (!narrationUrl) {
      const controller = new AbortController();
      narrationRequest = controller;
      syncNarrationButton();
      try {
        const audio = await request('narrate', { text: storyText(current), tone: current.preferences.tone },
          settings, apiKey, controller.signal);
        if (controller.signal.aborted || story !== current) return;
        if (!audio.size || !audio.type.startsWith('audio/')) throw new Error('No playable narration was returned.');
        narrationUrl = URL.createObjectURL(audio);
        storyAudio.src = narrationUrl;
      } finally {
        if (narrationRequest === controller) narrationRequest = undefined;
      }
    }
    if (step === 3 && story === current) {
      if (storyAudio.ended) storyAudio.currentTime = 0;
      await storyAudio.play();
    }
  } catch (error) {
    if (story !== current || error.name === 'AbortError') return;
    if (!storyAudio.error) {
      status.textContent = error.name === 'NotAllowedError' ? 'Audio is ready. Click Play again to listen.' : error.message;
    } else {
      clearNarration();
      status.textContent = 'The browser could not play this narration. Try again.';
    }
  } finally { syncNarrationButton(); }
}

function unlockSoundtrack() {
  if (!musicContext) {
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!AudioContext) return;
    musicContext = new AudioContext();
    musicGain = musicContext.createGain();
    musicGain.connect(musicContext.destination);
  }
  if (musicContext.state === 'suspended') void musicContext.resume().catch(() => {});
}

function updateSoundtrackVolume() {
  if (!musicGain) return;
  const volume = musicMuted ? 0 : storyAudio.paused || storyAudio.ended ? 0.20 : 0.06;
  musicGain.gain.setTargetAtTime(volume, musicContext.currentTime, 0.04);
}

for (const event of ['play', 'pause', 'ended']) {
  storyAudio.addEventListener(event, updateSoundtrackVolume);
}

function startSoundtrack() {
  if (!musicBuffer || musicSource) return;
  musicSource = musicContext.createBufferSource();
  musicSource.buffer = musicBuffer;
  musicSource.loop = true;
  musicSource.connect(musicGain);
  musicGain.gain.setValueAtTime(0, musicContext.currentTime);
  updateSoundtrackVolume();
  musicSource.start();
}

function stopSoundtrack() {
  if (!musicSource) return;
  musicSource.stop();
  musicSource.disconnect();
  musicSource = undefined;
}

function clearSoundtrack() {
  musicRequest?.abort();
  musicRequest = undefined;
  stopSoundtrack();
  musicBuffer = undefined;
}

async function prepareSoundtrack(description, tone, signal) {
  if (!musicContext) throw new Error('This browser does not support background audio.');
  const { job } = await request('soundtrack-start', { tone, description }, {}, '', signal);
  if (!job) throw new Error('The server did not return a music job.');
  const start = performance.now();
  while (!signal.aborted && performance.now() - start < 6 * 60_000) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    const result = await request('soundtrack-result', { job }, {}, '', signal);
    if (result instanceof Blob) {
      if (!result.size) throw new Error('MusicGen returned an empty audio file.');
      try { return await musicContext.decodeAudioData(await result.arrayBuffer()); }
      catch { throw new Error('The browser could not decode the generated soundtrack.'); }
    }
    if (result?.status !== 'composing') throw new Error('The server returned an unknown music job status.');
  }
  throw new Error('MusicGen did not finish in time.');
}

async function waitForSoundtrack(pending, delay = MUSIC_WAIT_BEFORE_READING) {
  let timer;
  try {
    return await Promise.race([pending, new Promise((resolve) => {
      timer = setTimeout(() => resolve(null), delay);
    })]);
  } finally { clearTimeout(timer); }
}

function renderSoundtrack() {
  const controls = $('story-soundtrack');
  controls.replaceChildren();
  if (story.musicStatus) {
    const status = element('p', story.musicStatus, 'narration-status');
    status.setAttribute('role', 'status');
    controls.append(status);
    return;
  }
  if (!musicBuffer) return;
  const musicButton = button(musicMuted ? 'Unmute music' : 'Mute music', () => {
    musicMuted = !musicMuted;
    updateSoundtrackVolume();
    musicButton.textContent = musicMuted ? 'Unmute music' : 'Mute music';
  });
  controls.append(musicButton, element('span', 'Looping instrumental'));
}

function applySoundtrack(outcome) {
  musicBuffer = outcome.buffer;
  story.musicStatus = outcome.error ? 'Background music unavailable. ' + outcome.error : '';
  renderSoundtrack();
  if (step === 3) startSoundtrack();
}

function storyHeader() {
  const header = element('header', undefined, 'story-header');
  const timestamp = element('time', story.createdAtLabel);
  timestamp.dateTime = story.createdAt;
  const dateLine = element('p', 'Generated ', 'story-date');
  dateLine.append(timestamp);
  header.append(element('h2', story.title), dateLine);
  const summary = element('dl', undefined, 'story-preferences');
  for (const [label, value] of [
    ['Tone', story.preferences.tone],
    ['Reading level', story.preferences.readingLevel],
    ['Target length', 'About ' + story.preferences.approximateWords + ' words'],
    ['Source images', String(story.sourceImages)]
  ]) {
    const entry = element('div');
    entry.append(element('dt', label), element('dd', value));
    summary.append(entry);
  }
  header.append(summary);
  if (story.provider === 'openai') {
    const controls = element('div', undefined, 'story-narration');
    const play = button('Play', toggleNarration);
    play.id = 'story-play';
    const status = element('p', undefined, 'narration-status');
    status.id = 'narration-status';
    status.setAttribute('role', 'status');
    controls.append(play, element('span', 'AI-generated narration'), status);
    header.append(controls);
  }
  const music = element('div', undefined, 'story-narration story-soundtrack');
  music.id = 'story-soundtrack';
  header.append(music);
  return header;
}

function renderStory(sequence) {
  $('story').replaceChildren(storyHeader());
  renderSoundtrack();
  for (const { scene, text } of story.scenes) {
    const item = sequence[scene - 1];
    const row = element('section', undefined, 'story-scene');
    if (item.image) {
      const figure = element('figure', undefined, 'story-image');
      figure.append(imageNode(item.image, item.alt));
      if (item.content.connectingEvent) figure.append(element('figcaption', 'Connecting scene'));
      row.append(figure);
    } else row.append(element('span', 'Connecting scene'));
    row.append(element('p', text));
    $('story').append(row);
  }
}

async function generateStory(previousStory) {
  // Resume Web Audio during the Generate click so playback can start after the model work.
  try { unlockSoundtrack(); } catch { /* Text generation still works without audio support. */ }
  storyAudio.pause();
  const sequence = namedScenes().flatMap((observation, index) => {
    const source = { content: observation, image: images[index].src, alt: images[index].name };
    const bridge = bridges.find(({ after, accepted }) => after === index + 1 && accepted);
    return bridge ? [source, { content: { connectingEvent: bridge.description }, image: bridge.inserted ? bridge.preview : '', alt: 'Connecting scene illustration' }] : [source];
  });
  notify(previousStory ? 'Writing another story with the same setup…' : 'Writing the story…');
  const storyPreferences = previousStory?.preferences ?? preferences();
  const result = await request('story', { ...storyPreferences,
    ...(previousStory && { variation: {
      previousTitle: previousStory.title,
      instruction: 'Write a new version with a different title and fresh narrative details while preserving the supplied scenes, bridges, names and preferences.'
    } }),
    scenes: sequence.map(({ content }, index) => ({ sequenceNumber: index + 1, ...content })) });
  const ordered = result.scenes.slice().sort((a, b) => a.scene - b.scene);
  if (!result.title.trim() || ordered.length !== sequence.length || ordered.some(({ scene, text }, index) => scene !== index + 1 || !text.trim())) {
    throw new Error('The story did not include every scene. Try generating again.');
  }
  clearSoundtrack();
  notify('Composing the background music…');
  const description = result.musicDescription?.trim().slice(0, 300) || 'Gentle acoustic storybook music.';
  const controller = new AbortController();
  musicRequest = controller;
  const pending = prepareSoundtrack(description, storyPreferences.tone, controller.signal)
    .then((buffer) => ({ buffer }), (error) => ({ error: error.message }));
  const soundtrack = await waitForSoundtrack(pending);
  clearNarration();
  const createdAt = new Date();
  const current = story = { title: result.title, scenes: ordered, sequence, preferences: storyPreferences,
    provider: settings.provider, sourceImages: images.length,
    createdAt: createdAt.toISOString(), createdAtLabel: storyTimestamp.format(createdAt),
    musicStatus: soundtrack?.error ? 'Background music unavailable. ' + soundtrack.error
      : soundtrack ? '' : 'Background music is still composing and will start automatically.' };
  musicBuffer = soundtrack?.buffer;
  if (!soundtrack) {
    void pending.then((outcome) => {
      if (story === current && !controller.signal.aborted) applySoundtrack(outcome);
      if (musicRequest === controller) musicRequest = undefined;
    });
  } else {
    musicRequest = undefined;
  }
  const naming = subjects.some(({ name }) => name.trim()) ? 'User-provided names' : 'Automatic names';
  runs.push({
    run_id: crypto.randomUUID(), created_at: story.createdAt,
    provider: settings.provider, configured_model: settings.model, source_images: images.length,
    tone: storyPreferences.tone, reading_level: storyPreferences.readingLevel, target_words: storyPreferences.approximateWords,
    naming_mode: naming, accepted_bridges: bridges.filter(({ accepted }) => accepted).length,
    bridge_image_generated: ledger.some(({ action, status }) => action === 'image' && status === 'completed'),
    bridge_images_inserted: bridges.filter(({ inserted }) => inserted).length,
    analysis_cache_hits: analysisCacheHits, match_cache_hit: matchCacheHit, calls: ledger
  });
  ledger = [];
  analysisCacheHits = images.length;
  matchCacheHit = true;
  let saved = true;
  try { localStorage.setItem('story-sequence-runs', JSON.stringify(runs)); } catch { saved = false; }
  highest = 3;
  renderStory(sequence);
  notify(saved ? '' : 'Run saved for this tab. Download metrics before closing it.');
  show(3);
}

$('generate').addEventListener('click', () => work(() => generateStory()));
$('generate-another').addEventListener('click', () => {
  if (story) void work(() => generateStory(story));
});

$('start-over').addEventListener('click', () => {
  const dialog = $('start-over-dialog');
  dialog.returnValue = '';
  dialog.showModal();
});
$('start-over-dialog').addEventListener('close', (event) => {
  if (event.currentTarget.returnValue !== 'reset') return;
  images = [];
  nextImageId = 1;
  subjects = [];
  subjectSignature = '';
  ledger = [];
  analysisCacheHits = 0;
  matchCacheHit = false;
  musicMuted = false;
  for (const id of ['tone', 'level', 'length']) {
    const field = $(id);
    const defaultIndex = Array.from(field.options).findIndex(({ defaultSelected }) => defaultSelected);
    field.selectedIndex = Math.max(0, defaultIndex);
  }
  $('upload').value = '';
  invalidateAfter(0);
  for (const id of ['subject-list', 'subject-preview-image', 'bridge-sequence', 'bridge-list', 'story']) $(id).replaceChildren();
  renderImages();
  notify();
  show(0);
});

function download(name, type, contents) {
  const url = URL.createObjectURL(new Blob([contents], { type }));
  const link = element('a');
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function csv(rows) {
  return rows.map((row) => row.map((cell) => {
    let value = String(cell ?? '');
    if (typeof cell === 'string' && /^\s*[=+\-@]/.test(value)) value = "'" + value;
    return '"' + value.replaceAll('"', '""') + '"';
  }).join(',')).join('\r\n') + '\r\n';
}

const ratingColumns = ['case_id', 'researcher_id', 'character_name_consistency', 'object_label_consistency',
  'logical_character_introductions', 'logical_scene_transitions', 'requested_tone_match', 'tone_consistency', 'reading_level_match'];
const runColumns = ['run_id', 'created_at', 'provider', 'configured_model', 'source_images', 'tone', 'reading_level',
  'target_words', 'naming_mode', 'accepted_bridges', 'bridge_image_generated', 'bridge_images_inserted', 'analysis_cache_hits', 'match_cache_hit'];
const timingActions = ['analyze', 'localize', 'refine', 'match', 'plan', 'image', 'story'];

$('exports').addEventListener('click', () => {
  $('run-count').textContent = runs.length + (runs.length === 1 ? ' completed run' : ' completed runs') + ' in this browser.';
  $('download-runs').disabled = !runs.length;
  $('download-details').disabled = !runs.length;
  $('research').showModal();
});
$('download-ratings').addEventListener('click', () => {
  const rows = Array.from({ length: 10 }, (_, index) => 'VIST-' + String(index + 1).padStart(2, '0'))
    .flatMap((id) => [1, 2, 3].map((researcher) => [id, researcher, ...Array(7).fill('')]));
  download('vist-ratings-template.csv', 'text/csv', csv([ratingColumns, ...rows]));
});
$('download-runs').addEventListener('click', () => {
  const rows = runs.map((run) => {
    const knownUsage = run.calls.filter(({ usage }) => usage);
    const knownPrice = run.calls.filter(({ usd }) => usd !== null);
    const total = (values, valueOf) => values.reduce((sum, value) => sum + valueOf(value), 0);
    return [...runColumns.map((column) => run[column]),
      ...timingActions.map((action) => total(run.calls.filter((call) => call.action === action), ({ wall_ms }) => wall_ms)),
      total(run.calls, ({ wall_ms }) => wall_ms),
      knownUsage.length ? total(knownUsage, ({ usage }) => usage.input_tokens || 0) : '',
      knownUsage.length ? total(knownUsage, ({ usage }) => usage.output_tokens || 0) : '',
      knownPrice.length ? total(knownPrice, ({ usd }) => usd).toFixed(6) : '',
      knownPrice.length, run.calls.length];
  });
  download('story-runs.csv', 'text/csv', csv([[...runColumns, ...timingActions.map((action) => action + '_ms'),
    'total_request_ms', 'reported_input_tokens', 'reported_output_tokens', 'estimated_usd_known_calls', 'priced_calls', 'total_calls'], ...rows]));
});
$('download-details').addEventListener('click', () => download('story-runs.json', 'application/json', JSON.stringify(runs, (key, value) => key === 'case_id' ? undefined : value, 2)));
$('download-story').addEventListener('click', async () => {
  if (!story) return;
  const current = story;
  const control = $('download-story');
  control.disabled = true;
  control.textContent = 'Preparing PDF…';
  try {
    const response = await fetch('/api/story.pdf', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: current.title,
        createdAtLabel: current.createdAtLabel,
        preferences: { ...current.preferences, sourceImages: current.sourceImages },
        scenes: current.scenes.map(({ scene, text }) => {
          const { image, content } = current.sequence[scene - 1];
          return { text, image: image || null, bridge: Boolean(content.connectingEvent) };
        })
      })
    });
    if (!response.ok) {
      const { error } = await response.json();
      throw new Error(error || 'The panel could not create the PDF.');
    }
    const pdf = await response.blob();
    if (pdf.type !== 'application/pdf' || !pdf.size) throw new Error('The panel did not return a PDF.');
    const filename = current.title.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '-')
      .replace(/^-+|-+$/g, '').slice(0, 72) || 'childrens-story';
    download(filename + '.pdf', 'application/pdf', pdf);
  } catch (error) {
    if (story === current) notify(error.message, true);
  } finally {
    control.disabled = false;
    control.textContent = 'Download story PDF';
  }
});

function settingsFields() {
  const openai = $('provider').value === 'openai';
  $('openai-fields').hidden = !openai;
  $('openai-model').required = openai;
  $('ollama-fields').hidden = openai;
  $('ollama-model').required = !openai;
  $('endpoint').required = !openai;
}

function draftSettings() {
  const provider = $('provider').value;
  return { provider, model: $(provider + '-model').value.trim(), endpoint: $('endpoint').value.trim(),
    bridgeImages: $('bridge-images').checked };
}

$('settings-button').addEventListener('click', () => {
  $('provider').value = settings.provider;
  $(settings.provider + '-model').value = settings.model;
  $('endpoint').value = settings.endpoint;
  $('ollama-model-list').hidden = true;
  $('openai-model-list').hidden = true;
  $('api-key').value = apiKey;
  $('bridge-images').checked = settings.bridgeImages;
  settingsFields();
  notify('', false, 'settings-status');
  $('settings').showModal();
});
$('provider').addEventListener('change', settingsFields);
document.querySelectorAll('dialog .close').forEach((node) => node.addEventListener('click', () => node.closest('dialog').close()));
$('settings').addEventListener('close', () => { $('api-key').value = ''; });

async function settingsWork(task) {
  if (settingsBusy) return;
  settingsBusy = true;
  for (const node of $('settings-form').querySelectorAll('input, select, button')) if (!node.classList.contains('close')) node.disabled = true;
  try { await task(); } catch (error) { notify(error.message, true, 'settings-status'); }
  finally {
    settingsBusy = false;
    for (const node of $('settings-form').querySelectorAll('input, select, button')) node.disabled = false;
  }
}

$('endpoint').addEventListener('input', () => { $('ollama-model-list').hidden = true; });
$('api-key').addEventListener('input', () => { $('openai-model-list').hidden = true; });

for (const [provider, buttonId, emptyMessage] of [
  ['ollama', 'list-models', 'No installed models found. Pull a vision model with Ollama first.'],
  ['openai', 'list-openai-models', 'No recognized vision models are available to this API key. You can enter a model ID manually.']
]) {
  const field = $(provider + '-model');
  const list = $(provider + '-models');
  const listLabel = $(provider + '-model-list');
  list.addEventListener('change', () => {
    field.value = list.value;
    notify('', false, 'settings-status');
  });
  field.addEventListener('input', () => { list.value = field.value.trim(); });

  $(buttonId).addEventListener('click', () => settingsWork(async () => {
    listLabel.hidden = true;
    notify('Loading models…', false, 'settings-status');
    const models = await request('models', null, draftSettings(), $('api-key').value.trim());
    list.replaceChildren(...models.map((name) => new Option(name, name)));
    list.size = Math.max(2, Math.min(models.length, 5));
    if (!field.value && models.length === 1) field.value = models[0];
    list.value = field.value.trim();
    listLabel.hidden = !models.length;
    notify(models.length ? '' : emptyMessage, !models.length, 'settings-status');
  }));
}
$('test-provider').addEventListener('click', () => settingsWork(async () => {
  notify('Testing with a small sample image…', false, 'settings-status');
  await request('check', null, draftSettings(), $('api-key').value.trim());
  notify('Connection passed. The model accepted an image and returned structured output.', false, 'settings-status');
}));
$('settings-form').addEventListener('submit', (event) => {
  event.preventDefault();
  if (settingsBusy) return;
  const next = draftSettings();
  if (!next.model) {
    notify('Enter a vision model ID.', true, 'settings-status');
    return;
  }
  const oldKey = modelKey();
  const oldBridges = settings.bridgeImages;
  settings = next;
  apiKey = $('api-key').value.trim();
  if (oldKey !== modelKey()) {
    ledger = [];
    subjectSignature = '';
    subjects = [];
    invalidateAfter(0);
    show(0);
  } else if (oldBridges !== settings.bridgeImages && highest >= 2) {
    renderBridges();
  }
  notify();
  $('settings').close();
  renderNavigation();
});

for (const id of ['tone', 'level', 'length']) $(id).addEventListener('change', () => invalidateAfter(1));
for (const node of document.querySelectorAll('[data-step], [data-back]')) {
  node.addEventListener('click', () => {
    notify();
    show(Number(node.dataset.step ?? node.dataset.back));
  });
}

renderImages();
show(0, false);
