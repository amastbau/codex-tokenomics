import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

function loadUiScript() {
  const html = fs.readFileSync(new URL("./index.html", import.meta.url), "utf8");
  const script = html.match(/<script>([\s\S]*)<\/script>/)?.[1];
  assert.ok(script, "index.html contains an inline script");

  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) {
      elements.set(id, {
        listeners: {},
        addEventListener(name, handler) { this.listeners[name] = handler; },
        className: "",
        innerHTML: "",
        textContent: "",
        value: "",
      });
    }
    return elements.get(id);
  };

  const context = {
    Intl,
    URLSearchParams,
    console,
    localStorage: {
      values: new Map(),
      getItem(key) { return this.values.get(key) ?? null; },
      setItem(key, value) { this.values.set(key, value); },
    },
    document: {getElementById: element},
    fetch: async () => ({
      ok: true,
      json: async () => ({
        agents: [],
        health: {},
        incidents: [],
        models: [],
        sessions: [],
        summary: {},
        usage: [],
      }),
    }),
  };
  vm.createContext(context);
  vm.runInContext(script, context);
  return context;
}

test("sortRows orders strings and toggles direction", () => {
  const ui = loadUiScript();
  const rows = [{label: "beta"}, {label: "Alpha"}, {label: "gamma"}];

  assert.deepEqual(
    ui.sortRows(rows, (row) => row.label, "asc").map((row) => row.label),
    ["Alpha", "beta", "gamma"],
  );
  assert.deepEqual(
    ui.sortRows(rows, (row) => row.label, "desc").map((row) => row.label),
    ["gamma", "beta", "Alpha"],
  );
});

test("table renders sortable column headers", () => {
  const ui = loadUiScript();

  const html = ui.table(
    [{label: "beta", tokens: 20}],
    [
      ["Label", (row) => row.label],
      ["Tokens", (row) => row.tokens],
    ],
    {tableId: "sessions"},
  );

  assert.match(html, /data-sort-table="sessions"/);
  assert.match(html, /data-sort-index="0"/);
  assert.match(html, /data-sort-index="1"/);
});

test("cost labels distinguish complete estimates, partial totals, and unknown prices", () => {
  const ui = loadUiScript();
  assert.equal(typeof ui.costLabel, "function", "a cost formatter is available");
  assert.equal(ui.costLabel({estimated_usd: 12.345, known_cost_usd: 12.345,
                            unpriced_responses: 0}), "$12.35");
  assert.equal(ui.costLabel({estimated_usd: 0, known_cost_usd: 0,
                            unpriced_responses: 0}), "$0.00");
  assert.equal(ui.costLabel({estimated_usd: null, known_cost_usd: 12.34,
                            unpriced_responses: 2}), "$12.34 + unpriced");
  assert.equal(ui.costLabel({estimated_usd: null, known_cost_usd: 0,
                            unpriced_responses: 1}), "Unpriced");
  assert.equal(ui.costLabel({}), "Unavailable");
});

test("small nonzero estimates are not rounded to zero", () => {
  const ui = loadUiScript();
  assert.equal(typeof ui.costLabel, "function", "a cost formatter is available");
  assert.equal(ui.costLabel({estimated_usd: 0.0002725}), "$0.000273");
});

test("model costs render and sort numerically with unknown costs last", () => {
  const ui = loadUiScript();
  assert.equal(typeof ui.renderCosts, "function", "cost tables are available");
  vm.runInContext(`state = {
    models: [
      {model: "cheap", total_tokens: 30, responses: 1, estimated_usd: 2},
      {model: "expensive", total_tokens: 10, responses: 1, estimated_usd: 10},
      {model: "mystery", total_tokens: 100, responses: 1,
       estimated_usd: null, known_cost_usd: 0, unpriced_responses: 1}
    ], agents: []
  }; sortState.models = {index: 3, direction: "desc"}; renderCosts();`, ui);
  const html = ui.document.getElementById("models").innerHTML;
  assert.ok(html.indexOf("expensive") < html.indexOf("cheap"));
  assert.ok(html.indexOf("cheap") < html.indexOf("mystery"));
  assert.match(html, /\$10\.00/);
  assert.match(html, /Unpriced/);
  assert.match(html, /data-sort-table="models"/);
});

test("pricing note shows rate date and missing coverage", () => {
  const ui = loadUiScript();
  assert.equal(typeof ui.renderPricing, "function", "pricing assumptions are visible");
  vm.runInContext(`state = {
    summary: {unpriced_responses: 2, unpriced_tokens: 200},
    pricing: {checked_at: "2026-10-08", assumptions: "Standard rates; excludes tool fees",
      rates_per_million: {"gpt-6.1-sol": {input: 2, cached_input: .1, cache_write: 2.5, output: 10}}}
  }; renderPricing();`, ui);
  const html = ui.document.getElementById("pricing").innerHTML;
  assert.match(html, /2026-10-08/);
  assert.match(html, /2 responses/);
  assert.match(html, /200 tokens/);
  assert.match(html, /excludes tool fees/);
  assert.match(html, /gpt-6.1-sol/);
});

test("published fractional cache-write rates retain their precision", () => {
  const ui = loadUiScript();
  vm.runInContext(`state = {
    summary: {unpriced_responses: 0},
    pricing: {checked_at: "2026-10-08", assumptions: "Standard",
      rates_per_million: {"gpt-6-luna": {input: .1, cached_input: .01, cache_write: .125, output: .5}}}
  }; renderPricing();`, ui);
  assert.match(ui.document.getElementById("pricing").innerHTML, /\$0\.125/);
});

