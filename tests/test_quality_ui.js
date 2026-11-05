// Run with: node tests/test_quality_ui.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web", "index.html"), "utf8");
const clone = (value) => JSON.parse(JSON.stringify(value));

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toLowerCase();
    this.children = [];
    this.dataset = {};
    this.events = {};
    this.className = "";
    this._text = "";
    this._value = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.classList = {
      toggle: (name, enabled) => {
        const names = new Set(this.className.split(/\s+/).filter(Boolean));
        enabled ? names.add(name) : names.delete(name);
        this.className = [...names].join(" ");
      },
    };
  }
  set value(value) {
    this._value = String(value);
  }
  get value() {
    return this._value;
  }
  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }
  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
  }
  get options() {
    return this.children.filter((child) => child.tagName === "option");
  }
  get selectedOptions() {
    return this.options.filter((option) => option.value === this.value);
  }
  append(...children) {
    this.children.push(...children);
  }
  scrollIntoView() {}
  replaceChildren(...children) {
    this._text = "";
    this.children = children;
  }
  setAttribute(name, value) {
    this[name] = value;
  }
  addEventListener(name, handler) {
    this.events[name] = handler;
  }
}

function option(text, value) {
  const element = new Element("option");
  element.textContent = text;
  element.value = value;
  return element;
}

