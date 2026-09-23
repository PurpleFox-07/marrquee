/*
 * The live layer for the Deploy screen. Everything this script draws comes
 * straight out of a whole `DeploySnapshotOut` JSON frame from
 * `GET /api/deploy/events` - it never composes a sentence of its own, and
 * every field name below is checked against the API's own models, not the
 * engine's internal ones. If this file fails to load, or JavaScript is
 * off, the page still reaches every frame through its own form posts and
 * a `<noscript>` refresh.
 */
(function () {
  "use strict";

  // Every field name below is read out of a live SSE frame. A test compares
  // each of these four lists against `DeploySnapshotOut` / `AppProgressOut`
  // / `WiringStepOut` / `FailureOut` in `routes/api.py`, so a field this
  // script does not actually use has no reason to be listed here.
  var SNAPSHOT_FIELDS = ["phase", "apps", "headline", "detail", "failure", "wiring"];
  var APP_FIELDS = ["app_id", "state", "chip", "line", "note"];
  var WIRING_FIELDS = ["index", "total", "line", "state", "chip", "note", "involved"];
  var FAILURE_FIELDS = ["headline", "what_to_do"];

  var root = document.querySelector("[data-deploy]");
  if (!root) {
    return;
  }

  var lastAnnounced = null;
  var reloadScheduled = false;
  var source = null;

  if (root.dataset.live === "true") {
    source = new EventSource("/api/deploy/events");
    source.onmessage = handleMessage;
    source.onerror = handleError;
  }

  function handleMessage(event) {
    var snapshot;
    try {
      snapshot = JSON.parse(event.data);
    } catch (error) {
      // A malformed frame is dropped rather than half-applied - the next
      // frame (or the server's own first-frame-on-subscribe guarantee
      // after a reconnect) repaints the true state.
      return;
    }

    render(snapshot);

    if (source && (snapshot.phase === "finale" || snapshot.phase === "error")) {
      source.close();
    }
  }

  function handleError() {
    if (!source || source.readyState !== EventSource.CLOSED) {
      // The browser retries a dropped connection on its own - there is
      // nothing to do until it gives up for good.
      return;
    }
    if (root.dataset.live !== "true" || reloadScheduled) {
      return;
    }
    reloadScheduled = true;
    window.setTimeout(function () {
      window.location.reload();
    }, 5000);
  }

  // --- Painting a whole snapshot, with no memory of the one before it -------

  function render(snapshot) {
    root.dataset.phase = snapshot.phase;
    root.dataset.live = isLivePhase(snapshot.phase) ? "true" : "false";
    var finaleNote = snapshot.phase === "finale" ? snapshot.detail : null;
    root.dataset.hasNote = finaleNote ? "true" : "false";
    setText("finale-note", finaleNote || "");

    setText("run-title", snapshot.headline);
    announce(snapshot.headline);

    var linkingIds = linkingAppIds(snapshot);
    var apps = snapshot.apps || [];
    for (var i = 0; i < apps.length; i += 1) {
      renderTile(apps[i], linkingIds);
    }

    renderWiring(snapshot);
    renderFailure(snapshot.failure);
  }

  function isLivePhase(phase) {
    return phase === "running" || phase === "wiring";
  }

  function renderTile(app, linkingIds) {
    var tile = document.querySelector('[data-app="' + app.app_id + '"]');
    if (!tile) {
      return;
    }
    tile.dataset.state = app.state;
    tile.dataset.hasNote = app.note ? "true" : "false";
    tile.dataset.linking = linkingIds.indexOf(app.app_id) !== -1 ? "true" : "false";
    setTextWithin(tile, "chip", app.chip);
    setTextWithin(tile, "line", app.line);
    setTextWithin(tile, "note-text", app.note || "");
  }

  function linkingAppIds(snapshot) {
    var wiring = snapshot.wiring || [];
    if (snapshot.phase !== "wiring" || wiring.length === 0) {
      return [];
    }
    return wiring[wiring.length - 1].involved;
  }

  function renderWiring(snapshot) {
    var block = document.querySelector('[data-view="wiring"]');
    var wiring = snapshot.wiring || [];

    if (snapshot.phase !== "wiring") {
      if (block) {
        block.dataset.state = "";
      }
      setText("wiring-count", "");
      setText("wiring-text", "");
      setText("wiring-note", "");
      setText("wiring-chip", "");
      return;
    }

    var step = wiring.length > 0 ? wiring[wiring.length - 1] : null;
    if (block) {
      block.dataset.state = step ? step.state : "";
    }
    setText("wiring-count", step ? stepLabel(step.index, step.total) : "");
    setText("wiring-text", step ? step.line : snapshot.headline);
    setText("wiring-note", step && step.note ? step.note : "");
    setText("wiring-chip", step ? step.chip : "");
  }

  function stepLabel(index, total) {
    var countNode = document.querySelector('[data-role="wiring-count"]');
    var template = countNode ? countNode.dataset.countTemplate || "" : "";
    return template.replace("{index}", String(index)).replace("{total}", String(total));
  }

  function renderFailure(failure) {
    setText("failure-headline", failure ? failure.headline : "");
    setText("failure-advice", failure ? failure.what_to_do : "");
  }

  function announce(headline) {
    if (headline === lastAnnounced) {
      // Re-writing an aria-live region with the same text re-announces it
      // on some screen readers, so a repeated snapshot must not touch it.
      return;
    }
    lastAnnounced = headline;
    setText("announce", headline);
  }

  // --- Small DOM helpers --------------------------------------------------

  function setText(role, value) {
    var node = document.querySelector('[data-role="' + role + '"]');
    if (node) {
      node.textContent = value;
    }
  }

  function setTextWithin(scope, role, value) {
    var node = scope.querySelector('[data-role="' + role + '"]');
    if (node) {
      node.textContent = value;
    }
  }
})();
