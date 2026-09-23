/*
 * Progressive enhancement for the drive screen: checks the typed folder as
 * the owner types, and offers the browser's own time zone when nothing
 * better is known. Every word this script shows comes from a server
 * response or a data- attribute on the page - it never composes a sentence
 * of its own. If this file fails to load, or JavaScript is off, the drive
 * screen still works end to end through its real form submit.
 */
(function () {
  "use strict";

  var form = document.querySelector("[data-drive-form]");
  if (!form) {
    return;
  }

  setupDriveCheck(form);
  setupTimezonePrefill();

  // --- Check as you type: clear instantly, debounce, abort the stale one ---

  function setupDriveCheck(driveForm) {
    var CHECK_DEBOUNCE_MS = 400;

    var pathInput = document.getElementById("path");
    var appsField = driveForm.querySelector('input[name="apps"]');
    var result = document.getElementById("drive-result");
    if (!pathInput || !appsField || !result) {
      return;
    }

    var checkingText = result.dataset.checkingText || "";
    var debounceTimer = null;
    var activeController = null;

    function appIds() {
      return appsField.value
        .split(",")
        .map(function (id) {
          return id.trim();
        })
        .filter(function (id) {
          return id.length > 0;
        });
    }

    function abortInFlightRequest() {
      if (activeController) {
        activeController.abort();
        activeController = null;
      }
    }

    function clearResult() {
      abortInFlightRequest();
      result.textContent = "";
      result.removeAttribute("data-tone");
      result.removeAttribute("aria-busy");
    }

    function showChecking() {
      result.textContent = checkingText;
      result.setAttribute("aria-busy", "true");
    }

    function renderMessage(message) {
      result.textContent = "";
      result.removeAttribute("aria-busy");
      result.setAttribute("data-tone", message.tone);

      var glyph = document.createElement("span");
      glyph.className = "drive-message__glyph";
      glyph.setAttribute("aria-hidden", "true");
      glyph.textContent = message.glyph;
      result.appendChild(glyph);

      var text = document.createElement("p");
      text.className = "drive-message__text";
      text.textContent = message.text;
      result.appendChild(text);

      if (message.guidance) {
        var guidance = document.createElement("p");
        guidance.className = "drive-message__guidance";
        guidance.textContent = message.guidance;
        result.appendChild(guidance);
      }

      if (message.suggestion) {
        var button = document.createElement("button");
        button.type = "button";
        button.className = "suggestion-button";
        button.dataset.suggestion = message.suggestion;
        // The suggested path itself, not a composed sentence - the
        // preceding "did you mean" text already carries the words.
        button.textContent = message.suggestion;
        result.appendChild(button);
      }
    }

    // One delegated listener, bound once, covers a suggestion button this
    // script just built *and* the real `<button type="submit"
    // name="use_suggestion">` the server renders on first paint after a
    // refused post - so either way, JS turns a click into "fill the field
    // and re-check" instead of a full-page submit.
    result.addEventListener("click", function (event) {
      var target = event.target;
      if (!(target instanceof Element)) {
        return;
      }
      var button = target.closest(".suggestion-button");
      if (!button) {
        return;
      }
      event.preventDefault();
      var suggestion = button.dataset.suggestion || button.getAttribute("value");
      if (!suggestion) {
        return;
      }
      pathInput.value = suggestion;
      pathInput.focus();
      runCheck();
    });

    function runCheck() {
      abortInFlightRequest();
      var controller = new AbortController();
      activeController = controller;
      showChecking();

      fetch("/setup/drive/check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: pathInput.value, apps: appIds() }),
        signal: controller.signal,
      })
        .then(function (response) {
          return response.json();
        })
        .then(function (message) {
          if (activeController === controller) {
            activeController = null;
            renderMessage(message);
          }
        })
        .catch(function (error) {
          // An aborted request just means a newer keystroke already won -
          // nothing to show. Anything else leaves the region cleared,
          // which degrades to "press Continue and the server will say",
          // never to a stuck "Checking..." message.
          if (!error || error.name !== "AbortError") {
            clearResult();
          }
        });
    }

    pathInput.addEventListener("input", function () {
      clearResult();
      if (debounceTimer) {
        window.clearTimeout(debounceTimer);
      }
      debounceTimer = window.setTimeout(runCheck, CHECK_DEBOUNCE_MS);
    });
  }

  // --- Time zone: only fill in the browser's guess over the host fallback -

  function setupTimezonePrefill() {
    var select = document.getElementById("timezone");
    if (!select || select.dataset.timezoneSource !== "host") {
      return;
    }

    var browserZone;
    try {
      browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    } catch (error) {
      return;
    }
    if (!browserZone) {
      return;
    }

    var aliases = {};
    try {
      aliases = JSON.parse(select.dataset.timezoneAliases || "{}");
    } catch (error) {
      aliases = {};
    }
    var canonicalZone = aliases[browserZone] || browserZone;

    var option = select.querySelector('option[value="' + canonicalZone + '"]');
    if (option) {
      select.value = canonicalZone;
    }
  }
})();
