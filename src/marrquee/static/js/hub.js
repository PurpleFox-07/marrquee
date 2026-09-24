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
  // all three lists against `HubStatusOut` / `HubTileOut` / `LinkTileOut`
  // in `routes/api.py`, so a field this script does not actually use has
  // no reason to be listed here.
  var STATUS_FIELDS = ["apps", "links", "announce", "any_down", "docker_unreachable"];
  var APP_FIELDS = ["app_id", "state", "chip", "line", "url", "aria"];
  // No "url" here: a link card's `href` is set once, by the server, and
  // stays put - the light is only ever a hint, so a poll never touches it.
  var LINK_FIELDS = ["link_id", "state", "chip", "line", "aria"];

  bindPanel();

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
    var links = payload.links || [];
    for (var j = 0; j < links.length; j += 1) {
      paintLink(links[j]);
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

  // A link card's light is a hint, never a lock (Marrquee checks it from
  // inside its own container, so "down" can be wrong) - so a repaint only
  // ever updates its state, chip, line and aria-label, and never touches
  // `href`. A card not on the page (removed between polls) is skipped.
  function paintLink(link) {
    var card = document.querySelector('[data-link="' + link.link_id + '"]');
    if (!card) {
      return;
    }
    card.dataset.state = link.state;
    setTextWithin(card, "chip", link.chip);
    setTextWithin(card, "line", link.line);
    card.setAttribute("aria-label", link.aria);
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

  // --- The "+" panel: a no-reload <dialog> on top of a real page ------------
  //
  // Every choice, close and back control already works with no JavaScript
  // at all - each is a plain `<a href="/?panel=...#hub-panel">` the server
  // answers by redrawing the whole page with the dialog already open. This
  // upgrades that into a `showModal()` dialog that never navigates: a click
  // on any hook below is caught, the mode is switched in place, and the
  // dialog's own built-in Esc key and backdrop click do the rest. When
  // `[data-role="hub-panel"]` isn't on the page at all, there is nothing to
  // bind - the function returns before touching anything.

  function bindPanel() {
    var dialog = document.querySelector('[data-role="hub-panel"]');
    if (!dialog) {
      return;
    }

    var opener = null;

    dialog.addEventListener("close", function () {
      if (window.location.search.indexOf("panel=") !== -1) {
        window.history.replaceState(null, "", "/");
      }
      if (opener) {
        opener.focus();
        opener = null;
      }
    });

    if (dialog.hasAttribute("open")) {
      // The server drew this dialog already open - the no-JS fallback for
      // a `?panel=...` link. A plain `open` attribute makes it a
      // non-modal dialog, so it's closed and reopened as a modal one to
      // pick up Esc, the focus trap and the backdrop for free.
      dialog.close();
      dialog.showModal();
    }

    document.addEventListener("click", function (event) {
      var target = event.target;
      if (!(target instanceof Element)) {
        return;
      }

      var openTrigger = target.closest("[data-panel-open]");
      if (openTrigger) {
        event.preventDefault();
        opener = openTrigger;
        setPanelMode(dialog, openTrigger.getAttribute("data-panel-open"));
        if (openTrigger.getAttribute("data-panel-open") === "edit") {
          fillEditForm(dialog, openTrigger);
        }
        dialog.showModal();
        return;
      }

      var choiceTrigger = target.closest("[data-panel-choice]");
      if (choiceTrigger) {
        event.preventDefault();
        setPanelMode(dialog, choiceTrigger.getAttribute("data-panel-choice"));
        return;
      }

      var closeTrigger = target.closest("[data-panel-close]");
      if (closeTrigger) {
        event.preventDefault();
        dialog.close();
      }
    });
  }

  function setPanelMode(dialog, mode) {
    dialog.setAttribute("data-panel-mode", mode);
  }

  // The Edit pill carries its own card's saved values and form targets as
  // plain attributes - the script composes no URL of its own, it only
  // copies what the server already rendered into the edit pane's inputs
  // and the two forms' `action`.
  function fillEditForm(dialog, trigger) {
    var editLabel = dialog.querySelector('[data-role="edit-label"]');
    var editUrl = dialog.querySelector('[data-role="edit-url"]');
    var editForm = dialog.querySelector('[data-role="edit-form"]');
    var removeForm = dialog.querySelector('[data-role="remove-form"]');

    if (editLabel) {
      editLabel.value = trigger.getAttribute("data-link-label") || "";
    }
    if (editUrl) {
      editUrl.value = trigger.getAttribute("data-link-url") || "";
    }
    if (editForm) {
      editForm.setAttribute("action", trigger.getAttribute("data-edit-action") || "");
    }
    if (removeForm) {
      removeForm.setAttribute("action", trigger.getAttribute("data-remove-action") || "");
    }
  }
})();