test("per-token prices convert all categories without rounding small prices to zero", () => {
  const ui = loadUiScript();
  vm.runInContext(`state = {
    summary: {unpriced_responses: 0},
    pricing: {checked_at: "2026-10-08", assumptions: "Standard",
      rates_per_million: {
        "gpt-6.1-sol": {input: 2, cached_input: .1, cache_write: 2.5, output: 10},
        "gpt-6-luna": {input: .1, cached_input: .01, cache_write: .125, output: .5}
      }}
  }; renderPricing();`, ui);
  const html = ui.document.getElementById("pricing").innerHTML;
  assert.match(html, /Price per token.*USD/);
  for (const price of ["$0.000002", "$0.0000001", "$0.0000025", "$0.00001",
                       "$0.00000001", "$0.000000125", "$0.0000005"]) {
    assert.ok(html.includes(`<td>${price}</td>`), `displays ${price} per token`);
  }
  assert.match(html, /data-sort-table="token-rates"/);
});

test("price chart compares all categories on one linear scale with exact USD labels", () => {
  const ui = loadUiScript();
  vm.runInContext(`state = {
    summary: {unpriced_responses: 0},
    pricing: {checked_at: "2026-10-08", assumptions: "Standard",
      rates_per_million: {
        "model-a": {input: 2, cached_input: .1, cache_write: 2.5, output: 10},
        "model-b": {input: .1, cached_input: .01, cache_write: .125, output: .5}
      }}
  }; renderPricing();`, ui);
  const html = ui.document.getElementById("pricing").innerHTML;
  assert.match(html, /class="price-chart"/);
  assert.match(html, /model-a · Uncached input: \$0\.000002 per token/);
  assert.match(html, /model-b · Cached input: \$0\.00000001 per token/);
  assert.match(html, /model-a · Cache writes: \$0\.0000025 per token/);
  assert.match(html, /model-a · Output: \$0\.00001 per token/);
  // Input=2 and output=10 are 20% and 100% of the same axis maximum.
  assert.match(html, /style="width:20%;/);
  assert.match(html, /style="width:100%;/);
  assert.match(html, /data-sort-table="token-rates"/);
});

test("price chart handles zero, missing, and empty rates without inventing prices", () => {
  const ui = loadUiScript();
  assert.equal(typeof ui.renderPriceChart, "function");
  const html = ui.renderPriceChart([{model: "zero", input: 0, cached_input: null,
                                   cache_write: 0, output: 0}]);
  assert.match(html, /width:0%;/);
  assert.match(html, /Unavailable/);
  assert.doesNotMatch(html, /NaN|Infinity/);
  assert.match(ui.renderPriceChart([]), /No published prices/);
});

test("global model filters are sent together with date filters", () => {
  const ui = loadUiScript();
  vm.runInContext(`modelFilters.include.add("gpt-6.1-sol");
    modelFilters.include.add("provider/model + preview");
    modelFilters.exclude.add("gpt-6-astra");`, ui);
  ui.document.getElementById("from-date").value = "2026-10-08T10:00";
  const params = new URLSearchParams(ui.apiQuery().slice(1));
  assert.deepEqual(params.getAll("include_model"), ["gpt-6.1-sol", "provider/model + preview"]);
  assert.deepEqual(params.getAll("exclude_model"), ["gpt-6-astra"]);
  assert.ok(params.get("since"));
});

test("pricing tables and price chart follow the applied model filters", () => {
  const ui = loadUiScript();
  vm.runInContext(`state = {
    summary: {unpriced_responses: 0},
    filters: {include_models: ["kept", "hidden"], exclude_models: ["hidden"]},
    pricing: {rates_per_million: {
      kept: {input: 2, cached_input: .1, cache_write: 2.5, output: 10},
      hidden: {input: 20, cached_input: 1, cache_write: 25, output: 100},
      other: {input: .1, cached_input: .01, cache_write: .125, output: .5}
    }}
  }; renderPricing();`, ui);
  const html = ui.document.getElementById("pricing").innerHTML;
  assert.match(html, /kept/);
  assert.doesNotMatch(html, /<(?:td|strong)>(?:hidden|other)<\//);
});

test("model choices stay available after exclusions and escape model labels", () => {
  const ui = loadUiScript();
  vm.runInContext(`modelFilters.exclude.add("hidden");
    state = {available_models: ["kept", "hidden", '<script>alert("x")</script>'],
      pricing: {rates_per_million: {}}}; renderModelFilters();`, ui);
  const html = ui.document.getElementById("model-filter-options").innerHTML;
  assert.match(html, /hidden/);
  assert.match(html, /checked/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});

test("model checkbox changes persist and clear resets models while retaining dates", () => {
  const ui = loadUiScript();
  const input = {name: "exclude", value: "gpt-6-astra", checked: true};
  ui.document.getElementById("model-filter-options").listeners.change({target: input});
  assert.deepEqual(new URLSearchParams(ui.apiQuery().slice(1)).getAll("exclude_model"), ["gpt-6-astra"]);
  assert.match(ui.localStorage.getItem("codex-tokenomics-model-filters"), /gpt-6-astra/);
  ui.document.getElementById("to-date").value = "2026-10-09T10:00";
  ui.document.getElementById("clear-models").listeners.click();
  const params = new URLSearchParams(ui.apiQuery().slice(1));
  assert.deepEqual(params.getAll("exclude_model"), []);
  assert.ok(params.get("until"));
});
