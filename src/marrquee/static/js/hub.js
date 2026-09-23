/*
 * The live layer for the Hub. Everything this script draws comes straight
 * out of a whole `HubStatusOut` JSON frame from `GET /api/hub/status` - it
 * never composes a sentence of its own, and every field name below is
 * checked against the API's own models. If this file fails to load, or
 * JavaScript is off, the page still shows the truth from its own last
 * server render, and a plain reload shows the current one.
 *
 * The Hub is a resting page, open for hours at a time, so this polls
 * rather than holding an always-open connection open per tab: a plain
 * `fetch` on a `setTimeout` loop, paused while the tab is hidden, so a
 * forgotten tab costs the NAS nothing.
 */
(function () {
  "use strict";

  // Every field name below is read out of a status frame. A test compares
  // both lists against `HubStatusOut` / `HubTileOut` in `routes/api.py`,
  // so a field this script does not actually use has no reason to be
  // listed here.
  var STATUS_FIELDS = ["apps", "announce", "any_down", "docker_unreachable"];
  var APP_FIELDS = ["app_id", "state", "chip", "line", "url", "aria"];

  var root = document.querySelector("[data-hub]");
  if (!root) {
    return;
  }

  var baseDelay = Number(root.dataset.pollMs);
  var maxDelay = baseDelay * 4;
  var currentDelay = baseDelay;
  var consecutiveFailures = 0;
  var lastAnnounced = null;
  var timer = null;

  document.addEventListener("visibilitychange", handleVisibilityChange);
  scheduleNext(baseDelay);

  function handleVisibilityChange() {
    if (document.visibilityState === "visible") {
      // A tab that was hidden may have missed several changes - the next
      // check happens right away instead of waiting out whatever delay
      // was left when it was last paused.
      scheduleNext(0);
    } else {
      cancelTimer();
    }
  }

  function cancelTimer() {
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
  }

  function scheduleNext(delay) {
    cancelTimer();
    if (document.visibilityState !== "visible") {
      return;
    }
    timer = window.setTimeout(tick, delay);
  }

  function tick() {
    if (document.visibilityState !== "visible") {
      return;
    }
    fetch("/api/hub/status", { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("hub status check failed");
        }
        return response.json();
      })
      .then(onCheckSucceeded, onCheckFailed);
  }

  function onCheckSucceeded(payload) {
    consecutiveFailures = 0;
    currentDelay = baseDelay;
    root.dataset.stale = "false";
    paint(payload);
    scheduleNext(currentDelay);
  }

  function onCheckFailed() {
    consecutiveFailures += 1;
    currentDelay = Math.min(currentDelay * 2, maxDelay);
    if (consecutiveFailures >= 2) {
      root.dataset.stale = "true";
    }
    scheduleNext(currentDelay);
  }

  // --- Painting a whole frame, with no memory of the one before it ----------

  function paint(payload) {
    var apps = payload.apps || [];
    for (var i = 0; i < apps.length; i += 1) {
      paintTile(apps[i]);
    }
    root.dataset.anyDown = payload.any_down ? "true" : "false";
    root.dataset.dockerUnreachable = payload.docker_unreachable ? "true" : "false";
    announce(payload.announce);
  }

  function paintTile(app) {
    var tile = document.querySelector('[data-app="' + app.app_id + '"]');
    if (!tile) {
      return;
    }
    tile.dataset.state = app.state;
    setTextWithin(tile, "chip", app.chip);
    setTextWithin(tile, "line", app.line);
    if (app.url) {
      tile.setAttribute("href", app.url);
      tile.setAttribute("aria-label", app.aria || "");
    } else {
      tile.removeAttribute("href");
      tile.removeAttribute("aria-label");
    }
  }

  function announce(text) {
    if (text === lastAnnounced) {
      // Re-writing an aria-live region with the same text re-announces it
      // on some screen readers, so a repeated frame must not touch it.
      return;
    }
    lastAnnounced = text;
    setText("announce", text);
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
