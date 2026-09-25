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
 * forgotten tab costs the NAS nothing. While an add or reconnect is
 * running, it polls faster (`data-add-poll-ms`) - the spotlight-to-green
 * moment is worth watching live.
 *
 * Installing an app is the one write this page makes without a page
 * navigation: a plain `fetch` POST to the one endpoint
 * `/api/hub/apps/{id}/install`, reading only the fields `HubInstallOut`
 * actually carries. A result that changes the grid's own shape (an app
 * appearing, disappearing, or gaining/losing its retry/reconnect buttons)
 * is never patched in place - the next poll notices and reloads the page
 * once, so the owner always sees the server's own, fully-rendered truth
 * rather than a script's guess at what changed.
 */
(function () {
  "use strict";

  // Every field name below is read out of a status frame. A test compares
  // all four lists against `HubStatusOut` / `HubTileOut` / `LinkTileOut` /
  // `HubInstallOut` in `routes/api.py`, so a field this script does not
  // actually use has no reason to be listed here.
  var STATUS_FIELDS = ["apps", "links", "announce", "any_down", "docker_unreachable", "busy"];
  var APP_FIELDS = ["app_id", "state", "chip", "line", "url", "aria", "add_state", "note", "actions"];
  // No "url" here: a link card's `href` is set once, by the server, and
  // stays put - the light is only ever a hint, so a poll never touches it.
  var LINK_FIELDS = ["link_id", "state", "chip", "line", "aria"];
  var INSTALL_FIELDS = ["ok", "message", "step_id", "field"];

  // The signature this tab has already reloaded for, once - so a stale
  // response that arrives again right after a reload (the same change,
  // re-read) can never send the page into a reload loop.
  var RELOAD_GUARD_KEY = "hubReloadSignature";

  bindPanel();
  bindInstall();

  var root = document.querySelector("[data-hub]");
  if (!root) {
    return;
  }

  var baseDelay = Number(root.dataset.pollMs);
  var addDelay = Number(root.dataset.addPollMs);
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
    currentDelay = payload.busy ? addDelay : baseDelay;
    root.dataset.stale = "false";
    if (reloadIfStructureChanged(payload)) {
      return;
    }
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
    if (app.add_state) {
      tile.setAttribute("data-add-state", app.add_state);
    } else {
      tile.removeAttribute("data-add-state");
    }
    setTextWithin(tile, "chip", app.chip);
    setTextWithin(tile, "line", app.line);
    setTextWithin(tile, "note", app.note);
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

  // --- Reloading once when the grid's own shape has changed ------------------
  //
  // An app appearing (a clean add finishing), disappearing (a Cancel) or
  // gaining/losing its retry/reconnect buttons is a shape the live layer
  // never tries to redraw itself - it reloads the page once instead, so
  // the owner always sees the server's own fully-rendered truth.

  function reloadIfStructureChanged(payload) {
    var incoming = actionsSignature(payload.apps || []);
    if (incoming === actionsSignature(pageApps())) {
      return false;
    }
    if (window.sessionStorage.getItem(RELOAD_GUARD_KEY) === incoming) {
      // Already reloaded once for exactly this change - a repeat must
      // never loop.
      return false;
    }
    window.sessionStorage.setItem(RELOAD_GUARD_KEY, incoming);
    window.location.reload();
    return true;
  }

  function pageApps() {
    var nodes = document.querySelectorAll("[data-app]");
    var apps = [];
    for (var i = 0; i < nodes.length; i += 1) {
      var item = nodes[i].closest("li");
      var actions = (item && item.getAttribute("data-actions")) || "none";
      apps.push({ app_id: nodes[i].getAttribute("data-app"), actions: actions });
    }
    return apps;
  }

  function actionsSignature(apps) {
    var pairs = apps.map(function (app) {
      return app.app_id + ":" + app.actions;
    });
    pairs.sort();
    return pairs.join(",");
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

  // --- Installing an app: the "+" panel's install pane -----------------------
  //
  // Every button here is rendered `hidden` by the server - a no-JS page
  // never shows a dead control, it shows the pane's own `<noscript>`
  // sentence instead. This un-hides them once the script has actually
  // loaded, then drives the one write this page makes without a page
  // navigation.

  function bindInstall() {
    var buttons = document.querySelectorAll("[data-install-app]");
    for (var i = 0; i < buttons.length; i += 1) {
      buttons[i].removeAttribute("hidden");
    }
    if (buttons.length === 0) {
      return;
    }

    document.addEventListener("click", function (event) {
      var target = event.target;
      if (!(target instanceof Element)) {
        return;
      }

      var installTrigger = target.closest("[data-install-app]");
      if (installTrigger) {
        event.preventDefault();
        startInstall(installTrigger.getAttribute("data-install-app"));
        return;
      }

      var nextTrigger = target.closest("[data-install-next]");
      if (nextTrigger) {
        event.preventDefault();
        stepBy(nextTrigger.closest("form"), 1);
        return;
      }

      var backTrigger = target.closest("[data-install-back]");
      if (backTrigger) {
        event.preventDefault();
        stepBy(backTrigger.closest("form"), -1);
        return;
      }

      var submitTrigger = target.closest("[data-install-submit]");
      if (submitTrigger) {
        event.preventDefault();
        submitInstall(submitTrigger.closest("form"));
      }
    });
  }

  // An app with no question steps has no form at all - the click itself
  // is the whole write.
  function startInstall(appId) {
    var form = document.querySelector('[data-install-form="' + appId + '"]');
    if (!form) {
      postAnswers(appId, {}, null);
      return;
    }
    form.removeAttribute("hidden");
    showStep(form, 0);
  }

  function questionSteps(form) {
    return form.querySelectorAll("[data-question-step]");
  }

  function showStep(form, index) {
    var steps = questionSteps(form);
    for (var i = 0; i < steps.length; i += 1) {
      steps[i].hidden = i !== index;
    }
    form.dataset.stepIndex = String(index);
    var backButton = form.querySelector("[data-install-back]");
    var nextButton = form.querySelector("[data-install-next]");
    var submitButton = form.querySelector("[data-install-submit]");
    var isLast = index === steps.length - 1;
    if (backButton) {
      backButton.hidden = index === 0;
    }
    if (nextButton) {
      nextButton.hidden = isLast;
    }
    if (submitButton) {
      submitButton.hidden = !isLast;
    }
  }

  function stepBy(form, delta) {
    if (!form) {
      return;
    }
    hideRefusal(form);
    var total = questionSteps(form).length;
    var index = Number(form.dataset.stepIndex || "0");
    var next = Math.min(Math.max(index + delta, 0), total - 1);
    showStep(form, next);
  }

  function submitInstall(form) {
    if (!form) {
      return;
    }
    var appId = form.getAttribute("data-install-form");
    postAnswers(appId, collectAnswers(form), form);
  }

  // A radio's own value only counts when it's the one checked in its
  // group - every other input (text, password) is read as-is.
  function collectAnswers(form) {
    var answers = {};
    var inputs = form.querySelectorAll("input[name]");
    for (var i = 0; i < inputs.length; i += 1) {
      var input = inputs[i];
      if (input.type === "radio") {
        if (input.checked) {
          answers[input.name] = input.value;
        }
        continue;
      }
      answers[input.name] = input.value;
    }
    return answers;
  }

  function postAnswers(appId, answers, form) {
    fetch("/api/hub/apps/" + appId + "/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answers: answers }),
    })
      .then(function (response) {
        return response.json().then(function (payload) {
          onInstallResult(form, response.status, payload);
        });
      })
      .catch(function () {
        // The same "we couldn't check just now" state a failed poll
        // already shows - a lost connection reads the same way however
        // it happened.
        root.dataset.stale = "true";
      });
  }

  function onInstallResult(form, status, payload) {
    if (status === 202) {
      window.location.assign("/");
      return;
    }
    if (!form) {
      // No form to show a refusal in (this app has no question steps) -
      // a fresh load of the page carries the server's own true reason
      // (busy, already installed, ...) in the install pane itself.
      window.location.reload();
      return;
    }
    if (status === 400) {
      showRefusal(form, payload.step_id, payload.field, payload.message);
      return;
    }
    showRefusal(form, null, null, payload.message);
  }

  function showRefusal(form, stepId, field, message) {
    if (stepId) {
      var appId = form.getAttribute("data-install-form");
      var steps = questionSteps(form);
      for (var i = 0; i < steps.length; i += 1) {
        if (steps[i].getAttribute("data-question-step") === appId + ":" + stepId) {
          showStep(form, i);
          break;
        }
      }
    }
    var banner = form.querySelector('[data-role="install-refusal"]');
    if (banner) {
      banner.removeAttribute("hidden");
      setTextWithin(banner, "install-refusal-text", message || "");
    }
    if (field) {
      var input = form.querySelector('[name="' + field + '"]');
      if (input) {
        input.focus();
      }
    }
  }

  function hideRefusal(form) {
    var banner = form.querySelector('[data-role="install-refusal"]');
    if (banner) {
      banner.setAttribute("hidden", "");
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