function makeDocument() {
  const elements = new Map(),
    steps = ["images", "settings", "result"].map((step) => {
      const element = new Element("button");
      element.dataset.step = step;
      return element;
    });
  for (const match of html.matchAll(/<([a-z][\w-]*)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const element = new Element(match[1]),
      attributes = match[2];
    for (const name of ["value", "type", "class"])
      if (attributes.match(new RegExp(`\\b${name}="([^"]*)"`)))
        element[name === "class" ? "className" : name] =
          attributes.match(new RegExp(`\\b${name}="([^"]*)"`))[1];
    for (const name of ["checked", "disabled", "hidden"])
      element[name] = new RegExp(`\\b${name}(?:\\s|$)`).test(attributes);
    elements.set(match[3], element);
  }
  for (const match of html.matchAll(/<select\b[^>]*id="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)) {
    const select = elements.get(match[1]);
    for (const item of match[2].matchAll(/<option([^>]*)>([\s\S]*?)<\/option>/g)) {
      const value = item[1].match(/\bvalue="([^"]*)"/)?.[1] ?? item[2],
        itemOption = option(item[2], value);
      select.append(itemOption);
      if (select.options.length === 1 || /\bselected\b/.test(item[1]))
        select.value = value;
    }
  }
  const get = (id) => {
      assert.ok(elements.has(id), "Unknown element #" + id);
      return elements.get(id);
    },
    walk = (element) => [element, ...element.children.flatMap(walk)],
    document = {
      createElement: (tag) => new Element(tag),
      createTextNode: (text) =>
        Object.assign(new Element("text"), { textContent: text }),
      querySelectorAll: (selector) =>
        selector === "[data-step]"
          ? steps
          : [...elements.values()]
              .flatMap(walk)
              .filter(
                (element) =>
                  selector === ".quality-select" &&
                  element.className === "quality-select",
              ),
    };
  return { get, document };
}

function reportFixture() {
  const image = (name, extra = {}) => ({
    name, decodable: true, flags: [], width: 320, height: 240,
    laplacian_variance: 100, mean_luminance: 120, dark_ratio: 0,
    bright_ratio: 0, sha256: name, ...extra,
  });
  return {
    fingerprint: "fixture-fingerprint",
    policy: { blur_threshold: 60, dark_mean: 40, bright_mean: 215, near_distance: 4, phash_distance: 6 },
    counts: { total: 5, decodable: 4, blurry: 0, underexposed: 0, overexposed: 0, exact_duplicate: 1, near_duplicate: 1, compared_pairs: 6, eligible_pairs: 2, dhash_pairs: 2, phash_pairs: 1, near_duplicate_pairs: 1 },
    algorithms: { dhash: { threshold: 4, bits: 64 }, phash: { threshold: 6, bits: 64 } },
    pair_sampling: { display_limit: 200, truncated: false },
    images: [
      image("a.jpg"),
      image("b.jpg", { similar_to: "a.jpg", hash_distance: 0, phash_distance: 0, flags: ["near_duplicate"] }),
      image("c.jpg"),
      image("copy.jpg", { sha256: "a.jpg", duplicate_of: "a.jpg", flags: ["exact_duplicate"] }),
      image("broken.png", { decodable: false, flags: ["decode_error"], error: "decode failed" }),
    ],
    pairs: [
      { a: "a.jpg", b: "b.jpg", eligible: true, exclusion_reasons: [], dhash_distance: 0, phash_distance: 0, dhash_match: true, phash_match: true, match: true },
      { a: "a.jpg", b: "c.jpg", eligible: true, exclusion_reasons: [], dhash_distance: 2, phash_distance: 15, dhash_match: true, phash_match: false, match: false },
      { a: "b.jpg", b: "c.jpg", eligible: false, exclusion_reasons: ["low_texture"], dhash_distance: 2, phash_distance: 2, dhash_match: false, phash_match: false, match: false },
    ],
  };
}

function makeHarness() {
  const { get, document } = makeDocument(),
    stored = new Map(),
    requests = [],
    messages = [],
    delayedResponses = new Map(),
    state = { image_set_id: "set-ui", report: reportFixture(), selection: null, requires_reanalysis: false },
    project = { image_sets: [{ id: "set-ui", name: "UI fixture", path: "data/ui-fixture", input_type: "images", count: 5, images: state.report.images }] };
  let serverState = clone(state),
    nextAnalyzeState = null;
  const context = vm.createContext({
    console, document, $: get, Option: option, URL, URLSearchParams,
    params: new URLSearchParams(), location: { search: "" },
    window: { activeProjectId: "project-ui", addEventListener() {} },
    sessionStorage: {
      setItem: (key, value) => stored.set(key, value),
      getItem: (key) => stored.get(key) ?? null,
    },
    setInterval() {}, confirm: () => true, initShell: async () => {},
    inputCount: (item) => item.count + " images",
    fmt: (value) => value == null ? "-" : String(value),
    message: (...args) => messages.push(args),
    api: async (url, body) => {
      requests.push({ url, body: body && clone(body) });
      const delayed = delayedResponses.get(url);
      delayedResponses.delete(url);
      const respond = async (value) => {
        if (delayed) await delayed;
        return value;
      };
      if (url.startsWith("/api/quality?")) return respond(clone(serverState));
      if (url.startsWith("/api/project?")) return respond(clone(project));
      if (url === "/api/image-sets")
        return respond({ ...clone(project.image_sets[0]), id: "registered-set" });
      if (url === "/api/quality-analyze") {
        serverState = clone(nextAnalyzeState || state);
        nextAnalyzeState = null;
        Object.assign(serverState.report.policy, body.thresholds);
        serverState.report.algorithms.phash.threshold = body.thresholds.phash_distance;
        return respond(clone(serverState));
      }
      if (url === "/api/image-selection") {
        assert.equal(serverState.requires_reanalysis, false);
        serverState.selection = { names: body.names };
        return respond(clone(serverState));
      }
      throw Error("Unexpected API request: " + url);
    },
    fixtureProject: clone(project),
  });
  for (const file of ["workbench.js", "quality.js"])
    vm.runInContext(fs.readFileSync(path.join(root, "web", file), "utf8"), context, { filename: file });
  vm.runInContext("projectData = fixtureProject", context);
  return {
    get, document, stored, requests, messages,
    run: (code) => vm.runInContext(code, context),
    checks: (name) => document.querySelectorAll(".quality-select").filter((check) => check.dataset.name === name),
    deferResponse: (url) => {
      let resolve, reject;
      const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
      delayedResponses.set(url, promise);
      return { resolve, reject };
    },
    setNextAnalyzeState: (next) => {
      nextAnalyzeState = clone(next);
    },
    load: async (next = state, item = project.image_sets[0]) => {
      serverState = clone(next);
      context.loadItem = clone(item);
      await vm.runInContext("chooseInput(loadItem)", context);
    },
    state,
  };
}

async function testReportAndSelection() {
  const h = makeHarness();
  await h.load();
  assert.equal(h.get("quality-pair-rows").children.length, 3);
  assert.equal(h.run("qualityChosen.size"), 4);
  assert.equal(h.get("quality-phash").value, "6");
  assert.equal(h.checks("broken.png")[0].disabled, true);
  const checks = h.checks("a.jpg");
  assert.equal(checks.length, 3);
  checks[1].checked = false;
  checks[1].onchange();
  assert.ok(checks.every((check) => !check.checked));
  assert.equal(h.run('qualityChosen.has("a.jpg")'), false);
  h.get("quality-all").onclick();
  h.get("quality-dedup").onclick();
  assert.equal(h.run('qualityChosen.has("copy.jpg")'), false);
  assert.equal(h.run('qualityChosen.has("b.jpg")'), true);
  for (const filter of ["matches", "disagree", "excluded"]) {
    h.get("quality-pair-filter").value = filter;
    h.get("quality-pair-filter").onchange();
    assert.equal(h.get("quality-pair-rows").children.length, 1);
  }
  assert.ok(h.get("quality-pair-rows").textContent.includes("纹理不足"));
  const excluded = h.get("quality-pair-rows").children[0];
  assert.ok(excluded.children[2].textContent.includes("不参与判定"));
  assert.ok(excluded.children[3].textContent.includes("不参与判定"));
  assert.ok(excluded.children[4].textContent.includes("不参与相似判定"));
}

async function testStaleAndLegacyReports() {
  const h = makeHarness(), stale = clone(h.state);
  stale.requires_reanalysis = true;
  await h.load(stale);
  assert.equal(h.run("qualityPolicyDirty"), true);
  assert.equal(h.get("quality-save").disabled, true);
  h.get("quality-issues").checked = true;
  h.get("quality-issues").onchange();
  h.get("quality-dedup").onclick();
  assert.ok(h.get("quality-summary").textContent.includes("重新检测"));
  assert.ok(h.get("quality-summary").textContent.includes("历史结果"));
  const before = h.requests.length;
  await h.get("quality-save").onclick();
  assert.equal(h.requests.length, before);
  h.run('goStep("settings")');
  assert.equal(h.run("currentStep"), "images");
  await h.get("form").onsubmit({ preventDefault() {} });
  assert.equal(h.requests.length, before);
  delete stale.report.pairs;
  delete stale.report.algorithms;
  delete stale.report.policy.phash_distance;
  for (const image of stale.report.images) delete image.phash_distance;
  await h.load(stale);
  assert.equal(h.get("quality-pair-table").hidden, true);
  assert.ok(h.get("quality-rows").textContent.includes("dHash 距离 0"));
  assert.equal(h.get("quality-phash").value, "6");
  h.get("quality-phash").value = "9";
  await h.get("quality-analyze").onclick();
  const analyze = h.requests.find((request) => request.url === "/api/quality-analyze");
  assert.equal(analyze.body.thresholds.phash_distance, 9);
  assert.equal(h.run("qualityPolicyDirty"), false);
  assert.equal(h.get("quality-save").disabled, false);
  assert.ok(!h.get("quality-summary").textContent.includes("历史结果"));
  await h.get("quality-save").onclick();
  assert.ok(h.get("quality-selected").textContent.includes("筛选已保存"));
}

async function testDraftCompatibility() {
  const h = makeHarness();
  await h.load();
  h.get("quality-phash").value = "7";
  h.get("quality-phash").events.input();
  h.get("quality-issues").onchange();
  assert.ok(h.get("quality-summary").textContent.includes("阈值已修改"));
  assert.equal(h.get("quality-save").disabled, true);
  const check = h.checks("a.jpg")[0];
  check.checked = false;
  check.onchange();
  h.run("saveDraft()");
  const key = "recon-draft-project-ui", draft = JSON.parse(h.stored.get(key));
  assert.equal(draft.thresholds.length, 5);
  assert.equal(draft.thresholds[4], "7");
  await h.load();
  await h.run("restoreDraft()");
  assert.equal(h.get("quality-phash").value, "7");
  assert.equal(h.run('qualityChosen.has("a.jpg")'), false);
  draft.thresholds = ["12", "20", "230", "2"];
  draft.chosen = ["a.jpg", "b.jpg"];
  h.stored.set(key, JSON.stringify(draft));
  await h.run("restoreDraft()");
  assert.equal(h.get("quality-phash").value, "6");
  assert.equal(h.get("quality-near").value, "2");
  assert.equal(h.run("qualityChosen.size"), 2);
  draft.policyDirty = false;
  h.stored.set(key, JSON.stringify(draft));
  const stale = clone(h.state);
  stale.requires_reanalysis = true;
  await h.load(stale);
  await h.run("restoreDraft()");
  assert.equal(h.run("qualityPolicyDirty"), true);
  assert.equal(h.get("quality-save").disabled, true);
  assert.ok(h.get("quality-summary").textContent.includes("历史结果"));
}

async function testThresholdReanalysisPreservesSelection() {
  const h = makeHarness();
  await h.load();
  const check = h.checks("a.jpg")[0];
  check.checked = false;
  check.onchange();

  const next = clone(h.state);
  next.report.images.push({
    name: "new.jpg",
    decodable: true,
    flags: [],
    width: 320,
    height: 240,
    laplacian_variance: 100,
    mean_luminance: 120,
    dark_ratio: 0,
    bright_ratio: 0,
    sha256: "new.jpg",
  });
  next.report.counts.total = 6;
  next.report.counts.decodable = 5;
  h.setNextAnalyzeState(next);
  h.get("quality-phash").value = "7";
  h.get("quality-phash").events.input();
  await h.get("quality-analyze").onclick();

  assert.equal(h.run('qualityChosen.has("a.jpg")'), false);
  assert.equal(h.run('qualityChosen.has("b.jpg")'), true);
  assert.equal(h.run('qualityChosen.has("c.jpg")'), true);
  assert.equal(h.run('qualityChosen.has("copy.jpg")'), true);
  assert.equal(h.run('qualityChosen.has("new.jpg")'), false);
  assert.equal(h.run("qualityDirty"), true);
  assert.equal(h.get("quality-save").disabled, false);

  await h.get("quality-save").onclick();
  const saved = h.requests.find(
    (request) => request.url === "/api/image-selection",
  );
  assert.deepEqual(saved.body.names, ["b.jpg", "c.jpg", "copy.jpg"]);
}

async function testPairDisplayBoundaries() {
  const h = makeHarness(), state = clone(h.state);
  state.report.pair_sampling.truncated = true;
  state.report.counts.compared_pairs = 1000;
  await h.load(state);
  assert.ok(h.get("quality-pair-summary").textContent.includes("1000"));
  assert.ok(h.get("quality-pair-summary").textContent.includes("上限 200"));
  assert.ok(h.get("quality-pair-summary").textContent.includes("不是随机抽样"));
  state.report.pairs = [];
  await h.load(state);
  assert.equal(h.get("quality-pair-empty").hidden, false);
  assert.equal(h.get("quality-pair-table").hidden, true);
}

function assertQualityEditors(h, busy) {
  for (const id of [
    "quality-blur", "quality-dark", "quality-bright", "quality-near", "quality-phash",
    "quality-all", "quality-dedup", "quality-none", "quality-analyze",
  ]) assert.equal(h.get(id).disabled, busy, id + " disabled state");
  for (const name of ["a.jpg", "b.jpg", "c.jpg", "copy.jpg"])
    assert.ok(h.checks(name).every((check) => check.disabled === busy), name + " checkbox state");
  assert.equal(h.checks("broken.png")[0].disabled, true);
}

async function testAnalysisLocksEditorsUntilResponse() {
  const h = makeHarness();
  await h.load();
  h.get("quality-phash").value = "8";
  h.get("quality-phash").events.input();
  const delayed = h.deferResponse("/api/quality-analyze"),
    pending = h.get("quality-analyze").onclick();
  assertQualityEditors(h, true);
  h.get("quality-issues").onchange();
  h.get("quality-pair-filter").onchange();
  assertQualityEditors(h, true);
  delayed.resolve();
  await pending;
  assertQualityEditors(h, false);
  assert.equal(h.get("quality-phash").value, "8");
  assert.equal(h.run("qualityPolicyDirty"), false);
  h.get("quality-phash").value = "9";
  h.get("quality-phash").events.input();
  assert.equal(h.run("qualityPolicyDirty"), true);
}

async function testSaveLocksSelectionUntilResponse() {
  const h = makeHarness();
  await h.load();
  const delayed = h.deferResponse("/api/image-selection"),
    pending = h.get("quality-save").onclick();
  assertQualityEditors(h, true);
  h.get("quality-issues").onchange();
  h.get("quality-pair-filter").onchange();
  assertQualityEditors(h, true);
  delayed.resolve();
  await pending;
  assertQualityEditors(h, false);
  assert.equal(h.run("qualityDirty"), false);
  const check = h.checks("a.jpg")[1];
  check.checked = false;
  check.onchange();
  assert.equal(h.run('qualityChosen.has("a.jpg")'), false);
  assert.equal(h.run("qualityDirty"), true);
}

async function testFailedRequestsRestoreEditors() {
  for (const [button, endpoint] of [
    ["quality-analyze", "/api/quality-analyze"],
    ["quality-save", "/api/image-selection"],
  ]) {
    const h = makeHarness();
    await h.load();
    const check = h.checks("a.jpg")[0];
    check.checked = false;
    check.onchange();
    const delayed = h.deferResponse(endpoint), pending = h.get(button).onclick();
    assertQualityEditors(h, true);
    delayed.reject(new Error("request failed"));
    await pending;
    assertQualityEditors(h, false);
    assert.equal(h.run('qualityChosen.has("a.jpg")'), false);
    assert.equal(h.run("qualityDirty"), true);
    assert.ok(h.messages.some(([message]) => message.includes("request failed")));
  }
}

async function testSwitchingInputIgnoresOldResponses() {
  for (const [button, endpoint] of [
    ["quality-analyze", "/api/quality-analyze"],
    ["quality-save", "/api/image-selection"],
  ]) for (const fail of [false, true]) {
    const h = makeHarness();
    await h.load();
    const delayed = h.deferResponse(endpoint), pending = h.get(button).onclick(),
      next = { ...clone(h.state), image_set_id: "other-set" };
    await h.load(next, { id: "other-set", path: "data/other", input_type: "images", images: [] });
    const summary = h.get("quality-summary").textContent, messages = h.messages.length;
    assertQualityEditors(h, true);
    fail ? delayed.reject(new Error("old request failed")) : delayed.resolve();
    await pending;
    assertQualityEditors(h, false);
    assert.equal(h.run("qualityState.image_set_id"), "other-set");
    assert.equal(h.get("quality-summary").textContent, summary);
    assert.equal(h.messages.length, messages);
  }
}

async function testSwitchingDuringRegistrationKeepsCurrentInput() {
  const h = makeHarness();
  await h.load();
  h.run("window.selectedSetId = null; resetQuality()");
  const delayed = h.deferResponse("/api/image-sets"), pending = h.get("quality-analyze").onclick(),
    next = { ...clone(h.state), image_set_id: "other-set" };
  await h.load(next, { id: "other-set", path: "data/other", input_type: "images", images: [] });
  delayed.resolve();
  await pending;
  assert.equal(h.run("window.selectedSetId"), "other-set");
  assert.equal(h.run("qualityState.image_set_id"), "other-set");
  assert.equal(h.requests.filter(({url}) => url === "/api/quality-analyze").length, 0);
  assertQualityEditors(h, false);
}

(async () => {
  for (const test of [
    testReportAndSelection, testStaleAndLegacyReports,
    testDraftCompatibility, testThresholdReanalysisPreservesSelection,
    testPairDisplayBoundaries,
    testAnalysisLocksEditorsUntilResponse, testSaveLocksSelectionUntilResponse,
    testFailedRequestsRestoreEditors, testSwitchingInputIgnoresOldResponses,
    testSwitchingDuringRegistrationKeepsCurrentInput,
  ]) {
    await test();
    console.log("PASS " + test.name);
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
