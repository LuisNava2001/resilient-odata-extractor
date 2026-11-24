// Static demo: renders recorded extraction traces from demo/data.js. DOM built with textContent only.
(function () {
  "use strict";

  var data = window.EXTRACTOR_DEMO;

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function status(value) {
    return el("span", "status status-" + value, value);
  }

  function kpi(label, value) {
    var box = el("div", "kpi");
    box.appendChild(el("div", "label", label));
    box.appendChild(el("div", "value", value));
    return box;
  }

  function findingList(items, kind) {
    var list = el("ul", "findings");
    items.forEach(function (item) {
      var li = el("li", kind);
      if (typeof item === "string") {
        li.textContent = item;
      } else {
        li.appendChild(el("span", "code", item.code));
        li.appendChild(document.createTextNode(item.message));
        if (item.sample && item.sample.length) {
          li.appendChild(el("div", "mono", "sample IDs: " + item.sample.join(", ")));
        }
      }
      list.appendChild(li);
    });
    return list;
  }

  var REASONS = {
    http_429: "rate limited", http_502: "bad gateway", http_503: "service unavailable", http_504: "gateway timeout",
    timeout: "timed out", malformed_response: "malformed body", connection_error: "connection error"
  };

  function reasonLabel(reason) {
    return REASONS[reason] || String(reason).replace(/_/g, " ");
  }

  function describe(event) {
    var page = event.page ? "page " + event.page + " " : "";
    if (event.kind === "request") return page + "request (attempt " + event.attempt + ")";
    if (event.kind === "response") {
      var s = event.status === null || event.status === undefined ? "no response" : "HTTP " + event.status;
      if (event.detail) return page + s + " (" + reasonLabel(event.detail) + ")";
      return page + s + (event.records !== undefined ? ", " + event.records + " records" : "");
    }
    if (event.kind === "retry") return page + "wait " + event.wait + "s, then retry (" + event.basis + ")";
    return page + (event.detail || event.kind);
  }

  function timeline(events) {
    var list = el("ol", "timeline");
    events.forEach(function (event) {
      var failed = event.kind === "response" && (event.detail || (event.status && event.status >= 300));
      var li = el("li", failed ? "k-retry" : "k-" + event.kind);
      li.appendChild(document.createTextNode(describe(event)));
      if (event.url && event.kind === "request" && (event.attempt > 1 || event.page === 1)) {
        li.appendChild(el("div", "mono", event.url));
      }
      list.appendChild(li);
    });
    return list;
  }

  function render(index) {
    var t = data.scenarios[index];
    var detail = document.getElementById("detail");
    detail.textContent = "";
    var head = el("div", "head");
    head.appendChild(el("h2", null, t.title));
    head.appendChild(status(t.status));
    head.appendChild(el("span", "tag", t.conformant ? "provider behaviour allowed by OData" : "simulated provider defect"));
    if (!t.as_expected) head.appendChild(el("span", "tag", "unexpected: wanted " + t.expected_status));
    detail.appendChild(head);
    detail.appendChild(el("p", "desc", t.description));

    var m = t.metrics;
    var kpis = el("div", "kpis");
    kpis.appendChild(kpi("HTTP requests", m.requests_total));
    kpis.appendChild(kpi("of which reconcile", m.requests_reconcile));
    kpis.appendChild(kpi("Pages received", m.pages));
    kpis.appendChild(kpi("Records received", m.records_received));
    kpis.appendChild(kpi("Unique records", m.unique_records));
    kpis.appendChild(kpi("Retries", m.retries));
    kpis.appendChild(kpi("Waited (s)", m.wait_seconds));
    kpis.appendChild(kpi("Problems found", m.problems));
    detail.appendChild(kpis);

    detail.appendChild(el("h3", null, "Why " + t.status));
    if (t.reasons.length) detail.appendChild(findingList(t.reasons, "reason"));
    if (t.warnings.length) detail.appendChild(findingList(t.warnings, "warning"));
    if (t.basis.length) {
      if (t.reasons.length || t.warnings.length) detail.appendChild(el("h3", null, "Checks that passed"));
      detail.appendChild(findingList(t.basis, "basis"));
    }
    if (!t.reasons.length && !t.warnings.length && !t.basis.length) detail.appendChild(el("p", null, "No checks could be made."));

    detail.appendChild(el("h3", null, "Extraction timeline"));
    detail.appendChild(timeline(t.timeline));

    document.querySelectorAll("#scenario-list button").forEach(function (b) {
      b.setAttribute("aria-current", b.dataset.index === String(index) ? "true" : "false");
    });
    if (window.history && window.history.replaceState) window.history.replaceState(null, "", "#" + t.name);
  }

  function init() {
    if (!data) {
      document.getElementById("detail").textContent = "demo/data.js is missing.";
      return;
    }
    var ok = data.scenarios.filter(function (s) { return s.as_expected; }).length;
    document.getElementById("summary-badge").textContent = ok + " of " + data.scenarios.length + " scenarios reached their expected status";
    var list = document.getElementById("scenario-list");
    var start = 0;
    data.scenarios.forEach(function (t, i) {
      if (window.location.hash === "#" + t.name) start = i;
      var item = el("li");
      var button = el("button");
      button.type = "button";
      button.dataset.index = String(i);
      button.appendChild(el("span", null, t.title));
      button.appendChild(status(t.status));
      button.addEventListener("click", function () { render(i); });
      item.appendChild(button);
      list.appendChild(item);
    });
    render(start);
  }

  init();
})();
